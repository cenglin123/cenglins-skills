# 知乎回答批量抓取工具

批量抓取知乎问题下的回答，保存为结构化 txt 文档。

使用 puppeteer-extra + stealth 插件绕过知乎反爬检测（40362 错误码）。

## 快速开始

### 1. 安装依赖（及环境自愈）

依赖与 Cookie 存放在 **skill 目录之外**（默认 `~/.zhihu-answer-extractor/`），以免 cc-switch 从 GitHub 重装 skill 时（只会带回仓库里的文件）把它们清空。推荐用自愈脚本一键就位：

```bash
node scripts/setup.mjs
```

每次用 cc-switch 重装本 skill 后重跑一次即可（几秒，只重建软链，不重新下载依赖）；`node scripts/setup.mjs --check` 可检查环境是否就绪。

### 2. 获取 Cookie

#### 方式一：一键获取（推荐）

```bash
node scripts/get-cookie.mjs
```

脚本会自动打开浏览器，你只需：
1. 在弹出的浏览器中登录知乎（扫码/验证码/密码均可）
2. 登录成功后脚本自动检测并导出 Cookie
3. Cookie 保存到外部路径 `~/.zhihu-answer-extractor/www.zhihu.com_cookies.txt`（跨重装存活）

> ⚠️ **风险提示**：导出的 Cookie 包含你的知乎登录凭证（`z_c0`），任何获得此文件的人都可以以你的身份访问知乎。请妥善保管，不要分享给他人或上传到公共仓库。

#### 方式二：手动导出

