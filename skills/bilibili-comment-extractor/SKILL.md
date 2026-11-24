---
name: bilibili-comment-extractor
description: >-
  无头抓取 B 站（bilibili）视频评论区并保存为结构化 txt。纯 API 方案
  （WBI 签名 + 游标分页），无需浏览器渲染，核心抓取脚本零 npm 依赖；
  支持热度/时间排序、全量深翻、楼中楼完整展开、Cookie 有效期自检。
  触发条件：用户要求下载/抓取/采集/导出 B 站或 bilibili 评论、分析某视频
  评论区、把 BV 号/av 号视频的评论保存为文本、批量获取 B 站视频评论，
  或给出 B 站视频链接想讨论其评论内容。
---

# B 站视频评论批量抓取

纯 API 无头抓取 B 站视频评论区，保存为结构化 txt。抓取本身只需要 Node.js，不需要浏览器。

## 前置条件

1. Node.js 18+（依赖全局 `fetch`）
2. Cookie 文件 `<user-home>/.bilibili-comment-extractor/www.bilibili.com_cookies.txt`（Netscape 格式，需含有效 `SESSDATA`）
3. Chrome 浏览器 —— 仅 `get-cookie.mjs` / `open.mjs` 需要；`extract.mjs` 零依赖

## 安装依赖

仅当需要运行 `get-cookie.mjs` / `open.mjs` 时：

```powershell
cd <SKILL_DIR>/scripts
npm install
```

将安装 `puppeteer-core`（不含浏览器二进制，复用本机 Chrome）、`puppeteer-extra`、`puppeteer-extra-plugin-stealth`，共约 118 个包。`extract.mjs` 本身零依赖，不需要这一步。

## Cookie 获取（两种方式）

### 方式一：一键获取（推荐）

脚本会打开浏览器，等用户手动登录，登录成功后自动提取 Cookie：

```powershell
node <SKILL_DIR>/scripts/get-cookie.mjs
```

流程：
1. 脚本自动打开 Chrome 浏览器并导航到 B 站登录页（passport.bilibili.com）
2. 用户在浏览器中手动登录（扫码/验证码/密码均可）
3. 脚本每 2 秒自动检测 `SESSDATA` cookie 是否出现
4. 登录成功后自动提取 Cookie 并保存为 Netscape 格式
5. 保存到 `<user-home>/.bilibili-comment-extractor/www.bilibili.com_cookies.txt`

> ⚠️ **风险提示**：导出的 Cookie 包含 B 站登录凭证（`SESSDATA`），任何获得此文件的人都可以以你的身份访问 B 站。请妥善保管，不要分享给他人或上传到公共仓库；若 skill 目录被纳入 git 管理，不要把该文件复制到代码仓库或技能目录。

### 方式二：手动导出

1. 用 Chrome 登录 bilibili.com
2. 安装浏览器扩展 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 或 [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)
3. 在 B 站页面点击扩展 → 导出 → 选择 "Netscape HTTP Cookie File" 格式
4. 保存到 `<user-home>/.bilibili-comment-extractor/www.bilibili.com_cookies.txt`

`SESSDATA` 有效期约 1 年。`extract.mjs` 启动时会自动检查其剩余有效期：不足 14 天给出临期警告，已过期直接终止并提示重新获取。

## 使用方法

### 1. 抓取评论到 txt 文件

```powershell
node <SKILL_DIR>/scripts/extract.mjs `
  --url "https://www.bilibili.com/video/BVxxxxxxxxxx" `
  --count 200 `
  --output "<OUTPUT_FILE>"
```

参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--url` | （必填） | 视频 URL 或 BV 号，从链接中提取 BV 号 |
| `--count` | 200 | 目标主评论数 |
| `--sort` | hot | `hot`=热度 / `time`=最新。**抓全量用 `--sort time`** |
| `--replies` | 不开启 | 展开楼中楼（抓全每条主评论下的回复；慢，每条约多 1-N 次请求） |
| `--delay` | 1.2 | 每次请求间隔秒数，风控关键参数，不要低于 0.5 |
| `--max-pages` | 100 | 最多翻页数（每页约 20 条主评论） |
| `--cookies` | 用户数据目录下的 www.bilibili.com_cookies.txt | Netscape 格式 cookie 文件 |
| `--output` | 脚本目录，自动命名 | 输出 txt 路径 |

停止条件：达到目标数量、接口返回 `is_end`、热度模式下连续 3 页无新增（判定到底）、连续触发风控 3 次、或达到最大翻页数。输出文件和控制台都会记录停止原因；若实际数量低于目标，应根据停止原因判断是评论总数不足、风控还是翻页限制，不要把"无新增"直接视为抓取失败。

### 两种排序的差异（实测结论）

- **`--sort time`（时间排序）**：游标逐页正常前进，可深翻到 `is_end`，适合全量抓取。实测 1256 评论的视频 17 页拿全 330 条主评论。
- **`--sort hot`（热度排序）**：第 2 页起接口返回的 `next_offset` 冻结不变，这是 B 站的正常行为而非故障——用同一游标继续请求仍会返回全新评论（实测 8 页 159 条零重复）。脚本自动按 rpid 去重累计，以"连续 3 页无新增"判定到底。

### 2. 获取 / 刷新 Cookie

```powershell
node <SKILL_DIR>/scripts/get-cookie.mjs
```

### 3. 打开浏览器手动浏览

```powershell
node <SKILL_DIR>/scripts/open.mjs <URL>
```

也可省略 URL 参数，修改 `open.mjs` 顶部的 `TARGET_URL` 默认值。

## Cookie 失效处理（执行 agent 必读）

