---
name: video-to-srt
description: >-
  还原 B 站/YouTube 等平台分享短链并下载视频，使用 faster-whisper 转录为 SRT 字幕
  （支持批量、GPU/CPU 自动选择、环境自动发现），可做多模型交叉验证、本地多模型
  集成校对、Agent 校对、高可信双语字幕，并按需导出纯文本。遇到视频转字幕、语音
  转录、B 站或 URL 视频下载、b23.tv/BV 链接还原、批量转写、字幕校对或修正、
  多模型交叉验证、双语字幕、SRT 转 TXT 等任务时使用；支持 Windows、macOS 和
  Linux，按实际硬件选择 CPU 或 CUDA，不强制依赖 Ollama。
compatibility: Python 3.10+；ffmpeg/ffprobe；Python 依赖见 requirements.txt；可选 ollama（本地多模型校对）、Node.js（YouTube 下载）、FunASR（交叉验证）。
---

# 视频转字幕工作流

按需执行「还原链接 → 下载 → 转写 → 校对/交叉验证 → 结构校验 → 双语/导出」。已有本地媒体时从转写开始，已有 SRT 时从校对开始。

## 准备环境

将本技能目录记为 `<SKILL_DIR>`。优先使用项目已有且能导入 `faster_whisper` 的 Python 环境，不要擅自替换或删除现有虚拟环境。

```powershell
Get-Command python, ffmpeg, ffprobe -ErrorAction SilentlyContinue
python -c "import faster_whisper; print(faster_whisper.__version__)"
$env:PYTHONUTF8 = '1'
```

仅在用户要求安装依赖或当前任务已授权配置环境时运行：

```powershell
python -m pip install -r "<SKILL_DIR>\requirements.txt"
```

可选增强（按需安装，不进 requirements）：`funasr` 用于 SenseVoice 交叉验证；本机 `ollama` 用于本地多模型校对；`node` 用于 YouTube 下载。

### 环境自动发现

`transcribe.py` 启动时会自动寻找可用的 faster-whisper 环境：优先 GPU（且会**实际加载 tiny 模型**验证 CUDA/cuDNN 真正可用，避免只看 `torch.cuda.is_available()`），回退 CPU，最后才用当前解释器；切换解释器时透明重启自身并注入该环境的 CUDA DLL 路径。

扫描范围：环境变量 `VIDEO_TO_SRT_VENV` 指定的 venv（最高优先级）；当前目录 `.venv`/`venv`；`~/.venvs/faster-whisper`；以及常见项目根（如 `C:\Project`、`%USERPROFILE%\Project*`）下深度 ≤3 层的 venv。

相关环境变量：

| 变量 | 作用 |
|---|---|
| `VIDEO_TO_SRT_VENV` | 强制使用指定 venv；不可用时警告并回退当前解释器 |
| `VIDEO_TO_SRT_MODEL_DIR` | 模型下载目录（优先级低于 `--model-dir`） |
| `VIDEO_TO_SRT_SKIP_DISCOVERY` | 设为 `1` 跳过自动发现，直接用当前解释器 |

## 下载媒体

```powershell
python "<SKILL_DIR>\scripts\download.py" <url1> <url2>
```

默认输出到当前工作目录的 `Videos/<上传者>/`。`b23.tv`、`youtu.be` 等分享短链会自动还原为网页端 URL 再下载（`--no-resolve` 关闭）。如果当前目录存在 `cookies.txt`，脚本会把它传给 yt-dlp；也可用 `--cookies` 指定。Cookie 是私密凭据：不要读取、打印、提交或打包其内容。

常用参数：

- `--input-file <path>`：从 UTF-8 文本逐行读取 URL。
- `--output-dir <path>`：指定输出目录。
- `--cookies <path>`：指定 Cookie 文件（B 站会员清晰度/充电视频需要登录态）。
- `--quality worst`：最低画质，只为转录省流量。
- `--audio-only`：只下载音频。
- `--with-danmaku`：同时下载弹幕。

平台说明（脚本已内置，无需手写）：

- **B 站**：`-f "bv*+ba/b"` 合流 mp4；免费清晰度一般到 720P，转录够用即可。
- **YouTube**：自动加 `--js-runtimes node --extractor-args "youtube:player_client=android"`（默认 web/ios/tv 客户端常返回 403 或缺格式），`-f 18`（360p 合流）。需要本机装有 Node.js。

## 生成字幕

单文件：

```powershell
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> -l zh
```

批量处理目录：

```powershell
python "<SKILL_DIR>\scripts\batch_transcribe.py" <media-directory> -l zh
```

默认模型 `turbo`，设备 `auto`（结合环境探测与 CTranslate2 检测；CUDA 加载失败自动回退 CPU `int8`）。可按实际环境指定：

```powershell
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --device cpu --compute-type int8
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --device cuda --compute-type float16
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --model <local-model-path>
```

语言判断：已知中文视频直接 `-l zh`；未知语言用默认 `auto`。模型目录优先级：`--model-dir` > `VIDEO_TO_SRT_MODEL_DIR` > 自动发现环境旁的 `models` 目录 > `~/.cache/video-to-srt/models`。首次使用会从 Hugging Face 下载模型。

批量脚本递归查找常见媒体文件并跳过已有 SRT；只有用户明确要求重跑时才使用 `--force`。脚本先写临时文件，再原子替换目标 SRT，避免中断留下半成品；非交互运行时按 5% 步进汇报进度。

## 由执行任务的 Agent 校对（默认路径）

校对由当前执行任务的 Agent 结合上下文完成，不要求部署 Ollama 或其他本地语言模型。

