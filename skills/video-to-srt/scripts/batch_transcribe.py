import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")


def collect_videos(root: Path, recursive: bool, extensions: set[str]) -> list[Path]:
    iterator = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        (path for path in iterator if path.is_file() and path.suffix.lower() in extensions),
        key=lambda path: str(path).casefold(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量调用 transcribe.py 生成 SRT")
    parser.add_argument("directory", nargs="?", default="Videos", help="视频目录；默认 Videos")
    parser.add_argument("-l", "--language", default="auto")
    parser.add_argument("-m", "--model", default="small")
    parser.add_argument("-d", "--device", choices=("cpu", "cuda", "auto"), default="auto")
    parser.add_argument("--compute-type")
    parser.add_argument("--extensions", default=",".join(DEFAULT_EXTENSIONS))
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("-f", "--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1

    extensions = {
        item.strip().lower() if item.strip().startswith(".") else "." + item.strip().lower()
        for item in args.extensions.split(",")
        if item.strip()
    }
    videos = collect_videos(root, not args.no_recursive, extensions)
    script = Path(__file__).with_name("transcribe.py")
    print(f"找到 {len(videos)} 个视频")

    failed: list[Path] = []
    skipped = 0
    for index, video in enumerate(videos, start=1):
        output = video.with_suffix(".srt")
        if output.exists() and not args.force:
            skipped += 1
            print(f"[{index}/{len(videos)}] 跳过已有字幕：{video.name}")
            continue

        command = [
            sys.executable,
            str(script),
            str(video),
            "--output",
            str(output),
            "--language",
            args.language,
            "--model",
            args.model,
            "--device",
            args.device,
        ]
        if args.compute_type:
            command.extend(("--compute-type", args.compute_type))
        if args.force:
            command.append("--force")

        print(f"[{index}/{len(videos)}] 转写：{video.name}")
        if subprocess.run(command).returncode != 0:
            failed.append(video)

    print(f"完成：成功 {len(videos) - skipped - len(failed)}，跳过 {skipped}，失败 {len(failed)}")
    if failed:
        for path in failed:
            print(f"失败：{path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
