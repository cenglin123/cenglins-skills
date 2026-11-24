# 机制篇：Win+X 菜单、ConsoleHostShortcutTarget 与 WinX 哈希

> 本文只讲"系统实际是怎么运作的"。操作步骤见 `toolkit/README.md`，判断方法见 `troubleshooting.md`。

## 1. Win+X 的 PowerShell 条目并不启动 WinX 目录里的 .lnk

直觉做法是去改 `%LOCALAPPDATA%\Microsoft\Windows\WinX\Group3\01a - Windows PowerShell.lnk`。
实际不是这样：TWINUI 对这两个条目（`Windows PowerShell(I)` / `Windows PowerShell (管理员)(A)`）
启动的是注册表指向的**开始菜单快捷方式**：

```
HKLM\SOFTWARE\Microsoft\PowerShell\3 : ConsoleHostShortcutTarget
  → %APPDATA%\Microsoft\Windows\Start Menu\Programs\Windows PowerShell\Windows PowerShell.lnk
```

TWINUI 用它确定条目的**身份、显示名、I/A 快捷键与提权行为**。

因此：**改这一个开始菜单快捷方式的目标 = 完整接管 Win+X I/A，且菜单外观、快捷键、"（管理员）"提权
一律保持不变**，完全不用碰受哈希保护的 WinX 目录。这是最小侵入接管点。

副作用（可接受）：开始菜单里的 "Windows PowerShell" 项也会指向新目标。

自查命令：

```powershell
(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\PowerShell\3').ConsoleHostShortcutTarget
$lnk = [Environment]::ExpandEnvironmentVariables($that)
(New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath
```

## 2. 若确实要改 WinX 目录：哈希校验机制

WinX 目录里的每个 .lnk 必须携带合法哈希，否则条目直接从菜单消失（无报错、无日志）。

- **存放位置**：lnk 属性存储中的 `PKEY {FB8D2D7B-90D1-4E34-BF60-6EAC09922BBF}:2`（VT_UI4）
- **算法**：`HashData`（shlwapi）作用于

  ```
  utf16le( lower( generalize(TargetParsingPath) + Arguments + SALT ) )
  ```

- **generalize**：仅替换三个前缀

  | 前缀 | 替换为 |
  |---|---|
  | `%ProgramFiles%` | `{905E63B6-C1BF-494E-B29C-65B732D3D21A}` |
  | `%SystemRoot%\System32` | `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}` |
  | `%SystemRoot%` | `{F38BF404-1D43-42F2-9305-67DE0B28FC23}` |

- **盐值陷阱**：公开 hashlnk 源码里的盐是
  `do not prehash links. this should only be done by the user.`（`links.` 后**一个空格**），
  而 Win10 22H2（19045.x）`twinui.dll` 里实际是
  `Do not prehash links.  This should only be done by the user.`（**两个空格**）。用错盐则全部校验失败。

  不要相信任何文档里的盐（包括本文）——**从本机 dll 里取**：

  ```
  python toolkit\tools\winxhash.py salt
  ```

- **显示名与快捷键**（I/A、"（管理员）"后缀）由 TWINUI 按"目标是否为它认识的系统控制台"硬编码分配，
  自定义目标拿不到这些待遇——这正是第 1 节接管点更优的原因。

### 2.1 本分发版实测修正：Arguments 必须从 .lnk 二进制读

原始笔记的算法描述正确，但直接实现会踩一个坑：属性存储的 `System.Link.Arguments`
（`{436BF266-43E4-4B1C-92B2-88F52888B7B1}:2`）**并非对每个快捷方式都可读**，读空后哈希就算错。

实测（Win10 19045，14 个微软自带 WinX 快捷方式做 oracle）：

| Arguments 来源 | 校验通过 |
|---|---|
| 仅属性存储 | 8 / 14（带参数的 Run、Search、资源管理器、任务管理器、设备管理器全失败）|
| 属性存储 + 回退到 .lnk 二进制 StringData | **14 / 14** |

`winxhash.py` 现在按 [MS-SHLLINK] 直接解析 `COMMAND_LINE_ARGUMENTS` 作为回退。
`ms-settings:` / AppUserModelID 类目标（控制面板、系统关于、电源和睡眠等）不是文件路径，
无法重建 parsing path，`verify` 会将其标记为 SKIP 而不是 FAIL。

跑一次 oracle 自证（**写任何哈希之前必做**）：

```
python toolkit\tools\winxhash.py verify
```

### 2.2 `addname.py` 的 LinkFlags 修正

原脚本插入 NAME_STRING 后置位 `0x8`，而按 [MS-SHLLINK]：

| 位 | 含义 |
|---|---|
| 0x1 | HasLinkTargetIDList |
| 0x2 | HasLinkInfo |
| **0x4** | **HasName** |
| 0x8 | HasRelativePath |
| 0x10 | HasWorkingDir |
| 0x20 | HasArguments |
| 0x40 | HasIconLocation |

置 `0x8` 会让解析器把刚插入的名字当成 RELATIVE_PATH。本分发版已改为 `0x4`。

## 3. 改快捷方式目标：二进制补丁 vs 重建

| 方式 | 做法 | 保留属性存储/AppUserModelID | 实测可靠性 |
|---|---|---|---|
| 二进制补丁 | 原地替换嵌入的路径字符串，用 NUL 补齐到原长度（不移动任何偏移） | 是 | 目标路径同时存在于 **LinkTargetIDList** 时，shell 仍按 IDList 解析出旧目标 → 补丁无效 |
| 重建 | `WScript.Shell.CreateShortcut(...).TargetPath = ...` | 否（属性存储丢失） | 稳定生效 |

`install.ps1` 的默认 `-Method Auto` 因此是：**先补丁 → 读回验证 → 不生效则自动改用重建**。
实测在已被接管过的机器上补丁替换了 1 处字符串但 shell 仍解析旧目标，自动回落到重建后通过。

新路径不能比原路径长（补丁按原槽位 NUL 补齐）；不够长时脚本会自动改用 8.3 短路径形式再试。

## 4. 控制台交接（默认终端）链路

与 Win+X 接管相互独立的另一条链路：

```
控制台客户端启动
  → conhost 读 HKCU\Console\%%Startup 的 DelegationConsole / DelegationTerminal
  → COM 激活 OpenConsole（-Embedding）
  → OpenConsole 查 com.microsoft.windows.console.host 应用扩展目录
  → 激活 WindowsTerminal
  → 交接会话
```

已知 GUID：

| 值 | 含义 |
|---|---|
| `{2EACA947-7F5F-4CFA-BA87-8F7FBEEFBE69}` | Windows Terminal 的 OpenConsole |
| `{E12CFF52-A866-4C77-9A90-F570A7AA2C6B}` | Windows Terminal 终端 |
| 全零 GUID | "让 Windows 决定"（Win10 上 = 旧版 conhost）|

前置条件：Win10 需 22H2 且 ≥ 19045.3031，Windows Terminal ≥ 1.17。

交接正常时根本不需要接管 Win+X——先用 `diagnose.ps1` 判定，再决定是否安装。
