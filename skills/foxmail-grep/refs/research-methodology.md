# Foxmail 版本适配研究方法

当 foxgrep 在某个 Foxmail 版本上无法使用时（目录结构不认识、解析大面积失败、提取为空），
按本文方法对当前版本做勘察，形成**研究报告**（模板见 `research-report-template.md`）
和**修正 foxgrep.py 的 unified diff**，一并反馈上游。

研究原则：**数据文件优先，索引最后考虑；先实证后假设；小样本验证再全量。**

## 总流程

1. **确认版本**：读 `Foxmail.exe` 的文件版本号（PowerShell：`(Get-Item <path>).VersionInfo.FileVersion`）。
2. **勘察目录结构**：列出安装根与各级子目录，按文件大小分类——
   大文件（MB~GB 级）几乎一定是邮件数据；小文件（KB 级）是索引或配置。
3. **定位邮件数据**：hexdump 大文件头部，判断是明文 RFC822、mbox 式串联，还是加密容器。
4. **决定索引策略**：索引（IND/FXIS 等）只是加速器，不是必需品。
   若数据文件本身可扫（分隔符/边界可识别），**直接扫数据文件**，跳过索引逆向。
5. **凭据与加密**：账号配置里找服务器/账号字段；密码混淆算法优先查公开实现；数据加密先量化（见下）。
6. **小样本验证**：选最小的数据文件手工解析 3~5 封，确认字段（主题/发件人/日期/附件）全部正确。
7. **全量+回归**：全量扫描统计解析失败率；跑通 list/grep/show/attach/extract/--json；
   并回归已有模式确认无破坏。
8. **产出物**：研究报告 + diff，反馈上游。

## 已知格式档案（实测确认）

