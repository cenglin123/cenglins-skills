# 作为 converge Spawn 后端（治理钩子对接）— 详细参考

> 本文件从 SKILL.md §三 下沉。主文件保留入口指针；本文件承载 OCSR 作为 converge Spawn 后端时的完整对接说明（五步原子化适配层、provenance 诚实降级、sentinel 模式）。

### 作为 converge Spawn 后端（治理钩子对接）

当 OCSR 作为 converge 的 Spawn 后端时，事件流（archive Contract v1 的 `begin-invocation`/`complete-invocation`/`recover-invocation`）与预算门控（`budget_gate reserve/settle`）必须端到端接线，否则 `archive_convergence.py archive` 会 fail-closed（`events-missing`）。converge 仓库侧提供适配层 `<user-home>/.agents/skills/converge/scripts/ocsr_spawn_adapter.py` 包装 `ocsr_dispatch.py`，把"reserve → begin-invocation → dispatch → complete/recover-invocation → settle"五步原子化：

```bash
python <converge-scripts>/ocsr_spawn_adapter.py dispatch \
  --converge-active <converge-active-dir> \
  --converge-scripts <converge-scripts-dir> \
  --ocsr-dispatch <ocsr-scripts>/ocsr_dispatch.py \
  --role outer-reviewer --phase reviewer-round-1 --round 1 --attempt 1 \
  --prompt <abs prompt path> \
  --model xiaomi/mimo-v2.5-pro --label r1-reviewer \
  --output-dir <active-dir> --output-name round-1.md \
  --watch --timeout 20
```

适配层定位与 ocsr SKILL §三"脚本不做编排判断"边界一致——它是 **converge 仓库侧** 的薄包装，**不**向 `ocsr_dispatch.py` 注入 converge 协议。本仓库（ocsr）的 `ocsr_dispatch.py` 保持框架无关，converge 是它的客户之一。

**provenance 诚实降级**：OCSR 派发的 `opencode run` 当前不在产物中暴露 per-invocation 的 provider/model 字段，故 archive Contract 的 resolved provenance 只能记 `evidence_level=configured + resolution_source=cli_argument + resolution_reason_code=backend-does-not-expose`（PROVENANCE_MATRIX 下的 strictest legal honest choice）。`--instance-id`（ocsr batch_id）与 `--receipt`（ocsr-dispatch-ledger.jsonl:<rid>）作为非约束性关联句柄保留，不升格 evidence level。未来若 opencode 暴露 per-invocation tool_response 的 provider/model，可升级到 host-reported。

**"edit X" 类任务的 sentinel 模式**：当 Spawn 的交付是"修改既有文件 + 写日志"而非"产出单一新文件"时（典型如 converge executor），`--output-name` 应指向一个 sentinel 文件（如 `done.marker`），prompt 显式让 executor 在完成所有工作后写入该 sentinel。否则 ocsr watcher 等不到期望产物、适配层会触发失败路径（recover-invocation + settle failed）——即使 executor 实际完成了工作。这是 faithful recording（archive Contract 捕获真实事件），但调用方需知晓此约定。

适配层的实现与治理细节以实际安装的 converge 技能中 `refs/framework-adapters.md` 及其适配脚本为准；本包只维护 OCSR 侧的接口约定。
