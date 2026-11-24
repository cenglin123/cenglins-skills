---
name: wechat-grep
description: >-
  查询、搜索、导出、统计本机微信聊天记录（WeChat 4.x，直读实时加密库，全程只读），按需解密
  图片、导出并转写语音为文字。触发：用户要求读取/搜索/导出/汇总/统计微信聊天数据、生成聊天
  年度报告、查某个会话或联系人的聊天、把聊天导出为 JSON/TXT/HTML/Excel、解析聊天中的截图
  图片、语音转文字/听录音内容。基于 weflow CLI（Rust，--json 友好）+ 自带密钥捕获脚本
  （纯标准库，无需第三方工具）+ 自带 Zipformer 本地 ASR。Windows only；微信 4.1.15.x
  和 4.2.0.53 实测通过。首次使用先运行 scripts/check-install.py --prepare，再继续原任务。
---

# wechat-grep

微信聊天数据的 grep 式检索、导出与按需媒体解析管道。**全程只读**（weflow CLI 对修改类命令
一律拒绝）。微信登录状态下工作最佳（密钥捕获必须微信在线；数据读取读库文件+WAL 快照）。

## Agent 首次使用：先准备，再继续原任务

**不要先调用 `weflow.exe`，也不要让用户手工安装依赖。** 找到当前加载的 `SKILL.md` 所在目录（下文 `<skill>`，不是同名 Git 工作区），把实际路径代入这条命令；Windows 上 `python` 不可用时用 `py -3`：

```text
python "<skill>/scripts/check-install.py" --prepare
```

这条命令幂等：缺资产就优先用技能目录的 `wechat-grep-dist.zip`，否则从固定 GitHub Release 下载；先校验 SHA-256 再安装，随后立即运行 `db test`。不依赖 PowerShell，不改账号配置，也不输出账号、密钥或聊天内容。按 JSON `status` 继续，不要只看非零退出码：

| 状态 | Agent 下一步 |
| --- | --- |
| `database_opened`（0） | **直接执行用户原来的查询、搜索或导出。** 若用户只问是否可用，再用 `chat sessions --json` 确认实际读库，不展示会话内容。 |
| `profile_needs_setup`（3） | 用 `"<skill>/bin/weflow.exe" config profiles --json` 检查 `current`、`complete` 和是否已设置解密密钥，不打印档案内容。按下方「账号分流」继续，即使配置文件已经存在也不要停在诊断。 |
| `cli_probe_failed`（4） / `database_probe_failed`（5） | 前者检查 CLI 是否能启动，后者检查已配置的数据库能否打开。不要把这两个状态直接判为微信版本不兼容，也不要未经授权重建档案。 |
| `download_failed`（6） | 有同一固定 Release 的 ZIP：运行 `python "<skill>/scripts/check-install.py" --prepare --install-from "<实际ZIP路径>"`。离线又没有 ZIP：停止并说明恢复条件是能访问发布地址，或提供对应 ZIP；不要循环重试。 |
| `archive_rejected`（6） | 本地 ZIP 摘要不符，不得解包。若可联网，将坏包移出技能目录后重跑；若另有正确 ZIP，显式用 `--install-from`。两者均不可用时停止。 |
| `install_failed`（6） | 检查 ZIP 路径、技能目录是否可写、磁盘空间；不改系统策略，不提权。仍不可写就报告阻塞，不继续下载。 |

### 账号分流

- 无当前档案，或当前档案是空白／不完整（例如 `complete: false`）：如果另一档案可能是用户要读的账号，先确认目标；不能确认时不要自行切换。如果用户要读当前登录账号，一次确认是否授权本机初始化或补齐：读取当前微信账号所需的数据库密钥，仅写入本机 WeFlow 档案并设为当前；读库成功后尝试配置图片解密。不要索要或显示密钥。
- 当前档案显示完整但仍报配置错误：不要自动写入或切换；说明现有档案未通过检查，请用户确认要修复的账号，或指出要切换的已有档案。切换或修复后重跑 `--prepare`。
- `config profiles --json` 自身失败时，报告现有配置无法读取；先恢复配置或确认修复方式，不从配置读取失败推断为首次安装。
- 明确同意后，微信保持登录，由 agent 运行 `python "<skill>/scripts/capture_db_key.py"`（不加 `--show`，不转贴原始日志）。成功后重跑 `--prepare`，再直接完成原任务。若未找到 `session.db`，或微信已登录但自动发现的全部候选未通过校验，在同一授权范围内确认目标账号的存储目录，加 `--db "<实际目录>"` 定向重试一次；不再次询问授权。仍失败时报告验证未通过，不改用其他手段。
- 用户拒绝、未答复，或系统拒绝访问：停止账号初始化和聊天读取，说明具体阻塞；不要反复询问、切换工具绕过或尝试提权。已有能通过 `--prepare` 的档案不需要重新登录或授权。