### Foxmail 7.2
- 布局：`Storage\<账号>\Mails\Index`（邮件索引，FXIS 变长记录）、`Indexes\msgBody\`（正文库）、
  `Indexes\recvdate.ind`、`Indexes\attach\attachInfo.rec0`、`Accounts\Account.rec0`
- `Mails\<id>` 邮件数据文件为加密存储。**取数不走解密**：正文 → `Indexes/` 搜索索引明文；
  附件 → Foxmail 渲染缓存（机制见 `render-harvest-recipe.md`）；新邮件 → POP3。
- 工具接入点：`--live` 模式（`_parse_fxis_mails` / `_parse_bodies` / `_parse_dates` / `_parse_attachments`）

### Foxmail 5.x / 6.x（5.0、6.5.0.29 实测）
- 布局：`mail\<账号>\<文件夹>.BOX` + 同名 `.IND`；另有 `Account.stg`、`accounts.cfg`（均为 OLE 复合文档）
- BOX = 未加密 RFC822 原文串联，分隔符为 `b'\x10'*7 + b'\x11'*6 + b'S\r\n'`（**5.x 与 6.x 相同**）
- IND = 变长记录（含 GBK 主题/发件人），记录顺序与 BOX 不一致，且含已删除邮件——**绕过不解析**
- 自定义文件夹也是 BOX（文件名即文件夹名）；trash/spam 同样可扫
- 账号目录名不一定是邮箱地址，可以是中文名（如用户自命名）
- 5.x 可能残留 `<文件夹>.ZXL.BOX/.IND` 空壳文件（16/64 字节，ZXL 格式试验残留），扫描时自然得到 0 封，无需特判
- 工具接入点：`--v6` 模式（`load_v6` / `_v6_scan_box`）

### Foxmail 7.0 / 7.1
- 未实测。拿到样本后先按本文流程勘察目录与 Mails 文件是否加密，再定接入点。

## 二进制格式勘察技巧

- **魔数识别**：先看文件头 16~64 字节。已知魔数：
  BOX 分隔符（7×0x10+6×0x11+"S"）、IND `ZXL\0`、OLE 复合文档 `D0CF11E0`（.xls/.stg/accounts.cfg 同）、
  ZIP/OOXML `PK\x03\x04`（.xlsx/.docx 附件校验用）。
- **定长假设检验**：猜测记录定长时，用 `(文件大小 - 头部长度) / 记录长` 是否整除初筛；
  整除≠正确（本案 88 字节整除纯属巧合），必须跨记录对比字段语义。
- **跨记录对齐对比**：连打前 3~5 条记录的 hexdump，找重复出现的字节模式
  （本案靠 `...90 26 41` 后缀认出时间戳字段、靠字符串起点对齐认出变长布局）。
- **用已知真值校验**：把猜测的偏移/大小字段与数据文件实测值对拍
  （本案用 BOX 分隔符实测邮件大小，反推 IND 字段含义后仍选择放弃 IND）。
- **加密量化识别**：数据文件长度 − 对应明文长度若为**恒定值**，是固定头加密；
  逐字节对比明文与密文看是否单字节 XOR/异或流。先评估绕过路径（索引里是否有明文副本）再决定是否投入。

## 凭据提取

- 账号配置（如 `Account.rec0`）中直接以 ASCII 搜索字段名：`IncomingServer`、`IncomingPort`、`Email` 等，
  字段后紧跟 4 字节小端长度 + 字符串值。
- Foxmail 7.2 密码混淆算法为公开已知（key `~F@7%m$~` 逐字节循环 XOR 后十六进制解码），
  参考公开实现 `ryoii/foxmail_password_recovery`。**密码本身属于用户隐私，不得写入任何产出物。**

## 编码与环境陷阱（Windows）

- Foxmail 6.x 邮件头/主题多为 GBK；Python `email` 用 `policy.compat32` + `decode_header` 处理，
  正文按 charset 声明解码并回退 gb18030。
- **批处理文件（.cmd/.bat）内禁止中文注释**——cmd.exe 按 GBK 解析，UTF-8 中文多字节序列会拆断行，
  把注释里的词当成命令执行（报 `'xxx' 不是内部或外部命令`）。bat 一律纯 ASCII。
- 向 heredoc/嵌套命令传含反斜杠的 Windows 路径时，注意 shell 转义层会折叠 `\`；
  在 Python 里拼路径用 `chr(92)` 或原始字符串，写文件后必须回读验证。
- 控制台中文输出：嵌入式运行时启动器已强制 `PYTHONUTF8=1`；手工调 Python 时设 `PYTHONIOENCODING=utf-8`。

## 邮件解析陷阱

- compat32 下 `msg.get('Content-Disposition')` 可能返回 **Header 对象而非 str**
  （垃圾邮件/畸形邮件常见），调 `.lower()` 前先 `str()` 强转；头部解码统一走 `_dh()`。
- 老版本邮件的 `Date` 头可能**不带时区**，解析得到 naive datetime，与带时区的 aware datetime
  混排会 `TypeError`——排序键统一做 naive→UTC 归一化（见 `_dt_sort_key`）。
- 扫描器必须容忍坏邮件：逐封 try/except 跳过并统计告警，失败率异常（>1%）说明有系统性问题，要追查。
- 附件提取后校验魔数（xls=`D0CF11E0`、xlsx/docx=`PK`）确认识别正确。

## 产出物规范

反馈上游的产出物 = **研究报告 + unified diff + 样本结构说明**：

1. **研究报告**：按 `research-report-template.md` 填写，含版本号、目录结构、hexdump 证据、
   字段解析结论、测试记录。报告中的路径一律占位化（如 `<foxmail-root>`），不含真实用户名/服务器地址/密码。
2. **diff**：针对 foxgrep.py 的 unified diff（`git diff` 或 `diff -u`）。约束：
   - 保持单文件、纯标准库、只读不改写用户数据
   - 新模式走独立 `--xxx` 参数接入 `main()`，不破坏已有模式（corpus / --live / --v6 必须回归通过）
   - 缓存类文件不进分发包
3. **反馈渠道**：产出物写入 skill 目录下的 `feedback\YYYYMMDD-<版本号>-<简述>\`
   （目录约定见 `feedback/README.md`），整目录交给本工具的分发来源（上游维护者）。
   上游评审合并后归档于 `_archive/feedback/`。
