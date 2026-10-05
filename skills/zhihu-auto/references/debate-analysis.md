# 分手册：争议题立场分析（debate-analysis）

当问题带**争议/站队性**（粉丝战争、圈地争议、政治话题）时，直接抓「前 N 高赞」会得到**排序偏置**：
高赞被最响亮的一方垄断，少数派与不表态的噪音被排序机制滤掉。本手册用「全量枚举 + 分层随机抽样 +
立场加权估计 + 评论层校验」给出可外推、带置信区间的立场分布。

> 实测：某争议题 top-50 对立阵营占 95%，分层随机抽样 165 条后回落到约 78–85%（另约 6% 中立、9–15% 噪音）。
> 排序偏置真实存在，但幅度有限；更有价值的是把「沉默的噪音」与不确定性也纳入描述。

## 第一步：分层随机抽样（strat-sample.mjs）

```powershell
node <SKILL_DIR>/scripts/strat-sample.mjs `
  --url "https://www.zhihu.com/question/XXXXXXXX" `
  --per-band 15 --facets <SKILL_DIR>/scripts/facets.example.json --out-dir <DIR>
```

- 走 answers API 全量枚举（`limit=20` 分页，纯 `fetch`），拿每条**精确赞同数**（比页面「N 万」更准）与正文
- 按赞同数分 11 层，每层随机抽 `--per-band` 条（`--seed` 固定，可复现）
- 产出：`_census.json`（全量）/ `_sample.txt`（供精读）/ `_ledger.json`（判读表，verdict 留空）/ `_stats.json`
- `--facets` 给每条打 `auto_hint` 关键词提示（**仅分诊，不是判读**）；`--census <f>` 复用已有 census
- 达 `--max-pages` 截断时会在 census/stats/sample 标 `truncated` 并告警（截断会系统性高估占比）

## 第二步：判读（agent）+ 加权估计（stance-estimate.mjs）

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

   > **`rid` 是什么 / 不是什么**（用 `rid` 前先读这段）：
   > - **是什么**：`strat-sample.mjs` 按**赞同数降序**（并列时按回答 id 升序）给全量回答编的序号，范围 `1..N`。
   > - **不是**「页面上第 N 条」：`extract.mjs` 的 `【回答 #N】` 是**页面渲染顺序**，两套排序互不对应
   >   （实测页面赞数序列有 8 处相邻降序违例，前 16 条内最大错位 10 位），**不可互相引用**。
   > - **只在一次枚举内有效**：`strat-sample.mjs` 每次运行都按**实时** `voteup_count` 重新排序，
   >   `rid` 会被重排。跨时间 / 跨枚举引用 `rid` 会**静默错位**。要跨轮引用，请用回答 id。
   > - **跨文件对齐用回答 id**（值是同一个纯数字），但**字段名各产物不同**——不要假设都叫 `aid`：
   >
   >   | 产物 | 回答 id 字段 | 另有 | 生成脚本 |
   >   |---|---|---|---|
   >   | `_census.json` → `rows[]` | `id` | `rid` | `strat-sample.mjs` |
   >   | `_ledger.json` → `rows[]` | `id` | `rid` | `strat-sample.mjs` |
   >   | `_comments.json` → `answers[]` | `answer_id` | `rid` | `fetch-comments.mjs` |
   >   | `extract.mjs` 输出 txt 标题行 | `(aid=…)` | `#N`（页面序） | `extract.mjs` |
   >
   >   没有回答 id 可用时，退而用「作者 + 赞同数 + 回答节选」三元组人工对齐。
2. 运行估计（脚本完成加权、区间、交叉验证）：
   ```powershell
   node <SKILL_DIR>/scripts/stance-estimate.mjs `
     --ledger <ledger.json> --census <census.json> --verdicts <verdicts.txt> `
     --categories "A=反X/挺Y,B=挺X,N=中立调和,O=无关难判" --output <report.md>
   ```
