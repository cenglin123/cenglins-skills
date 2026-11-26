# 处置清单与运行方法

## 清单契约

复制 plan.example.json 到案件目录，替换占位值并删除无关示例目标。不要直接运行示例。清单是本次处置范围，不是可疑名称库，不能填通配路径。

| 字段 | 格式与含义 |
| --- | --- |
| schemaVersion | 固定 1 |
| caseId / computer / userSid | 案件标识、计算机名、目标用户 SID；后两项必须与运行身份相符 |
| pupPattern | 可选，仅用于复核发现扫描；用完整产品/厂商特征，不用于删除 |
| allowedRoots | 允许删除目标所在根目录；实际目标必须是其严格子路径，任一列出的根（含嵌套根）本身都不可删除 |
| paths | 对象数组：path、kind=file/directory/shortcut；快捷方式需 expectedTarget，拼接 TargetPath、一个空格及 Arguments |
| registryKeys | 精确产品键或 CLSID/BHO/外壳叶键；WOW6432Node 视图根以及 Policies、Clients、RegisteredApplications、ODBC 等共享分支禁止整树删除，两个视图统一校验 |
| registryValues | key、name、expectedData；name 空串表示默认值；以原始未展开的数据比较 |
| services / drivers | name、expectedPath；expectedPath 必须复制注册表 ImagePath 原值，不能复制已展开的 CIM 路径代替 |
| tasks | path、name、expectedActions；只支持 Exec 动作（Execute 空格 Arguments），多个动作用“ ; ”连接，无参数时保留末尾空格；ComHandler 等非 Exec 任务需人工处置 |
| processes | path；只终止可读取且精确匹配此路径的进程，执行前复查身份；paths 与 processes 一样受 allowedRoots 约束 |
| svchostGroups | name、expectedMembers；成员必须全部在 services 清单，不删除正常共享组 |
| reviewContext | id、phase=baseline/final、notBefore；每轮生成不同 id，以带时区的 ISO 8601 时间标记本轮复核起点 |
| manualChecks | name、status、evidence、reviewedAt、phase、contextId；phase 和 contextId 必须匹配本轮 reviewContext，初始用 SKIP |

可省略无关目标数组；computer/userSid/caseId 必填。顶层未知字段被拒绝，旧清单必须重新编制。所有 HKCU、环境变量、桌面路径均指运行用户；其他用户目标需明确 SID，并由适用权限与工作流覆盖，不切换管理员冒充目标用户。

当前支持删除普通文件/目录/快捷方式、精确注册表项，拆除服务/驱动/任务、自启值和 Svchost 组。恢复系统原值、WMI 删除、卸载应用等另行执行并留证，不虚构脚本覆盖。

