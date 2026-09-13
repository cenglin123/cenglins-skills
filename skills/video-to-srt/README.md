# video-to-srt

一个可移植的视频下载、faster-whisper 字幕生成与 Agent 校对工作流。它可以在 Windows、macOS 和 Linux 上按硬件选择 CPU 或 CUDA，不依赖 Ollama。

## 能力

- 用 yt-dlp 下载单个或批量 URL。
- 用 faster-whisper 转写单个媒体文件或递归处理目录。
- 先备份 SRT，再由执行任务的 Agent 校对正文并验证时间轴结构。
- 将 SRT 导出为纯文本。

## 依赖

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`。Python 依赖可安装到调用项目自己的环境中：

```powershell
python -m pip install -r .\requirements.txt
```

CPU 环境建议使用 `--device cpu --compute-type int8`。有可用 NVIDIA CUDA 环境时可使用 `--device cuda --compute-type float16`；默认 `--device auto` 会自动选择。

## 目录

```text
video-to-srt/
├── SKILL.md
├── README.md
├── requirements.txt
├── evals/
│   └── evals.json
└── scripts/
    ├── batch_transcribe.py
    ├── download.py
    ├── self_test.py
    ├── srt_guard.py
    ├── srt_to_txt.py
    └── transcribe.py
```

仓库只保存工作流本身，不包含 Cookie、模型、虚拟环境、视频、字幕或用户数据。
