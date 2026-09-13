import argparse
import re
import sys
from pathlib import Path


def collect_srt(inputs: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for value in inputs or ["Videos"]:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".srt":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.srt") if recursive else path.glob("*.srt"))
        else:
            raise FileNotFoundError(path)
    return sorted(set(files), key=lambda item: str(item).casefold())


def extract_text(path: Path, join_lines: bool) -> str:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    paragraphs: list[str] = []
    for number, block in enumerate(re.split(r"\n{2,}", content), start=1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit() or " --> " not in lines[1]:
            raise ValueError(f"{path} 的第 {number} 个字幕块格式错误")
        text = "".join(line.strip() for line in lines[2:]) if join_lines else "\n".join(lines[2:]).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="移除 SRT 序号和时间轴并导出纯文本")
    parser.add_argument("inputs", nargs="*", help="SRT 文件或目录；默认 Videos")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--join-lines", action="store_true")
    parser.add_argument("-f", "--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        files = [path for path in collect_srt(args.inputs, not args.no_recursive) if not path.name.endswith(".srt.bak")]
        for source in files:
            output = source.with_suffix(".txt")
            if output.exists() and not args.force:
                print(f"跳过已有文件：{output}")
                continue
            output.write_text(extract_text(source, args.join_lines), encoding="utf-8", newline="\n")
            print(f"已导出：{output}")
        print(f"处理完成：{len(files)} 个 SRT")
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
