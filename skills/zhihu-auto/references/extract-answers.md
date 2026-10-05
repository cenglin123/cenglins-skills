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

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 返回 40362 / 重定向登录页 | Cookie 过期或失效 | 重跑 `get-cookie.mjs`（注意 `z_c0` 纸面未过期也可能被吊销） |
| 停止原因=触发登录墙 | 会话被服务端吊销（问题页匿名可看，但展开更多回答时强制登录） | 看页面右上角是否「登录/注册」；重跑 `get-cookie.mjs` |
| 数量不够 | 总数不足 / 登录墙 / 折叠 / 超时 / 结构变化 | 看输出「停止原因」；必要时加大 `--max-wait` |
| `Cannot find package 'puppeteer-extra'` | 依赖被重装清空（软链没了） | **脚本会自动自愈**（从 `~/.zhihu-auto/deps/` 重建软链）；若仍失败（外部备份也没了）再跑 `node <SKILL_DIR>/scripts/setup.mjs` |
