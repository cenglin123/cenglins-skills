# 更新日志

## 2026-11-26 · 1.0.7（证据来源补强）

- 来源：同日的 Kingsoft/猎豹残留案卷（Windows 7、PowerShell 2.0、仅 WinRM）。
- `references/known-threats.md` 新增「常见证据来源」：推装时间线（临时/下载目录安装包的 ProductName、签名主体与文件修改时间；lite 采集器只给路径/大小/时间）、弹窗投放（厂商弹窗追踪目录中的素材/截图）、主页/搜索（用户级与机器级三处注册表、SearchScopes 覆盖范围与渠道号）。补一条 `fltmc` 只列已加载 minifilter 的判读说明。
- `references/case-history.md` 的 2026-11-26 案例补三类可复用证据：主页三处写入与「360搜索」项、`PopWndTrackerLog` 弹窗横幅图与 `%TEMP%` 三个奇虎签名安装包的推装时间线、`KNBDrv` 注册为过滤驱动但当时未加载的残留形态。
- `SKILL.md` 的识别线索入口与 lite 采集覆盖说明同步细化。
- 未改脚本，`tests/run-tests.ps1` 不受影响。

## 2026-11-26 · 1.0.6（独立审计修正）

- 来源：两个独立上下文的整体审计（含 `openai/gpt-6.2-sol`）对 1.0.5 的 12 项阻断/若干非阻断；同日 Kingsoft 毒霸/卫士残留案卷（Windows 7、PowerShell 2.0、仅 WinRM）暴露的处置与复核缺口。规则来源随案例记入 `references/case-history.md`。
- 处置归属：`cleanup-residue.ps1` 增加任务、自启值与进程处理，固定顺序为任务 → 自启值 → 进程 → 目录与文件 → 注册表键，跳过硬保护阶段不再漏项目；`unprotect.ps1` 的任务处置改用 `common.ps1` 的共享函数，XML 为空/不可解析/无 `Task` 元素时阻止注销。
- 安全边界：`Assert-SafeTarget` 拒绝任何已列出的 allowedRoots 根（含嵌套根）作为目标；`registryValues.name` 为 `null` 时拒绝清单（空串才表示默认值）。
- 重启后删除：删除异常先记为候选，只有 `PendingFileRenameOperations` 写入并读回核验成功后才记 PENDING 并请求重启；登记失败逐项记 ERROR。
- 复核：新增全部运行中服务/驱动的孤儿检查（注册键、原始 ImagePath、还原后的文件，svchost 宿主再核对 ServiceDll），缺键或文件缺失记 FAIL，无法解析记 ERROR；文档明确自动检查与发现扫描的覆盖差异。
- 持久化：`File.Replace` 的备份参数在 PowerShell 5.1 传 `$null` 会绑定失败，改用 `[NullString]::Value`；状态/哈希/证据写入显式检查返回值，失败时该段 ERROR 且不写 DONE；`reg export` 额外校验 `.reg` 头，任务 XML 要求 `Task` 元素且有主体。
- 降级采集：`collect-lite-ps2.ps1` 默认拒绝覆盖已有输出（`-Force` 才覆盖），达到行数上限仍写出 TRUNCATED/结束行，`fltmc` 非零退出与各段静默错误写入 `# SECTION ERRORS`，写文件失败时不再打印 saved。
- 采集完整性：产物哈希失败时不写 DONE 并退出 1；状态、哈希、DONE 按序原子写入（DONE 最后）；`-Resume` 绑定计算机、用户 SID、完整参数集与保留文件哈希，任一不符拒绝；Prefetch/BAM/ShimCache/SRUDB/DNS 的预算命中记 PARTIAL；fltmc 失败记 itemErrors；`collection-status.json` 增加 `parameters` 与 `hashErrors`。
- 新增 `scripts/collect-lite-ps2.ps1`：PowerShell 2.0 兼容的只读降级采集（WMI、任务 XML、单文本输出），供仅 WinRM 的目标使用。
- 文档：清理案例日期与单机细节移入 `case-history.md`；备份范围改为“注册表/任务自动导出，目录与文件只记清单”；浏览器主页/搜索改为按变更前证据恢复；补充状态与退出码契约、baseline/final 两轮清单流程、备份保管与准删条件、示例 `expectedPath` 使用原始 ImagePath。
- 回归：`tests/run-tests.ps1` 75 例在 Windows PowerShell 5.1.19041.7802 通过；新增嵌套根、任务独立处置、任务 XML 无效、PENDING 登记失败、孤儿服务、续采身份/参数/哈希、示例清单可加载等用例。
- 发布：ZIP 内容通道；服务端版本号按上传递增，本版发布为 1.0.6。

