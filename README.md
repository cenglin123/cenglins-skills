# cenglins-skills

Codex agent skill 集合。以子目录组织，每个子目录下是独立的 `SKILL.md` 文件及其配套资源。

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

```powershell
git clone https://github.com/cenglin123/cenglins-skills.git "$env:USERPROFILE\.agents\skills\cenglins-skills"
```

### 更新

```powershell
git -C "$env:USERPROFILE\.agents\skills\cenglins-skills" pull
```

### 工作原理

Agent 框架自动扫描 `*.agents\skills\` 目录下所有 `SKILL.md` 文件，读取 frontmatter `description` 后进行场景匹配。

Guard skill 的设计理念是 **"描述即预防"**：agent 阅读 `description` 即可获得核心预防规则，只有实际遇到编码或 shell 执行问题时才需要读取正文排障。

## 目录约定

| 目录 | 内容 | 说明 |
|------|------|------|
| `01-guards/` | Guard 类 skill | 全局预防提示，agent 自动匹配 |
| `02-tools/` (预留) | 工具类 skill | 显式调用或场景匹配 |
| `03-workflows/` (预留) | 工作流 skill | 多步骤编排 |

---

*维护者：[cenglin123](https://github.com/cenglin123)*
