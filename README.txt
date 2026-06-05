================================================================================
  TimeSync - Lightweight NTP Time Synchronization Tool
  TimeSync - 轻量级 NTP 时间同步工具
================================================================================

[EN] A lightweight Windows time synchronization tool built with Python/Tkinter.
Supports NTP time sync, custom timezone offset, and silent auto-sync on startup.

[CN] 基于 Python/Tkinter 开发的轻量级 Windows 时间同步工具。
支持 NTP 授时同步、自定义时区偏移、开机静默自动同步。


================================================================================
  FEATURES / 功能
================================================================================

[EN]
  - NTP time sync from multiple servers (pool.ntp.org, time.google.com, etc.)
  - Custom timezone offset (UTC+5, UTC+8, UTC-5, and more presets)
  - Silent auto-sync on system startup (no UAC popup)
  - Clean, modern GUI with real-time clock display
  - Auto-start via Windows Task Scheduler (SYSTEM account, no login required)
  - Retry on network failure (3x in-code + 3x task-level)

[CN]
  - 多 NTP 服务器同步 (pool.ntp.org / time.google.com 等)
  - 自定义时区偏移 (UTC+5 / UTC+8 / UTC-5 等多种预设)
  - 开机静默自动同步 (无 UAC 弹窗打扰)
  - 简洁现代的 GUI 界面，实时时钟显示
  - 通过 Windows 任务计划程序开机自启 (SYSTEM 账户，无需登录)
  - 网络未就绪时自动重试 (代码级 3 次 + 任务级 3 次)


================================================================================
  USAGE / 使用方式
================================================================================

[EN]
  1. Download TimeSync-V2.exe from Releases
  2. Run it (requires Administrator privileges for time sync)
  3. Select your target timezone from the dropdown
  4. Click "Sync Now" to synchronize immediately
  5. Check "Auto-start on boot" to enable silent sync on startup

  Command-line silent mode:
    TimeSync-V2.exe -s        (used by scheduled task)

[CN]
  1. 从 Releases 下载 TimeSync-V2.exe
  2. 运行程序 (时间同步需要管理员权限)
  3. 从下拉菜单选择目标时区
  4. 点击 "立即同步" 同步时间
  5. 勾选 "开机自启" 启用静默自动同步

  命令行静默模式:
    TimeSync-V2.exe -s        (由计划任务调用)


================================================================================
  BUILD FROM SOURCE / 从源码构建
================================================================================

[EN]
  Requirements: Python 3.8+, PyInstaller

    pip install pyinstaller
    pyinstaller --onefile --windowed --noconsole --icon=icon.ico time_sync.py

  The built EXE will be in the dist/ folder.

[CN]
  依赖: Python 3.8+, PyInstaller

    pip install pyinstaller
    pyinstaller --onefile --windowed --noconsole --icon=icon.ico time_sync.py

  打包后的 EXE 位于 dist/ 目录。


================================================================================
  TECH STACK / 技术栈
================================================================================

[EN]
  - Python 3.11
  - Tkinter (GUI, built-in, no external dependencies)
  - ctypes (Windows SetSystemTime API)
  - socket (raw NTP client, RFC 868)
  - PowerShell Register-ScheduledTask (auto-start)
  - PyInstaller (packaging)

[CN]
  - Python 3.11
  - Tkinter (GUI，内置，零外部依赖)
  - ctypes (Windows SetSystemTime 系统 API)
  - socket (原始 NTP 客户端，RFC 868 协议)
  - PowerShell Register-ScheduledTask (开机自启)
  - PyInstaller (打包)


================================================================================
  LICENSE / 许可证
================================================================================

  MIT License - see LICENSE.txt for details.
  MIT 许可证 - 详见 LICENSE.txt。
