#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机械渲染 weflow 导出的会话 JSON——发送者、方向、附件路径只读字段与磁盘，绝不推断。

输入：`weflow export messages --format json` 产出的 <SESSION_ID>.json（可传多个，按时间合并去重）。
输出：每条消息一个块，方向标记 [我]/[对方]/[系统]，发送者取 senderDisplayName（缺失回退
senderUsername），类型标签取 type 字段；引用消息渲染被引用行；末尾给方向统计与附件索引。

附件索引定位文件附件（微信文件目录 <账号目录>\\msg\\file\\…，账号目录取当前 weflow 档案的 dbPath）：
有该消息的 fileMd5（weflow chat resources，按 serverId 关联）时，盘上文件内容（MD5）一致才给
「路径」——先比对同名及其 (N) 副本，再按「大小 + MD5」找回改了名的同一份文件；都不一致报
「未落盘」。没有 fileMd5 时不挑：盘上恰好只有一个同名文件才给路径并标「未校验内容」，否则只列
候选。另在同一会话内给出「被引用」（replyToMessageId 纯 ID 关联）与「紧随消息」（附件后 10 分钟
内的文字，纯时间窗）。

发送者、方向、路径都只来自记录字段与磁盘事实，脚本不做任何语气/长度/类型推断；需要分析对话
归属时一律用本脚本渲染，不要手写字段解析。字段契约见 SKILL.md「导出 JSON 字段契约」。

用法：
    python print_chat.py EXPORT.json [MORE.json ...] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                        [--last N] [--width N] [--stats-only] [--attachments-only]
                        [--no-locate] [--file-root <账号目录>]

退出码：0 正常；2 过滤后没有消息（或参数用法错误）；3 文件不存在、JSON 解析失败或结构不符。
纯标准库，无第三方依赖。
"""

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SYSTEM_LOCAL_TYPES = {10000, 10002}
ATTACHMENT_LABELS = {"文件消息", "图片消息", "视频消息"}
FILE_LABEL_RE = re.compile(r"^\[文件\]\s*(.+)$", re.S)
REDOWNLOAD_SUFFIX_RE = re.compile(r"\(\d+\)$")
FOLLOW_WINDOW_SECONDS = 600
FOLLOW_LIMIT = 5
COMPACT_INDEX_ABOVE = 40  # 附件很多时（图多的群）图片/视频压成单行；文件条目不压缩
MAX_CANDIDATES_SHOWN = 5
MAX_SIZE_CANDIDATES = 2000  # 按大小兜底时最多比对多少个同大小文件
MAX_RESOURCE_PAGES = 20


# ---- 基础工具 ----

def _out(text):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print(text)


def _fail(code, text):
    _out(f"print_chat: {text}")
    sys.exit(code)


def _flatten(text):
    return " ".join((text or "").split())


def _clip(text, width):
    if not width or len(text) <= width:
        return text
    return text[:width] + "…"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _label(m):
    return _flatten(str(m.get("type") or "").strip() or str(m.get("localType") or ""))


# ---- 读取 ----

def load_messages(paths):
    """读取一个或多个导出 JSON，按 (createTime, localId) 排序并去重；记下每条的来源目录与会话。"""
    msgs = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            _fail(3, f"文件不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            _fail(3, f"JSON 解析失败: {path}: {exc}")
        session, items = {}, None
        if isinstance(data, dict):
            items = data.get("messages")
            if items is None:
                items = data.get("data")
            if isinstance(data.get("session"), dict):
                session = data["session"]
        elif isinstance(data, list):
            items = data
        if not isinstance(items, list):
            _fail(3, f"结构不符（顶层应是消息数组，或含 messages/data 数组的对象）: {path}")
        if items and not any(isinstance(m, dict) for m in items):
            _fail(3, f"结构不符（消息数组的元素应是对象）: {path}")
        session_id = str(session.get("wxid") or path.stem)
        group = "群" in str(session.get("type") or "")
        for m in items:
            if not isinstance(m, dict):
                continue
            m["_src"] = str(path.parent)
            m["_session"] = session_id
            m["_group"] = group
            msgs.append(m)
    msgs.sort(key=lambda m: (_int(m.get("createTime")), _int(m.get("localId"))))
    seen, unique = set(), []
    for m in msgs:
        key = (
            m.get("createTime"),
            str(m.get("platformMessageId") or m.get("localId") or ""),
            str(m.get("senderUsername") or ""),
            str(m.get("content") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


def is_group(messages):
    return any(m.get("_group") for m in messages) or len({m.get("senderUsername") for m in messages}) > 2


def same_session(a, b):
    return a.get("_session") == b.get("_session")


# ---- 消息属性（只读字段） ----

def is_system(m):
    return m.get("localType") in SYSTEM_LOCAL_TYPES or "系统" in _label(m)


def attachment_kind(m):
    """附件类别；不是附件返回 None。

    「文件消息」标签下混有引用了文件的回复（正文是 `指令[引用 某人：[文件] xx]`），只有正文是
    `[文件] 名称` 的才是文件附件本身；其余按普通消息渲染。
    """
    label = _label(m)
    if label == "文件消息":
        return "文件" if FILE_LABEL_RE.match(str(m.get("content") or "")) else None
    if label in ATTACHMENT_LABELS:
        return {"图片消息": "图片", "视频消息": "视频"}[label]
    if m.get("localType") in (3, 43):
        return "附件"
    return None


def is_attachment(m):
    return attachment_kind(m) is not None


def is_file(m):
    return attachment_kind(m) == "文件"


def attachment_name(m):
    """附件名：`[文件] 名称` 取名称；其余取正文。HTML 实体（如 &#x20;）还原。"""
    content = str(m.get("content") or "")
    match = FILE_LABEL_RE.match(content)
    return html.unescape((match.group(1) if match else content).strip())


