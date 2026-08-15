# 层级指挥模式（orchestrator 无头运行）— 详细参考

> 本文件从 SKILL.md §十 下沉。主文件仅保留触发词与入口指针；本文件承载完整协议：detached 派发、state 文件 schema、monitor 配套、路径 B 续接、commander 裁决点、验收环、调研二分、跨 Phase 接口契约、verify-ownership。机制语义与原 §十 完全一致。

> **层级指挥**：planner（顶层规划模型）探索全貌、写 plan、终验签字；orchestrator（pro 模型）循环控制 + verdict 裁决 + 任务卡；worker（flash 执行档）承接全部写作类交付物。三层是默认形态而非定数——小任务可塌缩（orchestrator 直接带 worker），超大任务可长出第四层。
> **commander**（操作裁决者）与 planner 是同一角色在层级指挥不同阶段的两个职责面：planner 负责写 plan 与终验；同一角色在中断/预算续接场景下以 commander 身份做操作裁决（换模型、改 brief、终止），不改变层数计数。`refs/hierarchical-command.md` §commander 裁决点操作规程中的"commander"即此含义。
> **触发词**：「层级指挥 `<任务>`」/「用层级指挥推进 `<任务>`」= 启动本节全套流程（plan → 无头 orchestrator + monitor → 分层执行 → 验收环 → planner 终验）；与单次派发「OCSR 派 `<任务>`」区分——后者只派 worker，不启编排机制。
> **与 converge「层级收敛」区分**：层级收敛是 planner→多个 orchestrator 并行子收敛（同一 converge 循环内分解）；层级指挥是任务级的角色分层（planner/orchestrator/worker 各守其职），可嵌套 converge 循环于其中。

### detached 派发

编排者由上级（planner 模型）经 ocsr_dispatch 无头派发（--watch 或长看门狗）。巡航监控用 `ocsr_dispatch.py monitor`（脚本层，框架无关）：持续模式盯 active 目录新鲜度 + 进程存活；`--once` 单次检查可挂任何外部调度器（如 Windows 计划任务 / 任意框架的定时器）——监控职责不依赖特定宿主。orchestrator 自身不维持会话上下文，一切状态写入 _orchestrator-state.md / _phase-report.md。

代码类 executor 的任务卡验收须包含**真实数据源自测**：不只跑夹具测试，还要用真实输入实跑目标命令并记录退出码与输出摘要。验收项写入任务卡【验收标准】节，作为 executor 交付的必要条件。

### state 文件最小 schema

#### _orchestrator-state.md

必需字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_phase` | string | 当前 Phase 编号，如 `Phase 1` |
| `started` | ISO 8601 | Phase 启动时间戳 |

交付物归属表（6 列）：

| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| Phase 1 | 评审报告 | rounds/round-1.md | xiaomi/mimo-v2.5-pro | spawned:r1-reviewer | done |

`Spawn Label` 取值 `spawned:<label>`（对应 ledger 中的派发标签）或 `self-written`（orchestrator 自行写入）。

`Status` 取值 `done`、`failed` 或 `done (attempts=N, 原因摘要)`。记录 attempts 计数与失败原因摘要，如 `done (attempts=2, 首次路径错误)`。

spawn budget 计数器：

| 字段 | 说明 |
|------|------|
| `spawn_budget.total` | 本 Phase 总预算 spawn 次数 |
| `spawn_budget.used` | 已使用次数 |
| `spawn_budget.remaining` | 剩余次数 |

#### _phase-report.md

按 Phase 追加记录，每段格式：

```markdown
## Phase N

- started: <ISO 8601>
- completed: <ISO 8601>
- spawn_budget: <used>/<total>
- deliverables:
  - <path> → <status> (spawned:<label>)
```

### monitor 配套

`ocsr_dispatch.py monitor` 持续检测目录活性 + 进程存活：

```bash
python scripts/ocsr_dispatch.py monitor --process-name opencode.exe --watch-dir <output-dir> --stall-minutes 15 --once
```

`--once` 退出码：0=正常, 1=停滞/进程死亡/目录不可访问/空目录（合并语义，查 stderr 详情区分）。适合集成到外部调度器。

### 路径 B 续接协议

中断后 commander 发起 fresh orchestrator + resume 任务卡：

1. commander 读取 _orchestrator-state.md / _phase-report.md 确定当前 Phase
2. 换 family 派 fresh orchestrator（深度思考档 → 深度思考档不换族则规避无效重试）
3. resume 任务卡骨架：指定 target-phase=N、复述 remaining deliverables、引用状态文件
4. orchestrator 续接时先读 state → 确认从哪开始 → 继续

### resume 任务卡骨架

下列（a）–（d）项由 commander 根据 _orchestrator-state.md / _phase-report.md / dispatch-log 填充，纳入 prompt 【边界与禁区】残差（§四）：

```text
你是 orchestrator（续接模式）。读取 _orchestrator-state.md 和 _phase-report.md。
以下状态信息由 commander 根据 state/phase-report 文件填充：

