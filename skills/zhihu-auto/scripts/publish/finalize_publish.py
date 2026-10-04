#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finalize_publish.py — 知乎发布成功后的完工动作。

做两件事（第二件仅在检测到 Obsidian 知识库时执行，即「轻耦合」）：

  1. 在源文件 frontmatter 写入/更新 `published: <知乎链接>`（幂等，可重复运行）
  2. 若从源文件向上能找到 `.meta/scripts/changelog_append.py`，调用它把发布事实
     记入 `docs/CHANGELOG.md`（默认自动；用 --no-changelog 关闭）

用法:
    python finalize_publish.py <source.md> --url https://zhuanlan.zhihu.com/p/123
    python finalize_publish.py <source.md> --url <url> --time "2026-10-02 18:30"
    python finalize_publish.py <source.md> --url <url> --no-changelog
    python finalize_publish.py <source.md> --url <url> --dry-run

注意：本脚本不点发布、不碰浏览器。它只负责发布之后回写源文件与知识库账本，
发布本身（不可逆的公开动作）必须由用户明确授权后由 agent 在浏览器里完成。
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)", re.DOTALL)
TITLE_RE = re.compile(r"^title:[ \t]*(.+)$", re.MULTILINE)
H1_RE = re.compile(r"^[ \t]*#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def split_frontmatter(text: str) -> tuple[str, str, bool]:
    """返回 (frontmatter 内部文本, 正文, 是否存在 frontmatter)。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return "", text, False
    return m.group(1), text[m.end():], True


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def resolve_title(fm_text: str, body: str, stem: str) -> str:
    m = TITLE_RE.search(fm_text)
    if m:
        return unquote(m.group(1))
    m = H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return stem


def upsert_published(fm_text: str, url: str) -> str:
    """在 frontmatter 内写入或替换 published 字段。"""
    lines = fm_text.split("\n") if fm_text else []
    for i, line in enumerate(lines):
        if re.match(r"^published:[ \t]*", line):
            lines[i] = f"published: {url}"
            return "\n".join(lines)
    lines.append(f"published: {url}")
    return "\n".join(lines)


def find_vault_changelog(source: Path) -> Path | None:
    for parent in [source.parent, *source.parents]:
        cand = parent / ".meta" / "scripts" / "changelog_append.py"
        if cand.is_file():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="知乎发布后的完工动作")
    ap.add_argument("source", help="知识库源 Markdown 绝对路径")
    ap.add_argument("--url", required=True, help="知乎发布后的公开链接")
    ap.add_argument("--time", default=None, help="YYYY-MM-DD HH:MM（默认当前时间）")
    ap.add_argument("--no-changelog", action="store_true",
                    help="即使检测到知识库也不写 CHANGELOG")
    ap.add_argument("--dry-run", action="store_true", help="只报告将做什么，不落盘")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        print(f"FATAL: source not found: {source}", file=sys.stderr)
        return 2

    when = args.time or datetime.now().strftime("%Y-%m-%d %H:%M")

    raw = source.read_text(encoding="utf-8")
    fm_text, body, had_fm = split_frontmatter(raw)
    title = resolve_title(fm_text, body, source.stem)

    new_fm = upsert_published(fm_text, args.url)
    if had_fm:
        new_text = f"---\n{new_fm}\n---\n{body}"
    else:
        new_text = f"---\npublished: {args.url}\n---\n\n{raw}"

    summary = {
        "source": str(source),
        "url": args.url,
        "had_frontmatter": had_fm,
        "frontmatter_updated": new_fm != fm_text or not had_fm,
        "title": title,
        "changelog": None,
        "ok": True,
    }

    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json
              else f"[dry-run] 将写入 published: {args.url} 到 {source}")
        return 0

    source.write_text(new_text, encoding="utf-8", newline="")

    if not args.no_changelog:
        changelog_script = find_vault_changelog(source)
        if changelog_script is None:
            summary["changelog"] = "skipped (未检测到知识库 .meta/scripts/changelog_append.py)"
        else:
            bullet = (f"经 zhihu-publish skill 发布到知乎：{args.url}"
                      f"（导入 MD，源文件 frontmatter 已记 published）")
            proc = subprocess.run(
                [sys.executable, str(changelog_script),
                 "--time", when, "--prefix", "agent",
                 "--title", f"发布《{title}》到知乎",
                 "--content", bullet],
                capture_output=True, text=True, encoding="utf-8",
            )
            if proc.returncode == 0:
                summary["changelog"] = "written"
            else:
                summary["changelog"] = f"FAILED rc={proc.returncode}: {(proc.stderr or proc.stdout or '').strip()}"
                summary["ok"] = False

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"源文件    : {summary['source']}")
        print(f"published : {args.url}  (frontmatter 更新: {summary['frontmatter_updated']})")
        print(f"CHANGELOG : {summary['changelog']}")
        print(f"结果      : {'OK' if summary['ok'] else 'INCOMPLETE'}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
