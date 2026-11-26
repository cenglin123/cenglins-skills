# 技能来源案例与证据索引

## 来源与使用范围

本摘要依据用户提供的《流氓软件取证与复盘报告》（文件名：流氓软件取证报告.md，报告日期 2026-10-10）整理。原报告保留在事件目录，本技能不携带其个人账户、浏览历史和完整网络清单。下面的证据编号指原报告章节，便于追溯；未在此次技能修订中重新获取原始浏览器库、样本、证书或系统状态。

这是技能形成案例的历史记录，不作为其他机器的操作授权。原报告中“主谋”“同团伙”“来源标记被清除”等表述包含归因推断，本摘要只记录其证据描述和可支撑的范围。

## 两条渠道与时间线

| 时间（原报告本地时间） | 报告记载 | 证据 |
| --- | --- | --- |
| 2023 年 | 360 软件管家管理的输入法及 360 浏览器已存在 | E01：§三、§10.1，卸载项和目录时间 |
| 2026-10-07 | 360 软件管家相关“系统盘清理”卸载项出现 | E01：InstallDate、SoftMgr 卸载入口 |
| 2026-10-08 14:25:08–12 | 下载并运行 腾讯会.议_bl384525263.exe | E02：§四，Edge 下载记录、文件时间和签名描述 |
| 14:25:30–14:31 | NStore、推广模块、FolderGlance、SysMate、LuDaShi 等集中落地 | E03：§三、§五，目录时间与卸载清单 |
| 2026-10-10 上午 | 多款软件更新时间变化，新增壁纸、游戏等推广产品 | E03：目录创建/修改时间；更新与新增应区别 |
| 2026-10-10 12:08 | 360看图安装日志含 --silent --lnk --auto | E04：§10.2，360DownLoad-pic360Setup.log |

E01 表明该机存在更早渠道，不能将全部推广归于 10/8 下载。目录时间关联需与下载、日志共同解释。

## 下载链与签名关系

E02：来源页 softmgr.ludashi.com/downloader/soft/url 登记目标为官方 TencentMeeting 安装包，实际落地文件来自 cdn-file-ssl.ludashi.com/downloader/temp_package/，文件名为 腾讯会.议_bl384525263.exe。

原报告记载该包、NStore 和鲁大师主程序共用代码签名证书，主体为成都奇鲁科技有限公司。渠道 siteid=2105 与 VendorID=byimp_2105 相符。此处引用原报告描述，未重新比对证书指纹；不能仅用签名主体断言公司控制关系。

E05：§10.5 记载 NStore 随后下载了腾讯签名的正版 WeMeet 安装文件。区分推广入口和后来真实软件，是此案例的关键。

原报告记载推广包带 SmartScreen 流但无 Zone.Identifier；只能确认报告观察到的缺失，不能据此证明是哪个程序清除了来源流。

## 驻留与推广证据

- E06：§6.1–6.4：任务 NStoreTray、WinClean、beijing-zipgo；服务 FolderGlanceGuardService、SysMateGuaurdService（拼写按报告保留）；组 FolderGlanceGp、SysMateGp；驱动 GHallProtect、LudashiProtect、ProtectFilter、ExtensionProtect。System/Manual/Auto 启动类型并存。
- E07：§6.5–6.7：BHO、COM 类、资源管理器菜单；IE WebBrowser CLSID 的用户覆盖指向浏览器。报告中的部分 CLSID 已省略，不能据此编造完整删除目标。
- E08：§7、§八：推广配置、跳转快捷方式、日志参数 /autorun --from=task；SDK 与保护文件共现。
- E09：§10.3–10.4：浏览器扩展脚本与主页锁定器；应按实际扩展权限、来源、用户意愿进一步评审。
- E10：§六记载未发现 AppInit、IFEO、Winlogon、WMI、WinSock 等劫持，仅适用于原报告观察时点与覆盖范围。

原报告建议先拆保护、重启、卸载、清残留。该文件是取证与处置建议，未给出完整清理后结果；因此本摘要不声明此次事件已经清理完成。

## 样本指纹（原报告 §10.8）