## 2026-11-26 · 1.0.5（执行要点补强）

- `references/pitfalls.md` 环境与编码：新增「解析采集结果要用字面量分隔符、不用 `awk -F` 带转义写法」；「用脚本改别的文件时按字面量匹配并断言命中」；「类型转换与成员访问要显式加括号」；「目标机为 PowerShell 2.0 时的 cmdlet 替代与 `ps @文件` 约束」；「WinRM 会话里 `Shell.Application` 压缩会挂起」
- `references/pitfalls.md` 取证与结论：新增「复核脚本里的键名/路径/服务名必须取自本次采集结果」「报不存在要用独立信号交叉核对」「驱动与服务路径需先还原 `\??\`、`\SystemRoot\` 与启动参数再判断」
- `SKILL.md` 新增「维护约定」节：规范态写作、内容落位、规则须有可指认来源、脚本改动跑回归、准则/流程改动派独立子代理按八项复核

## 2026-11-17 · 1.0.4（复核修正 R1）

- R1：`collection-status.json` 的 `resumeSkipped` 原用 `$( … )` 包装，空数组被拆包（PowerShell 5.1 序列化为 `{}`、7 为 `null`）。改为先构造局部 `[string[]]` 再赋值，与 `itemErrors` 同一写法；补断言——非续采运行时 `resumeSkipped` 必须是空数组，续采时须按列表序列化

## 2026-11-17 · 1.0.3（审计修正）

- F1（blocker）：`verify-clean.ps1` 任务扫描在严格模式下直取 `$_.Execute`，ComHandler 任务无该属性会抛 PropertyNotFoundException 使整面扫描 ERROR（11-17 案卷 errors:1）。新增 `Get-TaskActionText` 防御式取属性（按 `PSObject.Properties` 存在性判断、缺失时按 `CimClass` 分类），扫描改用该函数
- F2（major）：`Collect-SruDatabase` 在非提权时 `Test-Path` 抛 `ItemExistsUnauthorizedAccessError`（ErrorId 不在豁免名单），导致 15 段误判 ERROR、退出码 2→1。改为 try/catch 记单项 ITEM ERROR（`(SRUDB.dat path not accessible: …)`），`Get-Item` 同样防御；不再误报「not found」
- F3（major）：`-Resume` 时 15 段的 raw 产物（`appcompatcache.bin`、`SRUDB.dat`）就地覆盖且无 `.prev` 备份，违反证据保全承诺。新增 `Backup-EvidenceFile`：resume 覆盖前先移为 `*.prev-<时间>`（同秒撞车自动加 `-N` 后缀）；备份失败则不覆盖原文件（F6），Save 与两个 raw 产物均接入
- F4：`collection-status.json` 的 `itemErrors` 固定为 `[string[]]`（修复 0 条时序列化为 `{}`、1 条时为字符串的不稳定类型）
- F10：补回归测试——严格模式任务扫描（含真实任务列表）、SRUDB 拒绝路径、`.prev` 备份与同秒命名、备份失败不覆盖、`itemErrors` 类型、OutDir 已存在/非法 `-PupPattern`/`-DnsPattern`/32 位 PowerShell 拒绝
- 回归：`tests/run-tests.ps1` 71 例在 PowerShell 5.1 与 7 均通过；真机复跑 16 段，15 段 PASS 且 SRUDB/Prefetch/BAM 拒绝均记为 itemErrors，退出码恢复为 2（非提权）而非 1；续采实测 raw 产物生成 `.prev-*` 备份

## 2026-11-17 · 1.0.2（采集器脚本，第二批 A1–A5）

- A1 分段持久化与断点续采（`collect-forensics.ps1`）：每类别落盘后立即更新 `collection-status.json` 与 `evidence-hashes.json`（逐段哈希，含缓存），全部完成写 `collection-done.json`；新增 `-Resume`——完成/部分/跳过的类别跳过，失败类别重采（旧文件保留 `*.prev-<时间>`），已完成目录拒绝续采（exit 2）。修复 1.0.0 遗留问题：`ErrorActionPreference=SilentlyContinue` 下 `throw` 被降级继续执行（`OutDir must be new`、64 位校验实际从未生效），改为显式报错+exit；`OutDir` 创建失败、非法 `-PupPattern/-DnsPattern` 同样显式退出
- A2 采集项空值防御：服务/驱动逐项 `try/catch` 与空值占位（`(registry key missing …)`、`(n/a)`），单项失败记入该段 `itemErrors` 与 `# ITEM ERROR` 行，整段不再 ERROR；任务动作/触发器空元素、其他用户配置单元不可读（HKU）同样按单项记录；增量哈希遇到被占用文件记 `(hash-failed: …)` 而不污染下一段
- A3 执行痕迹专项：新增 `15-execution-artifacts.txt`——Prefetch 列表（时间倒序、限项）、ShimCache 原始值（`-MaxShimBytes` 内保留 raw + SHA256）、BAM 逐用户执行记录（FILETIME 解码）、SRUDB.dat（`-MaxSruBytes` 内复制，`-SkipSruDatabase` 关闭）；受保护键/目录不可读按单项记录
- A4 安全软件状态专项：新增 `16-security-status.txt`——SecurityCenter2 全部 AntiVirusProduct、WinDefend 服务、Defender 策略值（`DisableAntiSpyware` 等）、排除项注册表叶、MpPreference（模块不可用按单项记录）
- A5 过滤驱动挂载：`05-drivers.txt` 增加 `fltmc filters` / `fltmc instances`（未提权时记 `unavailable (exit N)`，不影响整段）
- 测试：新增 `tests/collector-upgrade-tests.ps1`（13 例，函数级提取 + 文件/注册表/CIM/原生接口夹具），覆盖逐段落盘/DONE/续采计划、逐项防御、四个新采集器与 fltmc；`tests/run-tests.ps1` 全量回归在 PowerShell 5.1 与 7 均通过；真机以 `-ProbeSeconds 3 -SkipHashes` 实跑 16 段 + DONE，续采 14 段跳过、2 段重采
- SKILL.md：采集表补 15/16 与 fltmc；说明逐段落盘、DONE 标记与 `-Resume`；补充单项缺陷与整段错误的界限