(a) 前 orchestrator 模型/family：{model_family}
(b) 已完成交付物路径：
    - <path> — <status>
    - ...
(c) 中断原因（若已知）：{reason}
(d) 剩余 spawn 预算：{remaining}/{total}

当前停留在 Phase N-1 完成处。从 Phase N 继续执行。
剩余交付物：[列表]
执行原 plan 的 Phase N 到 Phase 4，投递所有剩余任务卡。
```

### commander 裁决点操作规程

- **换模型（换 family / 升档）**：
  1. 查 `opencode models --verbose` → 按 family 分组 → 排除当前 family → 选替代模型
  2. 评估失败模式是否模型相关：同错误重复出现 → 模型能力不足；间歇性 → API 抖动
- **改 brief**：
  1. 失败指纹判定：若两轮同模型失败且错误不同 → **brief 缺陷**（任务卡残差不充分）；若错误相同 → **模型能力不足**
  2. 修订 brief：补充缺失路径/禁用清单/术语表后重派
- **终止**：
  - 区分两个层面：**每 worker 3 次总尝试（§五 硬停止条件）** vs **orchestrator 级别整体预算（如 converge 的 budget_gate）**
  - 达到上限 / 预算耗尽 / 方向性设计需用户拍板 → 终止并上报

### 验收环

orchestrator 各 Phase 完成后、向 planner 汇报前，必须派**非 executor 族** acceptance-reviewer（按 `opencode models --verbose` 的 family 字段，acceptance-reviewer 与当次执行 executor 不同 family 即可），任务是执行确定性验收命令（pytest / CLI / 真实数据源），不是读报告写意见：

- 修复循环由 orchestrator 管理（converge 原生 inner loop 语义）；反复失败直至重试上限、或 verdict=需重新设计时，才升级 planner 介入。acceptance-reviewer 自身按 §五 计为独立 worker（3 次总尝试）；"反复失败"指 orchestrator 管理的修复循环轮次耗尽，不是 reviewer 单次失败
- planner 终验 = 证据链核验：复跑核心测试 + 审查 verdict 链 + 机械校验（如 verify-ownership） + 抽查关键产物内容一致性，不是全量复审。verify-ownership 是验收环中「机械校验」的具体工具实例（归属三查），acceptance-reviewer 可直接调用

**设计规则——机制兜底优先**：能用防呆机制机械兜底的问题（退出码、schema、归属校验）由机制兜底，让模型精力聚焦执行；机制兜不住的（幻觉、语义偏差）由独立审计（非族 reviewer）兜底。

### 调研二分

orchestrator 必须亲自读关键一手材料——verdict 裁决是第一手判断，输入验证是它的本职；判定规则：verdict 裁决所依赖的源码/配置/状态文件为关键一手材料（必亲自读），纯信息收集型读取可委托。输入 token 便宜的侦查、审阅类读取不做委托。调研报告撰写可委托 executor（输出 token 下沉）。不得以"读了摘要"替代对关键源码的亲自阅读。

### 跨 Phase 接口契约

跨 Phase 的数据契约（state 文件格式、ledger 格式、退出码约定）由 planner 在 plan 中定义并签署——planner 在 plan 文件中以「接口 spec」小节给出契约定义即视为签署；orchestrator 机械判定 plan 是否含该节。各 Phase executor 以此为共同事实源——schema 必须先于实现，防止"实现先于接口定义"的时序错位。跨 worker 的数据传递契约未在 plan 中定义前，禁止进入 Phase 实现。planner 未签署 spec 时唯一行为是阻断；仅接受 planner/commander 在进入该 Phase 前写入的显式 amendment，不接受 orchestrator 自签。

### verify-ownership（归属遥测机械校验）

```bash
python scripts/ocsr_dispatch.py verify-ownership \
  --state <_orchestrator-state.md> --ledger <ledger.jsonl> --repo <path>
```

三查：完整性（git 改动文件都在归属表中）/ 一致性（归属表中的 spawned label 在 ledger 中有记录）/ 合理性（mtime 窗口启发式，不阻断）。退出码：0=全通过, 1=缺漏/虚报, 2=参数/文件错误（state 不存在、ledger 缺失、--repo 缺失等）。

> orchestrator 在 converge 上下文派发时应传 `--ledger-dir <active 目录>`，verify-ownership 才能做完整三查（否则自动回退全局遥测，合理性检查降级）。