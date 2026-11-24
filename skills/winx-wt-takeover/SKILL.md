---
name: winx-wt-takeover
description: Diagnose and fix "Win+X PowerShell entry does not open Windows Terminal" / broken Windows console handoff (default terminal) on Windows 10/11. Use when a user reports that Win+X, Win+R or new consoles always land in the legacy conhost window, when the default-terminal setting has no effect, when Windows Terminal must be launched from the Win+X menu, or when WinX menu shortcuts vanish after being edited (hash validation). Also covers the general method for retargeting shell shortcuts safely and for reverse-engineering the WinX hash salt from twinui.dll.
---

# Win+X → Windows Terminal 接管 / 控制台交接排障

## 何时用本 skill

- 用户说"Win+X 打开的还是旧版 PowerShell 窗口""默认终端设了没用""交接失效"
- 用户想让 Win+X 的 I/A 两项打开 Windows Terminal（保留快捷键与提权行为）
- 用户改了 WinX 目录的 .lnk 之后条目从菜单里消失了（哈希校验失败）

不适用：只是想设置默认终端且交接本来就正常（直接改设置即可，见下面第 1 步的判定）。

## 硬性顺序：先测，再改，改完读回

### 1. 只读诊断（永远第一步）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/diagnose.ps1
```

看 `live handoff measurement` 段：

- `Windows Terminal (WT_SESSION=<guid>)` → 交接正常，**不要安装接管**，让用户在"设置 → 隐私和安全性 →
  开发者选项 → 终端"里选 Windows Terminal 即可。
- `LEGACY conhost` → 交接不生效，进入第 2 步。

判定"新控制台落在哪个终端"必须用 `%WT_SESSION%` 探针，禁止用截图肉眼判断窗口外观。

### 2. 决定：修根因 还是 绕过

按 `references/troubleshooting.md` 的分层隔离表（L0 版本前置 → L6 端到端）逐层证伪。
出现下列任一组合即判定为系统深层损伤，**停止点修**：跨补丁复现 / 跨账户复现 / 跨 WT 版本复现 /
各层单测正常但端到端不通 / 伴随 appx 注册损坏。

此时二选一：保留文件的原位修复安装（根治），或安装接管（绕过，不依赖交接被修好）。
向用户说明这两条路的区别再动手，不要替用户决定重装系统。

### 3. 安装接管

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -DryRun   # 先空跑
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

原理（细节见 `references/mechanism.md`）：TWINUI 启动的是
`HKLM\SOFTWARE\Microsoft\PowerShell\3:ConsoleHostShortcutTarget` 指向的开始菜单快捷方式，
不是 WinX 目录里的 .lnk。脚本只改这一个用户级 .lnk：备份 → 编译包装器 → 二进制补丁 →
读回验证 → 不生效自动改用重建 → 再读回。

**不要**手工去改 WinX 目录来实现这个需求：要对抗哈希校验，还拿不到 TWINUI 硬编码的显示名与 I/A 快捷键。

### 4. 验证（不许只凭"脚本没报错"）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/diagnose.ps1            # takeover state 段
powershell -ExecutionPolicy Bypass -File scripts/test-winx-key.ps1 -Key I
powershell -ExecutionPolicy Bypass -File scripts/test-winx-key.ps1 -Key A
```

`test-winx-key.ps1` 合成 Win+X + 加速键并截屏，走的是真实菜单。需要交互式桌面会话；
`-Key A` 在未配置静默提权的机器上会弹 UAC，此时需要用户配合点确认。

### 5. 还原

```powershell
powershell -ExecutionPolicy Bypass -File scripts/uninstall.ps1 [-RemoveLauncher]
```

## 若确实要改 WinX 目录（少见）

条目必须带合法哈希，否则静默消失。**写之前必须先自证**：

```powershell
python scripts/winxhash.py salt      # 从本机 twinui.dll 提取盐值，不要相信文档里的常量
python scripts/winxhash.py verify    # 用微软自带快捷方式做 oracle，必须 0 mismatched
python scripts/winxhash.py write <lnk> <target> [args]
```

`verify` 出现 FAIL 时禁止 write。`SKIP`（ms-settings: 等非文件目标）属正常。

## 操作纪律（本 skill 的核心）

- 动终端进程前先看父进程链——你的会话很可能就宿在那个 WindowsTerminal 里，`Stop-Process` 会自杀。
- 比对系统文件只用 SHA256，不用 FileVersionInfo（System32 的版本信息按路径缓存，会撒谎）。
- 每个写操作后立刻读回它自己的产物再进入下一步；一次只动一个变量。
- 不替换系统文件、不改注册表、不动 WinX 目录——除非用户明确要求且已跑通 oracle 自证。

## 参考

- `references/mechanism.md` — 接管点、WinX 哈希算法与盐值陷阱、二进制补丁 vs 重建
- `references/troubleshooting.md` — 交接链路分层隔离测试、判定方法、止损线
- `references/methodology.md` — 8 条可迁移排障方法论
- `references/diagnostic-logs.md` — 真实故障机脱敏日志 + 判读