## 2026-11-17

- 依据三份实战案卷（`pup-20261106`、`pup-case-20261111`、`20261117-LQSAP12`）评估结果补文档（第一批，文档级）：
  - pitfalls：`Get-ScheduledTask` 的 ComHandler 动作没有 `Execute` 属性（11-17 案 146 个内置任务中 79 个中招、整面扫描 ERROR）；CLSID 不做整树递归、只解析被引用项；`reg query /f` 做删后收口复查（11-17 案多抓到两个残留键）；多盘机器采集需覆盖非系统盘（11-06 的 F:\360Downloads、11-11 的 I:\Appbox）
  - SKILL §2：补"签名有效只说明来源主体可识别，不能说明行为无害"
  - SKILL §4：处置项固定顺序（注销任务 → 目录与注册表键（逐项备份）→ 主页/搜索恢复（手工）→ 卸载最后），任务路由表同步；新增卸载器缺失分支——`UninstallString`/`QuietUninstallString` 指向失效时按"文件清单 + 签名 + SHA256 + 注册表备份"留证后手工清除（11-17：影视大全 `unins000.exe` 缺失）
  - SKILL §5：主页/搜索恢复规范——先导出 `Main`、`SearchScopes` 的 32/64 位两视图再改值，主页恢复 `about:blank`，删推广 SearchScope、`DefaultScope` 回 Bing
  - SKILL §6：复核覆盖"孤儿驱动/僵尸服务"（Running 但 `CurrentControlSet\Services` 无注册键，11-06：六个 360 驱动）

## 2026-10-13

- 吸收跨设备反馈中的通用性能取证方法：症状与资源规模对照、系统工具调用者日志优先、审计配置变更边界；未收录反馈中的待验证软件归因或设备案例。
- 目录时间线与安装器哈希改为受限只读工作进程，限制时间、项数、输出记录、哈希数量与字节预算；增加默认关闭的临时目录规模、文本日志、显式配置字段及时间桶模块。
- 采集状态区分未启用、无命中、完成、部分和错误，截断退出码为 2；部分统计不作为总量，探针快照与诊断输出纳入证据哈希。

- 第二轮独立审计修复 COM/外壳采集遗漏：读取命名处理器的默认 CLSID 和直接 IconHandler，保留用户覆盖及机器/用户的各注册表视图，32 位 BHO 按相应视图解析；补充正确当前用户 32 位 COM 叶键的清单校验。
- DNS 原生命令采集增加退出码检查，非零退出明确写入采集错误，新增无系统副作用的真实原生 exit 5/0 夹具及外壳/COM 纯注册表夹具。

- 根据独立子代理评审修复共享注册表容器删除边界：统一剥离 WOW6432Node 视图前缀，拒绝视图根及 Policies、Clients 等共享分支，并增加执行入口拒绝测试。
- 手工复核新增 reviewContext、阶段/批次和 ISO 8601 时间校验，验证案件内证据文件存在且非空；处置维护 last-mutation.json，最终复核拒绝旧阶段、旧批次及修改前记录。
- 增加可变内存状态的系统接口模拟，验证成功备份后的服务/任务/注册表/进程/文件处置链、调用顺序、重启删除追加与结果核验，覆盖中途失败不继续删除。