1. 用 Chrome 浏览器登录 [zhihu.com](https://www.zhihu.com)
2. 安装 Cookie 导出扩展：
   - [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)（推荐）
   - [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
3. 在知乎页面点击扩展图标 → 导出 → 选择 **Netscape HTTP Cookie File** 格式
4. 保存为 `~/.zhihu-answer-extractor/www.zhihu.com_cookies.txt`（脚本也兼容读取 skill 内的旧路径）

### 3. 运行

```bash
node scripts/extract.mjs --url "https://www.zhihu.com/question/XXXXXXXX" --count 50 --output "回答.txt"
```

`--count` 默认为 50；省略 `--output` 时保存到脚本目录。`--max-wait` 可调整最长加载等待秒数，默认 180。

## 六种模式

### Cookie 获取（get-cookie.mjs）

打开浏览器，用户手动登录，自动导出 Cookie：

```bash
node scripts/get-cookie.mjs
```

### 抓取模式（extract.mjs）

headless 运行，速度快，输出 txt 文件：

```bash
node scripts/extract.mjs --url "https://www.zhihu.com/question/XXXXXXXX" --count 50
```

### 浏览模式（open.mjs）

打开可视化 Chrome 窗口，手动操作：

```bash
node scripts/open.mjs
```

修改 `open.mjs` 顶部的 `TARGET_URL` 即可。

### 分层抽样（strat-sample.mjs）

争议题（粉丝战争/圈地争议/政治话题）用：走 `answers` API 全量枚举，按赞同数分层随机抽样，避免「只看高赞」的排序偏置。纯 `fetch`，无需浏览器。

```bash
node scripts/strat-sample.mjs --url "https://www.zhihu.com/question/XXXXXXXX" \
  --per-band 15 --facets scripts/facets.example.json --out-dir ./out
```

产出 `_census.json`（全量）/ `_sample.txt`（抽样正文）/ `_ledger.json`（判读表）/ `_stats.json`（分层统计）。

### 立场估计（stance-estimate.mjs）

对抽样结果做**设计加权占比 + 置信区间**，并对全量做关键词交叉验证：

```bash
node scripts/stance-estimate.mjs --ledger out/X_ledger.json --census out/X_census.json \
  --verdicts verdicts.txt --categories "A=反X,B=挺X,N=中立,O=无关" --output report.md
```

`verdicts.txt` 由 agent 精读 `sample.txt` 后按 `rid` 归类填写：

```
# A
12 13 14
# B
119
```

无 `verdicts` 时可用 `--use-autohint --facets <file>` 做低置信预览。

### 评论层（fetch-comments.mjs）

抓「回答下面的热评」，作为与回答区相互独立的第二观察层（反驳往往沉在评论区）：

```bash
node scripts/fetch-comments.mjs --census out/X_census.json --ledger out/X_ledger.json \
  --only-sampled --per-answer 10 --out-dir ./out
```

- `--only-sampled`：只抓分层抽样那批回答（推荐，请求量最小）；省略则按赞同降序抓全量，可用 `--top n` / `--max-answers n` 限流
- `--per-answer K`：每条回答取前 K 条热评（默认 10）
- `--replies K`：每条热评再抓 K 条楼中楼（默认 0，会成倍增加请求）
- 产出 `_comments.json` + `_comments.txt`

> ⚠️ 评论是回答的**附属、非独立**样本（同意靠点赞、反对才留言），会放大分歧、压低共识。它给的是「反应/争议分布」，**不要并进回答区的立场统计**。

**复核清单与反串识别**：给 `fetch-comments.mjs` 加 `--ledger`，输出的每条回答会连同「判读 + 正文节选」一起列出——即一张**复核清单**。用它做**反串/阴阳校验**：文本语气可能骗人（如一句「劝对方别理会黑子」表面像支持，其评论区却全是反方在叫好 → 实为反串），此时以**评论区归属**定立场。校正后重跑 `stance-estimate.mjs --alt <校正判读>` 做**敏感性**：某阵营占比一校正就垮塌，说明结论不稳。

## 输出示例

```
知乎问题：为什么体制内至今仍不鼓励用人工智能？
URL: https://www.zhihu.com/question/2042649810709239000
提问者: 张三
话题: 人工智能, 体制, 科技政策
关注者: 12,345
被浏览: 1,234,567
2,345 个回答
抓取时间: 2026/8/10 16:56:05
本次抓取: 50 条回答
目标数量: 50 条回答
停止原因: 达到目标数量 50
================================================================================

【题干】
问题描述正文内容...

────────────────────────────────────────────────────────────────────────────────

【回答 #1】张三 (某领域优秀答主)
赞同: 2520

正文内容...

────────────────────────────────────────────────────────────────────────────────

【回答 #2】李四
赞同: 640

正文内容...
```

## 分析工作流（子代理初读 + 主代理独立裁决）

当需要对抓取结果进行总结分析时，采用**子代理初读 + 主代理独立裁决**模式，以避免单一视角的偏误：

1. **主代理**完成抓取，获得 txt 文件
2. **spawn 1~2 个子代理**独立精读 txt，产出结构化初读报告（节省主代理逐字精读的工作量）
3. **主代理自己也必须独立阅读原文**（题干 + 高赞 + 抽样），形成第一手判断——不能只压缩子代理报告交差
4. **主代理作为最终裁决者**对比双方：共识确认；分歧回到原文裁决；独有发现核实后并入
5. 输出最终总结，关键结论标注回答编号、作者、赞同数作为证据

这种方法的要点：
- 子代理初读节省工作量，但初读不能代替主代理的第一手判断
- 至少两个独立视角（子代理 + 主代理自己）才能避免偏误
- 分歧必须由主代理回到原文裁决，而不是取平均

> **争议题额外一步**：若问题带有明显站队性，先跑 `strat-sample.mjs` 做分层随机抽样，再对样本精读判读、用 `stance-estimate.mjs` 给出加权占比与置信区间——这样结论不会被高赞区（排序偏置）带跑，且能显式报告「中立/噪音」占比与不确定性。若还想看异议/反驳，用 `fetch-comments.mjs` 抓回答下面的热评作为**独立评论层**分析（见 SKILL.md「评论层分析」），但不要与回答区立场混算。

详见 `SKILL.md` 中的流程与子代理 Prompt 模板。

## 原理

### 为什么不用浏览器自动化工具（Selenium/Playwright）？

知乎检测到 WebDriver 标志会返回 40362 错误。本工具使用：

- **puppeteer-extra**：Puppeteer 的增强版，支持插件系统
- **stealth 插件**：自动隐藏 20+ 个自动化检测点（WebDriver、navigator.plugins、语言列表等）
- **evaluateOnNewDocument**：在每个页面加载前注入反检测代码
- **启动参数**：`--disable-blink-features=AutomationControlled` 禁用 Blink 引擎的自动化检测

### Cookie 获取原理

`get-cookie.mjs` 通过 Chrome DevTools Protocol (CDP) 直接从浏览器提取 Cookie：

1. 使用 `Network.getAllCookies` 命令获取浏览器中所有 Cookie
2. 过滤出 `zhihu.com` 相关的 Cookie
3. 转换为 Netscape HTTP Cookie File 格式保存

这比手动安装扩展导出更方便，且不需要额外的浏览器扩展。

### 回答加载机制

知乎首屏只静态渲染前几条回答，更多回答依赖交互加载。脚本按以下策略确保加载足够数量（2026-09 实测）：

1. **真实点击「查看剩余 N 条回答」按钮**：该按钮常是普通 div + cursor:pointer（React pointer 事件驱动），页内 `el.click()` 合成事件（isTrusted:false）无法触发。脚本先在页内按完整文本匹配定位按钮（含 div/span 兜底、取最内层匹配），返回 ElementHandle 后**在 Node 侧调用 `elementHandle.click()` 发送真实 CDP 鼠标事件**
2. **无限滚动兜底**：点击展开后页面可能切换为无限滚动，每轮继续定位最后一条回答下移（优先独立滚动容器，页面滚动兜底）
3. 自动展开折叠正文，关闭遮挡点击的弹窗
4. 使用多组回答选择器，并持续对照已加载数量和页面报告总数
5. 停止条件：达到目标数量、加载到页面报告总数、明确的无更多内容提示、滚动末端持续无新增，或触发登录墙
6. 在控制台和输出文件中记录停止原因，数量不足时给出警告
7. 赞同数支持「1.6 万」万级换算（优先读 `aria-label`，避免把 1.6 万解析成 16）

## 常见问题

### Q: 依赖或 Cookie 突然消失（`Cannot find package 'puppeteer-extra'` / `Cookie 文件不存在`）

原因：cc-switch 等管理器从 GitHub 重装本 skill 时，会整体替换 skill 目录，把被 `.gitignore` 排除的 `node_modules`、Cookie 一并清空。

解决：运行 `node scripts/setup.mjs` 自愈。Cookie 与依赖现已放在 `~/.zhihu-answer-extractor/`，重装不受影响，setup 只需重建软链（无需重新下载）。

### Q: 返回 40362 错误

Cookie 过期或被检测。解决：
1. 运行 `node scripts/get-cookie.mjs` 重新获取 Cookie
2. 或手动重新导出 Cookie

### Q: 重定向到登录页

Cookie 无效。解决：
- 运行 `node scripts/get-cookie.mjs` 重新获取 Cookie

### Q: 停止原因显示“触发登录墙”

Cookie 被服务端吊销（如在别处退出登录、知乎会话轮换）。注意两个易误判点：

- z_c0 纸面有效期未到（甚至还有数月）也可能已失效
- 问题页匿名也能看（HTTP 200、标题正常、能见前几条回答），**只有点击「查看剩余」展开更多回答时才强制登录**，所以加载页面的日志看起来一切正常

快速确认：页面右上角显示「登录/注册」按钮而非头像即为登录态失效。解决：运行 `node scripts/get-cookie.mjs` 手动重新登录。

### Q: 回答数量不够

正常现象。可能原因：
- 该问题回答数本身就不足目标数量
- Cookie 失效触发登录墙（见上一条）
- 部分回答被折叠且无法展开
- 网络或知乎接口响应超过 `--max-wait`
- 知乎页面结构发生变化，备用选择器也未匹配

先查看输出文件中的“停止原因”和终端显示的实际抓取数量。若是超时，可增大 `--max-wait` 后重试；若页面显示的回答数明显更多但仍提前结束，应保留日志并更新加载按钮或回答选择器。

### Q: 窗口模式下浏览器关闭了

进程退出时 Chrome 子进程被回收。解决：
```powershell
# Windows：用 Start-Process 后台启动
Start-Process -FilePath "node" -ArgumentList "scripts\open.mjs" -WindowStyle Normal
```

## Cookie 有效期

- `z_c0`（登录凭证）：约 6 个月
- `d_c0`（设备标识）：约 1 年
- 其他 cookie：多数为会话级或短期

如果突然无法抓取，优先检查 `z_c0` 是否过期。运行 `get-cookie.mjs` 可重新获取。

## 文件结构

```
zhihu-answer-extractor/
├── SKILL.md                    # Agent 技能定义
├── README.md                   # 本文档
└── scripts/
    ├── package.json                 # npm 依赖声明
    ├── get-cookie.mjs               # Cookie 一键获取脚本
    ├── setup.mjs                    # 环境自愈（外部依赖 + Cookie 迁移 + 建软链）
    ├── lib/env.mjs                  # 共享路径解析（外部家目录/Cookie/依赖链接）
    ├── extract.mjs                  # 批量抓取脚本（headless）
    ├── strat-sample.mjs             # 全量枚举 + 分层随机抽样
    ├── stance-estimate.mjs          # 立场加权估计 + 置信区间
    ├── fetch-comments.mjs           # 回答下面热评的评论层抓取
    ├── facets.example.json          # 阵营关键词配置示例
    ├── open.mjs                     # 浏览器打开模式（可视化）
    └── www.zhihu.com_cookies.txt    # Cookie 文件（由 get-cookie.mjs 生成）
```

## 环境要求

- Node.js 18+
- Chrome 浏览器（脚本会自动检测路径）
- Windows / macOS / Linux

## 许可

MIT
