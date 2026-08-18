# 派发操作模板与通道选择判据 — 详细参考

> 本文件从 SKILL.md §一/§三 下沉，收纳可复制代码模板与完整判据。主文件保留高频纪律（看门狗阈值、失败切换阶梯、并发纪律等）与本文件的入口指针。

## 通道选择判据（OCSR vs 框架原生子代理）

**通道选择判据**（某角色用 OCSR 还是框架原生子代理）：不枚举任务类型，按两条通用原则判断——本判据假设已通过上方「不用」判定需要派发，仅在选通道时生效——(a) **角色的价值来源**：评审的价值在跨 family 覆盖以规避同族盲区（`opencode run` 可跨厂商选模型、且天然全新上下文 → 评审类角色默认走 OCSR）；修复的核心价值在规格执行精度——确定性修复（规格逐字、验收客观）与通道无关；但修复含判断成分、或修复者与原作者同族时，跨族 executor 有规避共同盲区的价值（参照 converge 模型分层：确定性规格 + 用户授权 + 确定性验收是换通道/降档的安全前提）。(b) **协议成本与模型成本的双侧账本**：自足 prompt + 产物回收 + 看门狗是固定协议开销，任务越小占比越高；另一侧是模型档位——原生 executor 默认 inherit 主模型（验收链短但贵），OCSR 执行档便宜一个量级（验收链长但省），总效率最高的选择必须把两侧都计入。临界点无固定值，以本机遥测（§八 dispatch-log 的 wall_min / artifact_bytes）校准；本判据基于 2026-07 的模型能力与协议成本，随演进可能变化。判别样例（直觉参考，非决策规则）：几行的文字修复用原生 executor；批量转换、长收尾、成本敏感场景用 OCSR（模式 A/D）。

## 退出码契约（`ocsr_dispatch.py dispatch --watch`）

> **单一事实源。** 本节是 `dispatch` 退出码语义的权威定义，服务一切调用方——
> converge 适配层、手写观察器、层级指挥的上层编排、未来工具。
> 实现见 `scripts/ocsr_dispatch.py` 的 `EXIT_*` 常量与 `_watch_loop` 收口段。
> 修改本节须同步实现与 `tests/test_execution_layer_integrity.py`。

| 码 | 含义 | 判据 |
|---|---|---|
| `0` | 全部 worker 落盘 | 每个 worker 的期望产物存在、非 0 字节（预存文件还须内容变化） |
| `1` | 看门狗超时 | 至少一个 worker 到达自己的 deadline 仍未结案 |
| `2` | 确定性失败 | 至少一个 worker 确定性失败：launcher error（`error.log` 存在）/ opencode 非零退出 / **opencode 退出码为 0 但期望产物未落盘** |
| `3` | 路径碰撞 | 派发前后快照比对发现既有文件被非预期覆盖 |

**混合结局优先级**（同一次调用同时出现多种结局时返回的码）：

```
路径碰撞(3) > 看门狗超时(1) > 确定性失败(2) > 全部成功(0)
```

排序理由：3/1 为既有语义，保持最高优先级不变；确定性失败(2) 是**已结案的失败**，
排在**未结案失联**(1) 之后——避免已记录在案的失败掩盖仍在消耗预算的失联进程。

**`exit=0 且零产物` 为何算失败**：`_watch_loop` 判定进程结束只看
`exit_code is not None`，退出码为 0 同样成立。「opencode 正常退出但期望产物没落盘」
是 SKILL.md §五「越界写入 / 路径碰撞」的典型指纹（子代理自选文件名写到了别处）。
若契约只覆盖「非零退出」，这条真实终结路径会继续表现为成功。
该情形的遥测 `outcome_detail` 为 `error:exit_0_no_artifact`，便于事后归因。

**未启用 `--watch` 时不适用**：不做产物回收与路径碰撞检测，仅记 launched 并返回 0。

**调用方注意（接口变更）**：退出码 `2` 是 2026-08 新增。
在此之前 `dispatch --watch` 在 worker 失败时返回 **0** 并打印「✅ 全部 worker 完成」，
调用方无法用退出码区分成功与失败。converge 侧适配层
（`ocsr_spawn_adapter.py`，位于 converge 仓库）需相应接线，
把 `2` 解读为「spawn 确定性失败」而非未知错误，并据此 settle。

## 评审锚定防护（--forbid-paths 与 staging 布局）

> 动机：评审子代理读取并行/前轮 reviewer 报告会摧毁独立性（2026-08-04 ultraverge R2 事件）。禁令只靠 prompt 自觉不够——驱动器提供机械注入与落盘后审计。注意：本节机制与 prompt 禁令同为 best-effort，不构成安全沙箱（SKILL.md §七）。

```bash
python scripts/ocsr_dispatch.py dispatch   --worker "prompts/r1.md|xiaomi/mimo-v2.5-pro|R1"   --output-dir <round 输出目录> --output-pattern "reviewer-{label}.yaml"   --forbid-paths <收敛工作目录> --forbid-paths <其他 reviewer 报告目录>   --watch
```

