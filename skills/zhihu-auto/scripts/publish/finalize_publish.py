#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finalize_publish.py — 知乎发布成功后的完工动作。

做两件事：

  1. 在源文件 frontmatter 写入/更新 `published: <知乎链接>`（幂等，可重复运行）
  2. 让知识库记账脚本把发布事实记入 `docs/CHANGELOG.md`——**默认不做**；
     须显式 --changelog 且用 --vault-root（或 ZHIHU_VAULT_ROOT）指定受信知识库根，
     脚本路径固定为该根下的 `.meta/scripts/changelog_append.py`（绝不由源文件祖先目录
     决定，避免执行来源未受信的输入目录中的脚本）

用法:
    python finalize_publish.py <source.md> --url https://zhuanlan.zhihu.com/p/123
    python finalize_publish.py <source.md> --url <url> --changelog --vault-root <知识库根>
    python finalize_publish.py <source.md> --url <url> --time "2026-10-02 18:30"
    python finalize_publish.py <source.md> --url <url> --dry-run

注意：本脚本不点发布、不碰浏览器。它只负责发布之后回写源文件与知识库账本，
发布本身（不可逆的公开动作）必须由用户明确授权后由 agent 在浏览器里完成。
写入知识库 CHANGELOG 默认**关闭**：须显式 `--changelog` 且用 `--vault-root`
（或环境变量 `ZHIHU_VAULT_ROOT`）指定受信知识库根，脚本才会执行其下的
`.meta/scripts/changelog_append.py`——避免执行来源未受信的输入目录中的脚本。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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


def yaml_quote(s: str) -> str:
    """把值安全地写成双引号 YAML 标量，防止链接里的特殊字符破坏 frontmatter。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_ctrl(s: str) -> str:
    """剥离 C0/C1/DEL 控制字符（保留 tab/LF/CR），供写入账本/终端前使用。"""
    return _CTRL_RE.sub("", s)


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
    value = yaml_quote(url)
    for i, line in enumerate(lines):
        if re.match(r"^published:[ \t]*", line):
            lines[i] = f"published: {value}"
            return "\n".join(lines)
    lines.append(f"published: {value}")
    return "\n".join(lines)


def vault_changelog_script(trusted_root: Path) -> Path:
    """受信根下的记账脚本（唯一确定路径，绝不由源文件祖先目录决定）。"""
    return trusted_root / ".meta" / "scripts" / "changelog_append.py"


def main() -> int:
    ap = argparse.ArgumentParser(description="知乎发布后的完工动作")
    ap.add_argument("source", help="知识库源 Markdown 绝对路径")
    ap.add_argument("--url", required=True, help="知乎发布后的公开链接（必须 https 且属 zhihu.com）")
    ap.add_argument("--time", default=None, help="YYYY-MM-DD HH:MM（默认当前时间）")
    ap.add_argument("--changelog", action="store_true",
                    help="显式允许执行知识库记账脚本（默认不执行；还需 --vault-root 或 ZHIHU_VAULT_ROOT 指定受信根）")
    ap.add_argument("--vault-root", default=None,
                    help="受信知识库根；记账脚本必须位于其下才会被执行")
    ap.add_argument("--no-changelog", action="store_true",
                    help="即使给了 --changelog 也不写 CHANGELOG")
    ap.add_argument("--dry-run", action="store_true", help="只报告将做什么，不落盘")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        print(f"FATAL: source not found: {source}", file=sys.stderr)
        return 2

    parsed = urlparse(args.url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https"
            or not (host == "zhihu.com" or host.endswith(".zhihu.com"))
            or any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in args.url)):
        print("FATAL: --url 必须是 https 且属于 zhihu.com 的链接（不含控制字符）", file=sys.stderr)
        return 2
    if args.time:
        try:
            datetime.strptime(args.time, "%Y-%m-%d %H:%M")
        except ValueError:
            print("FATAL: --time 需为合法的 'YYYY-MM-DD HH:MM'", file=sys.stderr)
            return 2

    when = args.time or datetime.now().strftime("%Y-%m-%d %H:%M")

    raw = source.read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    fm_text, body, had_fm = split_frontmatter(raw)
    title = resolve_title(fm_text, body, source.stem)

    new_fm = upsert_published(fm_text, args.url)
    if had_fm:
        new_text = f"---\n{new_fm}\n---\n{body}"
    else:
        new_text = f"---\npublished: {yaml_quote(args.url)}\n---\n\n{raw}"

    summary = {
        "source": str(source),
        "url": args.url,
        "had_frontmatter": had_fm,
        "frontmatter_updated": new_fm != fm_text or not had_fm,
        "title": strip_ctrl(title),
        "changelog": None,
        "ok": True,
    }

    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json
              else f"[dry-run] 将写入 published: {args.url} 到 {source}")
        return 0

    # 原子替换目录项写回源文件（与 make_publish_copy 一致，避免中途截断损坏原件）
    fd, tmp_name = tempfile.mkstemp(dir=str(source.parent), prefix=source.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp_name, source)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass

    if args.changelog and not args.no_changelog:
        root = args.vault_root or os.environ.get("ZHIHU_VAULT_ROOT")
        if not root:
            summary["changelog"] = ("REFUSED (需 --vault-root 或 ZHIHU_VAULT_ROOT 指定受信知识库根；"
                                    "不执行来源未受信的脚本)")
            summary["ok"] = False
        else:
            trusted = Path(root).expanduser().resolve()
            script = vault_changelog_script(trusted)
            resolved = script.resolve()
            if not resolved.is_file():
                summary["changelog"] = f"skipped (受信根下无账本脚本: {script})"
            elif trusted not in resolved.parents:
                summary["changelog"] = f"REFUSED (账本脚本经解析不在受信根 {trusted} 之下)"
                summary["ok"] = False
            else:
                bullet = (f"经 zhihu-auto skill 发布到知乎：{args.url}"
                          f"（导入 MD，源文件 frontmatter 已记 published）")
                proc = subprocess.run(
                    [sys.executable, str(resolved),
                     "--time", when, "--prefix", "agent",
                     "--title", f"发布《{strip_ctrl(title)}》到知乎",
                     "--content", bullet],
                    capture_output=True, text=True, encoding="utf-8",
                )
                if proc.returncode == 0:
                    summary["changelog"] = "written"
                else:
                    summary["changelog"] = f"FAILED rc={proc.returncode}: {strip_ctrl((proc.stderr or proc.stdout or '').strip())}"
                    summary["ok"] = False
    else:
        summary["changelog"] = "skipped (默认不执行账本脚本；需 --changelog + --vault-root)"

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
