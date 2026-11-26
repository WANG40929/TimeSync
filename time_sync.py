#!/usr/bin/env python3
r"""
TimeSync v1.1 - 轻量级时间同步工具
NTP 授时 · 真实系统时区切换 · 开机静默自动同步

修复要点（相对 v1.0）：
  1. v1.0 从不真正修改 Windows 时区，只是把"假 UTC"写进系统时钟来凑显示时间，
     会被 Windows 时间服务随时纠正，观感就是"时区跳不掉、时间乱跳"。
     v1.1 改为：真正的 SetTimeZoneInformation / tzutil 切换时区 + 写入真实 UTC。
  2. v1.0 的开机任务以 SYSTEM 身份运行，却去读 %APPDATA% 配置，
     读不到就回落到默认值 UTC+5（哈萨克斯坦）——这就是"一直固定在哈萨克斯坦"的原因。
     v1.1 改为机器级配置 C:\ProgramData\TimeSync\config.json，GUI 与 SYSTEM 共读同一份。
  3. v1.0 注册计划任务时未检查 PowerShell 退出码，命令失败也当成成功，
     于是 config 里 auto_start=true 而任务实际不存在。
     v1.1 改为脚本文件 + 严格退出码 + 注册后回读校验。
"""

import ctypes
from ctypes import wintypes
import datetime
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time as _time_module
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "TimeSync"
VERSION = "1.1"

# ============================================================
# 路径 —— 机器级目录，GUI（管理员）与 SYSTEM 计划任务共用
# ============================================================
PROGRAM_DATA = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
DATA_DIR = os.path.join(PROGRAM_DATA, APP_NAME)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LOG_FILE = os.path.join(DATA_DIR, "sync.log")
TASK_EXE = os.path.join(DATA_DIR, "TimeSync.exe")

# v1.0 遗留的按用户存放的配置，仅用于一次性迁移
LEGACY_CONFIG = os.path.join(
    os.environ.get("APPDATA", ""), APP_NAME, "config.json"
)

CREATE_NO_WINDOW = 0x08000000

# 演练模式：只读网络与系统状态，不真正改时间/时区（用于排查问题）
DRY_RUN = False

NTP_SERVERS = [
    ("pool.ntp.org",      "全球 NTP 池"),
    ("asia.pool.ntp.org", "亚洲 NTP 池"),
    ("time.google.com",   "Google 时间"),
    ("time.windows.com",  "微软时间"),
]

# label / Windows 时区 ID / 参考偏移（分钟）
TZ_PRESETS = [
    ("中国 · 北京时间 (UTC+8)",          "China Standard Time",       480),
    ("哈萨克斯坦 · 阿拉木图 (UTC+5)",    "West Asia Standard Time",   300),
    ("哈萨克斯坦东部 · 阿斯塔纳 (UTC+6)", "Central Asia Standard Time", 360),
    ("巴基斯坦 · 卡拉奇 (UTC+5)",        "Pakistan Standard Time",    300),
    ("迪拜 (UTC+4)",                    "Arabian Standard Time",     240),
    ("莫斯科 (UTC+3)",                   "Russian Standard Time",     180),
    ("东欧 · 基辅/布加勒斯特 (UTC+2)",   "FLE Standard Time",         120),
    ("英国 · 伦敦 (UTC+0)",              "GMT Standard Time",           0),
    ("曼谷 (UTC+7)",                    "SE Asia Standard Time",     420),
    ("日本 · 东京 (UTC+9)",              "Tokyo Standard Time",       540),
    ("悉尼 (UTC+10)",                    "AUS Eastern Standard Time", 600),
    ("纽约 (UTC-5)",                     "Eastern Standard Time",    -300),
    ("洛杉矶 (UTC-8)",                   "Pacific Standard Time",    -480),
    ("自定义偏移",                        None,                        None),
]
CUSTOM_LABEL = TZ_PRESETS[-1][0]


# ============================================================
# 进程 / 系统调用小工具
# ============================================================
def _run(cmd, timeout=40):
    """静默执行外部命令（不闪黑框）"""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _ps(script, timeout=120):
    """执行一段 PowerShell，返回 CompletedProcess"""
    return _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_admin():
    """以管理员身份重新启动自身"""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, " ".join(sys.argv[1:])
    else:
        exe, params = sys.executable, f'"{os.path.abspath(sys.argv[0])}"'
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    sys.exit(0)


