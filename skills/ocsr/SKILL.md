---
name: ocsr
description: Use when host subagents cannot provide cross-vendor models, cheap parallel workers, or fresh-context adversarial reviewers. Drives headless `opencode run` as a framework-independent subagent backend. Covers residual-context prompt design, file-based recovery, hallucination checks, Windows encoding, model-lineage selection, and default roles (DeepSeek executes, MiMo judges). "ocsr" is the PRIMARY trigger (any mention means use this skill); also trigger on "opencode run 子代理", "opencode 驱动多代理", "异构 reviewer", "多模型并行评审", or "spawn subagents via opencode". NOT for simple tasks whose prompt cost exceeds the work, or tasks needing shared conversational context.
---

# ocsr（OpenCode Subagents Run）— 以 headless `opencode run` 驱动多个子代理

> 把 `opencode run` 当作**独立于宿主框架的子代理执行后端**：任何能跑 shell 命令的 agent（Claude Code、Codex CLI、opencode 自身、脚本）都可以用它派发全新上下文、可指定任意 provider 模型的子代理。
> 区别于框架内建的 task / Agent 子代理工具——那些通常继承主模型、不能跨厂商换模型。

---

## 一、何时用 / 何时不用

**用**（满足任一）：

| 场景 | 原因 |
|------|------|
| 需要真异构模型（不同厂商 lineage） | 框架内建子代理工具通常锁定模型：opencode 的 `task` 无 per-spawn model 参数；Claude Code 的 `Agent` 只能在同家族档位间选。跨厂商必须 `opencode run -m` |
| 批量机械任务要廉价并行 worker | 主对话模型贵；`opencode run -m <便宜模型>` 扇出成本低一个量级 |
| 需要 fresh-context 对抗评审 | `opencode run` 天然无对话历史继承——评审者不被主对话结论污染，这是特性不是缺陷 |
| 宿主框架没有子代理功能 | 有 shell 就能用 |

**价值前提**：上表"廉价并行"的省 token 收益以**派发链路已在本机验证、救火风险低**为前提。首次在新 harness 上使用时，链路调试（通道选择、超时适配、停滞看门狗）有一次性成本，此时真实卖点是**跨族多样性 + fresh-context 独立性**，不是净省 token（详见 §三/§五）；判断密集场景（模式 B/C）即便链路成熟，核心价值仍是异构视角与上下文隔离，而非计费节省。

**不用**：

- 单次简单任务——自己直接做，派发的 prompt 工程成本高于任务本身
- 需要与主对话共享上下文、频繁双向交互的工作——`opencode run` 是一次性投递，不适合密集对话协作
- 判断密集且不可验证的关键决策——子代理产出必须可事后验证（见 §五），纯"帮我拍板"型任务（如"哪个方案更划算"这类无客观验收标准的判断）留在主对话

**通道选择判据**（OCSR vs 框架原生子代理）：按两条通用原则判断（假设已通过「不用」判定需要派发）——(a) **角色价值来源**：评审默认走 OCSR（跨 family 规避同族盲区）；确定性修复（规格逐字、验收客观）与通道无关，含判断成分或与原作者同族时跨族 executor 有规避共同盲区的价值。**「几行的文字修复用原生 executor；批量转换、长收尾、成本敏感场景用 OCSR（模式 A/D）」**。

**治理上下文修正**（2026-08-04 原生 spawn 漏 gate 实证）：当派发属于 converge 等预算治理流程时，上述成本判据让位于一条硬规则——**凡不能自动过预算门的通道，使用前必须显式过门**。adapter/驱动器通道已自动 gate；原生通道在 auditable-only 宿主上无自动 gate，小任务也必须手动 `reserve`/`settle`，否则属降级，须按 converge 规则以 `orchestrator_self` 标注且禁止回填。通道成本再低，也不抵一条断裂的审计链。

---

## 二、核心心智模型：上下文残差

子代理是**全新上下文**：没有你的对话历史、没有和用户形成的隐含约定、知识截止日期可能早于当前事实。

**驱动成败 = prompt 是否把"残差信息"给足。**
残差 = 顶层对话中隐含存在、但子代理上下文中缺失的那部分约束与事实。顶层 agent 觉得"显然"的东西（术语的正确写法、禁改的文件、输出该放哪、什么算完成），子代理一概不知道，而且会**自信地用错误的先验补齐**。

实证过的两类典型失败（写 prompt 前先想它们）：

1. **报告幻觉**：子代理生成"10/10 成功"的漂亮报告，实际未调用 Write 工具，文件系统 0 产物。子代理的自我报告不可信，只有文件系统证据可信。
2. **术语反向矫正**：子代理知识截止早于某产品发布，把正确的新版本号"矫正"成它认识的旧版本号。不声明知识截止 + 术语对照表，它就会帮倒忙。

---

## 三、调用范式

### 派发驱动器（推荐）