以下任一信号都表明 Cookie 已失效或缺失，**应提醒用户重新获取 Cookie，而不是当作"视频没有评论"或"接口故障"**：

- `extract.mjs` 启动时提示 `SESSDATA` 已过期或缺失
- 日志中 nav 接口报告"未登录"
- 评论接口返回 `code=-101`（账号未登录）——脚本会直接终止并提示
- 官方评论数大于 0 但第 1 页返回空——脚本会直接终止并提示
- 匿名状态下只能拿到首页热评、分页全部为空

处理方式：告知用户运行 `node <SKILL_DIR>/scripts/get-cookie.mjs` 手动登录一次（扫码约 30 秒），或用浏览器扩展手动导出覆盖 cookie 文件。agent 无法代替用户完成登录。

## 输出格式

```
B站视频：只是概率模型的AI，却为何能解决数学难题？
URL: https://www.bilibili.com/video/BVxxxxxxxxxx
BV号: BVxxxxxxxxxx / avXXXXXXXXXXXXXX
UP主: 某某UP (mid=XXXXXXXXX)
分区: 知识 | 发布时间: 2026/8/31 23:51:00
官方评论数(含楼中楼): 1256 | 接口 all_count: 1256
抓取时间: 2026/9/10 22:46:10
排序方式: 按热度
本次抓取: 20 条主评论 + 190 条楼中楼
停止原因: 达到目标数量 20
================================================================================

【评论 #1】用户名 (Lv4)  👍54  2026/9/1 08:48:29  楼中楼:34

评论正文...

  └─ 用户A 回复 @用户名（👍20）：回复内容
  └─ 用户B 回复 @用户A（👍1）：回复内容

────────────────────────────────────────────────────────────────────────────────
```

## 分析工作流

用户要求总结、分析或归纳评论时，先完成抓取，再根据评论规模决定是否并行分析：

1. 小型结果可由当前 Agent 直接分析。
2. 大型评论集或需要减少单一视角偏差时，启动两个相互独立的低成本执行型子代理；具体模型以当前框架可用的低价档为准。
3. 两个子代理使用相同分析维度，不共享彼此过程：主要观点、高赞论点、态度分布、关键词、楼中楼争议和独特视角。
4. 主 Agent 对照原始评论合并共识、标注有证据的分歧并去重；任何结论都不能超出抓取文本。

若当前框架不支持子代理，由当前 Agent 分两遍独立阅读并做交叉核验。
## 技术原理

纯 HTTP API，四个接口，全程无浏览器：

| 步骤 | 接口 | 作用 |
|------|------|------|
| 1 | `GET /x/web-interface/view?bvid=` | BV → aid、标题、UP 主、官方评论数 |
| 2 | `GET /x/web-interface/nav` | WBI 签名密钥（img_key/sub_key）+ 登录态检测 |
| 3 | `GET /x/v2/reply/wbi/main` | 主评论游标分页（**必须 WBI 签名**） |
| 4 | `GET /x/v2/reply/reply` | 楼中楼完整展开（ps/pn 分页，同样需要签名） |

- **WBI 签名**：`img_key + sub_key` 按 MixinKeyEncTab 混排取前 32 字符得 mixinKey；参数加 `wts` 时间戳后按 key 排序拼接，`w_rid = md5(query + mixinKey)`；参数值需剔除 `!'()*`。
- **必须带登录 Cookie（SESSDATA）**：匿名只能拿首页热评，分页返回空。
- **请求头**：浏览器 UA + `Referer: https://www.bilibili.com/video/<BV>` + Cookie。
- **风控**：`code=-352` 或 `412` 为风控信号，脚本自动退避重试（5s/10s），连续 3 次后停止并提示加大 `--delay`。

### 接口字段坑（实测，2026-09）

- 主评论的楼中楼总数字段是 `count`（首选）/ `rcount`（兜底），旧接口的 `rcnt` 已不下发
- 每条主评论只自带 2 条楼中楼预览，完整楼中楼必须走接口 4
- `cursor.all_count` 和官方评论数都**含楼中楼**，主评论数远小于它（1256 总评论的视频主评论仅 330 条）
- 热度排序游标冻结见「两种排序的差异」

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 启动即报 SESSDATA 过期 | Cookie 失效 | 运行 `get-cookie.mjs` 重新获取 |
| nav 报告未登录 / code=-101 | Cookie 失效或不完整 | 确认 cookie 文件含 SESSDATA，重新获取 |
| 官方评论数>0 但返回空 | Cookie 失效 | 重新获取 Cookie |
| code=-352 / 412 | 触发风控 | 增大 `--delay`（如 2~3 秒），稍后重试 |
| 热度排序抓不到目标数量 | 采样已到底（连续无新增） | 正常现象，或改用 `--sort time` 全量抓取 |
| 评论区关闭 | 接口 code=12002 | 视频评论区被关闭，无法抓取 |
| 楼中楼数量对不上 | 未加 `--replies` | 默认只带 2 条预览，加 `--replies` 抓全 |

## 文件说明

| 文件 | 用途 |
|------|------|
| `scripts/extract.mjs` | 批量抓取脚本（纯 API 无头，零依赖，速度快） |
| `scripts/get-cookie.mjs` | 一键获取 Cookie（打开浏览器→用户登录→自动导出） |
| `scripts/open.mjs` | 浏览器打开模式（可视化，手动操作） |
| `scripts/package.json` | npm 依赖声明（仅 get-cookie/open 需要） |
| `<user-home>/.bilibili-comment-extractor/` | 运行时 Cookie 与浏览器配置目录，不随 Skill 分发 |
