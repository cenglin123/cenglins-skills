import argparse
import subprocess
import sys
from pathlib import Path

SHORT_LINK_HOSTS = ("b23.tv", "youtu.be")


def working_root() -> Path:
    return Path.cwd()


def resolve_url(url: str) -> str:
    """把 b23.tv / youtu.be 等分享短链还原为网页端 URL；失败时原样返回。"""
    try:
        import requests

        response = requests.get(
            url, allow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
        )
        return response.url
    except Exception:
        pass
    try:
        from urllib.request import Request, urlopen

        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return urlopen(request, timeout=15).geturl()
    except Exception:
        return url


def load_urls(values: list[str], input_file: str | None) -> list[str]:
    urls = list(values)
    if input_file:
        for line in Path(input_file).expanduser().read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                urls.append(value)
    return list(dict.fromkeys(urls))


def platform_args(url: str, quality: str) -> list[str]:
    """按平台给出已验证可用的格式参数；其他平台交给 yt-dlp 默认行为。"""
    args: list[str] = []
    if "youtube.com" in url or "youtu.be" in url:
        # web/ios/tv 客户端常返回 403 或缺格式；android 客户端可用，但需要 JS runtime
        args += ["--js-runtimes", "node", "--extractor-args", "youtube:player_client=android"]
        args += ["-f", "18"]  # 360p 合流，转录够用且稳定
    elif "bilibili.com" in url or "b23.tv" in url:
        if quality == "worst":
            args += ["-f", "worstvideo+worstaudio/worst"]
        else:
            args += ["-f", "bv*+ba/b"]
    elif quality == "worst":
        args += ["-f", "worstvideo+worstaudio/worst"]
    return args


def build_parser() -> argparse.ArgumentParser:
    root = working_root()
    parser = argparse.ArgumentParser(description="使用 yt-dlp 下载视频到项目 Videos 目录")
    parser.add_argument("urls", nargs="*", help="一个或多个视频/合集 URL（支持 b23.tv 等短链）")
    parser.add_argument("--input-file", help="UTF-8 文本，每行一个 URL")
    parser.add_argument("--output-dir", default=str(root / "Videos"))
    parser.add_argument("--cookies", default=str(root / "cookies.txt"))
    parser.add_argument(
        "--quality",
        choices=("auto", "worst"),
        default="auto",
        help="auto=常规可用画质；worst=最低画质（只为转录省流量）",
    )
    parser.add_argument("--no-resolve", action="store_true", help="跳过短链还原")
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

    if args.no_resolve:
        resolved = urls
    else:
        resolved = []
        for url in urls:
            final = resolve_url(url)
            if final != url:
                print(f"短链还原：{url} -> {final}")
            resolved.append(final)

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
    command.extend(platform_args(resolved[0], args.quality))
    if args.with_danmaku:
        command.extend(("--write-subs", "--sub-langs", "danmaku"))
    if not args.force:
        command.append("--no-overwrites")
    command.extend(resolved)

    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
