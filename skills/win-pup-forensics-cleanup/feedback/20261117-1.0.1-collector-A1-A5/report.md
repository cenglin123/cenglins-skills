# 采集器脚本升级建议（A1–A5）

- 日期：2026-11-17
- 环境：Windows 10 22H2（19045），64 位 Windows PowerShell 5.1 / PowerShell 7
- 技能版本：1.0.1（文档批次；本反馈不动 `collect-forensics.ps1`，按交接要求留待上游评估）
- 触发依据：三份真实案卷 `pup-20261106`、`pup-case-20261111`、`20261117-LQSAP12`
  （机名、账号、路径已占位化）

## 现象与诊断（逐项）

### A1 采集过程被安全软件结束，首轮证据不完整

- 现象：11-11 案中，取证脚本的 PowerShell 进程被安全软件判定为 `Trojan.Generic` 并结束，
  首轮采集中断；当轮已写出的 `collection-status.json` 与已落盘证据可读，但后续类别缺失。
  现场改为手工分段补采（`evidence-02`）才补齐。
- 诊断：全量采集单进程长跑，任何一段被杀都丢失剩余类别；单轮产物没有分段完成标记，
  复核者无法区分"没跑"与"被杀"。
- 建议：**分段持久化 + 反制防御**——每个类别写完立即落盘并更新 `collection-status.json`
  的逐段状态；全部完成后写 `DONE` 标记；每段产物单独计算哈希（现有
  `evidence-hashes.json` 改为逐段追加）。被杀后按已完成段断点续采，并把
  "被外部结束"写入状态（与采集错误区分）。

### A2 采集项空值导致整类 ERROR

- 现象：11-06 案 `04-services.txt` / `05-drivers.txt` 因部分条目缺少路径/显示名，
  报"不能对 Null 值表达式调用方法"，整类记为 ERROR。
- 诊断：属性链式调用未做存在性判断；单条异常吞掉整类。
- 建议：**采集项空值防御**——逐条 try/分类处理；缺值条目记为明确占位（如
  `<no ImagePath>`）并计数，不中断整类；与 A1 的逐段状态配合，部分成功记为 PARTIAL。

### A3 "谁装的"溯源依赖进程创建链，当前无标准采集项

- 现象：11-11 案"谁装的"完全靠 Prefetch 逐进程链还原
  （`DRVCEO → APPBOX → 360Safe`，总跨度约 70 秒）+ 用户活动库（SRUDB）。
  当前采集器无 Prefetch/ShimCache/BAM/SRUDB 类别，只能现场手工补。
- 诊断：默认采集偏"当前驻留面"，缺"历史执行面"；而推广安装链的关键证据恰在历史执行面。
- 建议：**Prefetch/ShimCache/BAM/SRUDB 专项**——作为标准采集项（Prefetch 需提权读取
  `System32\config` 之外的位置；ShimCache/BAM 用 `reg export` 或离线 hive；SRUDB 读取
  `SRUM` 数据库需停下服务或复制文件），或至少给出明确的手工补采流程与证据命名约定。

### A4 安全软件/安全中心状态缺失

- 现象：11-11 案定性核心是"安全中心被接管、排除项全空"，但当前采集器没有
  `root\SecurityCenter2` 的 `AntiVirusProduct` 枚举、Defender 服务/启动类型/组策略
  （`DisableAntiSpyware` 等）/排除项、第三方 AV 注册面。
- 诊断：处置决策（是否能依赖系统防护、清理是否会被拦截）依赖这一面，但没有采集。
- 建议：**安全软件/安全中心状态专项**——枚举 `root\SecurityCenter2` 全部
  `AntiVirusProduct`；读 Defender 服务与启动类型、策略键、`Exclusions`；记录第三方 AV
  注册；只读，不改配置。

### A5 过滤驱动挂载状态缺失

- 现象：11-11 案有 3 个文件过滤驱动挂载全部磁盘，直接影响"文件是否真的删干净"的判断；
  当前 `05-drivers.txt` 只列内核驱动与签名，不含 `fltmc` 实例。
- 建议：**`05-drivers` 增加 `fltmc instances` 与 `fltmc filters` 输出**（只读命令），
  与驱动签名表并列，供复核判断文件过滤层。

## 边界

- 以上 5 项均为**采集器脚本级改动**，本批不修改 `scripts/collect-forensics.ps1`
  （交接要求：脚本属构建资产，改动另评审）。
- 文档批次 1.0.1 已单独发布，与本反馈解耦。
- 复核与处置脚本（`verify-clean.ps1` / `cleanup-residue.ps1`）的配套改动（如孤儿驱动复核
  已入文档）在本批之外，按需另行评估。