语音转写额外需要匹配解释器的 ASR 依赖，只有用户要转写时才按「语音」一节从已下载的 wheels 安装。

## 工具与调用

- CLI：`<skill>/bin/weflow.exe`（v5.0.1，Rust 原生）
- 密钥捕获：`<skill>/scripts/capture_db_key.py`（**纯标准库**，无需 WDA/无第三方依赖）
- cmd 包装器：`wg.cmd`（cmd 环境的包装器，自动 chcp 65001；bash 环境直接调 `bin/weflow.exe`）；
  配置：`%APPDATA%\weflow\config.json`（顶层 `current_profile` + `profiles.<名称>.*`，每账号一个档案）
- 账号（wxid）与数据目录（xwechat_files）**每台机器不同，一律运行时发现**，见下节

**大件就位（bin/models/wheels，共 ~117MB）**：随 SKILL 分发时不进 git，走
`wechat-grep-dist.zip`（GitHub Release `wechat-grep-v1.0.0` 资产，或离线携带）。
默认由 `--prepare` 安装。只读诊断可省略 `--prepare`；从源码目录诊断实际部署时加 `--skill-dir "<部署目录>"`。

**调用铁律（实测踩坑）**：
- git-bash 直接调 exe；Python 解析输出必须 `export PYTHONUTF8=1`（PowerShell 等价
  `$env:PYTHONUTF8=1`；管道 GBK 解码必炸）
- Python subprocess 里 exe 路径必须 `C:\Users\...` 风格 + `encoding='utf-8', errors='replace'`
- 图片类命令前必须 `config set wxid <wxid>`（否则报"未配置账号或数据库路径"）
- `db_path` 必须是**账号目录**（如 `<数据根>\wxid_xxx_5678`），不是数据根也不是 `db_storage`

## 账号档案初始化 / 恢复（仅在上述流程需要时）

```bash
export PYTHONUTF8=1        # PowerShell：$env:PYTHONUTF8=1
# 仅在 --prepare 确认需要，且用户明确授权后运行；不要加 --show
python "<skill>/scripts/capture_db_key.py"
```

脚本自动完成：**定位 session.db**（读 weflow 配置 → 检查用户目录、Documents 和常见盘符下的 `xwechat_files`；兼容
数据根与账号目录两级布局，自动下钻到 `wxid_xxx_5678\db_storage\session\session.db`；
数据根下多账号共存时逐账号校验、命中账号的目录写配置）→
**定位 Weixin.dll**（优先从运行中的微信进程反查运行版本，回退常见安装目录 glob，仅支持微信 4.x）→
DLL 静态扫描内部密钥 →
只读扫描微信进程内存的混淆候选 → XOR 配对 + PBKDF2 页校验 → 自动写入 `db_path`
（**账号目录**；配置里误写数据根会自动纠正）、`decrypt_key`、`wxid` → `db test` 验证。
约 1-2 分钟，微信需登录在线。

参数：`--show` 显示密钥明文；`--db` 显式指定 session.db 文件 / 账号目录 / 数据根（三者皆可，
脚本自动下钻）；`--dll` 指定 Weixin.dll；`--no-set` 只捕获不写配置。

**agent 引导要点**：
- 已授权后，若脚本提示「未找到 session.db」，或确认在线但全部候选均未通过校验，请用户告知目标账号在微信设置中显示的数据目录：
  **给数据根（如 `D:\xwechat_files`）或账号目录（如 `D:\xwechat_files\wxid_xxx_5678`）都可以**，
  脚本会自动下钻匹配 `db_storage\session\session.db`，并把正确的**账号目录**写进配置。
  用 `python "<skill>/scripts/capture_db_key.py" --db "<实际目录>"` 重试，不提前写入配置。
- 密钥是敏感信息：脚本默认只显示掩码，不把明文写进日志/文档
- 验证通过后即可直接使用下方命令；图片类命令前补一次 `config set wxid`（脚本已自动写）

