---
name: wechat-grep
description: >-
  查询、搜索、导出、统计本机微信聊天记录（WeChat 4.x，直读实时加密库，全程只读），按需解密
  图片、导出并转写语音为文字。触发：用户要求读取/搜索/导出/汇总/统计微信聊天数据、生成聊天
  年度报告、查某个会话或联系人的聊天、把聊天导出为 JSON/TXT/HTML/Excel、解析聊天中的截图
  图片、语音转文字/听录音内容。基于 weflow CLI（Rust，--json 友好）+ 自带密钥捕获脚本
  （纯标准库，无需第三方工具）+ 自带 Zipformer 本地 ASR。Windows only；微信 4.1.15.x
  实测通过。密钥失效重跑 scripts/capture_db_key.py 即可。
---

# wechat-grep

微信聊天数据的 grep 式检索、导出与按需媒体解析管道。**全程只读**（weflow CLI 对修改类命令
一律拒绝）。微信登录状态下工作最佳（密钥捕获必须微信在线；数据读取读库文件+WAL 快照）。

## 工具与调用

- CLI：`<skill>/bin/weflow.exe`（v5.0.1，Rust 原生）
- 密钥捕获：`<skill>/scripts/capture_db_key.py`（**纯标准库**，无需 WDA/无第三方依赖）
- cmd 包装器：`wg.cmd`（cmd 环境的包装器，自动 chcp 65001；bash 环境直接调 `bin/weflow.exe`）；
  配置：`%APPDATA%\weflow\config.json`（顶层 `current_profile` + `profiles.<名称>.*`，每账号一个档案）
- 账号（wxid）与数据目录（xwechat_files）**每台机器不同，一律运行时发现**，见下节

**大件就位（bin/models/wheels，共 ~117MB）**：随 SKILL 分发时不进 git，走
`wechat-grep-dist.zip`（GitHub Release `wechat-grep-v1.0.0` 资产，或离线携带）。
缺失时运行 `powershell <skill>/scripts/install-dist.ps1`（自动下载 + SHA256 校验 +
解压；内网离线用 `-ZipPath <本地zip路径>`）。就位判据：`bin\weflow.exe` 存在。

**调用铁律（实测踩坑）**：
- git-bash 直接调 exe；Python 解析输出必须 `export PYTHONUTF8=1`（PowerShell 等价
  `$env:PYTHONUTF8=1`；管道 GBK 解码必炸）
- Python subprocess 里 exe 路径必须 `C:\Users\...` 风格 + `encoding='utf-8', errors='replace'`
- 图片类命令前必须 `config set wxid <wxid>`（否则报"未配置账号或数据库路径"）
- `db_path` 必须是**账号目录**（如 `<数据根>\wxid_xxx_8370`），不是数据根也不是 `db_storage`

## 新机器首次部署 / 密钥失效恢复（agent 引导流程，无需任何第三方工具）

```bash
export PYTHONUTF8=1        # PowerShell：$env:PYTHONUTF8=1
# 步骤 0：大件缺失（无 bin/models/wheels）时先安装分发包
powershell <skill>/scripts/install-dist.ps1     # 自动从 Release 下载；内网用 -ZipPath 指向离线包
python <skill>/scripts/capture_db_key.py        # 若 python 不可用，用 py -3 或具体版本（如 py -3.8）
```

脚本自动完成：**定位 session.db**（读 weflow 配置 → 常见盘符扫描 `xwechat_files`；兼容
数据根与账号目录两级布局，自动下钻到 `wxid_xxx_8370\db_storage\session\session.db`；
数据根下多账号共存时逐账号校验、命中账号的目录写配置）→
**定位 Weixin.dll**（优先从运行中的微信进程反查运行版本，回退常见安装目录 glob，仅支持微信 4.x）→
DLL 静态扫描内部密钥 →
只读扫描微信进程内存的混淆候选 → XOR 配对 + PBKDF2 页校验 → 自动写入 `db_path`
（**账号目录**；配置里误写数据根会自动纠正）、`decrypt_key`、`wxid` → `db test` 验证。
约 1-2 分钟，微信需登录在线。

参数：`--show` 显示密钥明文；`--db` 显式指定 session.db 文件 / 账号目录 / 数据根（三者皆可，
脚本自动下钻）；`--dll` 指定 Weixin.dll；`--no-set` 只捕获不写配置。

**agent 引导要点**：
- 若脚本提示"未找到 session.db"——这是唯一可能需要问用户的问题。让用户告知微信数据目录：
  **给数据根（如 `D:\xwechat_files`）或账号目录（如 `D:\xwechat_files\wxid_xxx_8370`）都可以**，
  脚本会自动下钻匹配 `db_storage\session\session.db`，并把正确的**账号目录**写进配置。
  拿到路径后重跑脚本，或 `weflow config set db_path <目录>`（写数据根/账号目录皆可；
  数据根仅作为脚本输入，脚本会自动纠正为账号目录，日常使用的 db_path 必须是账号目录）后再跑，
  也可直接 `python <skill>/scripts/capture_db_key.py --db <目录>`；旧配置里误写了数据根也无妨，
  脚本会自动纠正为账号目录
- 密钥是敏感信息：脚本默认只显示掩码，不把明文写进日志/文档
- 验证通过后即可直接使用下方命令；图片类命令前补一次 `config set wxid`（脚本已自动写）

**杀软 / EDR 拦截预案**：
- 判别：`weflow.exe` 首次运行弹 SmartScreen"Windows 已保护你的电脑"；执行报"文件不存在/
  拒绝访问"或 exe 凭空消失（被隔离）；Python 脚本运行即被拦截/闪退——多为杀软/EDR 拦截，
  不是脚本故障
- 处置：从杀软隔离区恢复文件 → 将整个 skill 目录加入白名单/排除目录 → 企业环境走 IT 渠道
  报备（说明用途：本地只读解析本机微信聊天库的内部工具）
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

## 按需媒体解析配方（截图与语音）

消息 `localType`：3=图片、34=语音、43=视频。**不需要批量预解析**，按需逐条处理。

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

- 转写依赖：**Python 3.8-3.13** + `sherpa-onnx==2.0.1 numpy`（默认 py3.14 无官方轮子）。
  先探测可用解释器（`py -0p`，或逐个试 `py -3.8 / py -3.12 / py -3.13`），任选一个
  3.8-3.13 装依赖并用它运行脚本。离线安装（随 SKILL 自带 `<skill>/wheels/`，已按
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

## 边界与注意

- 只读管道：不修改微信数据；删消息/防撤回会被 CLI 拒绝，如实转告
- 输出含聊天隐私：向用户汇报给计数与摘要，不整段转贴；导出数据含合同/人员信息，仅限内网
- 语音转文字、桌面弹窗不在 CLI 能力内（Electron 桌面版才有；未部署）
- 微信大版本更新后若捕获失败（DLL 特征变化），优先更新 weflow.exe 与特征，勿用 Hook 方案；
  勘察过程按「反馈机制」写入 feedback/ 供特征表更新
- 分析思路（周热度/事件时间线/多模态互证）：对导出的 JSON 按 `formattedTime` 聚周、
  以共享文件名等关键事件为里程碑骨架、关键词月度漂移看阶段变化
