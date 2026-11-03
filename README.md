# cenglins-skills

面向 Agent Skills 开放格式的个人技能集合，兼容 CC Switch、Claude Code、Codex、OpenCode，以及支持 `SKILL.md` 的其他 agent。

## 仓库结构

仓库采用通用的扁平 skills 目录：

```text
cenglins-skills/
├── skills/
│   └── <skill-name>/
│       ├── SKILL.md
│       ├── scripts/       # 按需
│       ├── references/    # 按需
│       └── assets/        # 按需
├── .githooks/
│   └── pre-commit
├── scripts/
│   ├── update-readme.py
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

### 自动维护技能列表

技能表由 `skills/*/SKILL.md` 的一级标题生成，按目录名排序；修改用途说明时编辑对应的一级标题。不要手改生成区域。需要 Python 3.10 或更新版本，无第三方依赖。

每次克隆后启用仓库自带的提交检查（Git 不会随克隆自动启用 hook）：

```powershell
git config --local core.hooksPath .githooks
```

Linux / macOS 还需运行 `chmod +x .githooks/pre-commit`，确保 hook 可执行。

新增、删除、重命名技能或修改一级标题后运行：

```powershell
python scripts/update-readme.py
python scripts/update-readme.py --check
```

将生成的 `README.md` 与技能改动一起暂存。`pre-commit` 使用 `--check --staged` 检查暂存区，列表遗漏、多余、顺序或标题不一致都会阻止提交；检查不会改文件或自动暂存。已有自定义 hook 的仓库需将检查命令合并到原 hook。Git hook 可被 `--no-verify` 跳过，不构成远端强制策略。

## 当前技能

<!-- skills:start -->

| Skill | 用途 |
|---|---|
| [`bilibili-comment-extractor`](skills/bilibili-comment-extractor/SKILL.md) | B 站视频评论批量抓取 |
| [`crlf-guard`](skills/crlf-guard/SKILL.md) | CRLF Guard |
| [`foxmail-grep`](skills/foxmail-grep/SKILL.md) | foxmail-grep |
| [`powershell-guard`](skills/powershell-guard/SKILL.md) | PowerShell Guard |
| [`push-to-server`](skills/push-to-server/SKILL.md) | Push To Server |
| [`utf8-guard`](skills/utf8-guard/SKILL.md) | UTF-8 Guard |
| [`video-to-srt`](skills/video-to-srt/SKILL.md) | 视频转字幕工作流 |
| [`win-pup-forensics-cleanup`](skills/win-pup-forensics-cleanup/SKILL.md) | Windows 流氓软件取证与清理 |
| [`winx-wt-takeover`](skills/winx-wt-takeover/SKILL.md) | Win+X → Windows Terminal 接管 / 控制台交接排障 |
| [`zhihu-auto`](skills/zhihu-auto/SKILL.md) | 知乎自动化（zhihu-auto） |

<!-- skills:end -->

维护者：[cenglin123](https://github.com/cenglin123)
