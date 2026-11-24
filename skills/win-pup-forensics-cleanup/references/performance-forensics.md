# 性能症状与资源规模取证

## 适用线索

登录、启动或系统功能异常缓慢，且存在非自愿清理/优化组件、周期调用系统工具或临时目录堆积时，先量化症状和资源规模。没有保护驱动不排除推广或非自愿行为；不能仅凭软件类别、签名主体或系统工具名称定性。

本页收录通用取证方法。跨设备的原始日志和设备特有事实保留在各自案件目录，不将未完成的归因链或软件关联写入技能案例。

## 采集参数

collect-forensics.ps1 的目录时间线与安装器哈希使用独立只读工作进程。每个来源默认最多 15 秒工作预算、20,000 个目录项、2,000 条输出记录；哈希最多 50 个文件、累计 512 MiB。父进程在预算加 2 秒启动余量后终止自己创建的超时工作进程。枚举、文件读取或哈希阻塞时也不会无限阻塞主流程。各来源分别计预算，不是整个脚本的总耗时上限。

参数：ProbeSeconds（1–45）、MaxEntries、MaxRecords、MaxHashFiles、MaxHashBytes、MaxTextBytes。需要排除安装器扫描时使用 SkipHashes。常规服务、注册表等类别仍不提供整个脚本的硬超时承诺。

| 可选模块 | 启用方法 | 范围 |
| --- | --- | --- |
| 11-tempfootprint.txt | -CollectTempFootprint，按需 -TempRoots | 当前用户 TEMP 与 Windows Temp 默认根；只统计第一层项数、名称模式、文件字节和目录创建时间桶，不递归计算目录总大小 |
| 12-toolcallers.txt | -CollectToolLogs，按需 -ToolLogPaths | DISM/CBS 文本日志的限量尾部，提取 Parent process command line / Client Process 字段；不解析 WindowsUpdate ETL |
| 13-configfields.txt | -CandidateConfigFiles '<file1>','<file2>' | 仅显式指定的配置文件，限量读取开头；提取数字 interval/delay/rp_interval_h 字段和 URL 域名，不保存 URL 用户信息或查询串 |
| 14-timeline-buckets.txt | -CollectTimelineBuckets | 复用 02 号同一快照中的第一层目录按日/月创建计数，不再次遍历或将创建时间当安装时间 |

这些模块默认关闭；临时目录、日志及配置来源每类最多 20 个，额外用户路径须明确范围与权限。不按厂商名自动搜索全盘。日志和配置以 UTF-8 输出，支持带 BOM 的 UTF-16 文本；未知二进制格式需专用解析，不将乱码当证据。

~~~powershell
& '<skill>\scripts\collect-forensics.ps1' -OutDir '<case>\evidence-performance-01' `
  -CollectTempFootprint -CollectToolLogs -CollectTimelineBuckets `
  -CandidateConfigFiles '<candidate>\config\schedule.ini' -ProbeSeconds 15 -MaxEntries 20000
~~~

probes/ 保存各工作进程的范围、配置、结果快照和诊断日志，并纳入证据哈希。原始结果中可能有用户路径与工具命令行，保存在非同步本地案件目录，报告按路径隐私规则占位化。

## 覆盖状态

collection-status.json 每节记录 enabled、status、dataState、details：
- NOT_ENABLED / SKIP：未启用，不是“没有问题”。DNS 和 TCP 子项分别记录是否启用；没有候选筛选时不执行其采集。
- NO_MATCH / PASS：在已声明范围内采集完成但未命中，不是全机干净。安装器哈希独立于 DnsPattern。
- COMPLETE / PASS：在已声明范围内完成采集，仍须解释证据。
- PARTIAL：达到时间、项数、输出记录、文本字节或哈希预算，不能将观察到的数量当总量。目录项数是本次快照下的已观察下界，时间桶也只代表已读取部分。
- ERROR：读取、权限或工作进程执行失败，不能代替无命中。

采集退出码 1 表示错误，2 表示部分采集或非提权范围不完整，0 表示启用类别在声明范围内无已记录错误/截断。默认关闭的可选模块不会单独导致退出码 2。并发创建/删除文件会影响统计，目录扫描不是事务一致性快照。

日志尾部和配置开头发生字节截断时明确标 PARTIAL。worker timeout 保留已保存检查点；若来不及保存，则不提供计数。重解析点不跟随，并在统计中记录跳过数。

## 调用者证据顺序

先阅读系统工具自身日志中的调用者字段，记录原始文件、时间、会话边界和字段名称。字段证明的是它实际记录的调用关系，不向上自动补全另一层父进程。

若已有 Security 4688，按事件时间、创建者进程、创建者 PID、新进程 PID 及命令行关联；注意 PID 重用，不能只按进程名串联。进程创建审计和命令行记录均需相应策略；开启策略属于独立的配置变更，不能包装成只读取证，也不能恢复启用之前未记录的事件。[微软进程命令行审计说明](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/component-updates/command-line-process-auditing)

配置调度字段只解释程序配置，不证明任务实际执行或某次系统工具调用。周期相近用于选择后续采集范围，归因报告只写由调用关系证据支持的结论。

## 症状量化与报告

使用统一设备、用户、时区和症状计时起止点，记录多次观察。处置前后使用相同测量范围和预算；部分采集与完整采集不可直接当成相同口径比较。

| 观察时点 | 症状与耗时 | 资源项数/字节 | 覆盖状态与预算 | 系统日志证据 | 已确认处置或其他变化 |
| --- | --- | --- | --- | --- | --- |
| <time> | <start/end/duration> | <observed-count> | <complete/partial, scope> | <evidence-id> | <recorded-change> |

写清“观察到了什么、证据覆盖哪里、确认了哪一层调用关系、处置后的指标如何变化”。没有完成根因验证的内容不写入技能案例或当作确认结论；统计总数与分项不一致时先核对采集时点、重解析点和统计口径。