3. 输出：**设计加权占比 + 95% CI**（分层方差 + 有限总体校正）、未加权 Wilson 参考、全量**关键词交叉验证**。
   - **敏感性**：把反串/待复核项校正成第二份判读，加 `--alt <校正判读>` → 报告多出「主判读 vs 校正判读」对比表（某阵营一校正就垮塌 = 该结论不稳）。
   - 判读里出现未声明类别会 **fail-closed 报错**（防拼写事故伪装成结论不稳）。

## 第三步（可选）：评论层（fetch-comments.mjs）

回答区易被压成同质化，**反驳/质疑/反例往往沉在评论区**。把「回答下面的热评」抓成独立观察层：

```powershell
node <SKILL_DIR>/scripts/fetch-comments.mjs `
  --census <census.json> --ledger <ledger.json> --only-sampled --per-answer 10 --out-dir <DIR>
```

- `--only-sampled`：只抓抽样那批回答（请求量最小）；省略则按赞同降序抓全量，可 `--top n` / `--max-answers n`
- `--per-answer K`（默认 10）、`--replies K`（楼中楼，默认 0，会成倍增加请求）
- 产出 `_comments.json` + `_comments.txt`；**给了 `--ledger` 时每条回答连同「判读 + 正文节选」输出，即一张「评论区复核清单」**
- 评论赞数字段是 `vote_count`（不是 answers 的 `voteup_count`），作者在 `author.member.name`
- **鉴权失败 fail-closed**：某条回答返回 401/403 时脚本**立即中止且不写任何 comments 文件**（退出码 1）。
  凭据失效不会被伪装成「这批回答没有评论」。**楼中楼（`--replies K`）路径同样 fail-closed** ——
  `child_comments` 返回 401/403 时同样立即中止，不会被未绑定的 `catch` 吞成「楼中楼凭空消失」。
- 单条回答抓取失败（非鉴权）会记进 `_comments.json` 的 `failures[]`，对应条目 `fetch_failed: true`，
  `_comments.txt` 里印 `(抓取失败 —— 不是「无评论」…)`；**全部**回答都失败则同样中止、不写文件。
  统计评论数时请先排除 `fetch_failed: true` 的条目。

**评论层只做三件事，且必须与回答区分开报告：** ① 争议点定位；② 两区对照（回答区 vs 评论区立场）；
③ 信息增量（新事实/反例）。**不要并进回答区统计**——评论是回答的附属、非独立样本，同意靠点赞、反对才留言，
会放大分歧、压低共识，且更易被刷。

## 反串/阴阳识别（评论区校验的核心用途）

回答的**语气 ≠ 阵营**——反串会让二者相反。判定启发式（**默认只降置信，不单独作为改判依据**）：

- **点赞来源一致性**（最重要）：一条「挺 A」的回答在明显反 A 的场里却高赞 → 可疑（A 方不会赞它）；看它评论区**谁在赞、谁在骂**。
- **敬语反讽**（「X 老师」）、**劝退套话**（「做好自己就行了」）只是可疑信号；**一句式 / 纯图回答**文本本就没立场，可用评论区归属辅助。
- **改判须同时满足**：① 文本没有可指认的立场表述，**且** ② 评论区归属一致且明确；否则标「待复核」。
- **对称性**：同样适用于反向情形，避免只往一个方向改判。
- **敏感性**：改判/待复核项做成第二份判读，重跑 `--alt` 对比主判读。

> 真实案例：一条「子午老师，做好自己就行了……他们骂你，你就当他们骂的是狗」表面像挺 A，其评论区却全是反 A 者在点赞
> 「你这劝架有水平的[doge]」——实为反串。单看文本必误判。

## 设计哲学

脚本负责一切**确定性**的事：枚举、分页、分层、随机、加权、置信区间、关键词交叉验证与记账；
「每条属于哪个阵营」这个**需要判断**的事留在 agent 手里——脚本只给 `auto_hint` 分诊，绝不替 agent 下判断。