- 按任务意图区分来源分析、已有材料报告、普通卸载、完整清理与重启后续办；自保护和重启按证据与实际状态选择。
- 依据《流氓软件取证与复盘报告》新增 `references/case-history.md`，保存两条推广渠道、下载链、样本哈希与章节证据索引，明确原始证据未重采、归因限制及缺少清理后结果。
- 新增版本化精确清单、格式说明和占位示例；破坏性脚本移除案例默认值及正则删除接口，绑定目标机器、用户 SID 和已审阅计划 SHA256；清单快照与执行内容使用同一份内存字节。
- 新增 `scripts/common.ps1`，提供路径根/祖先、允许边界、重解析点、注册表容器及共享 Svchost 组保护，注册表备份门控、原生工具退出码检查和结构化结果。
- 修正取证中系统宿主服务、系统目录驱动遗漏及同时间戳目录去重；补采 ServiceDll、Svchost 成员，记录采集身份、错误、覆盖范围和证据哈希，禁止覆盖已有证据目录。
- 清残留限定精确路径、快捷方式期望目标与注册表叶项；重启后删除改为显式开关，备份并保留已有操作，写入后核验。文件删除不宣称自动恢复。
- 复核区分 PASS、FAIL、ERROR、SKIP、PENDING，检查精确目标、运行中驱动与重启删除状态；浏览器等未自动覆盖类别需证据评审记录，不再将查询失败或未覆盖视为干净。
- 独立保存每次运行计划、日志、备份及结果，提供重启恢复状态模板；Word 报告使用可用文档技能回退，移除私有 runtime 的硬依赖。
- 新增离线回归检查，使用临时夹具与模拟接口验证误报、路径边界、重解析点、计划绑定、身份、共享组及备份门控，不执行真实系统清理。

## 2026-10-10 · 修订

- SKILL.md：主线调整为“取证 → 复盘 → 拆自保护并重启 → 卸载本体 → 清残留 → 复核 → 出报告”，复核移到出报告之前，并补充复核基线，使“清理前后对照”有据可依；新增处置原则（破坏性操作先确认、特征按本机定制）；取证阶段改为证据文件对照表，并区分脚本覆盖项与手工补查项；案例叙述移入 `known-threats.md`；修正“Clash 写入 Zone.Identifier”的错误说法
- `collect-forensics.ps1`：修复输出缓存变量 `$R` 被循环变量 `$r` 覆盖、导致 02–10 号证据文件全部为空的问题；修复 WOW6432Node 下 Run 键路径拼接错误；新增 `StartupApproved`、`AppInit_DLLs`、WMI 事件订阅、非微软 WinSock 提供程序、安装包 `Zone.Identifier` 采集；去除中文正则（无 BOM 时 PS 5.1 读乱）；DNS 过滤改为 `-DnsPattern` 参数
- `unprotect.ps1`：新增 `-DryRun`；`Run/RunOnce` 清理扩展到 HKLM 两个视图；守护驱动名、结束进程特征改为参数并锚定匹配；备份范围增加 Services 与 HKCU Run
- `cleanup-residue.ps1`：新增 `-DryRun`；删除注册表键前自动 `reg export` 备份；重启后删除按“先文件、后目录（由深到浅）”登记；改为纯 ASCII；JSON 按 UTF-8 读取
- `verify-clean.ps1`：默认特征与其他脚本对齐，去掉 `misc`、`_pp` 等过宽片段；新增 Run 键与开始菜单快捷方式核验
- references：`pitfalls.md` 新增变量大小写、匹配特征过宽两节并精简措辞；`known-threats.md` 通用识别清单前置，补充案例经过与本例特征串

## 2026-10-10

- 初始化 `win-pup-forensics-cleanup` skill
- 沉淀自一次完整的实战处置：从不请自来的软件出发，溯源到“鲁大师软件管家掉包下载→NStore 母体→全家桶”，经历取证、复盘、解除内核自保护、批量卸载、残留清理与出报告
- 提供四个可复用脚本：`collect-forensics.ps1`（只读取证）、`unprotect.ps1`（提权拆自保护）、`cleanup-residue.ps1`（清残留 + 重启后删除）、`verify-clean.ps1`（复核）
- 提供参考资料：`pitfalls.md`（踩坑与规避）、`known-threats.md`（家族识别参考）
