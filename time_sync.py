#!/usr/bin/env python3
"""
TimeSync - 轻量级时间同步工具
NTP 时间同步 · 自定义时区 · 开机自启 · 极简设计
"""

import socket
import struct
import ctypes
from ctypes import wintypes
import sys
import os
import json
import datetime
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time as _time_module

# ============================================================
# 配置路径 — 隐藏到 AppData
# ============================================================
APP_NAME = "TimeSync"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

NTP_SERVERS = [
    ("pool.ntp.org",       "全球 NTP 池"),
    ("asia.pool.ntp.org",  "亚洲 NTP 池"),
    ("time.google.com",    "Google 时间"),
    ("time.windows.com",   "微软时间"),
]

TIMEZONE_PRESETS = [
    ("哈萨克斯坦 · UTC+5", 5),
    ("哈萨克斯坦东部 · UTC+6", 6),
    ("中国 · UTC+8", 8),
    ("英国 · UTC+0", 0),
    ("东欧 · UTC+2", 2),
    ("莫斯科 · UTC+3", 3),
    ("迪拜 · UTC+4", 4),
    ("巴基斯坦 · UTC+5", 5),
    ("曼谷 · UTC+7", 7),
    ("日本 · UTC+9", 9),
    ("悉尼 · UTC+10", 10),
    ("纽约 · UTC-5", -5),
    ("洛杉矶 · UTC-8", -8),
    ("自定义偏移", None),
]

DEFAULT_CONFIG = {
    "timezone_offset": 5,
    "ntp_server": "pool.ntp.org",
    "auto_start": False,
    "last_sync": None,
}

# ============================================================
# NTP 客户端
# ============================================================
def get_ntp_time(server="pool.ntp.org", timeout=4):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(timeout)
    try:
        packet = b'\x1b' + b'\0' * 47
        client.sendto(packet, (server, 123))
        data, addr = client.recvfrom(1024)
        unpacked = struct.unpack('!12I', data[:48])
        t = unpacked[10] + unpacked[11] / 2**32
        t -= 2208988800
        return t, server
    finally:
        client.close()


def get_ntp_time_multi(servers=None, timeout=3):
    if servers is None:
        servers = [s[0] for s in NTP_SERVERS]
    for server in servers:
        try:
            t, srv = get_ntp_time(server, timeout=timeout)
            return t, srv
        except Exception:
            continue
    raise Exception("无法连接任何 NTP 服务器，请检查网络")


# ============================================================
# Windows 系统时间 API
# ============================================================
class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear",          wintypes.WORD),
        ("wMonth",         wintypes.WORD),
        ("wDayOfWeek",     wintypes.WORD),
        ("wDay",           wintypes.WORD),
        ("wHour",          wintypes.WORD),
        ("wMinute",        wintypes.WORD),
        ("wSecond",        wintypes.WORD),
        ("wMilliseconds",  wintypes.WORD),
    ]


def set_system_utc_time(timestamp):
    dt = datetime.datetime.utcfromtimestamp(timestamp)
    st = SYSTEMTIME()
    st.wYear = dt.year
    st.wMonth = dt.month
    st.wDayOfWeek = dt.weekday()
    st.wDay = dt.day
    st.wHour = dt.hour
    st.wMinute = dt.minute
    st.wSecond = dt.second
    st.wMilliseconds = int(dt.microsecond / 1000)
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetSystemTime(ctypes.byref(st)):
        raise OSError(f"SetSystemTime 失败，错误码: {ctypes.get_last_error()}")
    return True


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def request_admin():
    if sys.argv[0].endswith(".py"):
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{os.path.abspath(sys.argv[0])}"', None, 1
        )
    else:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.argv[0], " ".join(sys.argv[1:]), None, 1
        )
    sys.exit(0)


# ============================================================
# 任务计划 — 开机自启（无 UAC 弹框）
# ============================================================
TASK_NAME = "TimeSync_AutoSync"

def _get_exe_path():
    """获取当前 EXE 的完整路径（兼容 frozen 和脚本模式）"""
    if getattr(sys, "frozen", False):
        return sys.executable
    else:
        return os.path.abspath(sys.argv[0])