- **注入**：驱动器在每个 worker 的 prompt 副本末尾追加禁止块（原 prompt 文件不改动），列出全部禁止路径，并要求报告的执行证据含结构化 `reads:` 列表（实际读取的文件路径）。
- **审计**：watch 模式产物落盘后，驱动器解析 `reads:` 并与禁止路径对照（子路径命中、Windows 大小写不敏感），输出 `clean / violated(<路径>) / unavailable(未含 reads)` 并写入遥测 `read_audit` 字段。**审计不改变退出码**——violated 的处置（作废重派 / 降级保留）由 orchestrator 按 SKILL.md §五 准则裁决。
- **布局约定**：被审产物 staging 与收敛工作目录不共享可读父树（推荐 `.converge/tmp/staging-<slug>/` 放产物、`.converge/active/<slug>/` 只放编排证据）；无法分离时 `--forbid-paths` 为必备兜底。

## 脱管派发模式（前台超时不够用时）— 完整三步模板

> **入口条件**：当 `scripts/ocsr_dispatch.py` 可用时优先用驱动器 `dispatch --watch`；仅当驱动器不可用（非本机环境、脚本缺失）时回退本节手写模式。

当 harness 前台 shell 工具的超时上限 **小于** 预计单轮耗时（判断密集角色在 harness 规划阶段的估算单轮常 20–30 分钟——含 prompt 构建、排队长尾、完整收敛往返——而多数 harness 前台上限 ≤10 分钟）时，上文"前台 + 大超时"不可用，后台通道又会被 kill（§七）。此时用**脱管派发**：让 `opencode run` 脱离 harness 任务生命周期独立运行，harness 侧只跑一个纯 shell 观察器等产物。

三步（实证可靠的 Windows 模板）：

1. 把命令写入 launcher 脚本（避免命令行转义 + 显式 UTF-8 读 prompt）：
   ```powershell
   # launcher.ps1
   $prompt = Get-Content "$PSScriptRoot\prompt.txt" -Raw -Encoding UTF8
    opencode run $prompt -m xiaomi/mimo-v2.5-pro --title reviewer-r1 *> "$PSScriptRoot\run.log"
    # OCSR 可选模型仅限: deepseek/deepseek-v4-flash, xiaomi/mimo-v2.5, xiaomi/mimo-v2.5-pro
   ```
2. 用 `Start-Process` 完全脱离 harness 生命周期启动（`-WindowStyle Hidden` 不弹窗）：
   ```powershell
   Start-Process pwsh -ArgumentList "-NoProfile","-File","C:\path\launcher.ps1" -WindowStyle Hidden
   ```
3. harness 侧跑纯 shell 观察器，**双监视**（产物落盘 **且** opencode 进程存活）：
   ```bash
   until [ -f artifact.md ]; do
     if ! tasklist //FI "IMAGENAME eq opencode.exe" | grep -qi opencode; then
       echo "opencode exited WITHOUT artifact → 失败（见 §五 静默停滞/kill 判定）"; exit 1
     fi
     sleep 15
   done
   echo "artifact landed"
   ```

要点：
- 观察器本身是纯 shell 循环，不是 opencode 进程，不受 harness 后台 kill 影响。
- 产物一律由子代理 Write 直写文件（本节既有纪律），不依赖 stdout 回收。
- 脱管进程不在 harness 任务生命周期内，因此不被 harness 的后台通道终止——这是它与"后台通道派发"的本质区别（后者把 opencode 进程交给 harness 后台机制管理，会被 kill）。
- 观察器必须双监视：只盯产物不盯进程，模型端静默停滞时（§五）会无限空等；只盯进程不盯产物，进程正常退出但 0 产物时会误判成功。
- 观察器自身不限时；超时由 orchestrator 按 §五 看门狗阈值终止底层 opencode 进程，观察器随后检测到进程退出即停止。

## 并行扇出 — 完整脚本模板

### 并行扇出

```powershell
# Start-Job 是 PowerShell 进程内后台作业，主控进程用 Wait-Job 存活等待——
# 与 harness 级后台通道（见上文/§七）不是一回事，可安全使用
# 硬超时秒数；先 1 个试点，再从 2 并发上探，遇 429/超时回退
$timeoutSec = 600
$jobs = foreach ($w in $workers) {   # $workers: 每项含 promptFile / log
  Start-Job -ScriptBlock {
    param($promptFile, $log)
    opencode run (Get-Content $promptFile -Raw -Encoding UTF8) -m deepseek/deepseek-v4-flash *> $log
  } -ArgumentList $w.promptFile, $w.log
}
# 有限超时等待——不再无限 Wait-Job
$deadline = (Get-Date).AddSeconds($timeoutSec)
while ((Get-Date) -lt $deadline) {
  $running = $jobs | Where-Object State -eq 'Running'
  if (-not $running) { break }
  Wait-Job -Job $running -Any -Timeout 30 | Out-Null
}
# 超时和失败作业回收：停止、记录警告、保留日志
$timedOut = $jobs | Where-Object State -eq 'Running' | Stop-Job -PassThru
$failed   = $jobs | Where-Object State -eq 'Failed'
if ($timedOut) { Write-Warning "$($timedOut.Count) 个 worker 超时" }
if ($failed)   { Write-Warning "$($failed.Count) 个 worker 失败" }
# 后续按 §五 逐文件验证，只以产物存在且非0字节为准；不因作业"完成"即判成功
$jobs | Remove-Job
```

