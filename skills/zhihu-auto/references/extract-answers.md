# 分手册：抓取知乎问题回答（extract-answers）

把某问题下的高赞回答抓成结构化 txt。走 **API 枚举 + puppeteer 渲染**（渲染页才能拿到完整正文/折叠内容）。

## 头一次：获取 Cookie

两种方式，都写到外部路径 `~/.zhihu-auto/www.zhihu.com_cookies.txt`（跨 skill 重装存活）：

- **一键**：`node <SKILL_DIR>/scripts/get-cookie.mjs` → 打开浏览器 → 用户手动登录 → 自动导出。
  - `--no-wait`：存好 Cookie 后**立即关闭浏览器退出**，不等 Enter。**agent / 无人值守场景必加**
    （不加时脚本会等 Enter；stdin 若是管道或被关闭，最多 30 秒后也会自动关闭，不会挂住）。
  - `--max-wait <秒>`（也接受 `--max-wait=秒`）：最长等待登录时间，>= 60，默认 300。
    无效值会**报错退出（exit 2）而不静默回退默认**。等价环境变量：`ZHIHU_MAX_WAIT_MS`（毫秒）。
  - `--help` 打印用法；未知参数同样 exit 2。
- **手动**：登录 zhihu.com → 用 Cookie-Editor 等导出 Netscape 格式 → 存到上面那个路径（脚本也兼容 skill 内旧路径）。

> ⚠️ Cookie 含 `z_c0` 登录凭证，任何拿到它的人都能以你身份访问知乎；别分享、别入库。有效期约 6 个月。

## 抓取

```powershell
node <SKILL_DIR>/scripts/extract.mjs `
  --url "https://www.zhihu.com/question/XXXXXXXX" `
  --count 50 `
  --output "<OUTPUT_FILE>"
```

- `--count` 默认 50；省略 `--output` 存到脚本目录；`--max-wait <秒>` 调最长加载时间（默认 180）。
- 依赖缺失时**脚本会自愈**：自动从外部备份 `~/.zhihu-auto/deps/` 重建软链再继续（cc-switch 重装后无需手动处理）；仅当外部备份也没有时才跑 `node <SKILL_DIR>/scripts/setup.mjs`。

## 加载机制（2026-09 经验，页面结构可能变）

首屏只静态渲染前几条；更多回答靠「查看剩余 N 条回答」按钮加载。该按钮是 React pointer 事件，
页内 `el.click()` 合成事件无效，脚本在 Node 侧用 `elementHandle.click()` 发**真实 CDP 鼠标事件**；
点击后可能转无限滚动，脚本保留滚动兜底。停止条件：达目标数、加载到页面报告总数、出现「无更多」提示、
滚动末端持续无新增、或**触发登录墙**。输出文件会记「停止原因/目标数量/本次抓取」——
数量不足时**按停止原因判断**，别把「无新增」当抓取完成。

## 输出格式（节选）

```
知乎问题：…
URL: …
话题: …
关注者: … / 被浏览: … / N 个回答
本次抓取: 50 条回答
目标数量: 50 条回答
停止原因: 达到目标数量 50
编号说明: #N 是页面渲染顺序，不是 strat-sample 的 rid（rid 是赞数降序名次，每次重跑会重排）；aid 是回答唯一 id，可与 _census.json 的 rows[].id、_ledger.json 的 rows[].id、_comments.json 的 answers[].answer_id 对齐（值相同、字段名不同）。
aid 覆盖: 50/50（未取到的行标 (aid=?)）
================================================================================
【回答 #1】作者 (aid=12345678901) (签名)
赞同: 2520

正文…
```

## 专栏文章（zhuanlan.zhihu.com/p/*）

问题页脚本 `extract.mjs` **不支持**专栏文章 —— 解析器是问题页专用的。
专栏文章用另一个脚本：

```powershell
node <SKILL_DIR>/scripts/extract-article.mjs --url "https://zhuanlan.zhihu.com/p/<id>" [--out <文件>] [--no-links] [--timeout <秒>]
```

`--url` 也接受纯数字 id；输出 txt = 元信息头 + 正文 + 外链附录。

**三条实测事实（2026-10，决定了它的实现方式）：**

1. **HTML 层有 `zse-ck` 挑战**：纯 `fetch` / 无签名请求一律 403（挑战页）。因此必须走
   stealth 浏览器；脚本自动找 Chrome/Edge（含 Edge 兜底），无浏览器时报错退出。
2. **API 层帮不上忙**：`/api/v4/articles/<id>` 要求 `x-zse-96` 请求签名，未实现绕过
   （403 code 10003「请求参数异常」）；旧 `zhuanlan.zhihu.com/api/posts/<id>` 已 404。
3. **匿名即可读公开文章，脚本不注入 Cookie**：登录墙 / 付费 / 仅关注者可见的文章
   **明确不支持**（报错退出而非把登录态带进浏览器进程）。

**LinkCard 要单独处理**：知乎外链卡片（如「项目仓库已开源：」后面那张卡）的 `<a>`
**没有 innerText**，只取正文会丢掉最关键的链接。脚本会扫正文里的全部 `<a href>`、
解码 `link.zhihu.com/?target=<urlencoded>` 还原真实目标，并过滤站内
`topic/people/pin/question/zhida` 链接，附在输出末尾。

退出码：0 成功；1 被风控拦截 / 需登录 / 未取到正文；2 用法错误（未知参数、缺值同 exit 2）。

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 返回 40362 / 重定向登录页 | Cookie 过期或失效 | 重跑 `get-cookie.mjs`（注意 `z_c0` 纸面未过期也可能被吊销） |
| 停止原因=触发登录墙 | 会话被服务端吊销（问题页匿名可看，但展开更多回答时强制登录） | 看页面右上角是否「登录/注册」；重跑 `get-cookie.mjs` |
| 数量不够 | 总数不足 / 登录墙 / 折叠 / 超时 / 结构变化 | 看输出「停止原因」；必要时加大 `--max-wait` |
| `Cannot find package 'puppeteer-extra'` | 依赖被重装清空（软链没了） | **脚本会自动自愈**（从 `~/.zhihu-auto/deps/` 重建软链）；若仍失败（外部备份也没了）再跑 `node <SKILL_DIR>/scripts/setup.mjs` |
