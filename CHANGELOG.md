# CHANGELOG

本仓库是面向 Agent Skills 开放格式的技能集合（`skills/<name>/SKILL.md`，兼容 CC Switch、Claude Code、
Codex、opencode 等）。**2026-11-24 重建了 Git 历史**：旧历史整体丢弃、不保留任何提交，`master` 从一次
初始提交开始；本文件是仓库唯一的开发过程记录。

## 发展过程

### 2026-07 · 起步：编码规范守卫

- 初始化仓库，加入 `utf8-guard`、`powershell-guard`（Windows 中文环境的编码与 PowerShell 陷阱）
- 新增 `crlf-guard`（Git 行尾符 CRLF/LF），`.gitattributes` 统一 LF
- 副本同步从符号链接改为拷贝（`sync.ps1`）：agent 写文件是"先删后建"，会破坏软链

### 2026-08 · 知乎抓取与 OCSR

- 规范为通用 `skills/<name>/` 结构，加入知乎抓取技能
- 纳入 OCSR（多模型子代理调度），随后迁出为独立仓；调整模型池与 watcher 失败语义
- 新增 `winx-wt-takeover`（Win+X 接管 Windows Terminal / 控制台交接排障）

### 2026-09 · 视频转字幕与评论采集

- 新增 `video-to-srt`（B 站/YouTube 下载 → faster-whisper 转写）
- 新增 `bilibili-comment-extractor`
- 知乎回答抓取：修正懒加载与停止判定；全量枚举 + 分层随机抽样 + 立场加权估计

### 2026-10 · Foxmail、部署通道与知乎合并

- 新增 `foxmail-grep`（Foxmail 本地邮件免密检索、附件提取）
- 知乎相关三个技能合并为 `zhihu-auto`（分手册）+ 统一登录态；多轮安全加固与缺陷修复
- 新增 `push-to-server`（git bundle 安全部署，预检拦截 + 备份回滚 + 运行时数据保护）
- 新增 `win-pup-forensics-cleanup`（Windows 捆绑软件取证与清理）
- `push-to-server` 扩展：共享 /srv 服务器接入、面板 HTTPS、邀请码登记、项目注释边界

### 2026-11 · 微信数据管道与多轮收敛

- 新增 `wechat-grep`（微信 4.x 聊天检索/导出/多模态解析，全程只读），并加服务端反馈收件箱条款
- `win-pup-forensics-cleanup` 按实战案卷补文档与采集器，迭代到 1.0.4
- `wechat-grep` 增加自引导首次使用（`check-install.py`），支持 4.1.15.x / 4.2.0.53
- `wechat-grep` 增加 `print_chat.py`：发送者与方向机械读取（不靠语气推断）、文件附件按 MD5
  内容校验定位（同名多版本不会指错）、被引用与紧随消息提示；经两轮独立复审收敛到 1.0.4
- `zhihu-auto` 正文图片留痕与 `--images` 下载