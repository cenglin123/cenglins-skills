# `run --spec` 步骤运行器 — 完整 schema 与语义

> 从 SKILL.md §三 下沉。相关演进背景见 [`../DEVELOPMENT.md`](../DEVELOPMENT.md)。
> 实现：`scripts/ocsr_run_spec.py`（校验 + 执行）、`ocsr_dispatch.py run`（CLI）。

## 它解决什么

多步骤协议由 agent 逐步手工搬运时，会出现参数推导错误（把某个分组的序号填成另一个分组的轮次号）、
时序与记账错误（终局事件漏链接、可从事件图导出的字段被手填）、
资源归属错误（在派发进行中往被监视的目录写文件）。
这些**不需要判断力，只需要不出错地重复**——正是脚本该干的。

**立场：把搬运交给脚本，判断留给 agent。**
运行器不写 prompt、不判 verdict、不裁决分歧；它只做步骤序列、参数推导、路由、记账、断点续跑。
**它防不住内容事实错误**（引用了不存在的函数名、编号错位、计数错误）——那类错误的对治是评审，不是调度。

## 命令

```bash
# 离线干跑：只校验 + 输出结构化摘要，不发起任何模型调用
python scripts/ocsr_dispatch.py run --spec <file> --validate [--format json]

# 执行
python scripts/ocsr_dispatch.py run --spec <file>

# 断点续跑 / 回答暂停点
python scripts/ocsr_dispatch.py run --spec <file> --resume [--answer <step-id>=<option>]
```

### 退出码

| 码 | 含义 |
|---|---|
| `0` | run 完成 |
| `1` | 步骤失败（hook 非零/`expect` 不匹配、assert 不成立、dispatch 非零、被 `abort`） |
| `2` | spec 非法或用法错误 |
| `10` | **暂停待裁决**，已写 `pause-request.json` |
| `11` | **续跑状态不确定**，停机 |

> 与 `dispatch` 子命令的退出码契约（见 `dispatch-patterns.md` §退出码契约）是**两套**，勿混用。

## Spec schema（version: 1）

```yaml
version: 1
run:
  id: <标识，[A-Za-z0-9][A-Za-z0-9._-]{0,63}>
  workdir: <runner 独占的目录，可用模板>
vars:                      # 可选，字符串到字符串
  k: v
steps:                     # 非空；**第一个步骤是入口**
  - id: <唯一标识>
    type: dispatch | hook | pause | assert
    ...
```

### 步骤类型（**封闭**为四种）

新增任何步骤类型，都必须重新审视本包「不提供通用 runner」的设计边界，
并按治理文档变更走独立复查——**不得作为普通功能增强顺手加入**。
注意这是**治理约束（依赖人执行）**，不是代码级强制：`frozenset` 只挡住运行期的未知类型，
挡不住一次 commit。

> ⚠️ **边界的真实位置（2026-08-10 治理评审实证订正）**：
> `hook` 步骤执行 spec 显式声明的 argv，**这本身就足以执行任意命令**——
> 例如 `run: [python, -c, "<任意代码>"]`。校验层不做命令白名单，也拦不住它。
> 因此**不得声称运行器「不执行任意用户代码」**：那是一句事实性虚假声称。
> 准确的表述是：运行器**不提供** inline eval/exec 式的步进内代码解释语法，
> hook 的 argv 由 spec 显式声明、可审计；但**它不是安全沙箱，也不宣称是**——
> 与 SKILL.md §七「`--dir` 不是沙盒」同源。
> **spec 及其调用的命令必须当作可信输入对待**；不可信 spec 等同于不可信代码。

#### `dispatch` — 派发 OCSR worker

| 字段 | 必填 | 说明 |
|---|---|---|
| `model` | ✅ | OCSR 白名单内的 qualified ID |
| `prompt` | ✅ | prompt 文件路径（相对路径按 spec 文件所在目录解析） |
| `output` | ✅ | 期望产物路径，**必须在 `run.workdir` 之内**（D7） |
| `role` | | 遥测角色 |
| `scope` | | **runner 的分组计数键**，见下方专节 |
| `timeout_min` | | 看门狗阈值（正整数） |
| `forbid_paths` | | 禁读路径列表，注入 prompt 副本 |
| `ledger_dir` | | 派发账本目录 |
| `meta` | | 透传给 ocsr 遥测的归因字段（`task_id` / `plan_ref` / `scope` / `blocking_chain` / `converge-invocation-id`） |
| `pre` / `post` | | **内联 hook**，见下 |

进程内调用 `_dispatch_batch`（不起子进程）。派发前会**重新校验**模型白名单、
prompt 存在性与输出目录——不以「已经 `--validate` 过」为由跳过，
以封堵 validate 与 run 之间的窗口期。