**杀软 / EDR 拦截预案**：
- 判别：`weflow.exe` 首次运行弹 SmartScreen"Windows 已保护你的电脑"；执行报"文件不存在/
  拒绝访问"或 exe 凭空消失（被隔离）；Python 脚本运行即被拦截/闪退——多为杀软/EDR 拦截，
  不是脚本故障
- 处置：停止并说明拦截；由用户或企业 IT 审查可信发布包。不要自动恢复隔离文件、添加排除项或绕过系统防护。
- 性质说明：capture 脚本只做**只读内存扫描**（ReadProcessMemory）与 DLL 文件静态读取，
  无注入、无提权、不写微信进程；weflow.exe 同样只读本机数据库文件

**禁止**：任何 Hook/调试器式密钥捕获（如 334456777/WeFlow 的 `key db`）——微信 4.1.15
会触发自我保护崩溃（"Weixin遇到错误"），且有账号风控风险。

备选：WeChatDataAnalysis（GUI 工具）也能捕获，其密钥可用 `<skill>/scripts/sync-keys-from-wda.py`
同步——仅在需要它附带的明文库导出等功能时才有意义。

## 命令速查

```bash
WF="<skill>/bin/weflow.exe"
export PYTHONUTF8=1

# ---- 读 ----
"$WF" chat sessions --json                  # 全部会话
"$WF" chat messages <SESSION_ID> --json     # 某会话一页消息（50条/页，--offset 翻页，
                                            #   --before/--after/--since 按 cursor 跳转，
                                            #   --parsed 按桌面端语义解析）
"$WF" chat latest <SESSION_ID> --json       # 某会话最新一条
"$WF" chat search <关键词> --json           # 全库搜索（--session-id 限定、--limit N）
"$WF" chat contacts --json                  # 全部联系人
"$WF" chat dates <SESSION_ID> --json        # 会话有消息的日期
"$WF" chat counts --json <ID...>            # 多会话消息总数

# ---- 导出（结构化+媒体一条命令） ----
"$WF" export messages --out <OUT_DIR> --format json --media all [--start 日期 --end 日期] <SESSION_ID>
# 9 种格式：txt/json/arkme-json/chatlab/chatlab-jsonl/excel/weclone/html/sql，逗号分隔多选
# --media all 导出解密后的图片/语音(WAV)/视频/表情副本，消息 content 指向相对路径
# stdout 末尾给 format:/requested:/exported:/missing:/dir: 字段

# ---- 读导出 JSON（机械渲染，分析对话归属必用） ----
python "<skill>/scripts/print_chat.py" "<OUT_DIR>/<SESSION_ID>.json"
python "<skill>/scripts/print_chat.py" <file.json> --start 2026-11-20 --end 2026-11-24 --last 50
python "<skill>/scripts/print_chat.py" <file.json> --stats-only          # 只看方向统计
python "<skill>/scripts/print_chat.py" <file.json> --attachments-only    # 只看附件索引（落盘路径）
python "<skill>/scripts/print_chat.py" <file.json> --width 200           # 单行截断
# 末尾默认附「附件索引」：文件附件按 MD5 校验定位到本机路径 + 被引用/紧随消息；--no-locate 关闭
# --file-root <账号目录> 手动指定文件目录（默认取当前 weflow 档案的 dbPath）
# 可传多个 JSON 按时间合并去重；退出码 0 正常 / 2 过滤后无消息或参数用法错误 / 3 文件缺失、解析失败或结构不符

# ---- 统计与报告 ----
"$WF" analytics rankings --json             # 联系人消息数排行
"$WF" analytics time --json                 # 小时/星期/月份分布
"$WF" group --help                          # 群成员与群统计
"$WF" report --help                         # 年度报告 / 双人报告

# ---- 常驻服务（自动化用） ----
"$WF" serve --desktop --message-push        # 桌面 API :5031 + SSE 新消息推送
"$WF" serve                                 # 命令 API :18108（token 见启动输出）
# 端口被占用时报错信息里含端口号，加 --port <端口> 换端口重试
```

JSON 约定：所有命令加 `--json` → stdout 输出 `{"success": bool, "data": ...}`；错误也在
JSON 里（`error.code/message`）。长任务加 `--progress`（stderr NDJSON）。
会话 ID：好友为 wxid，群为 `xxx@chatroom`（`chat sessions` 的 `username` 字段）。

