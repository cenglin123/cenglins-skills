# video-to-srt

一个可移植的视频下载、faster-whisper 字幕生成与多模型校对工作流。它可以在 Windows、macOS 和 Linux 上按硬件选择 CPU 或 CUDA，自动发现可用的 faster-whisper 环境，不强制依赖 Ollama。

## 能力

- 还原 `b23.tv` / `youtu.be` 等分享短链，用 yt-dlp 下载单个或批量 URL，按平台自动选择已验证可用的格式参数。
- 用 faster-whisper 转写单个媒体文件或递归处理目录；启动时自动发现 GPU 可用的 Python 环境（实际加载 tiny 模型验证 CUDA/cuDNN）。
- 先备份 SRT，再由执行任务的 Agent 校对正文并验证时间轴结构（`srt_guard`）。
- 可选：本地 ollama 多模型集成校对（`proofread_multimodel`，双模型并集 + 仲裁 + 多轮收敛）。
- 多模型交叉验证：多份 SRT 按时间窗对比，报告低一致窗口（`compare_srt`）。
- 生成 EN+CN 高可信双语字幕（`build_bilingual`）。
- 将 SRT 导出为纯文本。

## 依赖

需要 Python 3.10+、`ffmpeg` 和 `ffprobe`。Python 依赖可安装到调用项目自己的环境中：

```powershell
python -m pip install -r .\requirements.txt
```

CPU 环境建议使用 `--device cpu --compute-type int8`。有可用 NVIDIA CUDA 环境时可使用 `--device cuda --compute-type float16`；默认 `--device auto` 会结合环境探测自动选择。

可选增强：`funasr`（SenseVoice 交叉验证）、本机 `ollama`（本地多模型校对，建议 ≥16 GB 空闲显存）、`node`（YouTube 下载的 JS runtime）。

## 目录

```text
video-to-srt/
├── SKILL.md
├── README.md
├── requirements.txt
├── agents/
│   └── openai.yaml
├── evals/
│   └── evals.json
└── scripts/
    ├── batch_transcribe.py
    ├── build_bilingual.py
    ├── compare_srt.py
    ├── download.py
    ├── proofread_multimodel.py
    ├── self_test.py
    ├── srt_guard.py
    ├── srt_to_txt.py
    └── transcribe.py
```

仓库只保存工作流本身，不包含 Cookie、模型、虚拟环境、视频、字幕或用户数据。
