---
name: zhihu-answer-extractor
description: >-
  批量抓取知乎问题下的回答并保存为 txt 文档。使用 puppeteer-extra + stealth 插件
  绕过知乎反爬检测（40362），支持「查看剩余 N 条回答」分页按钮真实点击加载、
  无限滚动兜底、展开折叠内容，提取题干/提问者/话题标签/关注数/浏览数/作者/
  赞同数（含「1.6 万」万级换算）/正文。另含争议题分析：answers API 全量枚举 +
  分层随机抽样 + 立场加权估计（含置信区间）与子代理独立裁决工作流。
  触发条件：用户要求下载/抓取/采集知乎回答、将知乎问题保存为文本、批量获取知乎内容，
  或要求估计某争议问题的真实立场分布（避免只看高赞的排序偏置）。
---

# 知乎回答批量抓取

批量抓取知乎问题下的回答，保存为结构化 txt 文档。

## 前置条件

1. Node.js 18+ 已安装
2. Chrome 浏览器已安装

## 安装依赖

```powershell
cd <SKILL_DIR>/scripts
npm install
```

## Cookie 获取（两种方式）

### 方式一：一键获取（推荐）

脚本会打开浏览器，等用户手动登录，登录成功后自动提取 Cookie：

```powershell
node <SKILL_DIR>/scripts/get-cookie.mjs
```

流程：
1. 脚本自动打开 Chrome 浏览器并导航到知乎登录页
2. 用户在浏览器中手动登录（扫码/验证码/密码/微信/QQ 均可）
3. 脚本每 2 秒自动检测登录状态
4. 登录成功后自动提取 Cookie 并保存为 Netscape 格式
5. 保存到 `<SKILL_DIR>/scripts/www.zhihu.com_cookies.txt`

> ⚠️ **风险提示**：导出的 Cookie 包含知乎登录凭证（`z_c0`），任何获得此文件的人都可以以你的身份访问知乎。请妥善保管，不要分享给他人或上传到公共仓库。

### 方式二：手动导出

1. 用 Chrome 登录 zhihu.com
2. 安装浏览器扩展 [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg) 或 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
3. 在知乎页面点击扩展 → 导出 → 选择 "Netscape HTTP Cookie File" 格式
4. 保存到 `<SKILL_DIR>/scripts/www.zhihu.com_cookies.txt`

Cookie 有效期约 6 个月。如果抓取返回 403 或重定向到登录页，重新获取即可。

## 使用方法

### 1. 抓取回答到 txt 文件

```powershell
node <SKILL_DIR>/scripts/extract.mjs `
  --url "https://www.zhihu.com/question/XXXXXXXX" `
  --count 50 `
  --output "<OUTPUT_FILE>"
```

`--count` 默认为 50；省略 `--output` 时，文件自动保存到脚本目录。网络较慢或回答较多时，可用 `--max-wait <秒数>` 调整最长加载时间（默认 180 秒）。

回答加载机制（2026-09 实测）：知乎首屏只静态渲染前几条回答，更多回答需点击「查看剩余 N 条回答」按钮加载。该按钮常是普通 div + cursor:pointer（React pointer 事件驱动），页内 `el.click()` 合成事件（isTrusted:false）无法触发，脚本因此**在 Node 侧用 puppeteer `elementHandle.click()` 发送真实 CDP 鼠标事件**；点击展开后页面可能切换为无限滚动，脚本保留滚动兜底。停止条件：达到目标数量、加载到页面报告的总回答数、出现明确的无更多内容提示、滚动末端持续无新增，或**触发登录墙**。输出文件和控制台都会记录停止原因；若实际数量低于目标，应根据停止原因判断是回答总数不足、Cookie 失效（见常见问题「触发登录墙」）还是加载超时，不要把“无新增”直接视为抓取完成。

### 2. 打开浏览器手动浏览

```powershell
node <SKILL_DIR>/scripts/open.mjs
```

修改 `open.mjs` 顶部的 `TARGET_URL` 为目标页面。

## 输出格式

```
知乎问题：为什么体制内至今仍不鼓励用人工智能？
URL: https://www.zhihu.com/question/XXXXXXXX
提问者: 张三
话题: 人工智能, 体制, 科技政策
关注者: 12,345
被浏览: 1,234,567
2,345 个回答
抓取时间: 2026/8/10 16:56:05
本次抓取: 50 条回答
================================================================================

