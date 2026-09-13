import argparse
import subprocess
import sys
from pathlib import Path


def working_root() -> Path:
    return Path.cwd()


def load_urls(values: list[str], input_file: str | None) -> list[str]:
    urls = list(values)
    if input_file:
        for line in Path(input_file).expanduser().read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                urls.append(value)
    return list(dict.fromkeys(urls))


def build_parser() -> argparse.ArgumentParser:
    root = working_root()
    parser = argparse.ArgumentParser(description="使用 yt-dlp 下载视频到项目 Videos 目录")
    parser.add_argument("urls", nargs="*", help="一个或多个视频/合集 URL")
    parser.add_argument("--input-file", help="UTF-8 文本，每行一个 URL")
    parser.add_argument("--output-dir", default=str(root / "Videos"))
    parser.add_argument("--cookies", default=str(root / "cookies.txt"))
    parser.add_argument("--with-danmaku", action="store_true", help="尝试下载 B 站弹幕字幕")
    parser.add_argument("--audio-only", action="store_true", help="只下载音频")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有下载")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    urls = load_urls(args.urls, args.input_file)
    if not urls:
        print("请提供 URL 或 --input-file", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--windows-filenames",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--console-title",
        "-o",
        str(output_dir / "%(uploader)s" / "%(title)s [%(id)s].%(ext)s"),
    ]

    cookies = Path(args.cookies).expanduser().resolve()
    if cookies.is_file():
        command.extend(("--cookies", str(cookies)))
    else:
        print(f"提示：Cookie 文件不存在，将尝试匿名下载：{cookies}")

    if args.audio_only:
        command.extend(("-x", "--audio-format", "m4a"))
    else:
        command.extend(("--merge-output-format", "mp4"))
    if args.with_danmaku:
        command.extend(("--write-subs", "--sub-langs", "danmaku"))
    if not args.force:
        command.append("--no-overwrites")
    command.extend(urls)

    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