def _enable_privilege(name):
    """
    启用当前进程的某个令牌特权。
    v1.0 直接调 SetSystemTime / SetTimeZoneInformation 而没有启用特权，
    在不启用的情况下这两个 API 会以 1314（权限不足）失败。
    """
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x0002

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Privileges", LUID_AND_ATTRIBUTES * 1),
        ]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None
        )
        # AdjustTokenPrivileges 在"部分成功"时仍返回非零，必须查 last error
        return bool(ok) and ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


# ============================================================
# 系统时间
# ============================================================
class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", wintypes.WORD),
        ("wMonth", wintypes.WORD),
        ("wDayOfWeek", wintypes.WORD),
        ("wDay", wintypes.WORD),
        ("wHour", wintypes.WORD),
        ("wMinute", wintypes.WORD),
        ("wSecond", wintypes.WORD),
        ("wMilliseconds", wintypes.WORD),
    ]


class TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Bias", wintypes.LONG),
        ("StandardName", wintypes.WCHAR * 32),
        ("StandardDate", SYSTEMTIME),
        ("StandardBias", wintypes.LONG),
        ("DaylightName", wintypes.WCHAR * 32),
        ("DaylightDate", SYSTEMTIME),
        ("DaylightBias", wintypes.LONG),
    ]


def set_system_utc_time(timestamp):
    """把系统时钟设置为给定的真实 UTC 时间戳（自 1970 起的秒）"""
    if DRY_RUN:
        log(f"[dry-run] 将把系统时钟设置为 UTC "
            f"{datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc):%Y-%m-%d %H:%M:%S}")
        return True
    _enable_privilege("SeSystemtimePrivilege")
    dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    st = SYSTEMTIME(
        dt.year, dt.month, dt.isoweekday() % 7, dt.day,
        dt.hour, dt.minute, dt.second, int(dt.microsecond / 1000),
    )
    if not kernel32.SetSystemTime(ctypes.byref(st)):
        raise OSError(
            f"SetSystemTime 失败（错误码 {ctypes.get_last_error()}），请确认以管理员身份运行"
        )
    return True


# ============================================================
# 时区 —— 真正修改 Windows 时区
# ============================================================
def get_current_tz_id():
    try:
        r = _run(["tzutil", "/g"], timeout=15)
        return r.stdout.strip() or "Unknown"
    except Exception:
        return "Unknown"


def get_current_offset_minutes():
    """返回当前系统时区相对 UTC 的偏移（分钟，东为正），含夏令时"""
    info = TIME_ZONE_INFORMATION()
    rc = kernel32.GetTimeZoneInformation(ctypes.byref(info))
    if rc == 0xFFFFFFFF:
        return 0
    bias = info.Bias
    bias += info.DaylightBias if rc == 2 else info.StandardBias
    return -bias


def _fmt_offset(minutes):
    sign = "+" if minutes >= 0 else "-"
    m = abs(int(minutes))
    if m % 60 == 0:
        return f"UTC{sign}{m // 60}"
    return f"UTC{sign}{m // 60}:{m % 60:02d}"


def tz_label_for_id(tz_id):
    for label, tid, _ in TZ_PRESETS:
        if tid and tid.lower() == (tz_id or "").lower():
            return label
    return tz_id or "未知"


def set_timezone_id(tz_id):
    """用 tzutil 切换到指定的 Windows 时区（保留该时区的夏令时规则）"""
    _enable_privilege("SeTimeZonePrivilege")
    r = _run(["tzutil", "/s", tz_id], timeout=20)
    if r.returncode != 0:
        raise OSError(f"切换时区失败（{tz_id}）：{(r.stderr or r.stdout).strip()}")
    now_id = get_current_tz_id()
    if now_id.lower() != tz_id.lower():
        raise OSError(f"时区切换未生效：期望 {tz_id}，实际 {now_id}")
    return now_id