【题干】
问题描述正文内容...

────────────────────────────────────────────────────────────────────────────────

【回答 #1】张三 (某领域优秀答主)
赞同: 2520

正文内容...

────────────────────────────────────────────────────────────────────────────────
```

## 分析工作流（子代理初读 + 主代理独立裁决）

当用户要求对抓取结果进行**总结、分析、归纳观点**时，必须按以下流程执行，以避免单一视角的偏误：

### 流程

1. **主代理**完成抓取，获得 txt 文件
2. **spawn 1~2 个子代理**（`task` with `subagent_type: "general"`）**独立精读**同一份 txt，产出结构化初读报告；多个子代理之间不共享分析过程。子代理的作用是**节省主代理逐字精读的工作量**
3. **主代理自己也必须独立阅读原文**（至少精读题干 + 高赞回答 + 抽样中低赞回答），形成自己的第一手判断——**不能只把子代理的报告压缩转述交差**
4. **主代理作为最终裁决者**，对比子代理报告与自己的独立阅读：
   - 共识点 → 确认
   - 分歧点 → **回到原文裁决**，注明谁对、错在哪，而不是各打五十大板或取平均
   - 任一方独有的发现 → 核实后并入
5. 输出最终总结，关键结论标注回答编号、作者、赞同数作为证据；并说明双视角的共识与裁决结果

### 子代理 Prompt 模板

```
请阅读以下知乎回答文件，独立完成分析报告。

文件路径：{txt_file_path}

分析维度：
1. 回答的主要观点分类（按赞同数加权）
2. 高赞回答（Top 10）的核心论点
3. 支持/反对/中立的态度分布
4. 出现频率最高的关键词或论据
5. 值得关注的独特视角或深度分析

输出格式：结构化 Markdown 报告，每个维度单独一节，注明关键回答的编号、作者和赞同数作为证据。
注意：仅基于文本内容分析，不要编造不存在的观点。
```

### 为什么这样分工

- 子代理初读为主代理节省工作量，但初读不能代替主代理的第一手判断
- 至少两个独立视角（子代理 + 主代理自己）才能避免单一视角偏误；两份子代理报告互查时，总赞同数等可复算指标应能互相咬合
- 分歧本身就是有价值的发现，但分歧必须由主代理回到原文裁决后才能进入最终结论

## 争议题采样分析（全量枚举 + 分层随机抽样 + 立场判读）

当问题带有**争议性/站队性**（粉丝战争、圈地争议、政治话题等）时，直接用 `extract.mjs` 抓「前 N 条高赞」会得到**排序偏置**：高赞被最响亮的一方垄断，少数派与不表态的噪音被排序机制滤掉，容易把「胜利者框架」误当成「全体共识」。此时改用两段式脚本，从 API 全量枚举后做分层随机抽样，得到可外推的立场分布。

> 实测：某争议题 top-50 里对立阵营占 95%，但按赞同分层随机抽样 165 条后回落到约 85%（另 6% 中立、9% 噪音）——排序偏置真实存在，但幅度有限；更重要的价值是**把「沉默的噪音」和置信区间也纳入了描述**。

### 第一步：分层随机抽样（strat-sample.mjs）

```powershell
node <SKILL_DIR>/scripts/strat-sample.mjs `
  --url "https://www.zhihu.com/question/XXXXXXXX" `
  --per-band 15 `
  --facets <SKILL_DIR>/scripts/facets.example.json `
  --out-dir <OUTPUT_DIR>
