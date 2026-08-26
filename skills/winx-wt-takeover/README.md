# winx-wt-takeover

让 Win+X 的 `Windows PowerShell(I) / (管理员)(A)` 打开 Windows Terminal，
以及诊断 Windows 10/11 "默认终端交接（console handoff）失效"问题的工具包。

Agent 入口是 [`SKILL.md`](SKILL.md)；本文件面向直接动手的人。

只想拿来用、不装 skill 的话，Release 里有打好的完整分发包（多一层 `docs/` 与 `toolkit/`
的人读布局，内容与本目录一致）：
[WinX-WT-Takeover.zip](https://github.com/cenglin123/cenglins-skills/releases/tag/winx-wt-takeover-v1.0.0)

## 核心结论

Win+X 的这两个条目，TWINUI 启动的**不是** WinX 目录里的 .lnk，而是
`HKLM\SOFTWARE\Microsoft\PowerShell\3:ConsoleHostShortcutTarget` 指向的开始菜单快捷方式。
改那一个快捷方式的目标，即可在完整保留菜单外观、I/A 快捷键与提权行为的前提下接管——
不用碰受哈希校验保护的 WinX 目录，也不需要先把坏掉的交接链路修好。

## 用法

```powershell
# 1) 只读诊断（永远第一步）
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1

# 2) 看 live handoff measurement 段：
#    Windows Terminal (WT_SESSION=...)  -> 交接正常，不需要本技能，去设置里选默认终端即可
#    LEGACY conhost                     -> 交接不生效，继续第 3 步

# 3) 接管（先空跑）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 4) 验证（走真实菜单，不是查文件）
powershell -ExecutionPolicy Bypass -File scripts\test-winx-key.ps1 -Key I

# 5) 还原
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [-RemoveLauncher]
```

`install.ps1` 只改**一个用户级 .lnk**：不改注册表、不动 WinX 目录、不替换系统文件。
改之前备份（首个原始版本单独留存），改之后读回验证，补丁不生效则自动回落到重建。

## 内容

| 路径 | 说明 |
|---|---|
| `scripts/diagnose.ps1` | 只读状态报告 + `%WT_SESSION%` 端到端交接探针 |
| `scripts/install.ps1` / `uninstall.ps1` | 接管 / 三级还原（pristine 备份 → 最近备份 → 重建标准快捷方式）|
| `scripts/build-launcher.ps1` + `scripts/src/wtlaunch.cs` | 用系统自带 csc.exe 编译包装器；运行时解析 `wt.exe`，找不到回退真 powershell |
| `scripts/winxhash.py` | WinX 哈希：`salt` 从本机 twinui.dll 提盐 / `verify` oracle 自证 / `read`·`hash`·`write` |
| `scripts/patchlnk.py` · `addname.py` | 保结构二进制补丁改 .lnk 目标；给缺 NAME_STRING 的 .lnk 补显示名 |
| `scripts/test-winx-key.ps1` · `capture-winx.ps1` | 合成按键 + 截屏的 GUI 兜底验证 |
| `references/mechanism.md` | 接管点、WinX 哈希算法与盐值陷阱、二进制补丁 vs 重建 |
| `references/troubleshooting.md` | 交接链路 L0–L6 分层隔离测试、判定方法、止损线 |
| `references/methodology.md` | 8 条可迁移排障方法论 |
| `references/diagnostic-logs.md` | 真实故障机的脱敏日志 + 判读方法 |

## 依赖

- Windows 10 22H2（≥ 19045.3031）或 Windows 11；PowerShell 5.1 即可
- 诊断与接管零第三方依赖，通常无需管理员权限
  （除非 `ConsoleHostShortcutTarget` 指向全用户开始菜单）
- `scripts/*.py` 需要 Python 3；`winxhash.py` 的属性存储操作需 `pip install comtypes`
  （`salt` 与 `hash` 两个子命令不需要）

## 验证状态

来源：2026-08-06/07 在一台 Win10 22H2 机器上的完整排障（交接链路系统级损坏，
换 WT 版本、重注册包、sfc、换 conhost、换账户均无效，最终以最小侵入接管收尾）。
打包时于 Win10 19045.7663 重新实测。

**已验证**：全部脚本语法/编译检查；`diagnose.ps1` 全流程（探针正确输出 `LEGACY conhost`，
与该机已知故障一致）；`build-launcher.ps1` 编译与包装器运行时解析；`install.ps1 -DryRun`
全流程（含补丁失效 → 自动回落重建）；`winxhash.py salt` 提盐与 `verify` oracle 14 ok / 0 mismatched。

**打包时修正的两个真实缺陷**：

1. 只从属性存储取 Arguments 会让带参数的快捷方式哈希算错（oracle 8/14）；
   回退解析 .lnk 二进制 StringData 后 14/14 通过。
2. `addname.py` 插入 NAME_STRING 后置位 `0x8`（HasRelativePath），
   按 [MS-SHLLINK] 应为 `0x4`（HasName）。

**未验证**：干净未接管机器上的实写安装（`-DryRun` 之外的分支）、`uninstall.ps1` 实跑、
`test-winx-key.ps1` / `capture-winx.ps1`、Windows 11、`addname.py` 的 `0x4` 修正实跑。
首次使用请先 `-DryRun`。