将"模板生成→launcher→脱管启动→双监视看门狗→产物验证→遥测"整链固化为单文件 CLI。默认派发路径：

```bash
python scripts/ocsr_dispatch.py dispatch --worker "<prompt>|<model>|<label>" --output-dir <dir> --output-pattern <name> --watch
```

**错峰**（多 worker 默认间隔 5s 启动）/**看门狗**（硬阈值到期，默认按角色自动解析——orchestrator/planner/commander 类角色报告/alive 保留进程，worker 类角色 kill 进程）/**产物快照比对**（派发前后目录快照 + 覆盖检测）/**遥测**（`~/.ocsr/dispatch-log.jsonl`）全部内置。

**脚本不做编排判断**——选模型、prompt 残注入、verdict 裁决、重试 prompt 修订仍由 orchestrator 负责。脚本化边界（与 converge 宪法流程先验判据同构）：可脚本化的只有确定性验证与过程监督（错峰、看门狗、产物契约校验[当前仅校验输出路径一致性与存在性]、快照比对、遥测）；预算门控由上层承担（如 converge 的 budget_gate）；不可脚本化的是判断力。三分判据：①机制不执行任务本身；②不收窄 orchestrator 编排空间；③对契约违反 fail-closed，对判断分歧 fail-open。`--meta` 用于遥测归因（task_id/role/plan_ref 等），与派发核心逻辑正交。

当前 skill 包以 `scripts/ocsr_dispatch.py` 为权威实现，宿主侧仅驻留适配层；演进摘要见 [`DEVELOPMENT.md`](DEVELOPMENT.md)。

**多步骤流程用步骤运行器**（`ocsr_dispatch.py run --spec`）：把「步骤序列 + 参数推导 + 路由 + 记账 + 断点续跑」交给脚本，判断仍归 orchestrator——路由键只能来自产物的确定性解析，未预期取值必须 fail-open 到 `pause` 交回 agent。完整 schema 与语义见 [`refs/run-spec.md`](refs/run-spec.md)。**它防搬运错，不防内容事实错**：后者的对治仍是评审。

**账本说明**：skill 版仅在显式传 `--ledger-dir` 时写派发账本（不做 `.meta/converge/` 自动探测）；vault 适配层会自动补全该参数。

### 作为 converge Spawn 后端（治理钩子对接）

当 OCSR 作为 converge 的 Spawn 后端时，事件流（archive Contract v1 的 `begin-invocation`/`complete-invocation`/`recover-invocation`）与预算门控（`budget_gate reserve/settle`）必须端到端接线，否则 `archive_convergence.py archive` 会 fail-closed（`events-missing`）。完整对接设计——converge 仓库侧 `scripts/ocsr_spawn_adapter.py` 五步原子化包装（reserve → begin-invocation → dispatch → complete/recover-invocation → settle）、provenance 诚实降级（configured + cli_argument + backend-does-not-expose）、"edit X" 类任务的 sentinel 模式——见 `refs/converge-integration.md`。适配层定位与 §三"脚本不做编排判断"边界一致：它是 **converge 仓库侧** 的薄包装，**不**向 `ocsr_dispatch.py` 注入 converge 协议；本仓库（ocsr）的 `ocsr_dispatch.py` 保持框架无关，converge 是它的客户之一。

### 基本命令

```powershell
opencode run "你的 prompt" -m deepseek/deepseek-v4-flash   # -m 指定模型 = 跨厂商异构的关键能力
```

长 prompt 写入临时文件再传（避免命令行转义地狱；PowerShell 5.1 的 `Set-Content` 默认 ANSI 会写坏中文，须显式 UTF-8）。完整命令示例（`--format`/`--title`/`--dir`）与 PowerShell/bash prompt-file 处理模板见 `refs/dispatch-patterns.md` §基本命令。

### Windows 中文编码说明

Windows 环境下中文 stdout **可能显示乱码**，但显示乱码不意味文件内容已损坏。影响范围取决于 PowerShell 版本：

| 版本 | 默认输出编码 | `*>` 落盘编码 | 推荐读取中文文件 |
|------|-------------|--------------|-----------------|
| PowerShell 5.1 | 系统 ANSI 代码页（中文 Windows 实测 CP936 / GBK） | UTF-16LE (带 BOM) | `Get-Content -Encoding UTF8` |
| PowerShell 7.x | UTF-8 | UTF-8 (无 BOM) | `Get-Content -Encoding UTF8`（参数通用） |

**（推荐）让子代理用 Write 工具直接把报告写到指定路径**，完全不依赖 stdout 回传。失败诊断以期望产物文件是否落盘为准（§五），不依赖 stdout。重定向策略、输入侧 BOM 陷阱、乱码故障诊断（区分显示问题 vs 文件损坏）见 `refs/dispatch-patterns.md` §Windows 中文编码策略细节。

### 从 agent harness 驱动时的超时与通道选择

单个 `opencode run` 常跑几分钟，超过多数 harness 的 shell 默认超时（如 2 分钟）——要调大的是 **harness 侧 shell 工具的超时参数**（如 Claude Code Bash 工具传 `timeout: 600000` 毫秒），不是 opencode 参数。**优先前台 + 大超时，慎用后台通道**：部分 harness 后台机制会直接终止 opencode 进程（后台失败指纹 = 日志 0 字节 + 无产物文件，详见 §七），遇到时先切前台重试。

### 脱管派发模式（前台超时不够用时）

> **入口条件**：当 `scripts/ocsr_dispatch.py` 可用时优先用驱动器 `dispatch --watch`；仅当驱动器不可用（非本机环境、脚本缺失）时回退本节手写模式。

当 harness 前台 shell 工具的超时上限 **小于** 预计单轮耗时（判断密集角色在 harness 规划阶段的估算单轮常 20–30 分钟——含 prompt 构建、排队长尾、完整收敛往返——而多数 harness 前台上限 ≤10 分钟）时，上文"前台 + 大超时"不可用，后台通道又会被 kill（§七）。此时用**脱管派发**：让 `opencode run` 脱离 harness 任务生命周期独立运行，harness 侧只跑一个纯 shell 观察器等产物。

三步实证模板（launcher 脚本 + `Start-Process` 脱离 harness 生命周期 + 纯 shell 双监视观察器）见 `refs/dispatch-patterns.md` §脱管派发。核心：脱管进程不在 harness 任务生命周期内、不被后台通道终止（与「后台通道派发」的本质区别）；观察器必须**双监视**（产物落盘 **且** opencode 进程存活）——只盯产物会在静默停滞（§五）时无限空等，只盯进程会在 0 产物退出时误判成功；观察器自身不限时，超时由 orchestrator 按 §五 看门狗阈值终止底层 opencode 进程。

### 并行扇出

完整 PowerShell `Start-Job`（有限超时 + 失败/超时作业回收 + 清理）与 bash 后台脚本模板见 `refs/dispatch-patterns.md` §并行扇出。

并发纪律：每个 worker 独立日志与独立产物路径，失败定位靠 §五 的文件验证而非解析 stdout；**先派 1 个试点 worker 走通链路（§五 验证通过），再逐步从 2 个并发上探**；遇 429/超时回退；扇出前向用户报预计调用数上限与所选模型，未经新鲜授权不突破已披露上限，不静默烧钱。

### 多轮续接（版本相关）

`opencode` 有 `--continue` / `--session <id>` / `--fork` 全局会话能力；对 `run` 子命令的支持以本机 `opencode run --help` 实测为准，不作为默认路径。注意：`opencode run --fork` 必须配合 `--continue` 或 `--session`（本机 `opencode run --help` 已确认），且仍是 **CLI 会话 fork**，不是 live 子代理句柄，不能当作上下文继承机制。默认设计成**一次投递、文件回收**，不依赖续接。

---

## 四、自足 prompt 模板（残差注入）

每个子代理 prompt 必须自包含以下**六要素**，缺一项就是给失败留门：

```text
【任务】<一句话目标> + <什么算完成（可验证的验收标准）>

【输入】只读以下文件：<绝对路径列表>
禁止读取上述之外的任何位置（即使技术上可以）。

【输出】把结果写入：<绝对路径>
- 优先使用 Write 工具直接写入
- 若无 Write 工具但在受控 shell 内，可回退到明确的 UTF-8 无 BOM 文件写入方法，并在执行证据中如实记录所用工具类型
- 不要依赖 stdout 回传。未实际写入文件的响应视为执行失败

【格式】<输出文件的 schema / 模板 / 示例>

【边界与禁区】
- 禁止修改输入文件；禁止写 <输出路径> 之外的位置
- 对无法确定的术语、版本号、专有名词：保留原文并标记 [UNCERTAIN]，
  禁止基于"已知信息"猜测——你的知识截止日期可能导致猜测方向错误
- 你的知识截止可能早于今天（<当前日期>），此后发布的产品/版本可能不被你知晓
- 以下术语已由任务派发方确认，不得"矫正"：<术语对照表>（涉及术语处理时必填）

【执行证据】返回中必须包含：产出文件完整路径列表 + 每个文件的字节大小 + 工具调用情况。
缺少上述证据的执行报告视为不完整。
```

填充说明：`<当前日期>` 填**今天**——作为风险锚点，提醒子代理其训练截止可能早于当前日期。不宣称这是模型的真实知识截止，只作为防止反向矫正的安全余量。

**残差约束四件套**与模板的映射：①文件交付约束→【输出】+【执行证据】；②术语对照表+知识截止声明→【边界与禁区】3、4 条；③不确定性标记→【边界与禁区】2 条；④执行证据要求→【执行证据】。批量派发时四件套原样注入每个 worker，不因任务简单而省略。

---

## 五、产物回收与验证（不可跳过）

orchestrator（= 发起 `opencode run` 的顶层 agent，也就是读到这里的你）收到子代理"完成"信号后，**逐项做确定性验证**，不采信自我报告：

- [ ] 期望产物清单中每个文件**存在**且**非 0 字节**
- [ ] 数量与期望一致（缺失 > 0 = 命中报告幻觉，重派该任务）
- [ ] **抽样打开**至少 1–2 个文件核对内容与格式（不只看存在性；核对标准 = 你在【任务】中定义的验收标准）
- [ ] 涉及术语矫正的：抽查关键术语未被反向矫正
- [ ] 评审类产物：检查报告是否引用了不该访问的路径（见 §七 `--dir` 陷阱）——**评审报告的执行证据必须含结构化 `reads:` 列表（实际读取的文件路径），优先用驱动器 `--forbid-paths` 在落盘后机械审计（clean / violated / unavailable），或人工对照禁止清单**；引用了不该访问的路径则该 verdict 作废、换子代理重评——除非能可核验地证明实质独立性保留（findings 与被读报告零重叠、且独立否定过被读报告的错误），此时可降级保留并必须如实标注（2026-08-04 R2 事件实证；best-effort 边界的另一处声明在 §七「安全边界」段，修改时两处需同步）

验证失败的处理：重派，并把失败原因编码进新 prompt 的【边界与禁区】作为额外残差（例："上次执行声称完成但未产出任何文件——本次必须实际写入文件，并在回复中列出文件路径与字节大小"）；不要自己脑补修复子代理的产出。

> **路径审计判断准则**：§五「检查报告是否引用了不该访问的路径」中，「不该访问」= 提前获取答案的（如提前看到产物/reviewer verdict）、作弊性质的（借用未来轮次信息、绕过任务边界）；「不算不该访问」= 合法的、面向任务的、非作弊性的信息收集（读必要前置文件/规范/先例）。完整裁定原文见 `refs/failure-modes.md` §路径审计判断准则，与 §七 越界写入覆盖防护、§七 `--dir` 风险提示配合。

**具名失败模式**（除"报告幻觉"外，派发链路层另有三类需主动裁决）：

- **报告幻觉**：子代理自称完成但 0 产物（§二）——按上方清单确定性验证即可识别。
- **静默停滞**：`opencode` 进程存活但**输出日志 0 字节 + 无子进程派生 + 超过看门狗阈值**。这是模型/API 端停滞，不是 prompt 问题，也不是后台 kill（kill 的指纹是进程已被终止）。**三条判据缺一不可**——尤其「无子进程派生」：层级指挥下的 orchestrator 会派发自己的 worker 并等待，此时它的日志静止是**正常等待**而非停滞。误判实证见下方「终止前置检查」。
- **越界写入 / 路径碰撞**：`opencode` **进程正常退出（exit=0）、期望产物未落盘、而写入照常发生在别处**——子代理自行选择了文件名，可能覆盖同目录内其他 agent 的产物并使其永久丢失。指纹与前两类都不同：进程已退出（非停滞）、且确实产出了内容（非报告幻觉），只是落在了错误路径。根因（`--output-pattern` 只告诉看门狗等哪个文件，**不约束子代理的写入路径**）、对治（① prompt【输出】节写死唯一绝对路径 + 说明覆盖后果；② 派发前备份 `--output-dir`；③ 调用方驱动器派发前后目录快照比对，检出即报错退出）见 `refs/failure-modes.md` §越界写入。

**看门狗**（禁止无阈值人工轮询等待）：对每个脱管/后台派发的进程设硬阈值 `max(10 分钟, 1.5 × 该模型该角色本机实测单轮耗时)`；无实测数据时默认 **15 分钟**（基于本地已跑通链路的实测样本分布 1.5–7.9 分钟——此为同机单轮耗时，不含规划阶段估算的 prompt 构建、跨 harness 收敛往返，与上方"20–30 分钟"估算属于不同口径，不可混用）；**单一大规模长任务（如整文件重写、多阶段编排）建议 60 分钟**——误杀收尾中的 worker（产物已落盘但被判失败）比多等更贵。看门狗超时行为默认按角色自动解析（`--timeout-policy auto`）：orchestrator/planner/commander 类角色报告/alive 保留进程（供 commander 裁决），worker 类角色 kill 进程。显式指定 `--timeout-policy leaf_kill` 或 `hierarchical_report` 时使用指定行为。阈值到期 → 按解析策略执行（进程/kill 或 report/alive）→ 如实记录（converge 场景下记 settle failed）→ 进入下方失败切换阶梯。**不允许**靠人工反复 `tasklist` 轮询"再等等"——那是在用主上下文 token 补贴一次本应止损的失败。

### 终止前置检查（硬纪律，四条全过方可 kill）

阈值是**授权终止的下限**，不是"感觉卡了就动手"的许可。**手动终止 opencode 进程前，四条必须逐条验证并记录**：

1. **已达自己设定的阈值**：实际等待时长 ≥ 派发时 `--timeout` 的值。**未达阈值一律不得手动 kill**——
   驱动器的看门狗会在到期时自行终止，抢在它前面等于废掉该机制。
2. **无子进程派生**：`tasklist`/`Get-Process` 确认没有该进程派生的子 opencode。
   **层级指挥下 orchestrator 等待自己的 worker 时，日志必然静止**——此时 kill 属误杀。
3. **已读日志尾部并确认其非"正在执行中"**：若日志末尾显示它正要/正在做某个操作
   （如"re-dispatch the acceptance reviewer"），那是**在工作**，不是停滞。
4. **已评估中断的副作用**：目标进程是否正处于**有还原步骤的操作**中间态？典型高危：
   **变异测试**（破坏实现 → 验证 → 还原三步，中途 kill 必留破坏残留）、
   文件批量改写、git 操作。此类中断后**必须**立即做残留扫描。

**终止方式**：优先只杀目标进程（按 PID），**禁止无差别 `taskkill /F /IM opencode.exe`**——
它会连带杀死正在正常工作的兄弟 worker。

**中断后强制残留扫描**（无论是否预期有副作用）：

```bash
git status --porcelain          # 含未跟踪：变异备份、临时文件
grep -rn "MUTATION\|XXX-TEMP" <被改动的源码路径>
<项目自有测试命令>              # 必须实跑，不得只读报告
```

> **实证（2026-07-26）**：orchestrator（glm-5.2）被派发时 `--timeout 40`，在第 25 分钟因
> "日志 20 分钟无增长"被手动 kill。事后查明：它当时**正在派发 acceptance-reviewer 并等待**
> （判据 2、3 均可证伪停滞判断），且被杀的 reviewer 正处于**变异测试中间态**——
> 还原步骤被打断，源码残留 `changed = True  # MUTATION` 与一个 `.mutation-bak` 文件。
> 该残留仅被"终验复跑测试"（2 failed）抓到；若终验只读报告，被变异过的实现会带着
> `# MUTATION` 注释进入提交。四条前置检查中，当时只验了一条（进程存活 + 日志静止）。

硬停止条件：每个 worker 最多 **3 次总尝试**（首次 + 2 次重派）。达到上限后停止重派，保留全部失败日志和产物证据，交回用户选择——换模型、缩小任务、提高预算或终止。有副作用的 worker 必须在 prompt 中声明幂等性；无法保证幂等性的副作用任务禁止自动重派，失败即止（即不进入下方失败切换阶梯的自动重派）。

失败切换阶梯（3 次总尝试上限含在内，幂等性约束逐字保留）：

1. 第 1 次失败 → **同模型重派一次**（排除偶发 API 抖动）。
2. 第 2 次失败 → 第 3 次尝试**切换 family**（判断密集角色在 `xiaomi/mimo-v2.5-pro` 与 `xiaomi/mimo-v2.5` 之间切换——后者通过 preflight 验证可用后方可作为切换目标，family 以 `opencode models --verbose` 为准）；切换时把前两次失败原因写进新 prompt 的【边界与禁区】残差。
3. 第 3 次失败 → 既有硬停止，交回用户。

**通道例外**：失败明确归因于**通道**（如后台 kill 指纹 = 日志 0 字节 + 无产物 + 进程被终止）时，**修通道不换模型**——通道失败换模型无意义。该通道修复次**不计入**"同模型重试名额"判断，但**计入总尝试 3 次上限**（总上限兜底，防止借"通道问题"无限重试）。

---

## 六、四种驱动模式

### 模式 A · 批量执行型（fan-out workers）

适合：文件批处理、格式转换、逐篇摘要/矫正等机械任务。

1. **拆解**：任务拆成单个子代理 2–5 分钟能完成的小步骤（粒度经验值）；每个 worker 独立输出文件/目录，互不覆写
2. **派发**：每个 worker 一份自足 prompt（§四），模型用执行档（§八）
3. **回收**：按 §五 逐 worker 验证，失败者重派
4. **汇总**：跨 worker 合并与一致性检查由 orchestrator 负责——worker 之间互相不可见

### 模式 B · 对抗评审型（fresh reviewer）

适合：对计划、文档、代码产物做独立审查，规避"作者自审盲区"。

1. 把**产物 + 必要 grounding 材料**（相关规范、上游依赖）复制到一个干净临时目录——**信息最小化**：不给评审者看草稿过程、前轮意见、orchestrator 的倾向性结论。**布局硬规则**：staging 目录与收敛工作目录（`.converge/active/**`、round 输出目录）不得共享可读父树；确实无法分离时，评审 prompt 必须显式列出禁止路径——优先用 `ocsr_dispatch.py dispatch --forbid-paths <路径>` 自动注入禁令块（并要求报告含 `reads:` 列表，落盘后机械审计）。实证：2026-08-04 ultraverge R2 读取并行 reviewer 报告的锚定污染事件。当前为手动规则——自动脚手架在观察到足够频率的锚定事件后激活（设计决策 #5：无运行时失败证据前不新增 wrapper）。
2. reviewer prompt 要点：只基于给定材料；输出结构化 verdict（如 通过 / 修订后通过 / 需重新设计）+ 按严重度排列的问题清单；报告 Write 到指定路径
3. 修复后再评审时**换一个全新子代理**——每次 `opencode run` 天然就是新会话/新上下文，无需额外操作；目的是避免同一评审者验收自己上轮的意见
4. 本 SKILL 只提供执行后端；完整的多轮 reviewer↔executor 对抗收敛机制（收敛判定、振荡检测、预算门控）若本机装有 converge 类 skill，以其为准，`opencode run` 作为其 Spawn 实现

### 模式 C · 异构评议型（multi-lineage）

适合：重大决策/治理类产物，需要不同模型家族的独立视角规避同族集体盲区。

1. 启动前跑 `opencode models --verbose`，按 `family` 字段分组（`family` 缺失时按 `providerID` 回退）；`family` 相同 = 同一 lineage。注意 `family` 可能比「厂商」更细：`deepseek/deepseek-v4-flash` 的 family 是 `deepseek-flash`，与 v4-pro（thinking 系）不同组——lineage 判定一律以 `family` 值为准，不凭厂商名推断（opencode 1.17.x 实测，每模型输出一段 JSON）
2. 选择规则：
   - `cost.input == 0 && cost.output == 0` 只表明**价格元数据可能缺失或免费**，不能单独证明模型弱；以此排除模型时标记为启发式裁决。判断密集角色默认要求：context ≥ 32K、toolcall 能力满足任务、模型当前 active，并有本机试点或历史质量证据——三者缺一且无替代证据时排除
   - 排除与 orchestrator 自身相同 family 的模型（先经宿主框架的模型信息确认自身身份；确认不了就跳过本排除项，只保证所选 reviewer 之间跨 family）
   - **同一轮的多条评议必须来自不同 family**；跨轮尽量错开
   - 可用 lineage 不足 2 个 → 如实向用户报告并降级为单 lineage 评议，不伪装异构
3. 每条评议独立派发、独立写报告文件；orchestrator 汇总共识点与分歧点，分歧点交用户裁决

### 模式 D · 释放/收口执行型（release / closeout executor）

适合：功能实现或收敛循环产出可发布终态后的文档、归档、清理、staging 收尾——把这类默认可结构化的收尾工作从昂贵主 orchestrator 转移到便宜执行档模型（对应上层薄编排/converge 类 plan 中的 "release executor" 角色）。

**规模边界**：模式 D 面向批量、长收尾、成本敏感的收口工作；单文件/几行的小规模任务（含小修复、单行 CHANGELOG 更新）不走模式 D——用框架原生 executor（判据见 §一「通道选择判据」，完整判据在 `refs/dispatch-patterns.md`）。

完整输入合同（§四自足 prompt 模板六要素之上，release executor 额外需要的残差：最终 diff、测试摘要、任务合同、归档状态、`allowed_paths`）、默认职责清单、JSON manifest 输出 schema（`status` / `allowed_paths` / `doc_updates` / `retrospective_path` / `digit_consistency_check` / `cleanup_manifest` / `staging_manifest` / `archive_precheck` / `protected_paths_reported` / `unclean_items_reported`）、与上层驱动器的分工（manifest 的 JSON schema 与四项最少机械检查由调用方 fail closed；release executor 只生成 manifest 不自行执行删除；Archive Contract 预检只读）、保护默认（凭据文件与非本任务临时/收敛目录）、模型选择——见 `refs/release-executor.md`。

---

## 七、陷阱清单

> 环境基线：opencode 1.18.5 · Windows 10 · pwsh 7+ / PowerShell 5.1 / Git Bash。版本升级后关键行为需复验（推荐调用方派发脚本的 selftest）。

**安全边界**（best-effort，不构成安全隔离）：`--dir` 只设工作目录提示、prompt 禁令、事后路径审计都是 best-effort 机制——能降低误访问风险，**不能阻止**恶意模型静默越界或外发数据。敏感/恶意/含 prompt injection 风险的材料不可交给不可信模型，须由宿主提供低权限账户、文件系统沙箱、出站网络限制。

**派发链路陷阱**（指纹 → 对策）：

- **harness 前台超时 < 单轮耗时**（判断密集角色在规划阶段估算单轮 20–30 min——含 prompt 构建、排队长尾、收敛往返，有别于同机单轮实测样本 1.5–7.9 min——多数 harness 前台上限 ≤10 min）→ §三 脱管派发模式（launcher + Start-Process + 双监视观察器）。
- **模型端静默停滞**（进程存活 + 日志 0 字节 + 无子进程 + 超看门狗阈值）→ §五 看门狗硬阈值终止，禁止无阈值人工轮询，按失败切换阶梯重派。
- **误杀正在工作的 orchestrator/worker**（未达阈值即手动 kill、或 `taskkill /IM` 无差别杀伤）→ §五「终止前置检查」四条全过方可 kill；层级指挥下 orchestrator 等待子 worker 时日志静止属正常；中断变异测试等有还原步骤的操作后**必须**残留扫描（`git status --porcelain` + `grep MUTATION` + 实跑测试）。
- **后台通道 kill**（日志 0 字节 + 无产物 + 进程被终止）→ 修通道不换模型（§五 通道例外）。
- **越界写入覆盖既有产物**（exit=0 + 期望产物缺失 + 同目录既有文件变化）→ prompt【输出】写死唯一绝对路径 + 备份 + 驱动器快照比对（详见 `refs/failure-modes.md` §越界写入）。
- **嵌套派发失账**（下层自发 `opencode run` 不经上层预算 gate）→ OCSR 作为 converge Spawn 后端时，由对接层驱动器向 converge active 目录自动追加账本（详见 `refs/converge-integration.md`）。
- **并发 DB 锁**（`database is locked`、实例秒退 exit=1、log <100B）→ 多 worker 错峰间隔 ≥5s；失败含 "database is locked" 时延迟 30s 重试 1 次（通道例外）。
- **Launcher 路径转义**（反斜杠经多层转义被误解析，launcher 秒退无日志）→ 驱动器已内置路径生成；手写模式用正斜杠路径 + try/catch。
- **静默烧钱 + 无界重派** → 派发前报数量上限与模型，每 worker 最多 3 次尝试，未经新鲜授权不突破。

其余陷阱（`--dir` 不是沙盒、框架内建子代理换不了厂商、`--fork` 不是子代理、stdout 中文乱码、自我报告不可信、`cost=0` 启发式[价格元数据可能缺失/不能单独证明弱]、harness 超时截断）见 `refs/pitfalls-reference.md`。

---

## 八、模型选择（默认池与分工）

OCSR 可选模型严格限于以下三个 qualified ID（未在列表中的模型——包括 `xiaomi/mimo-v2.5-pro-ultraspeed`——不可选）：

- `deepseek/deepseek-v4-flash`
- `xiaomi/mimo-v2.5`
- `xiaomi/mimo-v2.5-pro`

执行 worker 默认 `deepseek/deepseek-v4-flash`；判断密集角色默认 `xiaomi/mimo-v2.5-pro`（轻量判断可用 `xiaomi/mimo-v2.5`）。完整角色→模型→cost 对照表见 `refs/model-defaults.md`。

规则：

1. **先查本机派发遥测**（见下方 dispatch-log）：某模型在某角色上连续 ≥2 次 `stall`/`error` 时，本次优先换 family；本机实测数据优先于默认池（refs/model-defaults.md）
2. 每次启动前用 `opencode models --verbose` 确认目标模型在本机可用——模型池随配置演化，默认池是默认值不是硬编码；从模型块标题行原样复制 qualified ID，禁止凭块内 `id`、`providerID`、`name` 或裸名拼接 `-m`。首次使用某模型或链路需验证时，运行 `python scripts/ocsr_dispatch.py preflight --model <qualified ID>`（该子命令消耗一次真实模型调用，见 `--help`）
3. 对抗评审（模式 B）时让 reviewer 与产物作者尽量不同 lineage，双模型分工天然满足这一点
4. 异构评议（模式 C）的 lineage 选择规则优先于默认池
5. 提供 provider 前缀完整 ID（如 `deepseek/deepseek-v4-flash`），不写裸模型名

**派发遥测**（dispatch-log）：每次 `opencode run` 结束（含失败）后，向本机 `~/.ocsr/dispatch-log.jsonl` 追加一行（机器本地数据，不入库、不跨机污染）。追加行的 PowerShell 字段模板见 `refs/dispatch-patterns.md` §派发遥测记录片段。

**channel 字段**：填英文枚举值 `foreground` / `detached` / `background`，其中 `detached` 即 §三 脱管派发模式。

**翻转默认的门槛**：把某个并列共同默认翻转为**唯一主默认**，需 **≥5 次本机实测**支持（防 n=1 拍脑袋）；不足 5 次时维持并列。

---

## 九、最小可行流程（TL;DR）

1. 判定：这个任务值得派发吗？（§一）
2. 拆解任务 → 每个子代理一份自足 prompt，用六要素模板（残差四件套已内嵌其中，§四）
3. 运行 `opencode models --verbose`，从模型块标题行原样复制 qualified ID（§八规则 2），禁止凭块内 `id`、`providerID`、`name` 或裸名拼接 `-m`；然后用该 qualified ID 选模型：执行用 `deepseek/deepseek-v4-flash`、判断用 `xiaomi/mimo-v2.5-pro`（轻量判断可用 `xiaomi/mimo-v2.5`）。OCSR 可选模型仅限三个（见 §八），不在列表中的模型不可选。首次使用某模型的链路验证用 `preflight` 子命令（消耗真实模型调用）探查可用性；异构评议按 §六C 规则选择模型
4. 派发：prompt 写临时文件（UTF-8），`opencode run (Get-Content … -Raw -Encoding UTF8) -m <model>`；harness 前台超时够用就前台 + 调大超时，不够用就走 §三 脱管派发模式；产物优先 Write 直写文件
5. 回收：文件存在性 + 大小 + 抽样内容，逐项确定性验证，不信自我报告（§五）；脱管/后台派发的进程挂看门狗（§五），禁止无阈值人工轮询
6. 重派与上限：失败按 §五 失败切换阶梯（同模型→换 family→硬停），每 worker 最多 **3 次总尝试**；达到上限后停止，交回用户选择；有副作用的 worker 须声明幂等性，否则禁止自动重派
7. 汇总：跨子代理一致性由 orchestrator 负责
8. 可信指标：dispatch-log（`~/.ocsr/dispatch-log.jsonl`）含 task-session 元数据（prompt/response 字节、cost 估算、blocking 链）；`python scripts/ocsr_dispatch.py summary` 按角色/scope 聚合 spawn 数、成功率、wall_min、cost_estimate（新字段向后兼容）

---

## 十、层级指挥模式（orchestrator 无头运行）

> **触发词**：「层级指挥 `<任务>`」/「用层级指挥推进 `<任务>`」= 启动本节全套流程（plan → 无头 orchestrator + monitor → 分层执行 → 验收环 → planner 终验）；与单次派发「OCSR 派 `<任务>`」区分——后者只派 worker，不启编排机制。
> planner（顶层规划模型）/ orchestrator（pro 模型）/ worker（flash 执行档）三层是默认形态而非定数——小任务可塌缩（orchestrator 直接带 worker），超大任务可长出第四层；可嵌套 converge 循环于其中。与 converge「层级收敛」（planner→多个 orchestrator 并行子收敛）区分。

完整协议——detached 派发、`_orchestrator-state.md` / `_phase-report.md` state 文件最小 schema（含六列交付物归属表与 spawn budget 计数器）、`ocsr_dispatch.py monitor` 配套、路径 B 续接协议、resume 任务卡骨架、commander 裁决点操作规程（换模型/改 brief/终止）、验收环（非 executor 族 acceptance-reviewer 执行确定性验收命令）、调研二分、跨 Phase 接口契约、`verify-ownership`（归属遥测机械校验，`python scripts/ocsr_dispatch.py verify-ownership`）——见 `refs/hierarchical-command.md`。

---

## 拆分文件索引

主文件只保留高频规则与地图（何时用/不用、核心心智模型、驱动器用法、TL;DR、陷阱清单、模型默认池）；详细参考下沉到 `refs/`，按需加载，机制语义不变：

| 需求 | 文件 |
|------|------|
| 层级指挥模式完整协议（detached 派发 / state schema / monitor / resume 续接 / commander 裁决 / 验收环 / 调研二分 / 跨 Phase 契约 / verify-ownership） | [`refs/hierarchical-command.md`](refs/hierarchical-command.md) |
| OCSR 作为 converge Spawn 后端的治理钩子对接（五步原子化适配层 / provenance 诚实降级 / sentinel 模式） | [`refs/converge-integration.md`](refs/converge-integration.md) |
| 可复制派发模板（脱管派发三步 / 并行扇出脚本 / 多轮续接）+ 通道选择判据完整文本 | [`refs/dispatch-patterns.md`](refs/dispatch-patterns.md) |
| 模式 D release executor（输入合同 / 默认职责 / JSON manifest schema / 与上层驱动器分工 / 保护默认） | [`refs/release-executor.md`](refs/release-executor.md) |
| 具名失败模式细则（路径审计判断准则 / 越界写入根因·对治） | [`refs/failure-modes.md`](refs/failure-modes.md) |
| 模型默认池与角色→模型→cost 对照（数据层，随模型换代更新） | [`refs/model-defaults.md`](refs/model-defaults.md) |
| 完整 14 行陷阱清单表（事实/对策原文） | [`refs/pitfalls-reference.md`](refs/pitfalls-reference.md) |
| 步骤运行器 `run --spec` 完整 schema（步骤类型 / 取值器 / 路由 / 模板文法 / journal 与续跑 / workdir 独占） | [`refs/run-spec.md`](refs/run-spec.md) |

> `ocsr_dispatch.py` / `verify-ownership` 等脚本子命令的权威实现在 `scripts/ocsr_dispatch.py`；本表只指向文档参考。