> ⚠️ `step.scope`（runner 分组键）与 `step.meta.scope`（ocsr 遥测归因）是**两回事**，刻意不混用。

#### `hook` — 执行外部命令

```yaml
- id: reserve
  type: hook
  run: [python, "{{vars.gate}}", reserve, --role, outer-reviewer]
  expect: '^PROCEED:(?P<rid>\w+)$'      # 可选；不匹配即失败
  timeout_sec: 300                       # 可选
```

`rc ≠ 0` 或 `expect` 不匹配即步骤失败（契约违反 fail-closed）。
`expect` 的命名组捕获进 run context，后续步骤以 `{{steps.reserve.capture.rid}}` 引用
（文法见下方「模板文法」表）。

#### `pause` — 把决策交回 agent

```yaml
- id: ask
  type: pause
  question: verdict 非预期，请裁决
  options: [fix, closeout, abort]
```

写 `pause-request.json`（问题 / 选项 / 上下文快照 / 续跑提示）并以 **exit 10** 退出。
`--answer <step-id>=<option>` 续跑。

- 保留字 `abort`（终止本次 run）与 `retry`，**不是**步骤 id、也不构成图的边
- 其余选项**必须**是已定义的步骤 id
- 指向步骤 id 的选项是图的**真实出边**——某些步骤可能只经由 pause 裁决到达，
  不计入会被可达性检查误判为不可达

> `retry` 的语义（重跑触发暂停的上游步骤）**尚未实现**，命中时明确报错而非静默忽略。

#### `assert` — 确定性校验

```yaml
- id: check
  type: assert
  assert:
    file_exists: "{{run.workdir}}/report.md"
    non_empty: true
    matches: 'verdict'
```

条件仅支持 `file_exists` / `non_empty` / `matches`；后两者需与 `file_exists` 同时给出。

### 内联 hook：`pre` / `post`

**`pre` / `post` 不是独立步骤类型。** 语法（`run` argv + 可选 `expect`）与语义都同 `hook` 步骤，
区别只在生命周期：内联 hook 绑定在宿主步骤的前 / 后，独立 `hook` 步骤是图上的节点。

```yaml
    pre:
      - run: [python, "{{vars.gate}}", reserve, --role, outer-reviewer,
              --target-round, "{{scope.review-round.next_index}}"]
        expect: '^PROCEED:(?P<reservation_id>\w+)$'
    post:
      - run: [python, "{{vars.gate}}", settle,
              --reservation-id, "{{steps.r1.pre[0].reservation_id}}"]
```

任一内联 hook 失败即宿主步骤失败；`post` 不执行。

### 取值与路由

```yaml
    extract:
      verdict: "yaml:verdict"
    route_on: verdict          # extract 有多项时必填
    route:
      "可执行": closeout
      "阻断需修复": fix
      "*": ask                 # 必填，且必须指向 pause 步骤
```

**取值器封闭为三种**——没有任何让脚本「判断」的入口，这是结构性保证而非约定：

| 取值器 | 语义 |
|---|---|
| `yaml:<key>` | 取文本中**第一个** fenced YAML 块的顶层键；无 fenced 块则退回全文 |
| `regex:<pattern>` | 首个命名组（无命名组则取第一个分组，再无则取整个匹配） |
| `exitcode` | 步骤退出码 |

**取值来源按步骤类型确定，二者刻意不同**：

- `hook` → 进程的 **stdout + stderr**（外部命令的自然产出）
- `dispatch` → **产物文件的内容**（子代理的报告写在文件里，不靠 stdout 回传，
  这正是 SKILL.md §五「不采信自我报告、只信文件系统证据」的直接体现）
- `assert` → 只能用 `exitcode`

`route` **必须**显式写 `"*"` 兜底，且兜底目标**必须**是 `pause` 步骤：
未预期的取值是**判断分歧**，应 fail-open 交回 agent，而不是让运行器猜。

无 `extract` 的步骤用 `next: <step-id>` 单向推进；二者都没有即为终止步骤。

### 模板文法（封闭）

| 形式 | 说明 |
|---|---|
| `{{run.id}}` / `{{run.workdir}}` | run 元信息 |
| `{{vars.<name>}}` | `vars` 段的变量 |
| `{{scope.<key>.next_index}}` | **runner 按分组键派生的单调序号** |
| `{{steps.<id>.(pre\|post)[<n>].<name>}}` | 内联 hook 的命名组捕获 |
| `{{steps.<id>.capture.<name>}}` | **独立 `hook` 步骤自身** `expect` 的命名组捕获 |

