---
name: zhihu-auto
description: >-
  知乎（zhihu.com）自动化合集，一个 skill 覆盖四类任务：(1) 抓取/下载知乎问题下的回答为文本；
  (2) 争议问题的真实立场分析（全量枚举 + 分层随机抽样 + 立场加权估计 + 回答下热评/评论层）；
  (3) 修正/更新一篇已发布的知乎回答正文；(4) 把本地 Markdown 发布为知乎专栏文章。
  触发词：知乎 / zhihu、抓取知乎回答、下载知乎回答、知乎问题保存为文本、争议题立场分布、
  防止只看高赞的排序偏置、抓取知乎评论/热评、修正知乎回答、修改/更新知乎回答、发布到知乎、
  发到知乎、知乎专栏、导入知乎。
  本 skill 内部分手册组织，按任务读取对应的 references/ 文件。
---

# 知乎自动化（zhihu-auto）

一个 skill、四类任务，用 `references/` 分手册**按需加载**。上手先认清**两条截然不同的执行/鉴权通道**，不要混用：

| 通道 | 任务 | 执行方式 | 鉴权 |
|---|---|---|---|
| **API 通道** | 抓取回答、争议分析、抓评论 | `scripts/*.mjs`（纯 Node `fetch`，**无浏览器**） | 一个 **Cookie 文件**（存在 `~/.zhihu-auto/`，由 setup / get-cookie 管理） |
| **浏览器通道** | 改回答、发专栏 | **playwright MCP**（`browser_*`，**隔离实例**） | **用户本人扫码登录**（每次会话都要，MCP 不持久化登录态） |

> API 通道里 `strat-sample / stance-estimate / fetch-comments` **零依赖**（只用全局 `fetch`），
> 只要 Cookie 在就能跑；`extract / open / get-cookie` 需要 puppeteer（走外部依赖 + 软链）。

## 前置：环境自愈（仅 API 通道需要）

依赖与 Cookie 存放在 skill 目录之外（默认 `~/.zhihu-auto/`），以免重装 skill 时被清空。装好后运行一次：

```powershell
node <SKILL_DIR>/scripts/setup.mjs           # 安装外部依赖 + 迁移 Cookie + 建软链
node <SKILL_DIR>/scripts/setup.mjs --check   # 只检查环境（非 0 = 有缺失）
```

首次使用前，用 `scripts/get-cookie.mjs` 扫码登录一次，把 Cookie 写到 `~/.zhihu-auto/`（详见 `references/extract-answers.md`）。

## 统一登录态（一次登录，四类任务复用）

**唯一登录源 = `~/.zhihu-auto/www.zhihu.com_cookies.txt`**（由 `scripts/get-cookie.mjs` 写入）。

- **API 通道**：直接读该文件（`scripts/*.mjs` 自动解析）。
- **浏览器通道**：任务开始时把该文件**注入** playwright MCP，**不必每会话扫码**：
  ```powershell
  node <SKILL_DIR>/scripts/cookie-for-browser.mjs      # 把 Cookie 转成注入代码文件
  ```
  然后在 MCP 里：
  ```
  browser_run_code_unsafe({ filename: "<SKILL_DIR>/scripts/.browser-login-code.js" })
  # → 返回 { ok: true, count: N }；随后 browser_navigate 到 zhihu.com 即已登录
  ```
  之后 `edit-answer` / `publish-column` 全程复用该登录态。
- **原理**：playwright MCP 的代码沙箱没有 `fs`/`require`，读不了文件，但能调 `page.context().addCookies(...)`；
  故由本地脚本把 Cookie 内联成一段函数写文件，再交给 MCP 执行（含 `httpOnly` 的 `z_c0` 也能设）。
- **过期**：Cookie 纸面约 6 个月；一旦 API 返回 40362 或浏览器掉登录，重跑 `get-cookie.mjs` 刷新文件即可（两边同时恢复）。

## 路由：按任务读对应分手册

| 用户要… | 读分手册 | 关键脚本/工具 |
|---|---|---|
| 抓取/下载某问题的高赞回答为 txt | [`references/extract-answers.md`](references/extract-answers.md) | `scripts/extract.mjs` |
| 估计某争议问题的**真实立场分布**（防排序偏置） | [`references/debate-analysis.md`](references/debate-analysis.md) | `strat-sample.mjs` →（agent 判读）→ `stance-estimate.mjs` |
| 抓某回答下的**热评 / 评论层** | [`references/debate-analysis.md`](references/debate-analysis.md)「评论层」节 | `fetch-comments.mjs` |
| **修正 / 更新一篇已有知乎回答** | [`references/edit-answer.md`](references/edit-answer.md) | playwright MCP |
| **发布 Markdown 为知乎专栏文章** | [`references/publish-column.md`](references/publish-column.md) | playwright MCP + `scripts/publish/*` |

## 目录

```
zhihu-auto/
├── SKILL.md                    # 本文件：入口 + 两条通道 + 路由
├── references/                 # 分手册（按需读取）
│   ├── extract-answers.md      #   抓取问题回答（API 枚举 + puppeteer 渲染）
│   ├── debate-analysis.md      #   争议题：分层抽样 / 立场估计 / 评论层 / 反串识别
│   ├── edit-answer.md          #   修正已有回答（浏览器）
│   └── publish-column.md       #   发布专栏文章（浏览器）
└── scripts/
    ├── extract.mjs  get-cookie.mjs  open.mjs  setup.mjs
    ├── strat-sample.mjs  stance-estimate.mjs  fetch-comments.mjs
    ├── lib/env.mjs              # 外部家目录 / Cookie / 依赖链接解析（零依赖）
    ├── facets.example.json  package.json
    └── publish/                 # 专栏发布脚本
        ├── make_publish_copy.py       # 生成发布副本（去 frontmatter/H1、wikilink 转纯文本）
        ├── check_import_quality.js    # 导入质量探针（浏览器 IIFE）
        └── finalize_publish.py        # 回写 published + 记 CHANGELOG
```

## 通用边界

- 不代替用户登录、不读 cookie 库、不处理验证码。
- **浏览器通道**：未获用户明确授权**不点「提交修改」/「发布」**；只改/发当前登录账号自己的内容。
- **API 通道**：只读抓取，不写知乎。
- 关键事实须回原始来源核对；抓取文本可能与页面渲染有差异（尤其折叠/图片）。