def set_fixed_offset(minutes):
    """切换到固定偏移时区（关闭夏令时），用于"自定义偏移" """
    _enable_privilege("SeTimeZonePrivilege")
    info = TIME_ZONE_INFORMATION()
    name = f"TimeSync {_fmt_offset(minutes)}"
    info.Bias = -int(minutes)
    info.StandardName = name[:31]
    info.DaylightName = name[:31]
    info.StandardBias = 0
    info.DaylightBias = 0
    info.StandardDate = SYSTEMTIME()   # 全 0 = 不使用夏令时
    info.DaylightDate = SYSTEMTIME()
    if kernel32.SetTimeZoneInformation(ctypes.byref(info)) == 0xFFFFFFFF:
        raise OSError(
            f"设置固定时区失败（错误码 {ctypes.get_last_error()}）"
        )
    if get_current_offset_minutes() != int(minutes):
        raise OSError("固定时区设置未生效")
    return minutes


def apply_timezone(mode, tz_id, offset_minutes):
    """
    按配置应用时区，返回给用户看的说明文字。
    mode: "preset" 用 Windows 时区 ID；"custom" 用固定偏移。
    """
    if DRY_RUN:
        target = (_fmt_offset(offset_minutes) if mode == "custom"
                  else tz_label_for_id(tz_id))
        log(f"[dry-run] 将切换时区 → {target}")
        return f"[演练] 将切换时区 → {target}"

    if mode == "custom":
        cur = get_current_offset_minutes()
        if cur == int(offset_minutes):
            return f"时区已为 {_fmt_offset(offset_minutes)}"
        set_fixed_offset(int(offset_minutes))
        return f"时区已切换 → {_fmt_offset(offset_minutes)}"

    if not tz_id:
        raise ValueError("未指定目标时区")
    cur_id = get_current_tz_id()
    if cur_id.lower() == tz_id.lower():
        return f"时区已为 {tz_label_for_id(tz_id)}"
    set_timezone_id(tz_id)
    return f"时区已切换 → {tz_label_for_id(tz_id)}"


# ============================================================
# NTP 客户端
# ============================================================
def get_ntp_time(server="pool.ntp.org", timeout=4):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(timeout)
    try:
        packet = b"\x1b" + b"\0" * 47
        client.sendto(packet, (server, 123))
        data, _addr = client.recvfrom(1024)
        if len(data) < 48:
            raise OSError("NTP 响应长度异常")
        unpacked = struct.unpack("!12I", data[:48])
        # 第一个 32 位字：LI(2) | VN(3) | Mode(3) | Stratum(8) | Poll(8) | Precision(8)
        mode = (unpacked[0] >> 24) & 0x7  # 4=server 5=broadcast
        stratum = (unpacked[0] >> 16) & 0xFF
        if mode not in (4, 5) or stratum == 0:
            raise OSError(f"NTP 响应无效（mode={mode}, stratum={stratum}）")
        t = unpacked[10] + unpacked[11] / 2 ** 32 - 2208988800
        return t, server
    finally:
        client.close()


def get_ntp_time_multi(servers=None, timeout=3):
    if servers is None:
        servers = [s[0] for s in NTP_SERVERS]
    errors = []
    for server in servers:
        try:
            return get_ntp_time(server, timeout=timeout)
        except Exception as e:
            errors.append(f"{server}: {e}")
    raise OSError("无法连接任何 NTP 服务器，请检查网络或防火墙（" + "；".join(errors) + "）")


# ============================================================
# 配置
# ============================================================
def _default_config():
    """默认值跟随当前系统时区，而不是硬编码某个国家"""
    cur_id = get_current_tz_id()
    cur_off = get_current_offset_minutes()
    mode = "preset" if any(p[1] for p in TZ_PRESETS if p[1] == cur_id) else "custom"
    return {
        "version": VERSION,
        "timezone_mode": mode,
        "timezone_id": cur_id,
        "timezone_offset": cur_off,
        "sync_timezone": True,
        "ntp_server": "pool.ntp.org",
        "auto_start": False,
        "last_sync": None,
    }


def load_config():
    """读取配置；v1.0 只存了 timezone_offset，这里按偏移映射回真实时区"""
    cfg = _default_config()

    src = None
    if os.path.exists(CONFIG_FILE):
        src = CONFIG_FILE
    elif LEGACY_CONFIG and os.path.exists(LEGACY_CONFIG):
        src = LEGACY_CONFIG
    if not src:
        return cfg

    try:
        with open(src, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, json.JSONDecodeError, ValueError):
        return cfg

    base_offset = raw.get("timezone_offset", cfg["timezone_offset"])
    cfg.update(raw)

    if not raw.get("timezone_id"):
        try:
            off = int(base_offset)
        except (TypeError, ValueError):
            off = cfg["timezone_offset"]
        # v1.0 里这个值是"小时"，v1.1 统一用"分钟"
        if abs(off) <= 14:
            off *= 60
        preset = next((p for p in TZ_PRESETS if p[2] == off), None)
        if preset:
            cfg["timezone_mode"] = "preset"
            cfg["timezone_id"] = preset[1]
            cfg["timezone_offset"] = preset[2]
        else:
            cfg["timezone_mode"] = "custom"
            cfg["timezone_offset"] = off
            cfg.pop("timezone_id", None)
    return cfg


