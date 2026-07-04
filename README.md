# cenglins-skills

Agent skill 集合，兼容 OpenCode、Codex、Claude Code 等主流 agent 框架。以子目录组织，每个子目录下是独立的 `SKILL.md` 文件及其配套资源。

## 目录结构

```
cenglins-skills/
├── 01-guards/                  # Guard 类 skill（全局预防提示）
│   ├── utf8-guard/
│   │   └── SKILL.md            # Windows 中文 UTF-8/GBK 编码防护
│   └── powershell-guard/
│       └── SKILL.md            # Windows PowerShell 语法陷阱防护
└── README.md
```

## 安装与使用

### 首次安装

仓库本体克隆到 `~\.agents\cenglins-skills`（注意**不在** `~\.agents\skills\` 内，避免与手动安装的 skill 目录混淆）：

```powershell
git clone https://github.com/cenglin123/cenglins-skills.git "$env:USERPROFILE\.agents\cenglins-skills"
```

各框架的 skill 目录（如 `~\.agents\skills\`、`~\.claude\skills\`）由用户手动从仓库链接或拷贝叶子目录，见下文各框架集成说明。

### 更新

```powershell
git -C "$env:USERPROFILE\.agents\cenglins-skills" pull
```

更新后，凡是以**拷贝**方式安装的 skill 需重新拷贝一次；符号链接方式则自动生效。

### 工作原理

Agent 框架扫描各自的 skill 目录下的 `SKILL.md` 文件（具体目录因框架而异，见下文各框架集成说明），读取 frontmatter 中的 `name` 和 `description` 后进行场景匹配。

Guard skill 的设计理念是 **"描述即预防"**：

- `description` 独立承载**全部关键预防规则**，随框架注入系统提示词后即作为全局提示生效——正常工作时 agent **不需要读取正文**，只看描述就能规避编码和 PowerShell 语法陷阱；
- 正文只在实际遇到编码或 shell 执行故障时才按需读取，用于排障（症状 → 原因 → 修复的完整对照）；
- 因此维护时须保证：新增关键规则先写进 `description`，正文只放排障细节。

### Claude Code 集成

Claude Code 从以下目录发现 skill（要求 `<skill-name>/SKILL.md` 平铺一层，**不会递归扫描**本仓库的嵌套布局）：

- **个人级**：`~/.claude/skills/<skill-name>/` — 所有项目共享
- **项目级**：`<项目>/.claude/skills/<skill-name>/` — 仅当前项目可用

因此需要把各 skill 的叶子目录链接或拷贝进去。推荐符号链接（`git pull` 后自动同步，需管理员权限或开发者模式）：

```powershell
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\utf8-guard" -Target "$env:USERPROFILE\.agents\cenglins-skills\01-guards\utf8-guard"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\powershell-guard" -Target "$env:USERPROFILE\.agents\cenglins-skills\01-guards\powershell-guard"
```

无法创建符号链接时改用拷贝（注意 `git pull` 后需重新拷贝）：

```powershell
Copy-Item -Recurse -Force "$env:USERPROFILE\.agents\cenglins-skills\01-guards\utf8-guard" "$env:USERPROFILE\.claude\skills\"
Copy-Item -Recurse -Force "$env:USERPROFILE\.agents\cenglins-skills\01-guards\powershell-guard" "$env:USERPROFILE\.claude\skills\"
```

Claude Code 会在会话启动时把每个 skill 的 `name` + `description` 注入系统提示词，guard 规则因此全程生效；只有 agent 判断需要排障时才会通过 Skill 工具加载正文。

**验证**：新开一个 Claude Code 会话，直接询问"当前有哪些可用 skill"，确认 `utf8-guard` 和 `powershell-guard` 在列；或观察 agent 生成 PowerShell 命令时是否已主动避免 `&&`。

### OpenCode 集成

这些 skill 同样适用于 [OpenCode](https://github.com/opencode-ai/opencode) agent。OpenCode 会自动从以下目录加载 skill：

- **全局目录**：`~/.agents/skills/` — 所有项目共享
- **项目目录**：`.agents/skills/` — 仅当前项目可用

`~/.agents/skills/` 由用户手动维护：从仓库把叶子目录拷贝（或符号链接）过去即可：

```powershell
Copy-Item -Recurse -Force "$env:USERPROFILE\.agents\cenglins-skills\01-guards\utf8-guard" "$env:USERPROFILE\.agents\skills\"
Copy-Item -Recurse -Force "$env:USERPROFILE\.agents\cenglins-skills\01-guards\powershell-guard" "$env:USERPROFILE\.agents\skills\"
```

安装后，OpenCode 启动时会扫描这些目录中的 `SKILL.md` 文件，读取 frontmatter 中的 `name` 和 `description` 进行场景匹配。当用户请求与 skill 描述匹配的任务时，agent 会自动加载对应 skill 的完整内容。

#### 验证 skill 已加载

启动 opencode 后询问 agent 当前可用的 skill 列表，确认 `utf8-guard` 和 `powershell-guard` 在列即可。

#### 自定义配置

如需将 skill 集成到 OpenCode 的配置中，可以在 `opencode.json` 或 `~/.config/opencode/config.json` 中添加：

```json
{
  "skill": {
    "paths": [
      "~/.agents/cenglins-skills"
    ]
  }
}
```

> **注意**：大多数情况下把叶子目录拷贝进 `~/.agents/skills/` 即可被自动扫描，无需手动配置。

## 目录约定

| 目录 | 内容 | 说明 |
|------|------|------|
| `01-guards/` | Guard 类 skill | 全局预防提示，agent 自动匹配 |
| `02-tools/` (预留) | 工具类 skill | 显式调用或场景匹配 |
| `03-workflows/` (预留) | 工作流 skill | 多步骤编排 |

---

*维护者：[cenglin123](https://github.com/cenglin123)*
