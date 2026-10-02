---
name: foxmail-grep
description: >-
  Search and read local Foxmail 7.2 mail (subject/body/senders/attachment
  names) and extract attachment files, without opening Foxmail or needing a
  password. Use when the user asks to read, search, summarize, triage, or
  organize emails from their local Foxmail mailbox, or to extract mail
  attachments. Supports incremental new-mail fetch via POP3. Windows only.
---

# foxmail-grep

Foxmail 7.2 本地邮件的 grep 式检索工具。免密码、免启动 Foxmail、全程只读。

工具位置：`%USERPROFILE%\.agents\skills\foxmail-grep\`，自包含内置 Python 运行时（`runtime\`），
统一通过 `fg.cmd` 调用——**无需系统安装 Python**，中文输出无乱码。
`runtime\` 缺失时 fg.cmd 自动下载安装嵌入式 Python（华为云镜像优先、官方源回退、SHA256 校验），
离线环境才会失败——此时用分发包 `foxmail-grep-dist.zip`（自含运行时）。

## 运行环境（重要）

- **cmd.exe**：直接 `fg.cmd <命令>`
- **git-bash / MSYS shell**（agent 常见环境）：`fg.cmd` 不能直接执行，用
  `cmd //c "fg.cmd <命令>"`，或等效的 `runtime/python.exe foxgrep.py <命令>`
  （bash 下先 `export PYTHONIOENCODING=utf-8` 防中文乱码）
- bash 中**不要用 `%TEMP%` 和反斜杠路径**（`%VAR%` 不展开、`\` 会被吃掉）；
  `--out` 等路径参数用正斜杠绝对路径，如 `--out C:/Users/.../out`

## 命令速查

```cmd
cd /d "%USERPROFILE%\.agents\skills\foxmail-grep"

fg.cmd list                 # 邮件列表
fg.cmd grep <关键词> [-i]   # 全文检索（主题+正文+收发件人+附件名）
fg.cmd show <编号>          # 邮件全文
fg.cmd attach               # 附件清单
fg.cmd extract <编号> [--out 目录]   # 提取附件（本地优先，无副本自动从服务器拉取）
fg.cmd fetch                # POP3 拉取服务器新邮件进 corpus（只读不删）
```

所有子命令支持 `--json`（置于子命令前）输出结构化 JSON；`--live` 切换实时索引模式（见下）。
若设备已有 Python 3，也可直接 `python foxgrep.py ...`（等效）。

## Foxmail 6.x 备份扫描（--v6）

```cmd
fg.cmd --v6 <Foxmail6根目录> list
fg.cmd --v6 <Foxmail6根目录> grep <关键词>
```

- 指向含 `mail\<账号>\*.BOX` 的目录（安装根或 mail 目录均可），**只读扫描**，适合备份盘
- 首次全量扫描建立 sqlite 缓存（`v6cache\`，约 90 秒/7.5GB 参考），之后秒开
- 邮件按日期统一编号，列表条目带 `[账号/文件夹]` 标签
- 附件可正常 `extract`（从 BOX 原位读取 MIME 数据）
- 注意：BOX 扫描会包含已删除但未压缩清理的邮件（trash/spam 文件夹也在其中）
- `v6cache\` 是本地可再生缓存，**不参与分发**

## 两种数据模式（按需选择，不要混用编号）

| 模式 | 数据源 | 特点 |
|---|---|---|
| `--live` | Foxmail 搜索索引直读 | 新邮件收进 Foxmail 即可见；正文/主题/收发件人/附件名；附件内容在语料库有匹配副本时也可提取 |
| 默认 | `corpus/*.eml` | 完整 MIME，附件内容直接 `extract`；覆盖范围取决于导出时间 |

两模式编号体系不同：默认模式按时间重排 1..N，`--live` 用 Foxmail 内部 mailid。

## 典型用法

- "最近有什么邮件 / 查看最近 N 封" → `--live list`：**输出按时间升序，最新邮件在列表末尾**；
  取末尾 N 行的编号，逐条 `--live show <编号>` 读正文。不要猜测编号含义。
- "找关于 X 的邮件" → `--live grep X`（命中后按编号 `--live show` 读全文）
- "把那封邮件的附件给我" → 若 `--live show` 显示附件 → `--live extract <编号>`；
  提取失败时按「边界」节的提取边界处理，**不要自行升级手段**
- "整理/总结邮箱内容" → `--json` 拿结构化数据自行加工

## 版本适配矩阵

| Foxmail 版本 | 数据结构 | 支持方式 |
|---|---|---|
| 7.2 | `Storage\<账号>\Mails\Index` (FXIS) + Indexes/ | `--live` 实时索引 ✓ |
| 7.0 / 7.1 | 扁平 `Data\Mails\Index`（FMStorage.list 指示，两级桶 `Mails\<mid%32>\<(mid//32)%32>\<mid>`） | `--live` 自动识别 ✓（7.2.25.432 程序 + 扁平数据样本实测，74,805 封） |
| 5.x / 6.x (5.0、6.5 实测) | `mail\<账号>\*.BOX/.IND` | `--v6` BOX 分隔符扫描 ✓（不依赖 IND；5.x 与 6.x 分隔符相同） |

## 遇到不支持的版本/解析失败时

参考 `refs/research-methodology.md` 的研究方法勘察当前版本的数据结构
（版本确认 → 目录勘察 → 魔数识别 → 数据优先/索引绕行 → 小样本验证 → 全量回归），
按 `refs/research-report-template.md` 产出研究报告，连同 foxgrep.py 的 unified diff
写入 `feedback/YYYYMMDD-<版本号>-<简述>/`（约定见 `feedback/README.md`）反馈上游维护者。
研究原则：数据文件优先、索引最后考虑；先实证后假设；不破解加密、不写入用户数据。

## 边界

- 全程只读，不修改 Foxmail 数据；邮件内容属用户隐私，不外发、不入库。
- `fetch` 的服务器/账号/凭据自动从 Foxmail 本地配置解析，零配置；若 Foxmail 设为"收后即删"，服务器邮件停留短暂。

### 附件提取链路（extract 失败时按此处理）

`extract` 的取数链（自动逐级兜底，全程脚本化）：

1. 语料库 .eml（默认模式直接有附件数据）
2. `--v6` BOX 原位读取
3. `--live` 按大小匹配语料库副本
4. **Foxmail 渲染缓存收割**：邮件被打开/预览过时，附件已被 Foxmail 自行解密到
   `%APPDATA%\Roaming\Foxmail7\Temp-*\Attach\`，extract 自动收割
5. POP3 服务器按主题+日期匹配拉取（收后即删场景可能为空）

**全链路失败时**（报"提取失败"）：说明该邮件从未被渲染过。按 `refs/render-harvest-recipe.md`
的「渲染收割」配方操作——用 cua-driver 在 Foxmail 搜索框定位该邮件并**单击**（触发渲染=
附件自动进缓存），然后重试 `extract`。这是已验证的自动化路径，2~3 个 GUI 动作。

**GUI 操作纪律**：只执行渲染收割配方的最小动作集，不做配方外的任何窗口操作。