### 导出 JSON 字段契约（读取以本表为准，不猜字段名）

`<OUT_DIR>/<SESSION_ID>.json` 每条消息都有以下字段：

| 字段 | 含义 |
| --- | --- |
| `isSend` | **方向唯一依据**：1=我发出，0=对方发出 |
| `senderDisplayName` / `senderUsername` | 发送者显示名 / 微信 ID（**我自己发的消息也有昵称**） |
| `type` / `localType` | 类型标签（文本消息/图片消息/语音消息/视频消息/文件消息/引用消息/系统消息…）/ 数字类型：1 文本、3 图片、34 语音、43 视频、47 表情、10000 与 10002 系统。应用消息（文件、引用、链接等）的 `localType` 是复合值，**低 32 位为 49**（如文件消息 25769803825 = 6×2³²+49），本身不等于 49 |
| `formattedTime` / `createTime` | 时间字符串 / epoch 秒 |
| `content` | 已解析好的正文文本（**不是 XML**）：文件为 `[文件] <文件名>`，引用为 `正文[引用 昵称：…]` |
| `platformMessageId` | 消息的服务器 ID；引用消息的 `replyToMessageId` 指向被引用消息的此字段 |
| `quotedSender` / `quotedContent` / `quotedType` / `quotedSvrid` / `replyToMessageId` | 引用消息五件套 |
| `imageDatName` / `imageMd5` | 图片消息定位（按需解密见下节） |
| 文件附件 | `type` 为 `文件消息`，`content` 为 `[文件] <文件名>`；**本体不在导出里、记录里也没有路径**，落盘路径由附件索引按内容校验定位（见「文件附件」配方） |

**方向与发送者只允许机械读取**：归属分析用 `scripts/print_chat.py` 渲染导出 JSON（发送者取
`isSend` + `senderDisplayName`），不要手写字段解析去猜发送者字段名，也不要按消息长短、语气
或消息类型推断谁说的——「收到」「好的」这类短消息靠语气必错。时间过滤在渲染层做
（`print_chat.py --start/--end/--last`）：全量导出一次，不要带 `--start` 反复 export 覆盖原文件。

**相关附件必须主动打开本体**：附件索引里的文件/图片若与当前任务明显相关——对方的指令引用了
它（看「被引用」）、它紧挨着指令出现（看「紧随消息」）、或任务本身就是围绕这份材料——先读附件
本体再作答，不要只凭文件名或消息文字推断附件内容，也不要只提一句「对方发了 xx.pdf」就算看过。
读取方式与各状态的处理见「文件附件」配方；任何情况下都不凭文件名猜内容。

## 按需媒体解析配方（文件、截图与语音）

消息 `localType`：3=图片、34=语音、43=视频。**不需要批量预解析**，按需逐条处理。

### 文件附件（PDF / Office / 压缩包等）→ 本机路径 → 主动阅读