def direction_tag(m):
    if is_system(m):
        return "系统"
    if m.get("isSend") == 1:
        return "我"
    if m.get("isSend") == 0:
        return "对方"
    return "?"  # 字段缺失或异常时如实标注，不推断


def sender_name(m):
    return _flatten(str(m.get("senderDisplayName") or "").strip()
                    or str(m.get("senderUsername") or "").strip()
                    or "?")


def fmt_time(m):
    preset = _flatten(str(m.get("formattedTime") or "").strip())
    if preset:
        return preset
    created = _int(m.get("createTime"))
    if created:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created))
    return "?"


def first_line(text):
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


# ---- 渲染 ----

def render_content(m, width):
    """正文行（逐字，不做语义转换）；系统消息压成一行。"""
    content = str(m.get("content") or "")
    if is_system(m):
        return [_flatten(content) or f"[{_label(m)}]"]
    return [_clip(line, width) if width else line for line in (content.splitlines() or [""])]


def render_quote(m, width):
    quoted = str(m.get("quotedContent") or "").strip()
    if not quoted:
        return []
    who = _flatten(str(m.get("quotedSender") or "").strip() or "?")
    line = first_line(_flatten(quoted)) or f"(引用原文 {len(quoted)} 字)"
    return [_clip(f"  ↪ 引用 {who}: {line}", width or 200)]


def render(m, width):
    tag = direction_tag(m)
    if tag == "系统":
        head = f"{fmt_time(m)} [{tag}|{_label(m)}] "
    else:
        head = f"{fmt_time(m)} [{tag}|{sender_name(m)}|{_label(m)}] "
    body = render_content(m, width)
    lines = [head + (body[0] if body else "")]
    lines.extend("    " + b for b in body[1:])
    lines.extend(render_quote(m, width))
    return lines


def stats(messages):
    mine = sum(1 for m in messages if m.get("isSend") == 1 and not is_system(m))
    theirs = sum(1 for m in messages if m.get("isSend") == 0 and not is_system(m))
    system = sum(1 for m in messages if is_system(m))
    unknown = len(messages) - mine - theirs - system
    span = ""
    if messages:
        span = f" | 跨度 {fmt_time(messages[0])[:10]} ~ {fmt_time(messages[-1])[:10]}"
    parts = f"== 统计: 共 {len(messages)} 条 | 我 {mine} | 对方 {theirs} | 系统 {system}"
    if unknown:
        parts += f" | isSend 缺失或异常 {unknown}"
    _out(parts + span + " ==")


# ---- 附件机械定位 ----

def safe_name(name):
    """只接受纯文件名：不含路径分隔符、盘符、控制字符，不是上级引用或绝对路径。"""
    if not name or name in (".", "..") or name != name.strip():
        return False
    if any(ch in name for ch in "/\\:\0") or any(ord(ch) < 32 for ch in name):
        return False
    return os.path.basename(name) == name and not os.path.isabs(name)


