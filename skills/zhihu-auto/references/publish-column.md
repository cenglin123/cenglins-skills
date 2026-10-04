# 分手册：发布专栏文章（publish-column）

把一篇本地 Markdown 通过知乎「导入文档」发成**知乎专栏文章**（`zhuanlan`）。属于**浏览器通道**。
核心不是「会点按钮」，而是**保护原件**、**在隔离浏览器里重复一套已验证链路**、**发布后把溯源写回知识库**。

## 前置

- **playwright MCP 可用**（`browser_navigate / find / click / file_upload / type / evaluate`）。
- **复用统一登录态**（见 SKILL.md「统一登录态」）——先注入 API 通道的 Cookie，不必每会话扫码：
  ```
  node <SKILL_DIR>/scripts/cookie-for-browser.mjs
  browser_run_code_unsafe({ filename: "<外部家目录>/browser-login-code.js" })
  browser_navigate → https://www.zhihu.com
  ```
  判定已登录：页面标题出现「(N 封私信 / M 条消息)」前缀。若 Cookie 失效，重跑 `get-cookie.mjs`。**收尾用 `node <SKILL_DIR>/scripts/cookie-for-browser.mjs --rm` 删除注入代码（含凭证）。**
- **文件访问根**：MCP 只允许读「知识库根 / 工作目录下的 `.playwright-mcp/`」；发布副本落到 `<vault>/.playwright-mcp/`。

## 步骤链

### [1/7] 生成发布副本（禁止直接上传库内原件）
库内原件要留 frontmatter 做溯源，但 YAML 会被知乎当正文导入 → 用脚本生成副本：
```bash
python <SKILL_DIR>/scripts/publish/make_publish_copy.py "<source.md>" --out-dir "<vault>/.playwright-mcp"
```
脚本做三件事并自检：删 frontmatter、删正文首个 H1、`[[wikilink]]`→纯文本。报告 `ok: true` 表示「残留 wikilink 为 0、首行不是 `---`、且 frontmatter 已成功剥离（若原件以 `---` 起头却未剥掉则 fail）」。脚本会先剥掉 UTF-8 BOM（否则 frontmatter 不会被识别、私有 YAML 会被当正文发布）。**自检失败时脚本退出码 1 且不生成副本（fail-closed）；若输出路径已存在旧副本（报告字段 `stale_output`），先删除再修复源头重跑，切勿上传旧副本。**

### [2/7] 打开编辑器
`browser_navigate` → `https://zhuanlan.zhihu.com/write`。

### [3/7] 导入
点工具栏「导入」→ 选「导入文档 MD/Doc」→ **先点「点击选择本地文档或拖动文件到窗口上传」激活 file chooser**，
再 `browser_file_upload` 传副本绝对路径（跳过第 3 步会报 `... modal state present`）。
成功标志：URL 从 `/write` 跳到 `/p/<id>/edit`。

### [4/7] 填标题
`browser_type` 到 placeholder「请输入标题（最多 100 个字）」。标题用用户给的原文；中英文空格等排版分歧**先问，不擅改**。

### [5/7] 插入目录（长文建议）
点工具栏「目录」按钮。

### [6/7] 核对导入质量
把 `<SKILL_DIR>/scripts/publish/check_import_quality.js` 整个 IIFE 作为 `browser_evaluate` 的 `function` 传入，读回 JSON：

| 项 | 判据 | 已知表现 |
|---|---|---|
| 加粗 | `bold_spans`（`fontWeight >= 600`） | ✅ 保留；`legacy_bold_tags` 为 0 正常 |
| 表格 | `tables` | DOM 计数可能少于肉眼所见，**以用户肉眼为准** |
| 代码块 | `code_blocks` | ✅ 保留 |
| 标题层级 | `h2`/`h3` | ⚠️ 会被压平（二三级都变 h3） |
| 正文完整性 | `char_count` + `head`/`tail` | ✅ 万字级无截断 |

> 坑：知乎是 Draft.js，加粗由**带样式 span** 实现，`querySelectorAll('b,strong')` 会得 0，别据此误判「加粗丢失」。

### [7/7] 发布（需明确授权）
`browser_click` `button.Button--primary:has-text("发布")` → 展开「发布设置」（封面/话题/专栏/声明均可留空）→ 按钮变「发布中…」→ 跳 `?just_published=2` 即成功。**发布公开且不可静默撤销，必须用户明确说「可以发布」才点。**

## 完工
```bash
python <SKILL_DIR>/scripts/publish/finalize_publish.py "<source.md>" --url "https://zhuanlan.zhihu.com/p/<id>" --changelog --vault-root "<知识库根>"
```
脚本：① 源文件 frontmatter 写/更新 `published: <链接>`（幂等，值经 YAML 引号转义）；② **仅当显式 `--changelog` 且记账脚本位于 `--vault-root` 之下**才执行它写 `docs/CHANGELOG.md`（默认**不执行**，防执行来源未受信的脚本）。省略 `--changelog` 只回写 frontmatter。然后删除 `.playwright-mcp/` 下的副本。

## 边界
- 不代用户登录、不读 cookie 库、不处理验证码。
- 不擅自填话题/专栏/封面/声明——这些是对外表达，属用户决定。
- **未获明确授权不点「发布」**；「预览」「保存草稿」不受此限。
