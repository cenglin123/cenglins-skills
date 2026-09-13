import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


@dataclass(frozen=True)
class Entry:
    index: int
    timestamp: str
    text: str


def parse_srt(path: Path) -> list[Entry]:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not content:
        raise ValueError(f"SRT 为空：{path}")
    entries: list[Entry] = []
    for block_number, block in enumerate(re.split(r"\n{2,}", content), start=1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"第 {block_number} 个字幕块格式错误")
        timestamp = lines[1].strip()
        if not TIMESTAMP.fullmatch(timestamp):
            raise ValueError(f"第 {block_number} 个字幕块时间轴格式错误：{timestamp}")
        text = "\n".join(lines[2:]).strip()
        if not text:
            raise ValueError(f"第 {block_number} 个字幕块正文为空")
        entries.append(Entry(int(lines[0].strip()), timestamp, text))
    return entries


def backup(source: Path, destination: Path | None, force: bool) -> int:
    if not source.is_file():
        raise FileNotFoundError(source)
    parse_srt(source)
    target = destination or source.with_name(source.name + ".bak")
    if target.exists() and not force:
        raise FileExistsError(f"基线已存在：{target}；不会覆盖")
    shutil.copy2(source, target)
    print(f"已建立基线：{target}")
    return 0


def check(original: Path, edited: Path) -> int:
    before = parse_srt(original)
    after = parse_srt(edited)
    problems: list[str] = []
    if len(before) != len(after):
        problems.append(f"条目数变化：{len(before)} -> {len(after)}")
    for position, (left, right) in enumerate(zip(before, after), start=1):
        if left.index != right.index:
            problems.append(f"位置 {position} 序号变化：{left.index} -> {right.index}")
        if left.timestamp != right.timestamp:
            problems.append(f"条目 {left.index} 时间轴变化：{left.timestamp} -> {right.timestamp}")
    if problems:
        for problem in problems:
            print(f"错误：{problem}", file=sys.stderr)
        return 1
    changed = sum(left.text != right.text for left, right in zip(before, after))
    print(f"结构校验通过：{len(after)} 条，正文修改 {changed} 条，序号和时间轴未变化")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为 Agent 字幕校对建立基线并保护 SRT 结构")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="复制 SRT 为 .bak 基线")
    backup_parser.add_argument("source")
    backup_parser.add_argument("--output")
    backup_parser.add_argument("--force", action="store_true")
    check_parser = subparsers.add_parser("check", help="比较校对前后的序号和时间轴")
    check_parser.add_argument("original")
    check_parser.add_argument("edited")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "backup":
            output = Path(args.output).expanduser().resolve() if args.output else None
            return backup(Path(args.source).expanduser().resolve(), output, args.force)
        return check(Path(args.original).expanduser().resolve(), Path(args.edited).expanduser().resolve())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
