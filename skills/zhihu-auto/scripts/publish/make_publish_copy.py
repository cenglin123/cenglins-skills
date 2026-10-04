#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_publish_copy.py — 生成知乎「导入文档」用的发布副本。

知乎导入会把 YAML frontmatter 当正文、把库内 [[wikilink]] 当死链、且标题要另填，
所以不能直接上传知识库原件。本脚本把原件做三件事，落到一个独立副本：

  1. 删掉文件首部的 YAML frontmatter（原件必须保留它做溯源）
  2. 删掉正文首个 H1（知乎标题另填，留着会重复）
  3. [[wikilink]] / ![[embed]] → 纯文本（优先取 | 后的别名，否则取 / 后的文件名）

用法:
    python make_publish_copy.py <source.md>
    python make_publish_copy.py <source.md> -o <out.md>
    python make_publish_copy.py <source.md> --out-dir <dir>
    python make_publish_copy.py <source.md> --json        # 只输出机器可读报告

默认输出到源文件同目录，文件名 <stem>.zhihu-publish.md。
发布到知乎时，副本必须落在 playwright MCP 允许的根内（本机是知识库根与 .playwright-mcp/），
系统 %TEMP% 会被拒绝（File access denied ... outside allowed roots）。
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n?", re.DOTALL)
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]]+?)\]\]")
H1_RE = re.compile(r"^[ \t]*#[ \t]+.*$", re.MULTILINE)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_ctrl(s: str) -> str:
    """剥离 C0/C1/DEL 控制字符（保留 tab/LF/CR），供终端回显前使用。"""
    return _CTRL_RE.sub("", str(s))


def strip_frontmatter(text: str) -> tuple[str, bool]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, False
    return text[m.end():], True


def wikilink_to_text(inner: str) -> str:
    """把 [[target|alias]] 的 inner 部分转成展示文本。"""
    if "|" in inner:
        target, alias = inner.split("|", 1)
    else:
        target, alias = inner, None
    target = target.split("#", 1)[0].split("^", 1)[0].strip()
    if alias is None:
        alias = target.split("/")[-1]
        if alias.lower().endswith(".md"):
            alias = alias[:-3]
    return alias.strip()


def convert_wikilinks(body: str) -> tuple[str, int, int]:
    """返回 (转换后正文, 已转换链接数, embed 数)。"""
    stats = {"converted": 0, "embeds": 0}

    def repl(m: re.Match) -> str:
        stats["converted"] += 1
        if m.group(1) == "!":
            stats["embeds"] += 1
        return wikilink_to_text(m.group(2))

    converted = WIKILINK_RE.sub(repl, body)
    return converted, stats["converted"], stats["embeds"]


def remove_first_h1(body: str) -> tuple[str, str | None]:
    m = H1_RE.search(body)
    if not m:
        return body, None
    line = m.group(0).strip()
    end = m.end()
    while end < len(body) and body[end] == "\n":
        end += 1
    return body[:m.start()] + body[end:], line


def main() -> int:
    ap = argparse.ArgumentParser(description="生成知乎导入用的发布副本")
    ap.add_argument("source", help="知识库源 Markdown 绝对路径")
    ap.add_argument("-o", "--out", help="输出副本路径（默认 <stem>.zhihu-publish.md）")
    ap.add_argument("--out-dir", help="输出目录（与 --out 互斥；默认源文件同目录）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON 报告")
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        print(f"FATAL: source not found: {source}", file=sys.stderr)
        return 2

    if args.out and args.out_dir:
        ap.error("--out 与 --out-dir 互斥")

    if args.out:
        out = Path(args.out).expanduser().absolute()
    else:
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{source.stem}.zhihu-publish.md"

    # out 保留最终分量、**不 resolve**（resolve 会解引用末段符号链接，导致后续把链接目标当输出写穿）。
    # 同源判定用 realpath（跟随链接）：把「输出（含其符号链接目标）就是源文件」的情形拒掉。
    real_out = Path(os.path.realpath(out))
    if real_out == source or os.path.normcase(str(real_out)) == os.path.normcase(str(source)):
        print("FATAL: 输出路径不能与源文件相同（否则会破坏原件）", file=sys.stderr)
        return 2
    if out.exists():
        try:
            if os.path.samefile(out, source):
                print("FATAL: 输出路径与源文件是同一文件（硬链接/短名），拒绝覆盖", file=sys.stderr)
                return 2
        except OSError:
            pass
    out.parent.mkdir(parents=True, exist_ok=True)

    raw = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    if raw.startswith("\ufeff"):
        # 去 UTF-8 BOM：否则 frontmatter 正则（要求首字符为 '-'）匹配失败，
        # 库内私有 YAML 会被当正文导入并公开。
        raw = raw[1:]
    started_fm = raw.lstrip().startswith("---")
    body, fm = strip_frontmatter(raw)
    body, h1 = remove_first_h1(body)
    body, converted, embeds = convert_wikilinks(body)

    residual = body.count("[[")
    heads = {"h1": 0, "h2": 0, "h3": 0, "h4": 0, "h5": 0, "h6": 0}
    for h in HEADING_RE.finditer(body):
        heads[f"h{len(h.group(1))}"] += 1
    first_line = body.lstrip("\n").split("\n", 1)[0].strip()

    report = {
        "source": str(source),
        "output": str(out),
        "frontmatter_stripped": fm,
        "h1_removed": h1,
        "wikilinks_converted": converted,
        "embeds_converted": embeds,
        "residual_wikilinks": residual,
        "char_count": len(body),
        "heading_counts": heads,
        "first_line": first_line[:80],
        "ok": residual == 0 and first_line != "---" and not (started_fm and not fm),
    }

    if report["ok"]:
        # mkstemp 独占创建（O_EXCL，不跟随预置符号链接）同目录临时文件，再 os.replace 原子替换目录项本身：
        # 既避免写穿到指向他处的 symlink，也避免「先删后写」的失败窗口。
        fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(body)
            os.replace(tmp_name, out)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass
    # 自检失败（残留 wikilink / 未剥离的 frontmatter 等）则不生成可上传副本（fail-closed）
    report["written"] = report["ok"]
    if not report["ok"]:
        report["stale_output"] = out.exists()   # 自检未过：可能残留上次的好副本
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"源文件   : {report['source']}")
        print(f"发布副本 : {report['output']}")
        print(f"frontmatter 剥离: {fm} | 删除 H1: {strip_ctrl(h1) if h1 else '(无)'}")
        print(f"wikilink 转换: {converted} 处（含 embed {embeds} 处），残留 {residual}")
        print(f"正文 {report['char_count']} 字 | 标题分布 {heads}")
        print(f"自检: {'PASS' if report['ok'] else 'FAIL'}（残留 wikilink 须 0，首行不得为 ---）")
        if report.get("stale_output"):
            print("⚠️ 自检未过，且输出路径已存在旧副本（未更新），请勿直接上传")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