def save_config(cfg):
    """写配置失败不应该影响授时本身，只记日志"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
        return True
    except OSError as e:
        log(f"配置写入失败：{e}")
        return False


def log(msg):
    """写入机器级日志，超过 200KB 自动截断"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 200 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"{'[SYSTEM]' if _is_system_account() else ''} {msg}\n"
            )
    except Exception:
        pass


def _is_system_account():
    try:
        return os.environ.get("USERNAME", "").upper() in ("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE")
    except Exception:
        return False


# ============================================================
# 计划任务 —— 开机自启
# ============================================================
TASK_NAME = "TimeSync_AutoSync"


def _get_exe_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def _query_task():
    """一次 PowerShell 调用拿到「任务是否存在 + 指向的 exe」；注册表可能有残留键，不可信"""
    ps = (
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { $exe = ($t.Actions | Select-Object -First 1).Execute; "
        "Write-Output ('TASK|1|' + $exe) } else { Write-Output 'TASK|0|' }"
        % TASK_NAME
    )
    try:
        out = (_ps(ps, timeout=40).stdout or "").strip()
    except Exception:
        return False, ""
    for line in reversed(out.splitlines()):
        if line.startswith("TASK|"):
            parts = line.split("|", 2)
            if len(parts) >= 2 and parts[1] == "1":
                return True, (parts[2] if len(parts) > 2 else "").strip()
            return False, ""
    return False, ""


def _task_exists():
    """真实查询任务是否存在（v1.0 只看 config 里的布尔值，会骗人）"""
    return _query_task()[0]


def _task_action_exe():
    return _query_task()[1]


def _install_exe_copy():
    r"""
    把 exe 复制到 C:\ProgramData\TimeSync\TimeSync.exe，
    避免用户把桌面上的 exe 删了/挪了之后计划任务失效。
    """
    if not getattr(sys, "frozen", False):
        return _get_exe_path()
    src = os.path.abspath(sys.executable)
    if src.lower() == os.path.abspath(TASK_EXE).lower():
        return TASK_EXE
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TASK_EXE + ".new"
    shutil.copy2(src, tmp)
    try:
        os.replace(tmp, TASK_EXE)
    except OSError:
        # 目标正被占用（旧任务刚好在跑），退回到直接指向源文件
        try:
            os.remove(tmp)
        except OSError:
            pass
        return src
    return TASK_EXE


PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
try {
    $exe = '__EXE__'
    $name = '__NAME__'
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $exe -Argument '-s'
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = 'PT1M'
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $logon.Delay = 'PT30S'
    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $boot,$logon -Principal $principal -Settings $settings -Force | Out-Null
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { throw '任务注册后校验失败' }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


def install_auto_start():
    """注册开机任务，失败时抛异常（带 PowerShell 的真实报错）"""
    exe = _install_exe_copy()
    os.makedirs(DATA_DIR, exist_ok=True)

    ps1_path = os.path.join(DATA_DIR, "register_task.ps1")
    script = (PS_TEMPLATE
              .replace("__EXE__", exe.replace("'", "''"))
              .replace("__NAME__", TASK_NAME))
    with open(ps1_path, "w", encoding="utf-8-sig") as f:
        f.write(script)

    r = _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", ps1_path],
        timeout=180,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"退出码 {r.returncode}"
        raise OSError(f"创建计划任务失败：{msg}")

    if not _task_exists():
        raise OSError("创建计划任务后回读失败：任务不存在")

    log(f"开机自启已注册，指向 {exe}")


def remove_auto_start():
    r = _ps(
        "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false "
        "-ErrorAction SilentlyContinue" % TASK_NAME,
        timeout=60,
    )
    if _task_exists():
        raise OSError("删除计划任务失败，请确认以管理员身份运行")
    log("开机自启已移除")


def set_auto_start(enable):
    if enable:
        install_auto_start()
    else:
        remove_auto_start()


# ============================================================
# GUI
# ============================================================
COLORS = {
    "bg":            "#ffffff",
    "fg":            "#1a1a1a",
    "fg_secondary":  "#888888",
    "accent":        "#1a73e8",
    "accent_hover":  "#1557b0",
    "accent_light":  "#e8f0fe",
    "success":       "#0d904f",
    "success_light": "#e6f4ea",
    "danger":        "#d93025",
    "danger_light":  "#fce8e6",
    "border":        "#dadce0",
    "card_bg":       "#f8f9fa",
    "separator":     "#eeeeee",
}

FONTS = {
    "title":   ("Microsoft YaHei UI", 18, "bold"),
    "heading": ("Microsoft YaHei UI", 11, "bold"),
    "body":    ("Microsoft YaHei UI", 11),
    "small":   ("Microsoft YaHei UI", 9),
    "mono":    ("Consolas", 16),
}


class TimeSyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.resizable(False, False)
        self.root.configure(bg=COLORS["bg"])

        w, h = 460, 566
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(20, (sh - h) // 2)}")

        self.config = load_config()
        self.syncing = False
        self.task_installed = False

        self._build_ui()
        self._load_ui_from_config()
        self._refresh_tz_labels()
        self._refresh_task_state()
        self._update_clock()
        self._set_icon()

    # ---------- UI ----------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=COLORS["bg"], height=60)
        header.pack(fill="x", padx=20, pady=(18, 0))
        header.pack_propagate(False)

        tk.Label(header, text=APP_NAME, font=FONTS["title"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(header, text=f"v{VERSION}", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["bg"]).pack(
            side="left", padx=(8, 0), pady=(8, 0))

        self.status_dot = tk.Canvas(header, width=10, height=10,
                                    bg=COLORS["bg"], highlightthickness=0)
        self.status_dot.pack(side="right", pady=(6, 0))
        self._draw_dot("#dadce0")

        tk.Frame(self.root, bg=COLORS["separator"], height=1).pack(
            fill="x", padx=20, pady=(6, 0))

        # 时间卡片
        card = tk.Frame(self.root, bg=COLORS["card_bg"])
        card.pack(fill="x", padx=20, pady=(14, 0))
        inner = tk.Frame(card, bg=COLORS["card_bg"])
        inner.pack(fill="x", padx=14, pady=12)

        tk.Label(inner, text="当前系统时间", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["card_bg"]).pack(anchor="w")
        self.clock_label = tk.Label(inner, text="", font=FONTS["mono"],
                                    fg=COLORS["fg"], bg=COLORS["card_bg"])
        self.clock_label.pack(anchor="w", pady=(2, 0))
        self.tz_now_label = tk.Label(inner, text="", font=FONTS["small"],
                                     fg=COLORS["fg_secondary"], bg=COLORS["card_bg"])
        self.tz_now_label.pack(anchor="w")

        # 目标时区
        tz_sec = tk.Frame(self.root, bg=COLORS["bg"])
        tz_sec.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(tz_sec, text="目标时区", font=FONTS["heading"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(anchor="w")

        self.tz_var = tk.StringVar()
        self.tz_combo = ttk.Combobox(tz_sec, textvariable=self.tz_var,
                                     state="readonly", font=("Microsoft YaHei UI", 10))
        self.tz_combo["values"] = [p[0] for p in TZ_PRESETS]
        self.tz_combo.pack(fill="x", pady=(5, 0))
        self.tz_combo.bind("<<ComboboxSelected>>", self._on_tz_changed)

        self.custom_row = tk.Frame(tz_sec, bg=COLORS["bg"])
        tk.Label(self.custom_row, text="偏移量（小时）", font=FONTS["small"],
                 fg=COLORS["fg_secondary"], bg=COLORS["bg"]).pack(side="left")
        self.custom_offset = tk.IntVar(value=8)
        ttk.Spinbox(self.custom_row, from_=-12, to=14, textvariable=self.custom_offset,
                    width=5, font=("Microsoft YaHei UI", 10),
                    state="readonly").pack(side="left", padx=8)

        # NTP
        ntp_sec = tk.Frame(self.root, bg=COLORS["bg"])
        ntp_sec.pack(fill="x", padx=20, pady=(12, 0))
        tk.Label(ntp_sec, text="NTP 服务器", font=FONTS["heading"],
                 fg=COLORS["fg"], bg=COLORS["bg"]).pack(anchor="w")
        self.ntp_var = tk.StringVar()
        self.ntp_combo = ttk.Combobox(ntp_sec, textvariable=self.ntp_var,
                                      font=("Microsoft YaHei UI", 10))
        self.ntp_combo["values"] = [f"{s[0]}  ({s[1]})" for s in NTP_SERVERS]
        self.ntp_combo.pack(fill="x", pady=(5, 0))

        # 按钮
        btn_area = tk.Frame(self.root, bg=COLORS["bg"])
        btn_area.pack(fill="x", padx=20, pady=(14, 0))
        self.sync_btn = tk.Button(
            btn_area, text="立即同步", font=FONTS["heading"],
            bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["accent_hover"], activeforeground="#ffffff",
            relief="flat", padx=20, pady=7, cursor="hand2",
            command=self._on_sync,
        )
        self.sync_btn.pack(fill="x")
        self.last_sync_label = tk.Label(btn_area, text="", font=FONTS["small"],
                                        fg=COLORS["fg_secondary"], bg=COLORS["bg"])
        self.last_sync_label.pack(pady=(5, 0))

        # 开机自启
        auto_area = tk.Frame(self.root, bg=COLORS["bg"])
        auto_area.pack(fill="x", padx=20, pady=(12, 0))
        self.auto_var = tk.BooleanVar()
        tk.Checkbutton(
            auto_area, text="开机后自动同步（静默运行）",
            variable=self.auto_var, command=self._on_auto_toggle,
            font=FONTS["body"], fg=COLORS["fg"], bg=COLORS["bg"],
            activebackground=COLORS["bg"], selectcolor=COLORS["bg"],
            cursor="hand2",
        ).pack(anchor="w")
        self.auto_state_label = tk.Label(
            auto_area, text="", font=FONTS["small"],
            fg=COLORS["fg_secondary"], bg=COLORS["bg"])
        self.auto_state_label.pack(anchor="w", pady=(2, 0))

        tk.Label(self.root, text="NTP 授时 · 真实时区切换 · 配置位于 C:\\ProgramData\\TimeSync",
                 font=("Microsoft YaHei UI", 8), fg=COLORS["fg_secondary"],
                 bg=COLORS["bg"]).pack(side="bottom", pady=(0, 10))

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=color, outline="")

    # ---------- 数据 ----------
    def _load_ui_from_config(self):
        mode = self.config.get("timezone_mode", "preset")
        tz_id = self.config.get("timezone_id")
        idx = -1
        if mode == "preset" and tz_id:
            for i, p in enumerate(TZ_PRESETS):
                if p[1] and p[1].lower() == tz_id.lower():
                    idx = i
                    break
        if idx < 0:
            idx = len(TZ_PRESETS) - 1
            self.config["timezone_mode"] = "custom"
            self.config["timezone_offset"] = int(
                self.config.get("timezone_offset", get_current_offset_minutes()))
        self.tz_combo.current(idx)
        self.custom_offset.set(int(self.config.get("timezone_offset", 480)) // 60)

        ntp = self.config.get("ntp_server", "pool.ntp.org")
        for i, s in enumerate(NTP_SERVERS):
            if s[0] == ntp:
                self.ntp_combo.current(i)
                break
        else:
            self.ntp_var.set(ntp)

        self._on_tz_changed()

        last = self.config.get("last_sync")
        if last:
            self.last_sync_label.config(text=f"上次同步: {last}")

    def _on_tz_changed(self, event=None):
        idx = self.tz_combo.current()
        if idx < 0:
            return
        if TZ_PRESETS[idx][1] is None:
            self.custom_row.pack(fill="x", pady=(6, 0), after=self.tz_combo)
        else:
            self.custom_row.pack_forget()

    def _selected_tz(self):
        idx = self.tz_combo.current()
        if idx < 0:
            idx = 0
        label, tz_id, _off = TZ_PRESETS[idx]
        if tz_id is None:
            return "custom", None, int(self.custom_offset.get()) * 60
        return "preset", tz_id, _off

    def _get_ntp_server(self):
        text = self.ntp_var.get().strip()
        if "(" in text:
            text = text.split("(")[0].strip()
        return text or "pool.ntp.org"

    # ---------- 时钟 ----------
    def _refresh_tz_labels(self):
        cur_id = get_current_tz_id()
        off = get_current_offset_minutes()
        self.tz_now_label.config(text=f"系统时区：{tz_label_for_id(cur_id)}  ·  {_fmt_offset(off)}")

    def _update_clock(self):
        self.clock_label.config(
            text=datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.root.after(1000, self._update_clock)

    def _set_icon(self):
        icon_path = None
        for name in ("icon.ico", "extracted_ico.ico"):
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            candidate = os.path.join(base, name)
            if os.path.exists(candidate):
                icon_path = candidate
                break
        if not icon_path:
            return
        try:
            self.root.iconbitmap(icon_path)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
            hwnd = user32.GetAncestor(self.root.winfo_id(), 3)
            LR_LOADFROMFILE = 0x10
            for icon_type, metric_x, metric_y in ((0, 49, 50), (1, 11, 12)):
                cx, cy = user32.GetSystemMetrics(metric_x), user32.GetSystemMetrics(metric_y)
                hicon = user32.LoadImageW(0, icon_path, 1, cx, cy, LR_LOADFROMFILE)
                if hicon:
                    user32.SendMessageW(hwnd, 0x0080, icon_type, hicon)
        except Exception:
            pass

    # ---------- 同步 ----------
    def _on_sync(self):
        if self.syncing:
            return
        self.syncing = True
        self.sync_btn.config(text="同步中…", state="disabled", bg=COLORS["fg_secondary"])
        self._draw_dot("#f9ab00")

        mode, tz_id, offset = self._selected_tz()
        server = self._get_ntp_server()

        def work():
            try:
                others = [s[0] for s in NTP_SERVERS if s[0] != server]
                utc_ts, used = get_ntp_time_multi([server] + others, timeout=4)
                tz_msg = apply_timezone(mode, tz_id, offset)
                set_system_utc_time(utc_ts)

                self.config.update({
                    "timezone_mode": mode,
                    "timezone_id": tz_id,
                    "timezone_offset": offset,
                    "sync_timezone": True,
                    "ntp_server": used,
                    "last_sync": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                if mode == "custom":
                    self.config.pop("timezone_id", None)
                save_config(self.config)
                log(f"手动同步成功：{tz_msg}，源 {used}")

                utc_text = datetime.datetime.fromtimestamp(
                    utc_ts, datetime.timezone.utc).strftime("%H:%M:%S")
                self.root.after(0, lambda: self._sync_done(
                    True, f"{tz_msg} · UTC {utc_text} · {used}"))
            except Exception as e:
                log(f"手动同步失败：{e}")
                self.root.after(0, lambda: self._sync_done(False, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _sync_done(self, success, msg):
        self.syncing = False
        self._refresh_tz_labels()
        if success:
            self.sync_btn.config(text="已同步", bg=COLORS["success"], state="normal")
            self._draw_dot(COLORS["success"])
            self.last_sync_label.config(
                text=f"上次同步: {datetime.datetime.now():%H:%M:%S}  ·  {msg}")
            self.root.after(3000, self._reset_btn)
        else:
            self.sync_btn.config(text="同步失败，点击重试", bg=COLORS["danger"], state="normal")
            self._draw_dot(COLORS["danger"])
            self.last_sync_label.config(text=f"失败：{msg}")
            messagebox.showerror("同步失败", msg)

    def _reset_btn(self):
        self.sync_btn.config(text="立即同步", bg=COLORS["accent"])
        self._draw_dot("#dadce0")

    # ---------- 自启 ----------
    def _refresh_task_state(self):
        self.auto_state_label.config(text="正在检测计划任务…", fg=COLORS["fg_secondary"])

        def work():
            exists, exe = _query_task()
            self.root.after(0, lambda: self._apply_task_state(exists, exe))

        threading.Thread(target=work, daemon=True).start()

    def _apply_task_state(self, exists, exe):
        self.task_installed = exists
        self.auto_var.set(exists)
        if self.config.get("auto_start") != exists:
            self.config["auto_start"] = exists
            save_config(self.config)
        if exists:
            self.auto_state_label.config(
                text=f"已生效 · 计划任务 TimeSync_AutoSync → {exe or '未知路径'}",
                fg=COLORS["success"])
        else:
            self.auto_state_label.config(
                text="未生效 · 系统里没有对应的计划任务", fg=COLORS["fg_secondary"])

    def _on_auto_toggle(self):
        enable = self.auto_var.get()
        self.auto_state_label.config(text="正在写入计划任务…", fg=COLORS["fg_secondary"])
        self.root.update_idletasks()

        def work():
            ok, msg = True, ""
            try:
                set_auto_start(enable)
                self.config["auto_start"] = enable
                save_config(self.config)
            except Exception as e:
                ok, msg = False, str(e)
                log(f"设置开机自启失败：{e}")
            self.root.after(0, lambda: self._auto_toggle_done(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _auto_toggle_done(self, ok, msg):
        if not ok:
            messagebox.showerror("设置失败", msg)
        self._refresh_task_state()


# ============================================================
# 静默同步（计划任务以 SYSTEM 身份调用）
# ============================================================
def silent_sync():
    cfg = load_config()
    log(f"静默同步开始：mode={cfg.get('timezone_mode')} tz={cfg.get('timezone_id')} "
        f"offset={cfg.get('timezone_offset')}")

    tz_msg = "时区未改动"
    if cfg.get("sync_timezone", True):
        try:
            tz_msg = apply_timezone(
                cfg.get("timezone_mode", "preset"),
                cfg.get("timezone_id"),
                int(cfg.get("timezone_offset", get_current_offset_minutes())),
            )
            log(tz_msg)
        except Exception as e:
            tz_msg = f"时区设置失败：{e}"
            log(tz_msg)

    # 开机阶段网络/DNS 往往还没就绪：最多等 3 分钟，共 12 次尝试
    server = cfg.get("ntp_server") or "pool.ntp.org"
    servers = [server] + [s[0] for s in NTP_SERVERS if s[0] != server]
    last_error = ""
    for attempt in range(1, 13):
        try:
            utc_ts, used = get_ntp_time_multi(servers, timeout=5)
            set_system_utc_time(utc_ts)
            cfg["last_sync"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cfg["ntp_server"] = used
            save_config(cfg)
            log(f"静默同步成功（第 {attempt} 次尝试，源 {used}）：{tz_msg}")
            return True
        except Exception as e:
            last_error = str(e)
            log(f"第 {attempt} 次尝试失败：{last_error}")
            if attempt < 12:
                _time_module.sleep(15)

    log(f"静默同步最终失败：{last_error}")
    return False


# ============================================================
# 自检（python time_sync.py --selfcheck）
# ============================================================
def selfcheck():
    info = {
        "version": VERSION,
        "admin": is_admin(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": DATA_DIR,
        "config_file": CONFIG_FILE,
        "config_exists": os.path.exists(CONFIG_FILE),
        "legacy_config_exists": bool(LEGACY_CONFIG) and os.path.exists(LEGACY_CONFIG),
        "current_tz_id": get_current_tz_id(),
        "current_offset_minutes": get_current_offset_minutes(),
        "task_exists": _task_exists(),
        "task_action": _task_action_exe(),
    }
    try:
        utc_ts, used = get_ntp_time_multi(timeout=4)
        info["ntp"] = {
            "server": used,
            "utc": datetime.datetime.fromtimestamp(utc_ts, datetime.timezone.utc).isoformat(),
            "local_now": datetime.datetime.now().isoformat(),
            "drift_seconds": round(utc_ts - _time_module.time(), 2),
        }
    except Exception as e:
        info["ntp_error"] = str(e)
    text = json.dumps(info, ensure_ascii=False, indent=2)

    # 打包成无控制台 exe 时 print 无处可去，落一份文件方便排查
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "selfcheck.json"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    try:
        print(text)
    except Exception:
        pass


# ============================================================
# 入口
# ============================================================
def main():
    global DRY_RUN
    DRY_RUN = "--dry-run" in sys.argv

    if "--selfcheck" in sys.argv:
        selfcheck()
        return

    if "--silent" in sys.argv or "-s" in sys.argv:
        # 计划任务已用最高权限 / SYSTEM 运行，无需再提权
        ok = silent_sync()
        sys.exit(0 if ok else 1)

    if DRY_RUN:
        selfcheck()
        return

    if not is_admin():
        request_admin()
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    root = tk.Tk()
    TimeSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
