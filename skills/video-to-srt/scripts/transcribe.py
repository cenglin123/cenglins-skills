import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_MODEL = "small"
DEFAULT_MODEL_DIR = Path(
    os.environ.get("VIDEO_TO_SRT_MODEL_DIR", Path.home() / ".cache" / "video-to-srt" / "models")
)


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def get_duration(video_path: Path) -> float:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("未找到 ffprobe；请先安装 ffmpeg 并加入 PATH")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{result.stderr.strip() or '未知错误'}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法解析视频时长：{exc}") from exc


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def transcribe(args: argparse.Namespace) -> Path:
    try:
        from faster_whisper import WhisperModel
        from tqdm import tqdm
    except ImportError as exc:
        raise RuntimeError(
            "缺少转写依赖；请在项目虚拟环境中安装 requirements.txt"
        ) from exc

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"视频文件不存在：{video}")

    output = Path(args.output).expanduser().resolve() if args.output else video.with_suffix(".srt")
    if output.exists() and not args.force:
        raise FileExistsError(f"输出已存在：{output}；使用 --force 覆盖")
    output.parent.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    requested_device = args.device
    device = resolve_device(requested_device)
    compute_type = args.compute_type or ("float16" if device == "cuda" else "int8")
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)

    print(f"视频：{video}")
    print(f"时长：{format_timestamp(duration)}")
    print(f"模型：{args.model}；设备：{device}；计算类型：{compute_type}")

    try:
        model = WhisperModel(
            args.model,
            device=device,
            compute_type=compute_type,
            download_root=str(model_dir),
        )
    except Exception as exc:
        if requested_device == "auto" and device == "cuda":
            print(f"CUDA 初始化失败，回退到 CPU：{exc}", file=sys.stderr)
            device = "cpu"
            compute_type = args.compute_type or "int8"
            model = WhisperModel(
                args.model,
                device=device,
                compute_type=compute_type,
                download_root=str(model_dir),
            )
        else:
            raise

    language = None if args.language in (None, "", "auto") else args.language
    segments, info = model.transcribe(
        str(video),
        language=language,
        beam_size=args.beam_size,
        vad_filter=not args.no_vad,
        word_timestamps=False,
    )
    print(f"检测语言：{info.language}（置信度 {info.language_probability:.1%}）")

    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            with tqdm(total=duration, unit="s", desc="转写进度") as progress:
                for index, segment in enumerate(segments, start=1):
                    text = segment.text.strip()
                    if not text:
                        continue
                    handle.write(
                        f"{index}\n{format_timestamp(segment.start)} --> "
                        f"{format_timestamp(segment.end)}\n{text}\n\n"
                    )
                    progress.update(max(0.0, min(segment.end, duration) - progress.n))
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    print(f"SRT 已保存：{output}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 faster-whisper 将视频转写为 SRT")
    parser.add_argument("video", help="输入视频文件")
    parser.add_argument("-o", "--output", help="输出 SRT；默认与视频同名")
    parser.add_argument("-l", "--language", default="auto", help="语言代码；默认 auto")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="模型名称；默认 small")
    parser.add_argument("-d", "--device", choices=("cpu", "cuda", "auto"), default="auto")
    parser.add_argument("--compute-type", help="CTranslate2 计算类型；默认按设备选择")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR), help="模型缓存目录")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="关闭 VAD 过滤")
    parser.add_argument("-f", "--force", action="store_true", help="覆盖已存在的输出")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        transcribe(args)
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