修改前创建备份：

```powershell
python "<SKILL_DIR>\scripts\srt_guard.py" backup <subtitle.srt>
```

逐段检查：

- 人名、机构名、产品名、型号、英文缩写和专业术语。
- 数字、单位、同音误识别、漏字、多字和跨段一致性。
- 保留原意、事实与自然口语，不把字幕改写成文章。
- 保留序号、时间轴、段落数量与顺序；只修改字幕正文。

完成后验证结构：

```powershell
python "<SKILL_DIR>\scripts\srt_guard.py" check <subtitle.srt>.bak <subtitle.srt>
```

长字幕可以分段校对，但由同一个执行 Agent 汇总、复核术语一致性并完成最终结构校验。

## 可选：本地多模型集成校对（有 GPU + ollama 时）

有 GPU（≥16 GB 空闲显存）且本机 `ollama` 可用时，`scripts/proofread_multimodel.py` 提供远快于子代理的本地集成校对：每个 batch 由 `MODELS` 中多个模型并行交叉校对（并集去重），冲突时由 `ARBITER_MODEL` 带上下文裁定；多轮迭代收敛（默认最多 3 轮）且早期修正冻结防翻烧饼；内置 markdown 剥离、`[N]` 前缀剥离、装饰性修改与幻觉改写过滤；按空闲显存自动裁剪模型。

使用前在脚本顶部配置：`BASE`（默认当前工作目录）、`TARGETS`（必填，BASE 下的子目录/频道名）、`SYSTEM_PROMPT`（保持简短聚焦，只举 2-3 类典型错误 + 输出 schema，长规则列表会让模型直接返回 `[]`）、`SAFE_RULES`（硬编码模型反复漏掉的修正）。

关键坑：

- qwen 系 thinking 会静默吃光 `num_ctx`（`done_reason: length`、空 content）；脚本已用 API 顶层 `"think": false` 禁用，不要只靠 `/no_think`。
- 绝不要 `Stop-Process -Force` 杀 ollama——会产生孤儿 `llama-server` 每个永久占数 GB 显存；用 `ollama stop <model>`，跑前 `ollama ps` 检查驻留模型。
- LLM 校对非确定性：跑完后必须用关键词扫描验证残留。

## 多模型交叉验证（重要视频的质量把关）

单一 ASR 会有非确定性幻觉/坍缩。重要视频用多个模型跑同一音频再对比：

```powershell
python "<SKILL_DIR>\scripts\transcribe.py" <video> -o xv_turbo.srt -l en -f
python "<SKILL_DIR>\scripts\transcribe.py" <video> -o xv_large-v3.srt -l en -m large-v3 -f
python "<SKILL_DIR>\scripts\transcribe.py" <video> -o xv_small.srt -l en -m small -f
python "<SKILL_DIR>\scripts\compare_srt.py" <dir-with-srts> --bin 10
```

`compare_srt.py` 按时间窗对比多份 SRT（±1 窗容差），也支持把 `.txt`（如 SenseVoice 整段输出）做全文相似度对比。

裁决要点：

- **语言路由差异 ≠ 错误**：中英叠加音轨（原声+口译/配音）会让不同模型各抓一种语言，先判断是「路由」还是「真错」。
- 专名/数字至少 2-3 个模型一致才采信；Whisper 系全体出错时看 SenseVoice。
- 已知坑：`turbo` 易重复坍缩；`large-v3` 英文最全但个别段落凭空幻觉；`small` 弱、易漏整段；SenseVoice 中英混合最稳但无精确时间轴。

SenseVoice（可选，需 `pip install funasr`）：

```python
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
m = AutoModel(model="FunAudioLLM/SenseVoiceSmall", vad_model="fsmn-vad", hub="hf", disable_update=True)
res = m.generate(input="audio16k.wav", cache={}, language="auto", use_itn=True,
                 batch_size_s=60, merge_vad=True, merge_length_s=15)
text = "\n".join(rich_transcription_postprocess(r["text"]) for r in res)
```

注意 `hub="hf"`（默认 ModelScope 在部分网络会卡死）；音频用 `ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 audio16k.wav` 预处理。

## 高可信双语字幕

1. 以 large-v3 时间轴为骨架（通常最完整），先删重复段与幻觉段。
2. 中文优先用视频自带的口译/配音原文（在 ASR 输出里找对应中文），其余才翻译。
3. 关键术语按交叉验证统一（人名、机构、产品名）。
4. 用 `scripts/build_bilingual.py`（读取 `[{"start","end","en","cn"}, ...]` JSON，时间可用秒或 `HH:MM:SS,mmm`）生成 EN+CN 两行 SRT，支持 `--cn-only`/`--en-only`。
5. 输出后校验：段号/时间轴单调递增、每段行数一致、错误关键词 0 命中、关键术语全命中。

翻译类字幕的时间轴若错位，以原始语言 SRT 为准重新对齐。

## 导出纯文本

```powershell
python "<SKILL_DIR>\scripts\srt_to_txt.py" <subtitle.srt>
```

## 操作边界

- 所有路径作为独立参数传递，避免把未经检查的路径拼成 shell 命令。
- 默认不覆盖已有下载、字幕或备份；需要覆盖时先确认用户意图。
- 不把 Cookie、模型、虚拟环境、媒体、字幕或其他用户数据写入技能目录。
- 校对前务必备份原文件（`.bak`），保留 diff 基准；替换规则一次性加全，避免反复回滚。

维护者可运行确定性自测：

```powershell
python "<SKILL_DIR>\scripts\self_test.py"
```