内联 hook 用 `pre[n]` / `post[n]` 下标定位（一个宿主步骤可挂多个）；
独立 `hook` 步骤只有一个 `expect`，故用不带下标的 `capture.<name>`。
后者只对 `type: hook` 的步骤成立——`assert` / `pause` / `dispatch` 没有 `expect` 可捕获，
引用它们的 `capture.<name>` 在 `--validate` 阶段即 fail-closed。

数组下标 `[<n>]` 与命名组捕获是仅有的两种复合形式。
引用不可解析即 fail-closed（变量未定义、步骤不存在、下标越界、捕获组不存在）。

> **YAML 陷阱**：正则**必须用单引号标量**（`'^PROCEED:(?P<rid>\w+)$'`）。
> 双引号标量会按 YAML 转义规则处理反斜杠，`\w` 会报 `unknown escape character`。

## `scope`：分组计数键

`{{scope.<key>.next_index}}` 由运行器按分组键维护单调计数器，**调用方不写数字**。
这直接消灭「手填轮次号」那一类错误——调用方只声明「这是某组的第几个」。

**运行器把 `scope` 当作不透明字符串**：它只按键分组计数，不理解任何具体取值的含义。
该不变量有回归测试断言（实现文件中不得出现任何具体分组键字面值），
把边界从「靠自觉」变成「靠测试」。

含义归 spec 作者。converge 作为客户时的已知取值供参考：

| 取值 | 在 converge 中的含义 |
|---|---|
| `outer` | 主循环轮次，产物 `round-{n}.md` |
| `blind` | 盲审复核序号，产物 `blind-recheck-{n}.md` |
| `ultraverge` | 并行初评批次，产物 `uv-init-{n}.md` |

> 这三个取值是 **converge 的概念**，运行器不认识、不校验它们。
> 列在此处只为 spec 作者参考——「运行器不理解、但 spec 作者需要知道」的分工。
> 注意它们的编号语义**互不相同**（轮次 vs 序号 vs 批次），
> 历史上正是这处隐式差异两次绊倒了人工编排。

## journal 与断点续跑

workdir 下的 `journal.jsonl` 是 append-only 执行日志，采用 **started / completed 两段式**：
每步先写 `step-started`（含派生的分组序号），副作用发生后才写 `step-completed`
（含路由键、实际去向、hook 捕获）。

**四条 fail-closed，共同的立场是「不确定就停机，绝不猜」**：

| 情形 | 处置 |
|---|---|
| `step-started` 无 `completed`/`paused` | **停机(11)，禁止自动重跑**——该步可能已消耗真实模型调用与预算 reservation，重跑会双花且破坏账本 |
| 已有 journal 却未加 `--resume` | 拒绝覆盖既有执行记录 |
| spec 的 sha256 与上次不符 | 拒绝把新 spec 的语义套到旧 journal 上 |
| journal 存在无法解析的行 | 状态不确定 |

## workdir 独占

运行器创建并**独占** `run.workdir`。dispatch 步骤的产物路径若落在其外，
**在派发前**即 fail-closed。

**run 期间调用方不得写 workdir**——历史上「在派发进行中把基线快照写进被监视目录」
就触发过误报，现由所有权规则消除。

## `--validate` 能查什么、查不到什么

**能查**（各以特定 error code fail-closed）：spec schema、步骤 id 唯一性、
步骤类型合法性、`route` 目标存在性与 `"*"` 兜底、兜底必须指向 pause、
图无环与全可达、模板引用可解析、取值器在封闭枚举内、
正则可编译、模型在白名单、prompt 文件存在、pause 选项合法。

**查不到**：**语义错误**。比如把两条路由映射对调——spec 依旧完全合法，
静态校验必然放行。这类错误只有端到端比对能抓（执行序列 / 产物清单 / journal 三条独立判据）。
`tests/test_run_dogfood.py` 用注入语义错误的方式**证明了比对确有鉴别力**，
而不是只在正例上通过。

`--validate` 还会输出 spec 的**结构化摘要**（步骤表、路由图、模板引用表）供派发前肉眼复核，
以及对复杂 `regex` 取值器的**启发式提示**（命名组 > 3 或长度 > 100）——
提示 spec 作者可能在用正则把判断硬编码进 spec。**这是提示，不是阻断。**

## 与 SKILL.md §三 三分判据的对齐

| 判据 | 如何满足 |
|---|---|
| ① 机制不执行任务本身 | 不写 prompt、不判 verdict、不裁决分歧 |
| ② 不收窄编排空间 | spec 由 orchestrator 撰写；`pause` 可在任意点交回控制权 |
| ③ 契约违反 fail-closed，判断分歧 fail-open | 未匹配路由 / hook 断言失败 / 产物缺失 / 续跑不确定 → 停机；需要判断处 → `pause` 交回 agent |