微信把收发的文件存在 `<账号目录>\msg\file\` 下（账号目录即当前 weflow 档案的 `dbPath`）。导出与
`--media` 都不包含文件附件，消息记录里也没有路径。**路径只从 `print_chat.py` 的附件索引取**，
不要自己按文件名到目录里找：同一份文档改版重发后，盘上会同时存在 `xx.xlsx` 与 `xx(1).xlsx`，
按名找会拿到错的版本。

索引的定位是内容校验：用 `weflow chat resources` 里该消息的 `fileMd5` / `fileSize` 与盘上文件比对。
有 MD5 时，**内容一致才给「路径」**（先比同名及其 `(N)` 副本，再按「大小 + MD5」找回改了名的
同一份文件）。拿不到 MD5 时不猜：盘上恰好只有一个同名文件才给路径并标「未校验内容」，否则只列
候选。

```text
python "<skill>/scripts/print_chat.py" "<OUT_DIR>/<SESSION_ID>.json" --attachments-only
# 📎 行: 时间 [方向|发送者] 文件 <文件名>
#    路径: <本机路径>（MD5 与消息记录一致；…）           ← 直接打开
#    路径: <本机路径>（未校验内容：…）                    ← 没有 MD5；打开前先核对是不是这一版
#    未校验内容: 盘上有 N 个同名/副本候选…               ← 不给路径；需要时请用户确认是哪一个
#    未落盘: 盘上同名文件内容均不符 / 微信未下载到本机     ← 如实告知，不猜内容
#    未定位: 无法确定微信文件目录（补 --file-root 重跑）/ 文件名不合法（拒绝拼路径）
#    被引用: …      紧随消息（10 分钟内）: …              ← 纯 ID / 时间窗关联，用来判断是否相关
# 索引顶部的括号说明定位前提：未定位的原因、哪几个会话没取到 MD5、目录有没有读不了的地方；
# 附件超过 40 个时图片和视频压成单行（`📎 … → 路径`），文件条目保持完整
```

- PDF / Word / Excel / PPT：用宿主的 pdf / docx / xlsx / pptx 技能或任意可用的解析工具读正文
- 图片：按下节配方解密成 JPEG/PNG 后用图片读取工具查看
- 只读、不改动、不外传；附件可能含合同/人员信息，仅在用户要求分析的范围内使用

### 图片（截图）→ 本地 JPEG/PNG

```bash
# 1) 找图片消息（返回 imageMd5 / imageDatName / createTime / sessionId）
"$WF" chat resources --session <SID> --type image --start <日期> --limit 20 --json
# 2) 前提：wxid 已配置（部署脚本自动写入；缺省时 config set wxid <wxid>）
# 3) 按需解密单张（--force 高清优先，--prefer-file-path 返回路径而非 base64）
"$WF" image decrypt --session <SID> --md5 <imageMd5> --dat-name <imageDatName> \
      --create-time <createTime> --prefer-file-path --force --json
