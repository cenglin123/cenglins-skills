---
name: video-to-srt
description: >-
  下载本地或在线视频，使用 faster-whisper 生成或批量生成 SRT 字幕，再由执行任务的
  Agent 校对 ASR 错误、保护时间轴并按需导出纯文本。遇到视频转字幕、语音转录、
  B站或 URL 视频下载、批量转写、字幕校对或修正、SRT 转 TXT 等任务时使用；支持
  Windows、macOS 和 Linux，并按实际硬件选择 CPU 或 CUDA，不依赖 Ollama。
compatibility: Python 3.10+；ffmpeg/ffprobe；Python 依赖见 requirements.txt。
---

# 视频转字幕工作流

按需执行“下载 → 转写 → Agent 校对 → 结构校验 → 导出文本”，已有本地媒体时从转写开始，已有 SRT 时从校对开始。

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

## 下载媒体

```powershell
python "<SKILL_DIR>\scripts\download.py" <url1> <url2>
```

默认输出到当前工作目录的 `Videos/<上传者>/`。如果当前目录存在 `cookies.txt`，脚本会把它传给 yt-dlp；也可用 `--cookies` 指定。Cookie 是私密凭据：不要读取、打印、提交或打包其内容。

常用参数：

- `--input-file <path>`：从 UTF-8 文本逐行读取 URL。
- `--output-dir <path>`：指定输出目录。
- `--cookies <path>`：指定 Cookie 文件。
- `--with-danmaku`：同时下载弹幕。
- `--audio-only`：只下载并转换音频。

## 生成字幕

单文件：

```powershell
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> -l zh
```

批量处理目录：

```powershell
python "<SKILL_DIR>\scripts\batch_transcribe.py" <media-directory> -l zh
```

默认模型为 `small`，设备为 `auto`：CTranslate2 检测到可用 CUDA 时使用 GPU，否则使用 CPU `int8`。可按实际环境指定：

```powershell
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --device cpu --compute-type int8
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --device cuda --compute-type float16
python "<SKILL_DIR>\scripts\transcribe.py" <media-file> --model <local-model-path>
```

批量脚本递归查找常见媒体文件并跳过已有 SRT；只有用户明确要求重跑时才使用 `--force`。脚本先写临时文件，再原子替换目标 SRT，避免中断留下半成品。

## 由执行任务的 Agent 校对

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

## 导出纯文本

```powershell
python "<SKILL_DIR>\scripts\srt_to_txt.py" <subtitle.srt>
```

## 操作边界

- 所有路径作为独立参数传递，避免把未经检查的路径拼成 shell 命令。
- 默认不覆盖已有下载、字幕或备份；需要覆盖时先确认用户意图。
- 不把 Cookie、模型、虚拟环境、媒体、字幕或其他用户数据写入技能目录。

维护者可运行确定性自测：

```powershell
python "<SKILL_DIR>\scripts\self_test.py"
```
