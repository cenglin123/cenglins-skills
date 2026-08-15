# 陷阱清单（完整表）— 详细参考

> 本表从 SKILL.md §七 下沉。主文件保留速查摘要与 verify 锚点；本文件承载完整 14 行陷阱表（事实/对策原文）。

## 七、陷阱清单

> 本表及全文"实测"的环境基线：opencode 1.18.5 · Windows 10 · pwsh 7+ / PowerShell 5.1 / Git Bash。版本升级后关键行为需复验（推荐调用方派发脚本的 selftest）。

| 陷阱 | 事实 | 对策 |
|------|------|------|
| `--dir` 不是沙盒 | 用法 `opencode run "…" -m <model> --dir <绝对路径>`；它只设定工作目录提示，子代理仍可用绝对路径读父目录、兄弟目录、执行任意命令（实测确认） | 三层缓解：信息最小化（临时目录只放必要材料）+ prompt 明令禁读界外 + 回收时审计输出是否引用界外路径。注意：prompt 禁令和事后路径审计都是 best-effort，不构成安全隔离 |
| OCSR 不提供安全隔离 | `--dir`、prompt 禁令和路径审计都是 best-effort 机制：**能降低误访问风险，不能阻止恶意模型静默越界或外发数据**。不可用于把秘密、恶意输入或含 prompt injection 风险的材料交给不可信模型 | 含上述风险时，要求由宿主提供低权限账户、文件系统沙箱和出站网络限制；条件不满足则停止并报告安全边界，不派发 |
| 框架内建子代理换不了厂商 | opencode `task` 无 per-spawn model 参数；Claude Code `Agent` 仅同家族档位 | 跨厂商异构一律 `opencode run -m` |
| `opencode run --fork` 不是子代理 | 必须配合 `--continue` 或 `--session` 使用；它 fork 的是可恢复的 CLI 会话，非 live 子代理句柄 | 不要当上下文继承机制用 |
| stdout 中文显示乱码（Windows） | GBK/UTF-8 编码不匹配导致显示层乱码，不意味文件内容损坏 | `*>` 重定向进文件，或（推荐）子代理 Write 直写报告；日志乱码时用 UTF-8 重读区分显示问题与文件损坏 |
| 子代理自我报告不可信 | 会生成看似成功的报告而实际 0 产物；会反向"矫正"正确术语 | §四 残差四件套 + §五 确定性验证 |
| `cost=0` 不自动等于弱模型 | 只表明价格元数据可能缺失或免费，不能单独证明模型弱（见 §六C） | 排除模型时结合 context 限制、toolcall 能力、试点证据综合判断，标为启发式裁决 |
| harness 超时截断 / 后台通道 kill | 单次 run 常超 2 分钟；部分 harness 的后台任务机制会终止 opencode 进程（实测） | 前台执行 + 调大 harness 侧 timeout 参数；后台失败指纹 = 日志 0 字节 + 无产物文件 |
| 静默烧钱 + 无界重派 | 扇出 N 个 worker = N 次计费调用；无上限重派可形成计费循环 | 派发前报数量上限与所选模型，每 worker 最多 3 次尝试，未经新鲜授权不突破；先派 1 个试点 worker 走通链路，再逐步从 2 个并发上探，遇 429/超时回退 |
| harness 前台超时 < 单轮耗时 | 判断密集角色在规划阶段估算单轮 20–30 min（含 prompt 构建、排队长尾、收敛往返），多数 harness 前台上限 ≤10 min | 改用 §三 脱管派发模式（launcher + Start-Process + 双监视观察器） |
| 模型端静默停滞 | 进程存活 + 日志 0 字节 + 无子进程 + 超过看门狗阈值 | §五 看门狗硬阈值到期即终止，禁止无阈值人工轮询；记录后按失败切换阶梯重派 |
| **Launcher 路径转义** | 反斜杠路径经多层转义（harness JSON→heredoc→Python f-string→pwsh）可能被误解析（`\r`→回车等）；症状为 launcher 秒退无日志、无产物、无错误文件 | 驱动器 (`scripts/ocsr_dispatch.py`) 已内置路径生成，该陷阱仅手写模式需注意。手写模式下一律使用正斜杠路径（`$PSScriptRoot/run.log` 而非 `$PSScriptRoot\run.log`）；launcher 加启动 marker 与 try/catch 错误捕获 |
| **并发 DB 锁** | 多个 opencode 实例**同秒**启动时，本机会话 SQLite DB（`~/.config/opencode/sessions.db` 或其等价物）被并发写入撞锁，触发 `database is locked`；实例秒退 exit=1、log <100B、无产物 | 多 worker 启动加**错峰间隔** ≥5s（调用方派发脚本应有此能力）；失败日志含 "database is locked" 时延迟 30s 自动重试 1 次（通道例外，不计模型重试名额）；selftest 会检测当前 opencode 版本的并发容限 |
| **越界写入覆盖既有产物** | `--output-pattern` 只约束看门狗等待哪个文件，**不约束子代理往哪写**。prompt 输出路径含占位符时，子代理自行发明文件名，可覆盖同目录他人产物；指纹 = exit=0 + 期望产物缺失 + 同目录既有文件 mtime/size 变化 | prompt【输出】节写死唯一绝对路径且说明覆盖后果；派发前备份 `--output-dir`；调用方驱动器派发前后快照比对，检出即报错退出（详见 refs/failure-modes.md §越界写入） |
| **嵌套派发失账** | 下层 orchestrator 自行发起的 `opencode run` 不经上层的预算 gate，账本只记外层——治理看到的开销可能只有真实值的一小部分 | 当 OCSR 作为 converge 的 Spawn 后端时，每个 `opencode run` 调用应由对接层驱动器向 converge 的 active 目录自动追加派发账本记录，无需调用方逐次传参；驱动器应提供汇总命令（含嵌套派发）供上层流程审计 |

---