```bash
for f in prompts/worker-*.txt; do
  opencode run "$(cat "$f")" -m deepseek/deepseek-v4-flash > "logs/$(basename "$f" .txt).log" 2>&1 &
done
wait
```

并发纪律：每个 worker 独立日志与独立产物路径，失败定位靠 §五 的文件验证而非解析 stdout；**先派 1 个试点 worker 走通链路（§五 验证通过），再逐步从 2 个并发上探**；遇 429/超时回退；扇出前向用户报预计调用数上限与所选模型，未经新鲜授权不突破已披露上限，不静默烧钱。

## 多轮续接（版本相关）

### 多轮续接（版本相关）

`opencode` 有 `--continue` / `--session <id>` / `--fork` 全局会话能力；对 `run` 子命令的支持以本机 `opencode run --help` 实测为准，不作为默认路径。注意：`opencode run --fork` 必须配合 `--continue` 或 `--session`（本机 `opencode run --help` 已确认），且仍是 **CLI 会话 fork**，不是 live 子代理句柄，不能当作上下文继承机制。默认设计成**一次投递、文件回收**，不依赖续接。

## 基本命令与长 prompt 文件处理

> 从 SKILL.md §三 下沉的可复制命令模板。

```powershell
# 基本：positional message
opencode run "你的 prompt 内容"

# 指定模型（跨厂商异构的关键能力）
opencode run "你的 prompt" -m deepseek/deepseek-v4-flash

# 常用附加参数
opencode run "你的 prompt" -m xiaomi/mimo-v2.5-pro --format json --title reviewer-r1

# 设定子代理工作目录（仅提示性质，不构成沙盒——见 SKILL.md §七）
opencode run "你的 prompt" -m deepseek/deepseek-v4-flash --dir C:\work\sandbox
```

长 prompt 别在命令行里拼（转义地狱）——写入临时文件再传。写入时也要显式 UTF-8（Windows PowerShell 5.1 的 `Set-Content` 默认 ANSI，会写坏中文）：

```powershell
Set-Content -Path .\prompts\worker-01.txt -Value $prompt -Encoding UTF8
```

```powershell
opencode run (Get-Content .\prompts\worker-01.txt -Raw -Encoding UTF8) -m deepseek/deepseek-v4-flash
```

```bash
opencode run "$(cat prompts/worker-01.txt)" -m deepseek/deepseek-v4-flash
```

## Windows 中文编码策略细节

> 从 SKILL.md §三 下沉。PS5.1/7 差异表保留在主文件（含 verify 锚点）；本节承载策略展开与失败诊断。

两种推荐策略：

1. 重定向到文件再读：`opencode run "..." -m <model> *> run.log`（注意 PS5.1 落盘为 UTF-16LE BOM，用 `Get-Content -Encoding UTF8` 读；PS7 落盘为 UTF-8 无 BOM）
2. **（推荐）让子代理用 Write 工具直接把报告写到指定路径**，完全不依赖 stdout 回传

输入侧：BOM-less UTF-8 的 prompt 文件在 PowerShell 5.1 下会被按 ANSI 误读成乱码，`Get-Content` 必须显式 `-Encoding UTF8`（pwsh 7 默认 UTF-8，但加上此参数两个版本通用）。编码行为随 opencode 版本变化，升级后需重新验证。

失败诊断不依赖 stdout：成败判定以**期望产物文件是否落盘**为准（SKILL.md §五），退出码为辅。日志文件出现乱码时，先区分显示问题还是文件损坏——用 UTF-8 方式重读文件；若文件字节无误则仅为显示层乱码。

## 派发遥测记录片段（PowerShell）

> 从 SKILL.md §八 下沉的遥测追加行模板；主文件保留 dispatch-log 的语义说明与字段表。

```powershell
[ordered]@{
  ts=(Get-Date -Format o)
  model='<model>'
  role='<role>'
  harness='<harness>'
  channel='<foreground|detached|background>'
  outcome='<success|stall|killed|error>'
  wall_min=<n>
  artifact_bytes=<n>
  task_id='<task_id>'
  plan_ref='<plan_ref>'
  scope='<scope>'
  prompt_size_bytes=<n>
  response_size_bytes=<n>
  model_cost_input=<n>
  model_cost_output=<n>
  cost_estimate=<n>
  blocking_chain=@('<blk1>','<blk2>')
  outcome_detail='<outcome_detail>'
  failure_retry_index=<n>
  label='<label>'
  note='<note>'
  timeout_policy_requested='<auto|leaf_kill|hierarchical_report>'
  timeout_policy_resolved='<leaf_kill|hierarchical_report>'
} | ConvertTo-Json -Compress | Add-Content "$HOME/.ocsr/dispatch-log.jsonl"
```
