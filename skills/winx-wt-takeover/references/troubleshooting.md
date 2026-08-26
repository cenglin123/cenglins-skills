# 排障篇：控制台交接（默认终端 → Windows Terminal）

> 目标：把"新开的控制台落在哪个终端"这件事**测出来**，而不是看窗口猜。
> 全流程只读，普通权限即可；一键版：`toolkit\diagnose.ps1`。

## 0. 先回答"我自己在哪儿跑"

排障脚本常常宿于用户当前的 Windows Terminal 窗口。此时任何
`Stop-Process -Name WindowsTerminal` 都会**杀掉你自己的会话**（连同排障进度）。

```powershell
$env:WT_SESSION                      # 有 GUID = 你在 WT 里
Get-CimInstance Win32_Process -Filter "ProcessId=$PID" | Select ParentProcessId
```

必须重启终端进程时，用计划任务/`schtasks` 脱离当前会话执行，别在会话内自杀。

## 1. 判定：新控制台落在哪个终端

**唯一可靠判据**：让新开的控制台自己回显 `%WT_SESSION%`。

```powershell
$out = "$env:TEMP\probe.txt"
Start-Process cmd.exe -ArgumentList '/c', "echo %WT_SESSION%>`"$out`""
Start-Sleep 2; Get-Content $out
```

| 输出 | 结论 |
|---|---|
| 一个 GUID | 交接成功，落在 Windows Terminal |
| 字面量 `%WT_SESSION%` 或空 | 落在旧版 conhost（交接未发生）|

不要用"看窗口长得像不像"判定：WT 与 conhost 在主题/字体接近时肉眼极易看错。

## 2. 分层隔离测试（全部普通权限可做）

交接失败时，从下往上逐层证伪，**每层只动一个变量**：

| 层 | 检查什么 | 怎么查 |
|---|---|---|
| L0 版本前置 | Win10 ≥ 19045.3031；WT ≥ 1.17 | `diagnose.ps1` 的 environment 段 |
| L1 注册表意图 | `HKCU\Console\%%Startup` 的 `DelegationConsole`/`DelegationTerminal` | `Get-ItemProperty -LiteralPath 'HKCU:\Console\%%Startup'`；全零 GUID = 旧版 |
| L2 包注册 | WT 包是否完整注册、Status 是否 OK | `Get-AppxPackage Microsoft.WindowsTerminal*`，必要时看 manifest |
| L3 COM 激活 | 能否 `CoCreateInstance` 出 OpenConsole/终端接口（各 IID 分别试）| 小脚本调 COM；失败码比"没反应"信息量大得多 |
| L4 扩展目录 | `com.microsoft.windows.console.host` 扩展是否被查得到 | Python `winsdk` 的 `AppExtensionCatalog` |
| L5 打包应用激活 | `IApplicationActivationManager::ActivateApplication`（CLSID `45BA127D-...`）能否拉起 WT | 小脚本调 COM |
| L6 端到端 | 第 1 节的 `%WT_SESSION%` 探针 | `diagnose.ps1` 的 live handoff measurement |

进程树旁证：交接尝试发生时会出现 `OpenConsole.exe -Embedding`（父进程 svchost）；
成功则随后出现新的 `WindowsTerminal` / `OpenConsole --headless` 对。

## 3. 比对系统文件：只认 SHA256

`FileVersionInfo` 对 System32 里的文件**会撒谎**（版本信息按路径缓存，同一内容放不同路径读数不同）。
比较 conhost.exe / OpenConsole.exe 是否被换过，一律用哈希：

```powershell
Get-FileHash C:\Windows\System32\conhost.exe -Algorithm SHA256
```

替换系统文件前先记录原文件哈希，还原后再比一次——这是唯一能证明"确实还原了"的证据。

## 4. 什么时候停止修复，直接原位重装

出现下列组合，说明是系统深层损伤，继续点修的期望收益很低：

- 交接失败**跨补丁**复现（换 conhost/OpenConsole 版本无效）
- **跨账户**复现（新建本地用户仍然失败）
- **跨 WT 版本**复现（升级/降级 WT 均无效）
- 各层单独测试都"正常"，但端到端不通
- 伴随 appx 注册曾损坏 / sfc 有过修复记录

此时**保留文件的原位修复安装**（挂载同版本 ISO 跑 setup.exe，选保留个人文件和应用）
比继续逐点尝试划算。若不愿重装，就走本工具包的接管方案——它绕过交接链路，
不依赖交接被修好。

## 5. 接管方案的验证

安装后必须**读回验证**，不要凭"脚本没报错"下结论：

```powershell
# 1) 快捷方式目标是否真的换了
powershell -File toolkit\diagnose.ps1        # Win+X takeover state 段

# 2) 菜单真身验证（合成按键 + 截屏，无需人工配合）
powershell -File toolkit\tools\test-winx-key.ps1 -Key I
powershell -File toolkit\tools\test-winx-key.ps1 -Key A   # 提权项，可能弹 UAC
```

`test-winx-key.ps1` 走的是真实菜单（TWINUI 的实际行为），比检查 .lnk 文件更有说服力。
无自动化 MCP 时，`keybd_event` 合成按键 + `System.Drawing.CopyFromScreen` 截屏就是够用的 GUI 兜底。

## 6. 已知副作用与限制

- `Win+R` 里直接敲 `powershell` 仍落旧版窗口——接管只覆盖 Win+X 的两个条目，交接损坏本身没修。
- 开始菜单的 "Windows PowerShell" 项同样指向新目标。
- Store 更新 WT 不影响接管（包装器走 `wt.exe` 别名，不绑版本）。
- WT 未安装/别名缺失时，包装器回退到真正的 `powershell.exe`，菜单不会变成死项。
