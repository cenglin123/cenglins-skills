# cenglins-skills

面向 Agent Skills 开放格式的个人技能集合，兼容 CC Switch、Claude Code、Codex、OpenCode，以及支持 `SKILL.md` 的其他 agent。

## 仓库结构

仓库采用通用的扁平 skills 目录：

```text
cenglins-skills/
├── skills/
│   ├── crlf-guard/
│   │   └── SKILL.md
│   ├── ocsr/
│   │   ├── SKILL.md
│   │   ├── refs/
│   │   └── scripts/
│   ├── powershell-guard/
│   │   └── SKILL.md
│   ├── utf8-guard/
│   │   └── SKILL.md
│   └── zhihu-answer-extractor/
│       ├── SKILL.md
│       └── scripts/
├── scripts/
│   └── validate-skills.ps1
├── .gitattributes
├── README.md
└── sync.ps1
```

每个 `skills/<skill-name>/` 都是一个独立技能包。入口固定为 `SKILL.md`，YAML frontmatter 至少包含：

```yaml
---
name: skill-name
description: What the skill does and when to use it.
---
```

`name` 必须与技能目录名一致。配套文件如有需要，放在同一技能目录下的 `scripts/`、`references/` 或 `assets/` 中，使安装和更新能够按完整技能目录进行。

## 使用 CC Switch 安装与自动更新

在 CC Switch 的「Skills → 仓库管理 → 添加仓库」中填写：

| 字段 | 值 |
|---|---|
| Owner | `cenglin123` |
| Name | `cenglins-skills` |
| Branch | `master` |
| Subdirectory | `skills` |

刷新仓库后，CC Switch 会从各个 `SKILL.md` 的 `name` 和 `description` 生成技能列表。安装技能后，CC Switch 通过远端与本地内容哈希比较检测新版本；仓库更新后，在技能卡片上点击「更新」，或使用「全部更新」即可同步。

建议在 CC Switch 中把 Skills 源存储位置设为 `~/.agents/skills/`，供多个 agent 共用。分发到不同应用时，可按本机情况选择复制或链接。

> 仓库当前默认分支是 `master`，不要沿用界面默认的 `main`，否则 CC Switch 无法读取远端目录。

## 其他安装方式

支持 Vercel `skills` CLI 的环境可以直接发现或安装本仓库：

```powershell
npx skills add cenglin123/cenglins-skills --list
npx skills add cenglin123/cenglins-skills -g --skill '*'
```

也可以克隆仓库后运行同步脚本，把技能复制到 `~/.agents/skills/` 与 `~/.claude/skills/`：

```powershell
git clone https://github.com/cenglin123/cenglins-skills.git "$env:USERPROFILE\.agents\cenglins-skills"
& "$env:USERPROFILE\.agents\cenglins-skills\sync.ps1"
```

仓库是唯一事实源；直接修改仓库中的技能后，如果仍使用本地复制方式，请重新运行 `sync.ps1`。

## 维护约定

- 新技能放在 `skills/<name>/SKILL.md`，不要再增加分类层级。
- `name` 使用小写字母、数字和连字符，并与目录名一致。
- `description` 同时说明技能做什么、何时触发；它是 agent 发现技能的主要依据。
- `SKILL.md` 正文只保留核心工作流，较长资料按需拆到 `references/`。
- 文本统一使用 UTF-8（无 BOM）和 LF；`.gitattributes` 已固定该策略。
- 提交前运行结构校验：

```powershell
& .\scripts\validate-skills.ps1
```

## 当前技能

| Skill | 用途 |
|---|---|
| `crlf-guard` | 防止 Windows Git 项目中的 CRLF/LF 与文本写入事故 |
| `ocsr` | 通过 headless OpenCode 调度跨厂商子代理、廉价批处理 worker 与独立评审 |
| `powershell-guard` | 规避 Windows PowerShell 5.1 的语法、别名与编码陷阱 |
| `utf8-guard` | 区分并处理 Windows 中文文本的 UTF-8、GBK 与显示层问题 |
| `zhihu-answer-extractor` | 批量抓取知乎问题回答并保存为结构化文本 |

维护者：[cenglin123](https://github.com/cenglin123)