def set_auto_start(enable):
    exe_path = _get_exe_path()
    if enable:
        # 先清理旧版 schtasks / 注册表 Run 键残留
        _cleanup_old_tasks()

        # 使用 PowerShell Register-ScheduledTask：
        #   - SYSTEM 账户 + ServiceAccount 登录，无需用户登录即可运行
        #   - AtStartup + 随机延迟 60-120 秒，确保网络已就绪
        #   - StartWhenAvailable + 失败重试 3 次（间隔 1 分钟）
        ps_cmd = (
            '$action = New-ScheduledTaskAction -Execute "{exe}" -Argument "-s"; '
            '$trigger = New-ScheduledTaskTrigger -AtStartup -RandomDelay (New-TimeSpan -Minutes 2); '
            '$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\\SYSTEM" -LogonType ServiceAccount -RunLevel Highest; '
            '$settings = New-ScheduledTaskSettingsSet '
            '-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
            '-StartWhenAvailable '
            '-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); '
            'Register-ScheduledTask -TaskName "{name}" '
            '-Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force'
        ).format(exe=exe_path, name=TASK_NAME)

        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise OSError(f"创建任务计划失败: {result.stderr.strip()}")
    else:
        _cleanup_old_tasks()
        cmd = f'schtasks /delete /tn "{TASK_NAME}" /f'
        subprocess.run(cmd, capture_output=True, text=True, shell=True)


def _cleanup_old_tasks():
    """清理旧版任务（schtasks onlogon 方式、注册表 Run 键）"""
    # 删除旧版 schtasks 任务
    subprocess.run(
        f'schtasks /delete /tn "{TASK_NAME}" /f',
        capture_output=True, text=True, shell=True
    )
    # 清理注册表 Run 键
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        try:
            winreg.QueryValueEx(key, "TimeSync")
            winreg.DeleteValue(key, "TimeSync")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass


def get_auto_start():
    try:
        result = subprocess.run(
            f'schtasks /query /tn "{TASK_NAME}"',
            capture_output=True, text=True, shell=True
        )
        return result.returncode == 0
    except Exception:
        return False


# ============================================================
# 配置管理
# ============================================================
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ============================================================
# GUI — 极简现代风格
# ============================================================
COLORS = {
    "bg":           "#ffffff",
    "fg":           "#1a1a1a",
    "fg_secondary": "#888888",
    "accent":       "#1a73e8",
    "accent_hover": "#1557b0",
    "accent_light": "#e8f0fe",
    "success":      "#0d904f",
    "success_light":"#e6f4ea",
    "danger":       "#d93025",
    "danger_light": "#fce8e6",
    "border":       "#dadce0",
    "card_bg":      "#f8f9fa",
    "separator":    "#eeeeee",
}

FONTS = {
    "title":    ("Microsoft YaHei UI", 18, "bold"),
    "heading":  ("Microsoft YaHei UI", 12, "bold"),
    "body":     ("Microsoft YaHei UI", 11),
    "small":    ("Microsoft YaHei UI", 9),
    "mono":     ("Consolas", 16),
    "mono_sm":  ("Consolas", 10),
}


class TimeSyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.configure(bg=COLORS["bg"])

        w, h = 440, 500
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        self.config = load_config()
        self.syncing = False
        self.synced_offset = None

        self._build_ui()
        self._load_ui_from_config()
        self._update_clock()
        self._set_icon()

    # ---- UI 构建 ----
    def _build_ui(self):
        # 顶部标题栏
        header = tk.Frame(self.root, bg=COLORS["bg"], height=64)
        header.pack(fill="x", padx=20, pady=(20, 0))
        header.pack_propagate(False)

        tk.Label(header, text="TimeSync", font=FONTS["title"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(side="left")
        self.status_dot = tk.Canvas(header, width=10, height=10,
                                     bg=COLORS["bg"], highlightthickness=0)
        self.status_dot.pack(side="right", pady=(6, 0))
        self._draw_dot("#dadce0")  # 初始灰色

        tk.Label(header, text="系统时间校准工具", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["bg"]).pack(side="right", padx=(0, 6))

        # 分隔线
        sep = tk.Frame(self.root, bg=COLORS["separator"], height=1)
        sep.pack(fill="x", padx=20, pady=(8, 0))

        # ---- 当前时间卡片 ----
        time_card = tk.Frame(self.root, bg=COLORS["card_bg"])
        time_card.pack(fill="x", padx=20, pady=(14, 0))

        inner = tk.Frame(time_card, bg=COLORS["card_bg"])
        inner.pack(padx=14, pady=14)

        tk.Label(inner, text="当前系统时间", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["card_bg"]).pack(anchor="w")

        self.clock_label = tk.Label(inner, text="", font=FONTS["mono"],
                                     fg=COLORS["fg"], bg=COLORS["card_bg"])
        self.clock_label.pack(anchor="w", pady=(2, 0))

        # 偏移显示
        self.offset_label = tk.Label(inner, text="", font=FONTS["small"],
                                      fg=COLORS["fg_secondary"], bg=COLORS["card_bg"])
        self.offset_label.pack(anchor="w")

        # ---- 时区选择 ----
        tz_section = tk.Frame(self.root, bg=COLORS["bg"])
        tz_section.pack(fill="x", padx=20, pady=(14, 0))

        tk.Label(tz_section, text="目标时区", font=FONTS["heading"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(anchor="w")

        self.tz_var = tk.StringVar()
        self.tz_combo = ttk.Combobox(tz_section, textvariable=self.tz_var,
                                      state="readonly", font=("Microsoft YaHei UI", 11))
        self.tz_combo["values"] = [p[0] for p in TIMEZONE_PRESETS]
        self.tz_combo.pack(fill="x", pady=(6, 0))
        self.tz_combo.bind("<<ComboboxSelected>>", self._on_tz_changed)

        # 自定义偏移行
        self.custom_row = tk.Frame(tz_section, bg=COLORS["bg"])
        tk.Label(self.custom_row, text="偏移量（小时）", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["bg"]).pack(side="left")
        self.custom_offset = tk.IntVar(value=5)
        self.custom_spin = ttk.Spinbox(self.custom_row, from_=-12, to=14,
                                        textvariable=self.custom_offset,
                                        width=5, font=("Microsoft YaHei UI", 11),
                                        state="readonly")
        self.custom_spin.pack(side="left", padx=8)

        # ---- NTP 服务器 ----
        ntp_section = tk.Frame(self.root, bg=COLORS["bg"])
        ntp_section.pack(fill="x", padx=20, pady=(14, 0))

        tk.Label(ntp_section, text="NTP 服务器", font=FONTS["heading"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(anchor="w")

        self.ntp_var = tk.StringVar()
        self.ntp_combo = ttk.Combobox(ntp_section, textvariable=self.ntp_var,
                                       font=("Microsoft YaHei UI", 11))
        self.ntp_combo["values"] = [f"{s[0]}  ({s[1]})" for s in NTP_SERVERS]
        self.ntp_combo.pack(fill="x", pady=(6, 0))

        # ---- 操作按钮 ----
        btn_area = tk.Frame(self.root, bg=COLORS["bg"])
        btn_area.pack(fill="x", padx=20, pady=(16, 0))

        self.sync_btn = tk.Button(
            btn_area, text="立即同步", font=FONTS["heading"],
            bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["accent_hover"], activeforeground="#ffffff",
            relief="flat", padx=20, pady=8, cursor="hand2",
            command=self._on_sync
        )
        self.sync_btn.pack(fill="x")

        # 上次同步时间
        self.last_sync_label = tk.Label(btn_area, text="", font=FONTS["small"],
                                         fg=COLORS["fg_secondary"], bg=COLORS["bg"])
        self.last_sync_label.pack(pady=(6, 0))

        # ---- 开机自启 ----
        auto_area = tk.Frame(self.root, bg=COLORS["bg"])
        auto_area.pack(fill="x", padx=20, pady=(14, 0))

        self.auto_var = tk.BooleanVar()
        auto_cb = tk.Checkbutton(
            auto_area, text="开机后自动同步（静默运行）",
            variable=self.auto_var, command=self._on_auto_toggle,
            font=FONTS["body"], fg=COLORS["fg"], bg=COLORS["bg"],
            activebackground=COLORS["bg"], selectcolor=COLORS["bg"],
            cursor="hand2"
        )
        auto_cb.pack(anchor="w")

        # 隐藏的提示
        tk.Label(auto_area, text="配置保存在系统 AppData，无可见残留文件",
                 font=FONTS["small"], fg=COLORS["fg_secondary"],
                 bg=COLORS["bg"]).pack(anchor="w", pady=(4, 0))

        # ---- 底部版本 ----
        tk.Label(self.root, text="v1.0  ·  NTP 时间同步  ·  哈萨克斯坦优化",
                 font=("Microsoft YaHei UI", 8), fg=COLORS["fg_secondary"],
                 bg=COLORS["bg"]).pack(side="bottom", pady=(0, 12))

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        cx, cy = 5, 5
        r = 4
        self.status_dot.create_oval(cx - r, cy - r, cx + r, cy + r,
                                     fill=color, outline="")

    # ---- 数据加载 ----
    def _load_ui_from_config(self):
        offset = self.config.get("timezone_offset", 5)
        found = False
        for i, preset in enumerate(TIMEZONE_PRESETS):
            if preset[0] == "":
                continue
            if preset[1] == offset:
                self.tz_combo.current(i)
                found = True
                break
        if not found:
            self.tz_combo.current(len(TIMEZONE_PRESETS) - 1)
        self.custom_offset.set(offset)
        self._on_tz_changed()

        ntp = self.config.get("ntp_server", "pool.ntp.org")
        for i, s in enumerate(NTP_SERVERS):
            if s[0] == ntp:
                self.ntp_combo.current(i)
                break

        self.auto_var.set(self.config.get("auto_start", False))

        last = self.config.get("last_sync")
        if last:
            self.last_sync_label.config(text=f"上次同步: {last}")

    def _on_tz_changed(self, event=None):
        idx = self.tz_combo.current()
        if idx < 0:
            return
        preset = TIMEZONE_PRESETS[idx]
        if preset[0] == "":
            return
        if preset[1] is None:
            self.custom_row.pack(fill="x", pady=(6, 0))
        else:
            self.custom_row.pack_forget()
            self.custom_offset.set(preset[1])

    def _get_offset(self):
        idx = self.tz_combo.current()
        if idx < 0:
            return 5
        preset = TIMEZONE_PRESETS[idx]
        if preset[1] is not None and preset[0] != "":
            return preset[1]
        return self.custom_offset.get()

    def _get_ntp_server(self):
        text = self.ntp_var.get().strip()
        if "(" in text:
            text = text.split("(")[0].strip()
        return text or "pool.ntp.org"

    # ---- 时钟更新 ----
    def _update_clock(self):
        now = datetime.datetime.now()
        self.clock_label.config(text=now.strftime("%Y-%m-%d  %H:%M:%S"))
        if self.synced_offset is not None:
            offset = self.synced_offset
        else:
            offset = -_time_module.timezone / 3600
        sign = "+" if offset >= 0 else ""
        self.offset_label.config(text=f"UTC{sign}{int(offset)}")
        self.root.after(1000, self._update_clock)

    def _set_icon(self):
        """设置窗口图标（ctypes 直接调 Windows API，兼容所有 Tkinter 版本）"""
        icon_path = None
        if getattr(sys, "frozen", False):
            candidate = os.path.join(sys._MEIPASS, "extracted_ico.ico")
        else:
            candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted_ico.ico")
        if os.path.exists(candidate):
            icon_path = candidate
        if not icon_path:
            return

        # 方案1：Tk iconbitmap 设置标题栏图标
        try:
            self.root.iconbitmap(icon_path)
        except Exception:
            pass

        # 方案2：Windows API 设置任务栏图标（按系统DPI精确匹配尺寸）
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            hwnd = ctypes.windll.user32.GetAncestor(hwnd, 3)
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            LR_LOADFROMFILE = 0x10
            # 获取系统图标标准尺寸（适配高DPI）
            cx_small = ctypes.windll.user32.GetSystemMetrics(49)  # SM_CXSMICON
            cy_small = ctypes.windll.user32.GetSystemMetrics(50)  # SM_CYSMICON
            cx_big = ctypes.windll.user32.GetSystemMetrics(11)    # SM_CXICON
            cy_big = ctypes.windll.user32.GetSystemMetrics(12)    # SM_CYICON
            hicon_small = ctypes.windll.user32.LoadImageW(0, icon_path, 1, cx_small, cy_small, LR_LOADFROMFILE)
            hicon_big = ctypes.windll.user32.LoadImageW(0, icon_path, 1, cx_big, cy_big, LR_LOADFROMFILE)
            if hicon_small:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            if hicon_big:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        except Exception:
            pass

    # ---- 同步操作 ----
    def _on_sync(self):
        if self.syncing:
            return
        self.syncing = True
        self.sync_btn.config(text="同步中…", state="disabled",
                             bg=COLORS["fg_secondary"])
        self._draw_dot("#f9ab00")

        def do_sync():
            try:
                server = self._get_ntp_server()
                others = [s[0] for s in NTP_SERVERS if s[0] != server]
                utc_ts, used = get_ntp_time_multi(servers=[server] + others, timeout=4)
                offset = self._get_offset()

                # 目标：让系统时钟显示所选时区的时间
                # 系统显示时间 = 系统UTC + 系统时区偏移
                # 要让显示 = NTP_UTC + 所选偏移，则需写入的UTC = NTP_UTC + 所选偏移 - 系统时区偏移
                sys_offset = -_time_module.timezone / 3600
                target_utc_ts = utc_ts + (offset - sys_offset) * 3600
                set_system_utc_time(target_utc_ts)
                self.synced_offset = offset

                self.config["timezone_offset"] = offset
                self.config["ntp_server"] = server
                self.config["last_sync"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_config(self.config)

                dt_str = datetime.datetime.utcfromtimestamp(utc_ts).strftime("%H:%M:%S")
                self.root.after(0, lambda: self._sync_done(
                    True, f"已同步 · UTC {dt_str} · {used}"
                ))
            except Exception as e:
                self.root.after(0, lambda: self._sync_done(False, str(e)))

        threading.Thread(target=do_sync, daemon=True).start()

    def _sync_done(self, success, msg):
        self.syncing = False
        if success:
            self.sync_btn.config(text="已同步", bg=COLORS["success"],
                                 state="normal")
            self._draw_dot(COLORS["success"])
            self.last_sync_label.config(
                text=f"上次同步: {datetime.datetime.now().strftime('%H:%M:%S')}"
            )
            self._update_clock()
            self.root.after(2000, self._reset_btn)
        else:
            self.sync_btn.config(text="同步失败，重试", bg=COLORS["danger"],
                                 state="normal")
            self._draw_dot(COLORS["danger"])
            self.root.after(3000, self._reset_btn)

    def _reset_btn(self):
        self.sync_btn.config(text="立即同步", bg=COLORS["accent"])
        self._draw_dot("#dadce0")

    # ---- 开机自启 ----
    def _on_auto_toggle(self):
        enable = self.auto_var.get()
        try:
            set_auto_start(enable)
            self.config["auto_start"] = enable
            save_config(self.config)
        except Exception as e:
            messagebox.showerror("设置失败", str(e))
            self.auto_var.set(not enable)


# ============================================================
# 静默模式
# ============================================================
def silent_sync():
    cfg = load_config()
    offset = cfg.get("timezone_offset", 5)
    ntp_server = cfg.get("ntp_server", "pool.ntp.org")
    others = [s[0] for s in NTP_SERVERS if s[0] != ntp_server]
    servers = [ntp_server] + others

    max_retries = 3
    retry_delay = 10  # 秒，开机时等网络就绪
    last_error = ""

    for attempt in range(1, max_retries + 1):
        try:
            utc_ts, used = get_ntp_time_multi(servers=servers, timeout=5)
            sys_offset = -_time_module.timezone / 3600
            target_utc_ts = utc_ts + (offset - sys_offset) * 3600
            set_system_utc_time(target_utc_ts)
            cfg["last_sync"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cfg["ntp_server"] = ntp_server
            save_config(cfg)
            return True
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                _time_module.sleep(retry_delay)

    # 全部重试失败，写日志
    try:
        log_dir = CONFIG_DIR
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "sync_error.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"静默同步失败（{max_retries}次重试）: {last_error}\n")
    except Exception:
        pass
    return False


# ============================================================
# 入口
# ============================================================
def main():
    if "--silent" in sys.argv or "-s" in sys.argv:
        # 计划任务已以最高权限运行，无需再做提权检查
        silent_sync()
        sys.exit(0)

    if not is_admin():
        request_admin()
        return

    os.makedirs(CONFIG_DIR, exist_ok=True)

    root = tk.Tk()

    app = TimeSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