def base_name(name):
    """去掉重下载后缀 (N)：报告(1)(1).pdf → 报告.pdf，archive.tar.gz(1) → archive.tar.gz。"""
    while True:
        match = REDOWNLOAD_SUFFIX_RE.search(name)
        if not match:
            break
        name = name[:match.start()]
    stem, ext = os.path.splitext(name)
    while True:
        match = REDOWNLOAD_SUFFIX_RE.search(stem)
        if not match:
            return stem + ext
        stem = stem[:match.start()]


def meta_file_name(meta):
    """消息记录里的文件名（还原 HTML 实体）；没有则返回空串。"""
    return html.unescape(str((meta or {}).get("fileName") or "").strip())


def human_size(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def file_md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(path):
    """“大小，落盘时间”；读不到时如实说明。"""
    try:
        st = path.stat()
    except OSError:
        return "无法读取文件属性"
    saved = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
    return f"{human_size(st.st_size)}，落盘 {saved}"


class FileIndex:
    """msg\\file 下全部文件的一次性索引：基本名 → [路径]，大小 → [路径]；读不了的目录记在 errors。"""

    def __init__(self, root):
        self.root = Path(root)
        self.by_base, self.by_size, self.errors = {}, {}, []
        for r, _dirs, files in os.walk(self.root, onerror=self.errors.append):
            for f in files:
                path = Path(r) / f
                self.by_base.setdefault(base_name(f), []).append(path)
                try:
                    self.by_size.setdefault(path.stat().st_size, []).append(path)
                except OSError:
                    pass

    def candidates(self, name):
        """同名文件及其 (N) 副本；与查询名完全相同的在前，其余按修改时间新→旧。"""
        hits = self.by_base.get(base_name(name), [])

        def sort_key(path):
            try:
                return (path.name != name, -path.stat().st_mtime_ns)
            except OSError:
                return (path.name != name, 0)

        return sorted(hits, key=sort_key)


class FileLocator:
    """文件附件定位器：磁盘索引 + 消息记录里的 fileMd5/fileSize，内容一致才给路径。"""

    def __init__(self, messages, file_root_arg=None):
        self.root, self.root_error = None, None
        self.meta, self.meta_errors, self.index = {}, {}, None
        self._md5_cache = {}
        if not any(is_file(m) for m in messages):
            return  # 没有文件附件：不需要文件目录，也不调用 weflow
        if file_root_arg is not None:
            self.root = resolve_file_root(file_root_arg)
            self.root_error = None if self.root else "--file-root 下找不到 msg\\file"
        else:
            self.root, self.root_error = weflow_file_root()
        if self.root:
            self.index = FileIndex(self.root)
        session_ids = sorted({m["_session"] for m in messages if is_file(m) and m.get("_session")})
        if session_ids:
            self.meta, self.meta_errors = fetch_file_meta(session_ids)

    def display_name(self, m):
        meta = self.meta.get(str(m.get("platformMessageId") or ""))
        return meta_file_name(meta) or attachment_name(m)

    def unverified_reason(self, m):
        """没有 MD5 的原因：按会话区分「取不到」与「记录里没有」。"""
        error = self.meta_errors.get(m.get("_session"))
        return f"未能取得该会话的文件校验信息：{_flatten(error)}" if error else "消息记录里没有该文件的 MD5"

    def _md5(self, path):
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self._md5_cache:
            self._md5_cache[key] = file_md5(path)
        return self._md5_cache[key]

    @staticmethod
    def _size(path):
        try:
            return path.stat().st_size
        except OSError:
            return -1

    def _verify(self, paths, want_md5, want_size):
        """逐个比对 MD5；大小只决定先后（大小相同的先比），不作为排除依据。"""
        paths = list(paths)
        if want_size:
            paths.sort(key=lambda p: self._size(p) != want_size)
        for path in paths:
            try:
                if self._md5(path) == want_md5:
                    return path
            except OSError:
                continue
        return None

    def _size_bucket(self, m, want_size):
        """同大小文件，消息月份目录里的优先，其次修改时间新→旧。"""
        month = ""
        if _int(m.get("createTime")):
            month = time.strftime("%Y-%m", time.localtime(_int(m.get("createTime"))))
        bucket = self.index.by_size.get(want_size, [])[:MAX_SIZE_CANDIDATES]

        def sort_key(path):
            try:
                return (path.parent.name != month, -path.stat().st_mtime_ns)
            except OSError:
                return (path.parent.name != month, 0)

        return sorted(bucket, key=sort_key)

    def resolve(self, m):
        """返回 (状态, 数据)：
        verified (路径, 是否改名) / unverified [候选] / absent ([候选], 是否做过内容校验) / unsafe None。"""
        meta = self.meta.get(str(m.get("platformMessageId") or "")) or {}
        name = meta_file_name(meta) or attachment_name(m)
        safe = safe_name(name)
        try:
            candidates = self.index.candidates(name) if safe else []
        except OSError:
            candidates = []
        want_md5 = str(meta.get("fileMd5") or "").strip().lower()
        want_size = _int(meta.get("fileSize"))
        if want_md5:
            hit = self._verify(candidates, want_md5, want_size)
            if hit is not None:
                return "verified", (hit, False)
            if want_size:
                hit = self._verify(self._size_bucket(m, want_size), want_md5, want_size)
                if hit is not None:
                    return "verified", (hit, True)
            return "absent", (candidates, True)
        if not safe:
            return "unsafe", None
        if candidates:
            return "unverified", candidates
        return "absent", ([], False)


def resolve_file_root(explicit):
    """把账号目录（或 msg\\file 目录、其下的库文件路径）统一解析成 msg\\file 目录。"""
    p = Path(explicit)
    if p.is_dir() and p.name.lower() == "file" and p.parent.name.lower() == "msg":
        return p
    for cand in [p] + list(p.parents)[:4]:
        target = cand / "msg" / "file"
        if target.is_dir():
            return target
    return None


def _weflow_exe():
    return Path(__file__).resolve().parent.parent / "bin" / "weflow.exe"


def _run_weflow(args):
    """运行 weflow（自动加 --json），返回 (data, 失败原因)。"""
    exe = _weflow_exe()
    if not exe.is_file():
        return None, "未找到 <skill>/bin/weflow.exe"
    try:
        proc = subprocess.run([str(exe), *args, "--json"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return _parse_weflow_json(proc.stdout)
    except Exception as exc:  # 超时、启动失败等都要落成可读原因
        return None, f"weflow {' '.join(args[:2])} 失败: {exc}"


def _parse_weflow_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "weflow 输出不是 JSON"
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        return None, "weflow 输出不是 JSON"
    if not isinstance(payload, dict):
        return None, "weflow 输出不是 JSON"
    if not payload.get("success"):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = _flatten(f"{error.get('code') or ''}: {error.get('message') or ''}").strip(": ")
        else:
            detail = _flatten(str(error or "")) or "未知原因"
        return None, f"weflow 报错: {detail}"
    return payload.get("data"), None


def weflow_file_root():
    """当前 weflow 档案的 dbPath → 该账号的 msg\\file 目录；不确定时返回原因，不猜档案。"""
    data, error = _run_weflow(["config", "profiles"])
    if data is None:
        return None, f"{error}；可用 --file-root 指定账号目录"
    profiles = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    current = [p for p in profiles if p.get("current")]
    if len(current) != 1:
        return None, "无法确定当前 weflow 档案（没有或不止一个 current）；可用 --file-root 指定账号目录"
    if not current[0].get("dbPath"):
        return None, "当前 weflow 档案未配置数据库目录；可用 --file-root 指定账号目录"
    root = resolve_file_root(current[0]["dbPath"])
    if root is None:
        return None, "账号目录下没有 msg\\file；可用 --file-root 指定账号目录"
    return root, None


def fetch_file_meta(session_ids):
    """按会话取文件消息的 fileName/fileMd5/fileSize，键为 serverId（= 导出的 platformMessageId）。

    返回 (元数据表, {会话: 未取全的原因})。
    """
    meta, errors = {}, {}
    for sid in session_ids:
        cursor, pages = None, 0
        while pages < MAX_RESOURCE_PAGES:
            args = ["chat", "resources", "--type", "file", "--session", sid, "--limit", "2000"]
            if cursor:
                args += ["--before", cursor]
            data, error = _run_weflow(args)
            if data is None:
                errors[sid] = error
                break
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                errors[sid] = "weflow chat resources 返回结构不符"
                break
            for item in data["items"]:
                if isinstance(item, dict) and item.get("serverId"):
                    meta[str(item["serverId"])] = item
            cursor, pages = data.get("nextCursor"), pages + 1
            if not data.get("hasMore") or not cursor:
                break
        else:
            errors[sid] = "weflow chat resources 分页超过上限，结果可能不全"
    return meta, errors


def media_path(m):
    """导出带 --media 时 content 是相对导出文件的副本路径；必须落在导出目录内且存在才返回。"""
    content = _flatten(m.get("content"))
    if not content or content.startswith("["):
        return None
    try:
        base = Path(m.get("_src") or ".").resolve()
        path = Path(content) if os.path.isabs(content) else base / content
        path = path.resolve()
    except OSError:
        return None
    if path.is_file() and base in path.parents:
        return path
    return None


def _speaker(m, group):
    """上下文行里的说话人：私聊给方向；群聊给 方向(昵称)，方向不能丢。"""
    return f"{direction_tag(m)}({sender_name(m)})" if group else direction_tag(m)


def _context_line(m, group, width):
    text = first_line(_flatten(str(m.get("content") or "")))
    return _clip(f"{fmt_time(m)[11:16]} {_speaker(m, group)}: {text}", width or 120) if text else None


def following_instructions(messages, index, group, width):
    """附件后 FOLLOW_WINDOW_SECONDS 内、同一会话的文字消息（不含系统与附件），纯时间窗关联。"""
    anchor = messages[index]
    anchor_time = _int(anchor.get("createTime"))
    found = []
    for m in messages[index + 1:]:
        if _int(m.get("createTime")) - anchor_time > FOLLOW_WINDOW_SECONDS:
            break
        if not same_session(m, anchor) or is_system(m) or is_attachment(m):
            continue
        line = _context_line(m, group, width)
        if line:
            found.append(line)
        if len(found) >= FOLLOW_LIMIT:
            break
    return found


def quoting_messages(messages, target, group, width):
    """同一会话里引用了该附件的消息（replyToMessageId == 附件的 platformMessageId），纯 ID 关联。"""
    target_id = str(target.get("platformMessageId") or "")
    if not target_id:
        return []
    found = []
    for m in messages:
        if same_session(m, target) and str(m.get("replyToMessageId") or "") == target_id:
            line = _context_line(m, group, width)
            if line:
                found.append(line)
    return found[:FOLLOW_LIMIT]


def _candidate_lines(candidates):
    lines = [f"      - {p}" for p in candidates[:MAX_CANDIDATES_SHOWN]]
    if len(candidates) > MAX_CANDIDATES_SHOWN:
        lines.append(f"      - …等共 {len(candidates)} 个")
    return lines


def _file_lines(m, locator, width):
    name = _flatten(locator.display_name(m) if locator else attachment_name(m))
    lines = [f"📎 {fmt_time(m)} [{direction_tag(m)}|{sender_name(m)}] 文件 {_clip(name, width) or '?'}"]
    if locator is None or locator.root is None or locator.index is None:
        reason = _flatten(locator.root_error) if locator and locator.root_error else "无法确定微信文件目录"
        return lines + [f"   未定位: {reason}"]
    status, payload = locator.resolve(m)
    if status == "verified":
        path, renamed = payload
        note = "；盘上文件名与消息里的不同" if renamed else ""
        lines.append(f"   路径: {path}（MD5 与消息记录一致{note}；{_describe(path)}）")
    elif status == "unverified":
        if len(payload) == 1 and payload[0].name == name:
            path = payload[0]
            lines.append(f"   路径: {path}（未校验内容：{locator.unverified_reason(m)}；{_describe(path)}）")
        else:
            lines.append(f"   未校验内容: 盘上有 {len(payload)} 个同名/副本候选，没有 MD5 无法确认是哪一个（打开前先核对）:")
            lines.extend(_candidate_lines(payload))
    elif status == "absent":
        candidates, md5_checked = payload
        if candidates:
            lines.append(f"   未落盘: 盘上有 {len(candidates)} 个同名/副本文件但内容均不符"
                         "（微信存的是别的版本，或该文件已过期/被清理）:")
            lines.extend(_candidate_lines(candidates))
        elif md5_checked:
            lines.append("   未落盘: 盘上没有与消息记录内容一致的文件（微信未下载到本机，或已过期/被清理）")
        else:
            lines.append("   未落盘: 盘上没有同名文件（未做内容校验；改名保存的无法发现）")
    else:
        lines.append("   未定位: 消息里的文件名不是合法文件名，已拒绝拼接路径")
    return lines


def _media_lines(m, compact, width):
    kind = attachment_kind(m)
    name = os.path.basename(attachment_name(m).replace("\\", "/")) or kind
    head = f"📎 {fmt_time(m)} [{direction_tag(m)}|{sender_name(m)}] {kind} {_clip(name, width)}"
    path = media_path(m)
    if compact:
        return [f"{head} → {path if path else '未随导出落盘'}"]
    lines = [head]
    if path:
        lines.append(f"   路径: {path}")
    else:
        lines.append("   未随导出落盘: 按 SKILL「图片 / 语音」配方解密后再看（export --media 或 image decrypt）")
    return lines


def attachment_index(messages, locator, width):
    attachments = [(i, m) for i, m in enumerate(messages) if is_attachment(m)]
    if not attachments:
        return ["== 附件索引: 无 =="]
    group = is_group(messages)
    compact = len(attachments) > COMPACT_INDEX_ABOVE
    lines = ["== 附件索引（路径只来自磁盘事实，未作内容推断） =="]
    if locator is not None and locator.root_error:
        lines.append(f"（未定位：{_flatten(locator.root_error)}）")
    elif locator is not None and any(is_file(m) for _i, m in attachments):
        failed = [f"{sid}：{_flatten(err)}" for sid, err in sorted(locator.meta_errors.items())]
        if failed:
            lines.append(f"（未取得部分会话的文件 MD5 —— {'；'.join(failed)}；这些会话的文件路径未做内容校验）")
        elif not locator.meta:
            lines.append("（未取得文件 MD5：消息记录里没有文件校验信息；下列文件路径未做内容校验）")
        if locator.index is not None and locator.index.errors:
            lines.append(f"（微信文件目录有 {len(locator.index.errors)} 处无法读取，可能漏掉文件）")
    for i, m in attachments:
        if is_file(m):
            lines.extend(_file_lines(m, locator, width))
        else:
            lines.extend(_media_lines(m, compact, width))
        if not compact or is_file(m):
            quoted = quoting_messages(messages, m, group, width)
            if quoted:
                lines.append("   被引用: " + " | ".join(quoted))
            follow = following_instructions(messages, i, group, width)
            if follow:
                lines.append("   紧随消息（10 分钟内）: " + " | ".join(follow))
    lines.append("提示: 与当前任务相关的附件须打开本体阅读（PDF/Office 用对应解析技能，图片用图片读取工具），不要凭文件名推断内容")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="机械渲染 weflow 导出的会话 JSON")
    parser.add_argument("exports", nargs="+", help="导出 JSON 文件路径（可多个）")
    parser.add_argument("--start", help="只渲染该日期（含）之后，YYYY-MM-DD")
    parser.add_argument("--end", help="只渲染该日期（含）之前，YYYY-MM-DD")
    parser.add_argument("--last", type=int, help="只渲染最后 N 条（N ≥ 1）")
    parser.add_argument("--width", type=int, default=0, help="单行截断宽度（默认不截断；负数按 0 处理）")
    parser.add_argument("--stats-only", action="store_true", help="只输出方向统计（优先于其他输出开关）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--attachments-only", action="store_true", help="只输出附件索引")
    mode.add_argument("--no-locate", action="store_true", help="不输出附件索引")
    parser.add_argument("--file-root", help="微信账号目录（或其 msg\\file）；默认取当前 weflow 档案的 dbPath")
    args = parser.parse_args(argv)
    if args.last is not None and args.last < 1:
        parser.error("--last 必须 ≥ 1")
    if args.file_root is not None and not args.file_root.strip():
        parser.error("--file-root 不能为空")
    width = max(args.width, 0)

    messages = load_messages(args.exports)
    if args.start:
        messages = [m for m in messages if fmt_time(m)[:10] >= args.start]
    if args.end:
        messages = [m for m in messages if fmt_time(m)[:10] <= args.end]
    if args.last:
        messages = messages[-args.last:]

    if not messages:
        _fail(2, "过滤后没有消息")

    if args.stats_only:
        stats(messages)
        return

    index_lines = []
    if not args.no_locate:
        locator = None
        if any(is_attachment(m) for m in messages):
            locator = FileLocator(messages, args.file_root)
        index_lines = attachment_index(messages, locator, width)

    if args.attachments_only:
        for line in index_lines:
            _out(line)
        return

    _out("# print_chat — 发送者/方向机械读取自 isSend + senderDisplayName，未经推断；引用行以 ↪ 标注")
    for m in messages:
        for line in render(m, width):
            _out(line)
    stats(messages)
    for line in index_lines:
        _out(line)


if __name__ == "__main__":
    main()
