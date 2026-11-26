================================================================================
  TimeSync v1.1 - Lightweight NTP Time Synchronization Tool
  TimeSync v1.1 - 轻量级 NTP 时间同步工具
================================================================================

[EN] A small Windows time tool built with Python/Tkinter: real Windows timezone
     switching, NTP time sync, and a silent sync task that runs at boot.

[CN] 基于 Python/Tkinter 的 Windows 时间小工具：真正切换 Windows 时区、NTP 授时、
     开机静默自动同步。


================================================================================
  WHAT'S NEW IN v1.1 / v1.1 修复了什么
================================================================================

  1) Timezone was never actually changed / 时区从来没真正改过
     v1.0 只把"被时区偏移换算过的假 UTC"写进系统时钟，Windows 的时区设置一直没动。
     于是"设置-时间和语言"里看到的仍是旧时区，Windows 时间服务一校正，时间又跳回去。
     v1.1 改为真正调用 SetTimeZoneInformation / tzutil 切换系统时区，
     再写入 NTP 的真实 UTC。系统时区和时间从此一致。

  2) Silent sync always used Kazakhstan (UTC+5) / 静默同步永远用哈萨克斯坦
     v1.0 的计划任务以 SYSTEM 身份运行，却去读 %APPDATA%\TimeSync\config.json。
     SYSTEM 的 %APPDATA% 是另一个目录，读不到就回落到代码里的默认值
     timezone_offset = 5（哈萨克斯坦），所以每次开机都被拉回 UTC+5。
     v1.1 把配置放到机器级目录 C:\ProgramData\TimeSync\config.json，
     GUI（管理员）和 SYSTEM 任务读的是同一份；默认值也改为"跟随当前系统时区"。

  3) "Auto-start" showed ON but no task existed / 勾了开机自启，任务其实没建成
     v1.0 用 powershell -Command 注册任务，却不检查退出码；PowerShell 打印红色报错
     但进程仍返回 0，程序就当成成功，还把 auto_start=true 写进配置。
     v1.1 改为生成 .ps1 脚本文件执行，检查退出码，注册后再回读校验；
     界面上的勾选状态也改为实时查询系统，不再相信配置文件。

  4) 其他 / Misc
     - 启用 SeSystemtimePrivilege / SeTimeZonePrivilege，避免 API 1314 权限错误
     - NTP 响应校验（mode/stratum），对齐 RFC 4330
     - exe 自动复制到 C:\ProgramData\TimeSync，桌面上的 exe 被删/挪也不影响开机任务
     - 静默模式最多等 3 分钟网络（12 次 × 15 秒），开机 DNS 未就绪也能成功
     - 失败时返回非 0 退出码，配合计划任务的"失败重试 3 次"
     - 日志：C:\ProgramData\TimeSync\sync.log


================================================================================
  USAGE / 使用方式
================================================================================

  1. 双击 TimeSync v1.1.exe（会弹 UAC，必须允许管理员权限）
  2. 在"目标时区"里选择要锁定的时区（如 中国 · 北京时间 UTC+8）
  3. 点"立即同步"：切换系统时区 + 按 NTP 校准时钟
  4. 勾选"开机后自动同步（静默运行）"，确认下面显示"已生效"
  5. 想立刻验证：重启电脑，登录后看时间是否自动正确

  命令行：
    TimeSync v1.1.exe -s              静默同步一次（计划任务用，无需登录）
    TimeSync v1.1.exe --selfcheck     输出诊断信息到 C:\ProgramData\TimeSync\selfcheck.json
    TimeSync v1.1.exe --verify        立即触发一次开机任务并记录结果到 verify.json
    TimeSync v1.1.exe --dry-run -s    演练：只测网络和判断，不改时间和时区
    TimeSync v1.1.exe --install-autostart     无界面注册开机任务（需管理员）
    TimeSync v1.1.exe --uninstall-autostart   移除开机任务（需管理员）


================================================================================
  FILES / 文件位置
================================================================================

  C:\ProgramData\TimeSync\config.json        配置（GUI 与计划任务共用）
  C:\ProgramData\TimeSync\sync.log           同步日志
  C:\ProgramData\TimeSync\TimeSync.exe       计划任务实际调用的副本
  C:\ProgramData\TimeSync\selfcheck.json     自检结果
  计划任务名：TimeSync_AutoSync（SYSTEM，开机 + 登录 两个触发器）

  v1.0 的 %APPDATA%\TimeSync\config.json 会在首次运行时自动迁移，
  之后可以删掉旧文件。


================================================================================
  TROUBLESHOOTING / 排查
================================================================================

  - 勾选后仍显示"未生效"：UAC 没给权限。关掉程序，右键"以管理员身份运行"。
  - 时间对了但时区名没变：确认选的是预设时区；"自定义偏移"会显示为
    "TimeSync UTC+X"，因为 Windows 没有对应的具名时区，这是正常的。
  - 想看开机那次到底干了什么：看 sync.log，带 [SYSTEM] 的行就是计划任务写的。
  - 时间仍被拉回别的值：检查是否有其它授时软件或域策略在改时间。


================================================================================
  BUILD FROM SOURCE / 从源码构建
================================================================================

  Requirements: Python 3.9+, PyInstaller

    pip install pyinstaller
    pyinstaller --onefile --windowed --icon icon.ico --add-data "icon.ico;." ^
                --name "TimeSync v1.1" time_sync.py

  The built EXE will be in the dist/ folder.


================================================================================
  TECH STACK / 技术栈
================================================================================

  - Python 3.12 + Tkinter（零第三方依赖）
  - ctypes: SetSystemTime / SetTimeZoneInformation / AdjustTokenPrivileges
  - tzutil: 具名时区切换
  - socket: 原生 SNTP 客户端（RFC 4330）
  - PowerShell: Register-ScheduledTask（SYSTEM + 开机/登录触发器）
  - PyInstaller: 单文件打包


================================================================================
  LICENSE / 许可证
================================================================================

  MIT License - see LICENSE.txt for details.
