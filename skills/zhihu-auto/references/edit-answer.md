# 分手册：修正已有知乎回答（edit-answer）

把一篇**已发布的知乎回答**正文替换为新的 Markdown。属于**浏览器通道**。
与「发布专栏」的区别：这里改的是**回答**（question 页内的回答），入口、编辑器、提交按钮都不同。

## 前置

- **playwright MCP 可用**（`browser_navigate / find / click / file_upload / evaluate / take_screenshot`）。
- **复用统一登录态**（见 SKILL.md「统一登录态」）——先注入 API 通道的 Cookie，不必每会话扫码：
  ```
  node <SKILL_DIR>/scripts/cookie-for-browser.mjs
  browser_run_code_unsafe({ filename: "<外部家目录>/browser-login-code.js" })   # → {ok:true,count:N}
  browser_navigate → https://www.zhihu.com
  ```
  判定已登录：页面标题出现「(N 封私信 / M 条消息)」前缀。若 Cookie 失效，重跑 `get-cookie.mjs`。**收尾用 `node <SKILL_DIR>/scripts/cookie-for-browser.mjs --rm` 删除注入代码（含凭证）。**
- **文件访问根**：MCP 只允许读「工作目录下的 `.playwright-mcp/`」（本机 `C:\Users\<user>\.playwright-mcp\`）。
  要导入的 MD 副本必须落到那里；放系统 `%TEMP%` 会被拒。

## 步骤链

1. **打开回答页**：`browser_navigate` → `https://www.zhihu.com/question/<qid>/answer/<aid>`；看标题确认已登录，否则按「前置」注入 Cookie（或重跑 get-cookie.mjs）。
2. **进入编辑**：`browser_find`「编辑回答」→ `browser_click`。**只有自己账号的回答才有该按钮**；没有 → 不是当前账号的，停下。
3. **确认编辑器**：正文在 `role=textbox`；底部有「字数：N」+「Markdown 语法输入中」+「取消 / **提交修改**」。提交按钮是**「提交修改」**。
4. **清空正文（关键）**：工具栏「导入」是**插入**行为，正文非空会重复。做法：点正文聚焦 → 
   `browser_run_code_unsafe`: `async (page)=>{await page.keyboard.press('Control+a');await page.waitForTimeout(200);await page.keyboard.press('Delete');await page.waitForTimeout(400);return 'cleared';}`
   → `browser_find`「字数」确认变 **0**。
5. **导入 MD**：先把新正文写到 `<cwd>/.playwright-mcp/<name>.md`（回答正文一般不需要 frontmatter/H1）。
   - `browser_click`「导入」→ 点「导入文档 MD/Doc」
   - 点「点击选择本地文档或拖动文件到窗口上传」**激活 file chooser**（跳过会报 `... modal state present`）
   - `browser_file_upload` 传该 MD 的绝对路径
   - 成功标志：底部「字数」变为新正文字数；编辑器是 **Markdown 模式，导入的表格会渲染成真表格**（实测 ✓）
6. **核对渲染**：`browser_find`（正则）抽查新数字/关键词命中**新内容**；`browser_find` 表格（`table`/`cell`）确认渲染；`browser_take_screenshot` 给用户看。
7. **提交（需明确授权）**：`browser_find`「提交修改」→ `browser_click`。**这是公开且不可逆的动作，必须用户明确说「提交」才点。**
8. **线上核对**：`browser_navigate` 重载回答页，确认线上已是新版（且旧串不在正文）。

## 已知坑

| 坑 | 现象 | 对治 |
|---|---|---|
| 未登录 | 右上角「登录/注册」，标题无「(N 封私信)」前缀 | 请用户在隔离浏览器扫码后再继续 |
| 导入是插入 | 不清空会正文翻倍 | 先 Ctrl+A/Delete 清空、字数 0，再导入 |
| file chooser 未激活 | `outside allowed roots` 或 modal 报错 | 先点「点击选择本地文档…」再 `browser_file_upload` |
| 文件根 | `%TEMP%` 被拒 | MD 副本放 `<cwd>/.playwright-mcp/` |
| 会话不持久 | 每次会话要重扫码 | 别假设登录态还在 |

## 边界
- 不代用户登录、不读 cookie 库、不处理验证码；**未授权不点「提交修改」**；只改当前账号自己的回答。