# 产物落在 %APPDATA%\weflow\cache\Images\<会话>\<年-月>\（实测 1200x1200 JPEG）
```
agent 拿到路径后即可用视觉能力读图。批量用 `export media --type image`。

**图片密钥**：部署脚本已自动捕获并写入当前档案（委托原生 `key image --save`：从微信
kvcomm 缓存派生 code，账号目录 `_t.dat` 模板实测校验，多 code 候选时自动消歧）。
若个别机器 kvcomm 缓存缺失导致未捕获，`image decrypt` 会报
`"V2 .dat requires an AES key"`——处置：在微信里浏览若干聊天图片（生成 kvcomm 缓存）
后重跑 `capture_db_key.py`；或用 WDA 捕获后 `python <skill>/scripts/sync-keys-from-wda.py` 同步。

### 语音 → WAV → 文本（本地 ASR，零外部服务）

```bash
# 1) 列语音消息
"$WF" chat resources --session <SID> --type voice --start <日期> --json
# 2) 导出整会话语音为 WAV（SILK 转码内置；24kHz 单声道，已验证标准 RIFF/WAVE）
"$WF" chat voice <SID> --out <OUT_DIR> --json
# 3) 本地转写（Zipformer CTC INT8，模型已随 SKILL 自带于 models/，中英文）
py -3.x <skill>/scripts/transcribe_voice.py <OUT_DIR>   # 3.x = 3.8-3.13 任一可用解释器
# 输出每行：<文件名>\t<文本>；--json 输出结构化
```

- 若 `<skill>/runtime/asr/Scripts/python.exe` 已部署，优先直接用该解释器运行 `transcribe_voice.py`，无需重装依赖。
- 转写依赖：**Python 3.8-3.13** + `sherpa-onnx==2.0.1 numpy`（默认 py3.14 无官方轮子）。
  先探测可用解释器（`py -0p`，或逐个试 `py -3.8 / py -3.12 / py -3.13`），任选一个
  已安装的 3.8／3.12／3.13 优先用离线轮子，并用同一个解释器运行脚本。离线安装（分发包的 `<skill>/wheels/`，已按
  3.8/3.12/3.13 三轮打包 win_amd64 轮子）：`py -3.x -m pip install --no-index
  --find-links <skill>/wheels sherpa-onnx==2.0.1 numpy`；轮子不匹配时再走网络，
  优先镜像源
  `py -3.x -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple sherpa-onnx==2.0.1 numpy`。
  脚本自动重采样 24k→16k、按 15 秒分段推理
- 模型：`models/zipformer-small-ctc-int8`（28MB，k2-fsa Apache-2.0，随 SKILL 分发）。
  错字率约 10%（专业术语需结合上下文校正），语义完整，适合汇总检索；追求精度可换
  Qwen3-ASR（WDA 生态）或云 API
- 微信自带的"转文字"结果**不落库**（服务端接口，仅 UI 可见），程序无法复用——故采用本地 ASR

## 多账号机器（一机多微信账号）

每账号的数据库密钥**相互独立**，且只有**当前登录账号**的密钥会出现在内存中——部署脚本
通过多账号目录逐一校验自动鉴别当前账号，并以 wxid 命名创建独立配置档案（`config profiles`
可列出全部档案及完备状态）。

- **捕获新账号**：微信切换登录到该账号 → 重跑 `capture_db_key.py` → 自动创建该账号的
  档案并切换为当前（此前捕获的其他账号档案不受影响）
- **已捕获账号间切换**：**无需重新登录微信**（读库是离线的）——
  `weflow config set current_profile <wxid>` 一条命令即切；`config profiles` 看清单
- 图片密钥同样随档案保存（`key image --save` 原生从 kvcomm 派生 + `_t.dat` 模板校验，
  多账号 code 共存时自动消歧）
- 注意：chat/export/serve 等命令都作用于**当前档案**的账号；跨账号查询需先切换档案

## 反馈机制（发现 SKILL 未预料的问题时）

本 SKILL 的长期可用性依赖真实运行反馈。以下情况**写一条反馈**（约定见
`feedback/README.md`，产出目录 `feedback/YYYYMMDD-<微信版本>-<简述>/`）：

- `capture_db_key.py` 报"DLL 中未找到内部密钥特征"或全部候选校验失败（微信版本升级特征失效）
- `key image` 模板校验失败或多 code 消歧行为异常
- 数据目录/账号目录/权限等环境差异导致部署链路卡点（SKILL 指引走不通的）
- 转写大面积空文本或可归纳的错字模式
- weflow CLI 行为与本文档描述不符

写反馈时**严守隐私红线**：密钥明文、wxid、聊天内容、媒体文件一律不入反馈
（掩码/占位化规则见 feedback/README.md）。反之，一次性偶然问题（网络抖动、误操作）
不写——先按本文档各节恢复指引尝试。

掩码完成后**同步提交到服务端反馈收件箱**（管理员人审汇总；信封同样严守上述
隐私红线，body 引用本地目录名以便追溯）：

```powershell
$kb = Get-Content "$env:LOCALAPPDATA\OpsClient\kb-agent.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$body = @{
  schema_version  = 1
  kind            = "environment"   # bug | environment | doc_mismatch | suggestion
  title           = "wechat-grep <微信版本> <一句话现象>"
  body            = "现象 / 勘察要点 / 已脱敏日志；附本地 feedback/ 目录名以便追溯"
  matter          = "wechat-grep"
  idempotency_key = "wechat-grep-<YYYYMMDD>-<微信版本>-<简述>"
} | ConvertTo-Json
Invoke-RestMethod -Uri "$($kb.url)/api/agent-feedback" -Method Post -ContentType "application/json; charset=utf-8" -Headers @{ Authorization = "Bearer $($kb.api_key)" } -Body $body
```

`kb-agent.json` 与环境变量都没有时（未连接公司知识库）跳过上报，本地目录照写。
403 或其他错误不重试轰炸，书面记录转管理员。

## 边界与注意

- 只读管道：不修改微信数据；删消息/防撤回会被 CLI 拒绝，如实转告
- 输出含聊天隐私：向用户汇报给计数与摘要，不整段转贴；导出数据含合同/人员信息，仅限内网
- 对话归属分析：发送者与方向机械读取（print_chat.py），不手写字段解析、不按语气推断
- 附件内容不靠猜：与任务相关的文件/图片从附件索引取路径、打开本体阅读；索引标「未落盘 /
  未定位 / 未校验内容」的按「文件附件」配方处理，不凭文件名替代
- 语音转文字、桌面弹窗不在 CLI 能力内（Electron 桌面版才有；未部署）
- 微信大版本更新后若捕获失败（DLL 特征变化），优先更新 weflow.exe 与特征，勿用 Hook 方案；
  勘察过程按「反馈机制」写入 feedback/ 供特征表更新
- 分析思路（周热度/事件时间线/多模态互证）：对导出的 JSON 按 `formattedTime` 聚周、
  以共享文件等关键事件为里程碑骨架、关键词月度漂移看阶段变化。聚合统计可以直接做，但方向与
  发送者一律取自 `print_chat.py` 的输出，同一文件名也不等于同一份文件（以附件索引的 MD5 为准）