64 位进程下当前用户 32 位 COM 的物理叶路径使用 HKCU:\SOFTWARE\Classes\WOW6432Node\CLSID\{完整GUID}；该 CLSID 叶键可列清单，Classes\WOW6432Node 及其 CLSID 容器不可整树删除。Windows 不同注册表分支的共享/重定向规则不同，不对所有 HKCU 路径机械添加 WOW6432Node；参照 [微软 WOW64 注册表共享与重定向说明](https://learn.microsoft.com/en-us/windows/win32/winprog64/shared-registry-keys)。

## 从证据生成清单

只从本次证据取值：`services`/`drivers.expectedPath` 复制 04/05 段里的 `REG ImagePath` 原值（保留 `%SystemRoot%`、引号与启动参数），不要复制 WMI `PathName`；`tasks.expectedActions` 复制 03 段的任务动作；`registryValues.expectedData` 复制导出/查询到的原始值；`paths` 使用 02、05 段和手工确认的绝对路径。清单中不要出现通配符或短片段。

## 提权运行

打开目标用户身份下的 64 位管理员 PowerShell，在该窗口直接执行以避免 Start-Process 的空格/正则引号丢失。下面路径均是占位示例。

第一轮用 `reviewContext.phase=baseline`、`manualChecks` 全部 SKIP（phase 同为 baseline）。基线因手工证据未填而返回 2 属正常：

~~~powershell
$skillPath = '<skill>'
$casePath = '<case>'
$planPath = Join-Path $casePath 'plan.json'
& "$skillPath\scripts\verify-clean.ps1" -Config $planPath -CaseDir $casePath -Phase baseline
~~~

预览处置：保持 baseline 批次不变（final 批次必须等外部卸载与最后重启后再建立），对同一份计划做 DryRun，批准针对该版本的哈希。卸载与最后重启完成后，再把 `reviewContext` 与 `manualChecks` 改为 final 批次并复核；这一步只改变复核记录，不再执行处置。

~~~powershell
& "$skillPath\scripts\unprotect.ps1" -Config $planPath -CaseDir $casePath -DryRun
& "$skillPath\scripts\cleanup-residue.ps1" -Config $planPath -CaseDir $casePath -DryRun
~~~

读取两次生成目录中的 transcript.txt 和 results.json，呈现目标、依据、保留项及计划哈希。经授权且确认目标后，将日志显示的哈希作为固定字符串传入：

~~~powershell
& "$skillPath\scripts\unprotect.ps1" -Config $planPath -CaseDir $casePath -ApprovedPlanHash '<reviewed-sha256>'
# 按结果重启，核对状态，再完成正规卸载。
& "$skillPath\scripts\cleanup-residue.ps1" -Config $planPath -CaseDir $casePath -ApprovedPlanHash '<reviewed-sha256>'
# 完成卸载、清理、最后重启，补采手工证据后建立 final 记录并复核。
& "$skillPath\scripts\verify-clean.ps1" -Config $planPath -CaseDir $casePath -Phase final
~~~

只有普通程序、没有服务/驱动/Svchost 目标时可以只跑 `cleanup-residue.ps1`；它同样会处理任务、自启值与进程。省略 `pupPattern` 时发现扫描记 SKIP，复核退出码至少为 2（存在 FAIL/ERROR 时为 1），不能据此宣称全机无残留。

只有重启后删除获准时，正式清残留增加 -ScheduleLockedFiles。不要在执行行自动重新计算哈希传入，那会失去绑定已审阅文件的意义。

## 状态与恢复

每次运行创建唯一的 <stage>-<time>-<random>/，包含计划副本、transcript.txt、results.json。注册表备份和任务 XML 与该次运行放在一起，backup-index.txt 映射文件与目标。目录与文件只写 `deletion-inventory.txt`（路径、大小、时间；超过 5 万条截断），它不是恢复源。不覆盖前次记录。results.json 记录 verificationPhase；执行修改前和阶段结束时更新案件目录中的 last-mutation.json，写入失败阻止该次修改或完成判定。DryRun 不更新修改记录。

退出码：0 本阶段无错误/待办；1 ERROR/FAIL；2 SKIP/PENDING/需重启。DryRun 的 PASS 表示目标预检成功，不表示已删除。ERROR 阻止对应动作；其他独立目标可能已执行，必须按结果逐项续办。

三个状态文件的分工：

| 文件 | 关键字段 | 判读 |
| --- | --- | --- |
| results.json | stage、dryRun、rebootRequired、verificationPhase、planHash、computer、userSid、errors、skipped、results[target/status/detail/time] | 每次运行一份；PENDING 只在重启后删除登记已核验时出现 |
| last-mutation.json | caseId、computer、userSid、stage、runId、planHash、updatedAt | 存在时作为 final 复核时间下界；缺失需在 case-state 说明手工处置 |
| case-state.md | 下表字段 | 跨重启的人工交接，不参与脚本判定 |

重启前维护案件目录内的 case-state.md：
~~~text
案件与运行身份：
授权范围与保留软件：
当前阶段：
计划路径与已审阅 SHA256：
基线/本次结果目录：
已完成目标：
ERROR / PENDING / SKIP：
重启原因：
下一步及验收条件：
~~~

复核中以下 manualChecks 名称固定：
browser-policies-extensions、shell-com-bho、injection-wmi-winsock、other-user-profiles、post-reboot-observation、preserved-software。

每轮 reviewContext 使用独立 id 和 phase。notBefore、reviewedAt 使用带 Z 或时区偏移的 ISO 8601，例如 2026-10-13T14:00:00+08:00；记录时间不能早于本轮起点（等于起点可接受），存在 last-mutation.json 时 final 记录还不能早于它，不能填未来时间。清理前 PASS 即使同名也不能沿用；外部卸载与最后重启后建立新的 final 批次。

evidence 使用案件内相对文件路径，可附 #证据编号；文件必须存在且非空，不接受越界路径。例：reports/browser-review.md#E07。每项记录的 contextId、phase 必须与 reviewContext 一致。缺少、重复、失效记录或无效时间显示 SKIP。证据内容真实性和实际采集时点仍由人或 agent 审阅，文件存在不代表内容正确。未取得某用户证据时保留 SKIP，不填猜测的 PASS。

备份与导出的保管：注册表备份用于逐项人工恢复，不导入整份 Services/Svchost 备份覆盖清理后其他变更。任务可依据 XML 恢复；删除的文件需依靠事前副本或其他备份。运行结果本身不证明可完整回滚。备份、导出、任务 XML 与证据副本默认保留；只有已归档到案件目录并验证哈希、确认不再需要回滚或留证，且获得用户明确授权时，才可按精确路径删除指定副本，并在报告或 case-state 记录删除项与理由。