```

- 走 `answers` API 全量枚举（`limit=20` 分页、纯 `fetch`、无需浏览器），拿到每条**精确赞同数**（比页面显示的「1 万」更准）与正文
- 按赞同数分 11 层，每层随机抽 `--per-band` 条（`--seed` 固定，结果可复现）
- 产出四件套：
  - `<qid>_census.json` — 全量数据（含正文/赞同/时间）
  - `<qid>_sample.txt` — 抽样正文（供 agent 精读）
  - `<qid>_ledger.json` — 逐条判读表（`verdict` 留空，交 agent 填）
  - `<qid>_stats.json` — 分层统计（层规模/赞同/抽样数）
- `--facets` 给每条样本打 `auto_hint` 关键词提示，**仅辅助分诊，不是判读**
- 已有 census 时加 `--census <file>` 可跳过枚举直接重抽样
- 指定 `--bands` 可自定义分层上界

### 第二步：立场判读（agent）+ 加权估计（stance-estimate.mjs）

1. **agent 精读** `sample.txt`，逐条判读立场，写成 `verdicts.txt`（分组文本，按 `rid` 归类）：
   ```
   # A
   12 13 14 20
   # B
   119
   # N
   22 33
   # O
   17 18
   ```
2. 运行估计（脚本完成加权、区间、交叉验证）：
   ```powershell
   node <SKILL_DIR>/scripts/stance-estimate.mjs `
     --ledger <qid>_ledger.json --census <qid>_census.json `
     --verdicts <verdicts.txt> `
     --categories "A=反X/挺Y,B=挺X,N=中立调和,O=无关难判" `
     --output <report.md>
   ```
3. 输出：**设计加权占比 + 95% 置信区间**（分层方差 + 有限总体校正）、未加权 Wilson 参考区间、以及对全量 census 的**关键词交叉验证**（不依赖 agent 判读）

无 `verdicts` 时可用 `--use-autohint --facets <file>` 做**低置信预览**（关键词粗分类，会大量漏判，仅用于快速摸底）。

### 设计哲学（与 harness 原则一致）

脚本负责一切**确定性**的事：枚举、分页、分层、随机、加权、置信区间、关键词交叉验证与记账；「每条回答属于哪个阵营」这个**需要判断**的事留在 agent 手里——脚本只给 `auto_hint` 辅助分诊，绝不替 agent 下判断（`verdict` 字段由 agent 填）。

### 两条路径怎么选

| 场景 | 用什么 |
|---|---|
| 总结主流观点、需要完整正文 | `extract.mjs`（前 N 高赞） |
| 估计真实立场分布、防排序偏置 | `strat-sample.mjs` + `stance-estimate.mjs` |
| 两者结合 | 先 `strat-sample` 抽样，再精读 `sample.txt`，高赞部分可另用 `extract.mjs` |

## 反检测原理

- **puppeteer-extra + stealth 插件**：自动隐藏 WebDriver、修改浏览器指纹
- **evaluateOnNewDocument**：在每个页面加载前注入 `navigator.webdriver = false` 和 `window.chrome` 对象
- **启动参数**：`--disable-blink-features=AutomationControlled` 禁用自动化控制检测
- **headless 模式**：服务端看不到屏幕，但 CDP 协议正常工作

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 返回 40362 | Cookie 过期或被检测 | 重新运行 `get-cookie.mjs` 获取 |
| 重定向到登录页 | Cookie 无效 | 确认 `z_c0` 存在且未过期 |
| **停止原因=触发登录墙** | **Cookie 被服务端吊销**（别处退出登录/会话轮换）。注意：z_c0 纸面有效期未到也可能失效；问题页匿名可看（状态 200、标题正常），但点击「查看剩余」展开更多回答时强制登录 | 用页面右上角显示「登录/注册」而非头像来快速确认登录态失效；重新运行 `get-cookie.mjs` 手动登录 |
| 回答数量不够 | 页面报告总数不足、网络超时、加载按钮失效或知乎结构变化 | 查看输出中的“停止原因”和“实际抓取/目标数量”；必要时增大 `--max-wait` 并检查日志中的滚动与加载按钮状态 |
| 窗口打开后关闭 | 进程退出回收 | 用 `Start-Process` 后台启动 |

## 文件说明

| 文件 | 用途 |
|------|------|
| `scripts/get-cookie.mjs` | 一键获取 Cookie（打开浏览器→用户登录→自动导出） |
| `scripts/extract.mjs` | 批量抓取脚本（headless，速度快） |
| `scripts/strat-sample.mjs` | 全量枚举 + 分层随机抽样（争议题防排序偏置） |
| `scripts/stance-estimate.mjs` | 立场加权估计 + 置信区间 + 关键词交叉验证 |
| `scripts/facets.example.json` | 阵营关键词配置示例（供 strat-sample 的 auto_hint） |
| `scripts/open.mjs` | 浏览器打开模式（可视化，手动操作） |
| `scripts/package.json` | npm 依赖声明 |
| `scripts/www.zhihu.com_cookies.txt` | Cookie 文件（由 get-cookie.mjs 生成或手动导出） |
