---
name: zhihu-answer-extractor
description: >-
  批量抓取知乎问题下的回答并保存为 txt 文档。使用 puppeteer-extra + stealth 插件
  绕过知乎反爬检测（40362），支持滚动加载、展开折叠内容、提取题干/提问者/话题标签/
  关注数/浏览数/作者/赞同数/正文。触发条件：用户要求下载/抓取/采集知乎回答、
  将知乎问题保存为文本、批量获取知乎内容。
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

脚本会滚动回答区的独立容器并以页面滚动兜底，同时尝试点击“加载更多/查看更多回答”。停止条件是达到目标数量、加载到页面报告的总回答数、出现明确的无更多内容提示，或在滚动末端持续无变化/达到最长等待时间。输出文件和控制台都会记录停止原因；若实际数量低于目标，应根据停止原因判断是回答总数不足、页面结构变化还是加载超时，不要把“无新增”直接视为抓取完成。

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

## 分析工作流（双子代理独立总结）

当用户要求对抓取结果进行**总结、分析、归纳观点**时，必须按以下流程执行，以避免单一视角的偏误：

### 流程

1. **主代理**完成抓取，获得 txt 文件
2. **同时 spawn 2 个子代理**（`task` with `subagent_type: "general"`），各自独立读取同一份 txt 文件进行总结
3. 两个子代理的 prompt 应相同，包含：
   - 明确的分析维度（如：主要观点分类、赞同数分布、情感倾向、代表性论述等）
   - 要求输出结构化的分析报告
   - **不共享彼此的分析过程**
4. **主代理等待两个子代理返回**后，对比两份报告：
   - 提取共识点（两个子代理都识别出的观点/趋势）
   - 标注分歧点（仅一方识别的内容）
   - 合并去重，形成最终结论

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

输出格式：结构化 Markdown 报告，每个维度单独一节。
注意：仅基于文本内容分析，不要编造不存在的观点。
```

### 为什么用双子代理

- 单一代理可能因初始倾向忽略少数派观点
- 两个独立分析的共识更可靠
- 分歧点本身也是有价值的发现（说明社区存在争议）

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
| 回答数量不够 | 页面报告总数不足、网络超时、加载按钮失效或知乎结构变化 | 查看输出中的“停止原因”和“实际抓取/目标数量”；必要时增大 `--max-wait` 并检查日志中的滚动与加载按钮状态 |
| 窗口打开后关闭 | 进程退出回收 | 用 `Start-Process` 后台启动 |

## 文件说明

| 文件 | 用途 |
|------|------|
| `scripts/get-cookie.mjs` | 一键获取 Cookie（打开浏览器→用户登录→自动导出） |
| `scripts/extract.mjs` | 批量抓取脚本（headless，速度快） |
| `scripts/open.mjs` | 浏览器打开模式（可视化，手动操作） |
| `scripts/package.json` | npm 依赖声明 |
| `scripts/www.zhihu.com_cookies.txt` | Cookie 文件（由 get-cookie.mjs 生成或手动导出） |