| 文件 | SHA256 |
| --- | --- |
| 腾讯会.议_bl384525263.exe | A216DEBACE43F80A4D28D73C4E1C8B492669F2107C6EBB2EEBCFB1AB2B3EC16D |
| ins38E.tmp.exe | 8D8959840031B7E2621265FABC7889233B71EFAE4AC33D0B4B8B1F9E61ACD86E |
| ins4BF2.tmp.exe | CA3719C29C37B8345DB6DDCA7548AB16CD19A0E1B802A4B21924E3E32D5DE14C |

指纹仅用于样本比对；版本变化后不能用未命中排除推广行为。

## 补充案例索引

以下记录只用于说明判据来源，不是其他机器的处置授权。日期为案件归档日。

### 2026-10 推广链（NStore / 鲁大师）

报告中的产品候选包括 WinClean、MasterPDF、AnyWhere、ProZip、EasyClean、OmniImage、CleanCut、AIBiZhi、TaskbarFX、ZipGo、YqdWxClean；守护对象包括 FolderGlance、SysMate、GHallProtect、LudashiProtect、ProtectFilter、ExtensionProtect；配置项包括 lockhomepage、StarGatherAge、DeskIcon{GUID}、keytask。核对下载链、共享签名证书描述、siteid=2105 / VendorID=byimp_2105、原始 ImagePath、ServiceDll 与组成员。正式采集以本机实际路径、签名和版本为准，不把这些名称复制成通用删除正则。

### 2026-11-06 卸载后驱动仍在运行

某机卸载 360 后，6 个驱动仍处于运行状态而注册键已缺失，WMI 仍能枚举到停止中的对象。由此确定复核必须枚举运行中的服务/驱动并核对注册与文件，不能只看注册表。

同案发现下载器把文件投放到系统盘之外的盘符（F:\360Downloads 一类），因此时间线采集必须覆盖非系统盘。

### 2026-11-11 注册表残留检索

某机靠 `reg query /f <词> /s` 在 HKCU\SOFTWARE 中额外找到两个按目录和卸载项遗漏的残留键；该方式成为删后收口复查的标准步骤。

### 2026-11-17 卸载器缺失与 ComHandler 任务

某影视软件的 unins000.exe 已缺失，按“文件清单 + 签名 + SHA256 + 注册表备份”留证后手工清除，没有另找绿色卸载入口。同案复核扫描 146 个内置任务，其中 79 个是 ComHandler 动作（无 Execute 属性），直接取属性会让整轮扫描报 ERROR，因此引入按动作类型防御式读取。

### 2026-11-26 安全软件残留与办公软件共享组件

某内网 Windows 7 主机（PowerShell 2.0，仅 WinRM 可达）残留 360 推广组件、猎豹浏览器驱动与用户数据、金山毒霸/卫士注册表与数据目录，WPS 与毒霸共用 `Kingsoft` 注册表根和 `ProgramData\kingsoft` 目录。处置按所有者拆分：`Antivirus`、`antivirus`、`kwspriEx`、`KISCommon`、`KISWsc`、`liebaocopy`、`KBROWSER` 及 `duba/kis/nse/kdata/kdesk/KSafeCommon/KIS` 等目录判为毒霸/卫士；`Office`、`wpscloud`、`KVip`、`PDF` 与两处 `office6` 判为 WPS 并逐项复核保留。归属不清的键值（`nemesis`、`kintranetauthsdk`、`kfp`、`Logger` 等）不删。

本案还确认：PowerShell 2.0 目标不能用完整采集器（`Get-CimInstance`、`Get-ScheduledTask`、`ConvertTo-Json`、`Get-FileHash` 均不可用），改用 WMI、任务 XML 与单文本输出；WinRM 会话里 `Shell.Application` 压缩会挂起，备份按文件取回并在分析机计算哈希；`robocopy` 退出码 1 表示已复制；`sc delete` 删除驱动后注册键可能仍在，需要补删。

## 沉淀到技能的经验

优先还原下载链和多渠道时间线；采集系统宿主服务及驱动，不能按系统目录排除；持久化既有服务/驱动，也有任务、COM、菜单、浏览器和推广快捷方式。清单必须从现场证据生成，自保护处置、重启和卸载顺序按实际状态选择，最终结论依赖清理后的范围复核。
