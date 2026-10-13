# PUP 识别线索

## 通用线索与判定

以下现象用于生成调查候选，单项不足以定性：
- 多款产品共享签名主体，或同批时间落地。
- OEM/联盟 SDK、Guard/Protect 驱动、私有 Svchost 组。
- 登录任务、更新任务及厂商参数。
- 桌面图标经过跳转器，下载请求和实际落地文件不同。
- 来源数据流缺失、主页配置或扩展权限异常。
- 非自愿清理/优化组件周期调用系统工具，伴随临时文件、日志或其他资源堆积；这类行为可以没有内核保护驱动。按 [性能取证方法](performance-forensics.md) 量化症状并核对实际调用关系。

继续核对用户安装意愿、静默安装日志、下载链、签名有效性、具体配置变更与回装行为。正常安全软件、更新器和合法分发器也可能具有部分特征。禁止按公司名相似、SDK 文件名或单一时间戳断言共同控制或恶意。

## 本例线索：NStore / 鲁大师相关推广链

具体来源与证据编号见 case-history.md。以下仅为 2026-10 案例线索，不是当前版本软件的通用定性，也不是删除配置。

| 对象 | 案例中需关注的证据 |
| --- | --- |
| NStore / LuDaShi | 下载链、共享签名证书描述、siteid=2105 / VendorID=byimp_2105、推广日志 |
| FolderGlance / SysMate | 精确守护服务名、SysWOW64 svchost 宿主、ServiceDll 和组成员 |
| GHallProtect / LudashiProtect / ProtectFilter / ExtensionProtect | 原始 ImagePath、驱动签名、运行状态、启动类型 |
| misc / DeskIcon{GUID} / keytask | 实际跳转器路径、配置和快捷方式目标，目录名本身不定性 |
| lockhomepage / StarGatherAge | 锁主页配置及推广框架文件 |
| 360 系软件 | 区分长期安装、独立推广和后来重装；核对用户保留意愿及共享依赖 |

报告中的产品候选包括 WinClean、MasterPDF、AnyWhere、ProZip、EasyClean、OmniImage、CleanCut、AIBiZhi、TaskbarFX、ZipGo、YqdWxClean。正式采集以本机实际路径、签名和版本为准；不要将这些名称复制成通用删除正则。

## 需要避免的因果误读

本例有更早的 360 软件管家渠道和后来 NStore 下载渠道。正版腾讯会议也在后续被下载，不能把所有腾讯签名文件当成推广包。源码中的短片段 misc、_pp、Gt、360 不适合跨机器批量匹配。厂商关系需独立材料证明；样本共现只支持技术关联。
