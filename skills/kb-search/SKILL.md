---
name: kb-search
description: >-
  检索公司知识库（只读）：查制度文档、看部门制度目录与同义词表，或设备连接知识库后需要查内容时使用。
  从本机配置读取知识库地址与只读密钥，调用检索 API 取回真实结果；总结、解释等生成类问题由
  agent 自身的 LLM 基于检索结果作答。触发场景：用户或 agent 要检索公司知识库、查制度文档、
  连接知识库后查内容。
---

# 公司知识库检索（kb-search）

对已「连接知识库」的设备，用只读密钥调用知识库检索接口，取回真实检索结果供 agent 作答。全程只读：只调检索类接口，不调问答、研究或管理接口。

## 第 1 步：读取连接配置

按以下顺序取 `url` 与 `api_key`，取到即停：

1. **配置文件（优先）**：`%LOCALAPPDATA%\OpsClient\kb-agent.json`。JSON，按 UTF-8 读取，字段：
   - `url`：知识库地址；
   - `api_key`：`ak-` 开头的只读设备密钥。
2. **环境变量（兜底）**：`OPS_KB_URL`、`OPS_KB_API_KEY`。
3. **两者都没有**：停止检索。提示用户先在智能体助手里「连接知识库」，连接成功（连接后配置会自动写入上面位置）再重试本技能。

读取到的值只放进变量和请求头，不要把 `api_key` 打印到终端、日志或对话里。

## 第 2 步：调用检索接口

### 检索（主接口）

`GET {url}/api/search?q=<关键词>&limit=10`，请求头 `Authorization: Bearer <api_key>`：

- `q` 至少 2 个字符，单个字符返回 422；关键词只有一个字时先扩展（补字、换词）再查；
- `limit` 最大 30，默认 10；
- 返回 JSON `{total, results}`：`total` 是本次返回的条数（不超过 `limit`），不是命中总数；需要更多结果就把 `limit` 调大（最大 30）或换更精确的关键词。

curl 示例（`{url}` 与 `<api_key>` 用第 1 步读到的实际值代入，不回显）：

```bash
curl -sG "{url}/api/search" \
  -H "Authorization: Bearer <api_key>" \
  --data-urlencode "q=<关键词>" \
  --data-urlencode "limit=10"
```

PowerShell 示例（配置文件直接读进变量，密钥不经回显）：

```powershell
$cfg = Get-Content "$env:LOCALAPPDATA\OpsClient\kb-agent.json" -Encoding UTF8 -Raw | ConvertFrom-Json
$q = [uri]::EscapeDataString("制度关键词")
Invoke-RestMethod -Uri "$($cfg.url)/api/search?q=$q&limit=10" `
  -Headers @{ Authorization = "Bearer $($cfg.api_key)" }
```

### 目录与同义词

- `GET {url}/api/docs`：部门制度目录。检索前先看目录，可确定要查的制度归属哪个部门。
- `GET {url}/api/synonyms`：同义词表。命中过少时按同义词扩展关键词再查。

## 边界（必须遵守）

- **只读检索**。`/api/chat`、`/api/research`、`/api/admin/*`、`/api/user/*` 等接口对只读密钥一律 403：不要调用，也不要尝试绕过。
- **唯一的写放行是 `POST /api/agent-feedback`**（检索质量反馈上报），且仍受密级约束：反馈内容不得带入密级信息。
- **生成类问题不调问答接口**：总结、对比、解释、续写等，用 agent 自己的 LLM 基于检索结果作答，并注明来源条目。

## 保密

- `api_key` 只在本机的请求头里使用：不打印、不写入日志或对话、不提交到任何文件或仓库。
- 请求返回 **401** 说明密钥已被吊销（设备退役、换人等场景服务端会自动吊销）。告知用户密钥已失效，请其在智能体助手里重新「连接知识库」获取新密钥；不要拿旧密钥反复重试。
