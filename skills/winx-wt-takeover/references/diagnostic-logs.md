# 诊断日志样例（脱敏）

取自 2026-08-06/07 那台真实故障机（Win10 22H2 / 19045，交接链路损坏）。
用户名已替换为 `<user>`。用途：拿你机器上的输出与这里对照，判断"这算正常还是异常"。

---

## 1. 交接探针：异常（本案例的核心症状）

`downgrade-test.log` 摘录——脚本在两个 WT 版本下各测一次端到端交接：

```
15:21:53 === phase 0: close WT ===
15:21:56 === phase 1: handoff test, current WT 1.23.20211, no WT running ===
15:22:07 WT-1.23.20211 : LEGACY conhost (fallback)
15:22:09 === phase 2: uninstall 1.23.20211 ===
15:22:12 uninstall OK
15:22:12 === phase 3: install 1.23.13503 ===
15:22:15 install OK, version now 1.23.13503.0
15:22:17 === phase 4: handoff test, WT 1.23.13503, no WT running ===
15:22:27 WT-1.23.13503 : LEGACY conhost (fallback)
15:22:27 === phase 5: relaunch wt ===
```

**怎么读**：

- `LEGACY conhost (fallback)` 来自 `%WT_SESSION%` 探针输出字面量而非 GUID（见 `docs/troubleshooting.md` 第 1 节）。
- 关键信息不是"失败"，而是**换 WT 版本后仍然失败**——一次跨版本证伪，排除了"WT 某版本回归"这个假设。
- 对应 `diagnose.ps1` 的 `live handoff measurement` 段。正常机器该段应输出
  `result : Windows Terminal (WT_SESSION=<guid>)`。

## 2. 系统文件替换与还原：只认哈希

```
16:34:35 renamed current to conhost.exe.old-7548
16:34:35 copied 5198 into System32
SHA256 的 C:\Windows\System32\conhost.exe 哈希:
b02ee54fb2ec69673386d41119ee8ed083a6eab3bfca6aa2155d20ce68ef8963
16:34:35 done
```

还原后再测一次：

```
SHA256 的 C:\Windows\System32\conhost.exe 哈希:
5bb5ac50aafbb537377d4167a431c7a6642ff7c99910f485d9dc5c541395ae2e
17:35:08 restored to 7548 content
```

**怎么读**：两次哈希不同 = 替换/还原确实发生。
不要用版本号判断（方法论第 1 条：System32 的 FileVersionInfo 会撒谎）。
`diagnose.ps1` 的 `binaries` 段做的就是这件事。

## 3. 权限失败：典型且无害的一类噪音

```
17:34:39 FAIL: 对路径"C:\Windows\System32\conhost.exe"的访问被拒绝。
...
FAILED C:\Windows\System32\conhost.exe.old-4522 : 对路径"..."的访问被拒绝。
```

**怎么读**：System32 里被系统占用/受 TrustedInstaller 保护的文件即使提权也可能拒写。
本案例最终不靠替换系统文件解决——出现这类错误说明你正在走"点修交接"的死路，
考虑切换到绕过方案（`install.ps1`）或原位修复。

## 4. 静默提权可用性探针

```
elevated-ok
```

**怎么读**：由 `Start-Process -Verb RunAs -WindowStyle Hidden` 启动的进程写出的标记。
本机 `ConsentPromptBehaviorAdmin=0`，故无 UAC 提示即可从中等完整性 shell 提权。
**这不是通用前提**——默认配置的机器会弹 UAC，脚本需要按"会弹窗"来设计。

自查：

```powershell
(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System').ConsentPromptBehaviorAdmin
```

## 5. 跨账户复现：把"用户配置损坏"排除掉

```
New-LocalUser OK
Name   Enabled Description
----   ------- -----------
wttest True
```

**怎么读**：新建干净本地账户后交接仍失败 ⇒ 不是当前用户配置的问题。
这是止损判据之一（`docs/troubleshooting.md` 第 4 节）。

---

## 6. WinX 哈希 oracle：正常输出长这样

`python tools\winxhash.py verify`（Win10 19045 实测）：

```
OK    01a - Windows PowerShell.lnk    stored=0x1A60D163 computed=0x1A60D163  C:\Windows\system32\WindowsPowerShell\v1.0\powershell.exe
OK    05 - Device Manager.lnk         stored=0xBDD12552 computed=0xBDD12552  C:\Windows\system32\control.exe
SKIP  06 - SystemAbout.lnk            (non-filesystem target: windows.immersivecontrolpanel_...)

14 ok, 0 mismatched, 5 skipped
Salt and algorithm validated against 14 Microsoft-shipped shortcut(s).
```

**怎么读**：

- 只要出现 `FAIL`，就**不要**用 `write` 写任何哈希——先跑 `salt` 核对盐值。
- `SKIP` 是正常的：`ms-settings:` / AppUserModelID 目标不是文件路径，无法重建 parsing path。
- 修正前（Arguments 读空）的输出是 `8 ok, 6 mismatched`，失败项全是带参数的条目——
  这正是"多因素混淆"被 oracle 拆开的实例。
