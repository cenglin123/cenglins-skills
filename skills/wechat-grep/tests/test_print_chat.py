"""print_chat.py 的机械归属与附件定位回归测试。

核心契约：方向与发送者只来自 isSend + senderDisplayName/senderUsername 字段；文件附件路径只在
内容（MD5）与消息记录一致时给出，拿不到 MD5 时如实标「未校验内容」而不挑一个；脚本不做任何
语气/长度/类型推断。测试数据全部为合成值；附件类用例在进程内运行并打桩 weflow，不依赖
bin/weflow.exe，也不触碰真实微信目录。
"""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "print_chat.py"
ENV = dict(os.environ, PYTHONUTF8="1")

ME, PEER = "wxid_me", "wxid_peer"
MY_NAME, PEER_NAME = "我方昵称", "同事甲"
FILE_TYPE = 25769803825  # 6 << 32 | 49：应用消息里的文件消息
REPLY_TO_FILE_TYPE = (57 << 32) | 49  # 引用了文件的回复，类型标签同样是「文件消息」


def msg(**kw):
    base = {
        "localId": 1,
        "createTime": 1795507200,
        "formattedTime": "2026-11-24 16:00:00",
        "type": "文本消息",
        "localType": 1,
        "content": "hello",
        "isSend": 0,
        "senderUsername": PEER,
        "senderDisplayName": PEER_NAME,
        "platformMessageId": "1",
    }
    base.update(kw)
    return base


def file_msg(**kw):
    base = dict(type="文件消息", localType=FILE_TYPE, content="[文件] 计划表.xlsx",
                platformMessageId="9001", formattedTime="2026-11-24 16:00:59", createTime=1795507259)
    base.update(kw)
    return msg(**base)


def run_cli(*args, locate=False):
    """命令行方式运行；默认 --no-locate，避免触达真实 weflow 与微信目录。"""
    extra = [] if locate else ["--no-locate"]
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, *extra],
        capture_output=True, text=True, encoding="utf-8", env=ENV,
    )


def load_module():
    spec = importlib.util.spec_from_file_location("print_chat_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


def run_in_process(argv, weflow="missing"):
    """进程内运行。weflow: 可调用对象（收到不含 exe 的参数表，返回 stdout）或 "missing"（没有 weflow.exe）。
    两种情况都不依赖真实的 bin/weflow.exe。返回 (退出码, stdout, 调用记录)；调用记录为
    [{"args": 参数表, "kwargs": subprocess.run 的关键字参数}]。"""
    mod = load_module()
    buf, calls = io.StringIO(), []

    def fake_run(cmd, **kwargs):
        calls.append({"args": cmd[1:], "kwargs": kwargs})
        return SimpleNamespace(stdout=weflow(cmd[1:]))

    code = 0
    with redirect_stdout(buf):
        if weflow == "missing":
            context = patch.object(mod, "_weflow_exe", return_value=Path("Z:/nowhere/weflow.exe"))
            with context:
                try:
                    mod.main(argv)
                except SystemExit as exc:
                    code = exc.code or 0
        else:
            with patch.object(mod, "_weflow_exe", return_value=Path(sys.executable)), \
                    patch.object(mod.subprocess, "run", side_effect=fake_run):
                try:
                    mod.main(argv)
                except SystemExit as exc:
                    code = exc.code or 0
    return code, buf.getvalue(), calls


def weflow_json(data):
    return json.dumps({"success": True, "data": data}, ensure_ascii=False)


def make_weflow(acct=None, items=(), profiles=None, pages=None, fail_for=()):
    """打桩的 weflow：config profiles 返回当前档案；chat resources 按会话返回文件元数据。

    items 可给列表（所有会话相同）或 {会话: 条目列表}；pages 为 {游标或 None: (本页条目, 还有, 下一页游标)}；
    fail_for: 对这些会话报错。"""
    def run(args):
        if args[:2] == ["config", "profiles"]:
            if profiles is not None:
                return profiles
            return weflow_json([{"name": "acct", "current": True, "dbPath": str(acct)}] if acct else [])
        if args[:2] == ["chat", "resources"]:
            sid = args[args.index("--session") + 1]
            if sid in fail_for:
                return json.dumps({"success": False, "error": {"code": "E_BOOM", "message": "库打不开"}})
            if pages is not None:
                before = args[args.index("--before") + 1] if "--before" in args else None
                page_items, has_more, next_cursor = pages[before]
                return weflow_json({"items": page_items, "hasMore": has_more, "nextCursor": next_cursor})
            own = items.get(sid, []) if isinstance(items, dict) else list(items)
            return weflow_json({"items": own, "hasMore": False, "nextCursor": None})
        return json.dumps({"success": False, "error": {"code": "unknown", "message": "x"}})
    return run


def file_item(server_id="9001", name="计划表.xlsx", data=b"version-1", size=True):
    item = {"serverId": server_id, "fileName": name, "fileMd5": hashlib.md5(data).hexdigest()}
    if size:
        item["fileSize"] = len(data)
    return item


def index_blocks(output):
    """把附件索引拆成 {📎 行: [后续行…]}，便于逐条断言。"""
    blocks, current = {}, None
    for line in output.splitlines():
        if line.startswith("📎 "):
            current = [line]
            blocks[line] = current
        elif current is not None and (line.startswith("== ") or line.startswith("提示: ")):
            current = None
        elif current is not None:
            current.append(line)
    return blocks


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp2.cleanup)
        self.tmp = Path(self._tmp.name)
        self.tmp2 = Path(self._tmp2.name)

    def write_export(self, messages, sid=PEER, session_type="私聊", name=None):
        path = self.tmp / (name or f"{sid}.json")
        payload = {"messages": messages, "session": {"wxid": sid, "type": session_type}}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def make_account(self, files):
        """files: {相对 msg/file 的路径: 字节内容}；返回账号目录。"""
        root = self.tmp / "acct" / "msg" / "file"
        for rel, body in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return self.tmp / "acct"


# ---- 命令行：归属与渲染（不触达 weflow） ----

class RenderTests(Base):
    def test_direction_and_sender_are_mechanical(self):
        path = self.write_export([
            msg(isSend=1, senderUsername=ME, senderDisplayName=MY_NAME, content="收到"),
            msg(isSend=0, senderDisplayName=PEER_NAME, content="分一下类"),
        ])
        out = run_cli(path).stdout
        self.assertIn(f"[我|{MY_NAME}|文本消息] 收到", out)
        self.assertIn(f"[对方|{PEER_NAME}|文本消息] 分一下类", out)

    def test_tone_is_never_used_for_direction(self):
        # 「收到」这类短消息方向只跟 isSend 走：对方发的短消息也必须标对方。
        path = self.write_export([msg(isSend=0, content="收到")])
        self.assertIn(f"[对方|{PEER_NAME}|文本消息] 收到", run_cli(path).stdout)

    def test_missing_display_name_falls_back_to_username(self):
        path = self.write_export([msg(senderDisplayName="", senderUsername="wxid_abc")])
        self.assertIn("[对方|wxid_abc|", run_cli(path).stdout)

    def test_missing_or_abnormal_isSend_is_marked_unknown(self):
        without = msg(localId=1)
        without.pop("isSend")
        records = [without, msg(localId=2, platformMessageId="b", isSend="1"), msg(localId=3, platformMessageId="c", isSend=2)]
        path = self.write_export(records)
        proc = run_cli(path, "--stats-only", locate=True)
        self.assertEqual(proc.stdout.strip(),
                         "== 统计: 共 3 条 | 我 0 | 对方 0 | 系统 0 | isSend 缺失或异常 3 | 跨度 2026-11-24 ~ 2026-11-24 ==")
        self.assertEqual(run_cli(path).stdout.count("[?|"), 3)

    def test_system_message_has_no_sender_attribution(self):
        path = self.write_export([
            msg(localType=10000, type="系统消息", content="[系统: 以上是打招呼的消息]", isSend=0, senderUsername=ME,
                senderDisplayName=MY_NAME),
        ])
        out = run_cli(path).stdout
        self.assertIn("[系统|系统消息] [系统: 以上是打招呼的消息]", out)
        self.assertNotIn("[对方|", out)
        self.assertNotIn(MY_NAME, out)

    def test_quote_renders_quoted_sender_mechanically(self):
        path = self.write_export([
            msg(type="引用消息", localType=FILE_TYPE, content="分一下类", quotedSender="wxid_other", quotedContent="[图片]"),
        ])
        self.assertIn("  ↪ 引用 wxid_other: [图片]", run_cli(path).stdout)

    def test_text_starting_with_angle_bracket_is_rendered_verbatim(self):
        # 正文以 < 开头是普通文本，不能被当成 XML 藏起来。
        for body in ("<- 看这里", "<script>alert(1)</script>"):
            path = self.write_export([msg(content=body)])
            out = run_cli(path).stdout
            self.assertIn(f"|文本消息] {body}", out)
            self.assertNotIn("XML 正文", out)

    def test_start_end_filters_by_date(self):
        path = self.write_export([
            msg(localId=1, platformMessageId="1", formattedTime="2026-09-10 08:00:00", createTime=1788979200, content="早"),
            msg(localId=2, platformMessageId="2", formattedTime="2026-11-20 16:00:00", createTime=1795507200, content="晚"),
        ])
        out = run_cli(path, "--start", "2026-11-01").stdout
        self.assertIn("晚", out)
        self.assertNotIn("早", out)
        out = run_cli(path, "--end", "2026-10-01").stdout
        self.assertIn("早", out)
        self.assertNotIn("晚", out)

    def test_last_keeps_only_tail(self):
        records = [msg(localId=i, platformMessageId=str(i), createTime=1789000000 + i,
                       formattedTime=f"2026-09-10 15:00:{i:02d}", content=f"第{i}条") for i in range(5)]
        path = self.write_export(records)
        out = run_cli(path, "--last", "2").stdout
        self.assertIn("第3条", out)
        self.assertIn("第4条", out)
        for gone in ("第0条", "第1条", "第2条"):
            self.assertNotIn(gone, out)

    def test_stats_only_partitions_counts(self):
        path = self.write_export([
            msg(localId=1, platformMessageId="1", isSend=1, senderUsername=ME, senderDisplayName=MY_NAME, content="a"),
            msg(localId=2, platformMessageId="2", content="b"),
            msg(localId=3, platformMessageId="3", localType=10000, type="系统消息", content="[系统: x]"),
        ])
        self.assertEqual(run_cli(path, "--stats-only").stdout.strip(),
                         "== 统计: 共 3 条 | 我 1 | 对方 1 | 系统 1 | 跨度 2026-11-24 ~ 2026-11-24 ==")

    def test_multiple_files_merge_by_time_and_dedupe_repeats(self):
        early = self.write_export([msg(localId=1, platformMessageId="1", formattedTime="2026-09-10 08:00:00",
                                      createTime=1788979200, content="早")], name="early.json")
        late = self.write_export([msg(localId=7, platformMessageId="2", formattedTime="2026-11-20 16:00:00",
                                     createTime=1795507200, content="晚")], name="late.json")
        out = run_cli(early, late, early).stdout
        self.assertLess(out.index("早"), out.index("晚"))
        self.assertEqual(out.count("|文本消息] 早"), 1)

    def test_same_second_same_text_from_two_people_is_not_deduplicated(self):
        # 没有 platformMessageId 时，同秒同文但发送者不同的两条消息必须都保留。
        def bare(record):
            record.pop("platformMessageId")
            return record

        first = self.write_export(
            [bare(msg(localId=5, content="好的", senderUsername="wxid_a", senderDisplayName="甲"))], name="a.json")
        second = self.write_export(
            [bare(msg(localId=5, content="好的", senderUsername="wxid_b", senderDisplayName="乙"))], name="b.json")
        out = run_cli(first, second).stdout
        self.assertEqual(out.count("|文本消息] 好的"), 2)

    def test_empty_export_exits_2_and_missing_file_exits_3(self):
        empty = self.write_export([], name="empty.json")
        self.assertEqual(run_cli(empty).returncode, 2)
        self.assertEqual(run_cli(str(self.tmp / "nope.json")).returncode, 3)

    def test_malformed_json_structures_exit_3(self):
        for i, payload in enumerate(("null", "42", '{"data": 5}', '{"messages": 7}', "[1, 2]", "{not json", '["ok", 5, null]')):
            path = self.tmp / f"bad{i}.json"
            path.write_text(payload, encoding="utf-8")
            self.assertEqual(run_cli(str(path)).returncode, 3, payload)

    def test_last_zero_is_rejected(self):
        proc = run_cli(self.write_export([msg()]), "--last", "0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--last", proc.stderr)

    def test_attachments_only_with_no_locate_is_rejected(self):
        proc = run_cli(self.write_export([msg()]), "--attachments-only", "--no-locate", locate=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not allowed with", proc.stderr)

    def test_header_fields_cannot_forge_a_message_line(self):
        # 发送者名、类型标签、时间字段里带换行时都不能在第 0 列伪造出一行消息头。
        forged = f"{PEER_NAME}\n2026-01-01 00:00:00 [我|伪造|文本消息] 假消息"
        path = self.write_export([msg(senderDisplayName=forged, type="文本\n消息",
                                      formattedTime="2026-11-24 16:00:00\n2026-02-02 00:00:00 [我|伪造|文本消息] 假消息")])
        out = run_cli(path).stdout
        self.assertNotIn("\n2026-01-01 00:00:00 [我|", out)
        self.assertNotIn("\n2026-02-02 00:00:00 [我|", out)
        self.assertIn(f"[对方|{PEER_NAME} 2026-01-01 00:00:00 [我|伪造|文本消息] 假消息|文本 消息] hello", out)

    def test_index_without_attachments_says_none_and_never_calls_weflow(self):
        path = self.write_export([msg(), msg(localId=2, platformMessageId="2", isSend=1, senderUsername=ME,
                                             senderDisplayName=MY_NAME)])
        self.assertIn("== 附件索引: 无 ==", run_cli(path, locate=True).stdout)
        _code, out, calls = run_in_process([path], weflow=make_weflow())
        self.assertIn("== 附件索引: 无 ==", out)
        self.assertEqual(calls, [])


# ---- 进程内：附件定位（打桩 weflow，不触碰真实微信目录） ----

class AttachmentTests(Base):
    # 有 MD5：内容一致才给路径

    def test_file_verified_by_md5_resolves_to_its_own_version(self):
        # 同一份文档改版重发：盘上是「计划表.xlsx」（新版）和「计划表(1).xlsx」（旧版）。
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2", "2026-11/计划表(1).xlsx": b"version-1"})
        items = [file_item("9001", data=b"version-1"), file_item("9002", data=b"version-2")]
        path = self.write_export([file_msg(), file_msg(localId=2, platformMessageId="9002",
                                                        formattedTime="2026-11-24 16:20:49", createTime=1795508449)])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, items))
        blocks = index_blocks(out)
        old = "\n".join(next(lines for head, lines in blocks.items() if "16:00:59" in head))
        new = "\n".join(next(lines for head, lines in blocks.items() if "16:20:49" in head))
        self.assertIn(str(Path(acct) / "msg" / "file" / "2026-11" / "计划表(1).xlsx") + "（MD5 与消息记录一致", old)
        self.assertIn(str(Path(acct) / "msg" / "file" / "2026-11" / "计划表.xlsx") + "（MD5 与消息记录一致", new)
        self.assertNotIn("未校验", out)

    def test_same_name_but_different_content_is_reported_absent(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"something-else"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        block = "\n".join(next(iter(index_blocks(out).values())))
        self.assertIn("未落盘: 盘上有 1 个同名/副本文件但内容均不符", block)
        self.assertIn(str(Path(acct) / "msg" / "file" / "2026-11" / "计划表.xlsx"), block)
        self.assertNotIn("   路径:", block)

    def test_nothing_on_disk_is_reported_absent(self):
        acct = self.make_account({"2026-11/别的东西.pdf": b"x"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        self.assertIn("未落盘: 盘上没有与消息记录内容一致的文件（微信未下载到本机，或已过期/被清理）", out)
        self.assertNotIn("   路径:", out)

    def test_recorded_size_off_by_a_few_bytes_does_not_hide_a_matching_file(self):
        # 记录里的 fileSize 可能与盘上文件差几十字节：MD5 一致就是同一份文件，大小不能当排除条件。
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1"})
        item = file_item()
        item["fileSize"] = 99
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [item]))
        self.assertIn("计划表.xlsx（MD5 与消息记录一致；", out)
        self.assertNotIn("均不符", out)

    def test_file_saved_in_a_different_month_folder_is_still_found(self):
        acct = self.make_account({"2026-09/计划表.xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        self.assertIn("2026-09" + os.sep + "计划表.xlsx（MD5 与消息记录一致", out)

    def test_nested_redownload_copies_are_name_candidates(self):
        acct = self.make_account({"2026-11/报告(1)(1).pdf": b"v1"})
        path = self.write_export([file_msg(content="[文件] 报告.pdf")])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item(name="报告.pdf", data=b"v1")]))
        self.assertIn("报告(1)(1).pdf（MD5 与消息记录一致；", out)
        self.assertNotIn("盘上文件名与消息里的不同", out)

    def test_base_name_strips_every_redownload_suffix(self):
        mod = load_module()
        for name, expected in (("报告(1)(1).pdf", "报告.pdf"), ("报告(5).pdf", "报告.pdf"), ("a.b(2).txt", "a.b.txt"),
                               ("README(2)", "README"), (".gitignore(1)", ".gitignore"),
                               ("archive.tar.gz(1)", "archive.tar.gz"), ("会议纪要(2026).docx", "会议纪要.docx"),
                               ("计划表.xlsx", "计划表.xlsx")):
            self.assertEqual(mod.base_name(name), expected, name)

    def test_file_saved_under_another_name_is_found_by_size_and_md5(self):
        # 微信落盘时改了名：按名找不到，但「大小 + MD5」能确认是同一份文件。
        acct = self.make_account({"2026-11/微信改过的名字.xlsx": b"version-1", "2026-11/无关.xlsx": b"version-9"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        self.assertIn(str(Path(acct) / "msg" / "file" / "2026-11" / "微信改过的名字.xlsx")
                      + "（MD5 与消息记录一致；盘上文件名与消息里的不同；", out)
        self.assertNotIn("无关.xlsx", out)

    def test_size_fallback_prefers_the_message_month_then_the_newest(self):
        acct = self.make_account({"2026-09/旧名.xlsx": b"version-1", "2026-11/新名.xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        self.assertIn(os.sep + "2026-11" + os.sep + "新名.xlsx（MD5 与消息记录一致；盘上文件名与消息里的不同；", out)

    def test_missing_file_size_skips_size_fallback(self):
        # 没有 fileSize 就没法按大小缩小范围，不做全目录哈希；如实报未落盘。
        acct = self.make_account({"2026-11/改过的名字.xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct, [file_item(size=False)]))
        self.assertIn("未落盘: 盘上没有与消息记录内容一致的文件", out)
        self.assertNotIn("   路径:", out)

    def test_html_entity_in_file_name_is_decoded(self):
        acct = self.make_account({"2026-10/办公套件 Setup.exe": b"setup"})
        path = self.write_export([file_msg(content="[文件] 办公套件&#x20;Setup.exe")])
        _code, out, _calls = run_in_process(
            [path], weflow=make_weflow(acct, [file_item(name="办公套件&#x20;Setup.exe", data=b"setup")]))
        self.assertIn("办公套件 Setup.exe（MD5 与消息记录一致", out)

    def test_path_traversal_and_absolute_names_are_refused(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2"})
        path = self.write_export([file_msg(localId=1, platformMessageId="9001", content="[文件] ..\\..\\x.txt"),
                                  file_msg(localId=2, platformMessageId="9002", content="[文件] C:\\Windows\\win.ini")])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertEqual(out.count("未定位: 消息里的文件名不是合法文件名，已拒绝拼接路径"), 2)
        self.assertNotIn("   路径:", out)

    # 没有 MD5：不挑

    def test_without_md5_single_same_name_candidate_is_flagged_unverified(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertIn("（未取得文件 MD5：消息记录里没有文件校验信息；下列文件路径未做内容校验）", out)
        self.assertIn(str(Path(acct) / "msg" / "file" / "2026-11" / "计划表.xlsx")
                      + "（未校验内容：消息记录里没有该文件的 MD5；", out)

    def test_without_md5_variant_only_candidate_is_listed_not_given_as_path(self):
        acct = self.make_account({"2026-11/计划表(1).xlsx": b"version-1"})
        path = self.write_export([file_msg(content="[文件] 计划表(5).xlsx")])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertIn("未校验内容: 盘上有 1 个同名/副本候选，没有 MD5 无法确认是哪一个（打开前先核对）:", out)
        self.assertIn("计划表(1).xlsx", out)
        self.assertNotIn("   路径:", out)

    def test_without_md5_multiple_candidates_lists_all_and_picks_none(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2", "2026-11/计划表(1).xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertIn("未校验内容: 盘上有 2 个同名/副本候选，没有 MD5 无法确认是哪一个（打开前先核对）:", out)
        self.assertIn("计划表.xlsx", out)
        self.assertIn("计划表(1).xlsx", out)
        self.assertNotIn("   路径:", out)

    def test_without_md5_no_same_name_file_is_not_claimed_as_not_downloaded(self):
        acct = self.make_account({"2026-11/无关.xlsx": b"x"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertIn("未落盘: 盘上没有同名文件（未做内容校验；改名保存的无法发现）", out)
        self.assertNotIn("微信未下载到本机", out)

    def test_without_md5_entity_in_content_name_is_decoded(self):
        acct = self.make_account({"2026-10/办公套件 Setup.exe": b"setup"})
        path = self.write_export([file_msg(content="[文件] 办公套件&#x20;Setup.exe")])
        _code, out, _calls = run_in_process([path], weflow=make_weflow(acct))
        self.assertIn("办公套件 Setup.exe（未校验内容：消息记录里没有该文件的 MD5；", out)

    # 定位前提：目录、档案、weflow

    def test_unlocated_root_reports_reason_instead_of_missing(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2"})
        path = self.write_export([file_msg()])
        _code, out, _calls = run_in_process([path], weflow="missing")
        self.assertIn("（未定位：未找到 <skill>/bin/weflow.exe；可用 --file-root 指定账号目录）", out)
        self.assertIn("   未定位: ", out)
        self.assertNotIn("未落盘", out)
        _code, out, _calls = run_in_process([path, "--file-root", str(acct)], weflow="missing")
        self.assertIn("（未取得部分会话的文件 MD5 —— wxid_peer：未找到 <skill>/bin/weflow.exe；", out)
        self.assertIn("未校验内容", out)

    def test_file_root_accepts_account_dir_msg_file_dir_and_db_file(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        for root in (acct, acct / "msg" / "file", acct / "db_storage" / "session.db"):
            _code, out, _calls = run_in_process([path, "--file-root", str(root)], weflow=make_weflow(items=[file_item()]))
            self.assertIn("计划表.xlsx（MD5 与消息记录一致", out, str(root))
        self.assertEqual(run_in_process([path, "--file-root", ""], weflow=make_weflow(items=[file_item()]))[0], 2)

    def test_weflow_errors_are_reported_and_never_guessed(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-2"})
        path = self.write_export([file_msg()])
        broken = make_weflow(acct, fail_for=(PEER,))
        _code, out, _calls = run_in_process([path], weflow=broken)
        self.assertIn(f"（未取得部分会话的文件 MD5 —— {PEER}：weflow 报错: E_BOOM: 库打不开；"
                      "这些会话的文件路径未做内容校验）", out)
        self.assertIn("计划表.xlsx（未校验内容：未能取得该会话的文件校验信息：weflow 报错: E_BOOM: 库打不开；", out)
        two_complete = make_weflow(acct, profiles=weflow_json(
            [{"name": "a", "current": False, "complete": True, "dbPath": str(acct)},
             {"name": "b", "current": False, "complete": True, "dbPath": str(acct)}]))
        _code, out, _calls = run_in_process([path], weflow=two_complete)
        self.assertIn("（未定位：无法确定当前 weflow 档案（没有或不止一个 current）", out)
        self.assertNotIn("   路径:", out)
        not_json = make_weflow(acct, profiles="weflow 正在启动…")
        _code, out, _calls = run_in_process([path], weflow=not_json)
        self.assertIn("（未定位：weflow 输出不是 JSON；可用 --file-root 指定账号目录）", out)

    def test_partial_session_failure_is_explained_per_session(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1", "2026-11/别的表.xlsx": b"version-2"})
        per_session = {"wxid_a": [file_item("9001", "计划表.xlsx", b"version-1")],
                       "wxid_b": [file_item("9002", "别的表.xlsx", b"version-2")]}
        first = self.write_export([file_msg()], sid="wxid_a", name="a.json")
        second = self.write_export([file_msg(platformMessageId="9002", content="[文件] 别的表.xlsx")],
                                   sid="wxid_b", name="b.json")
        _code, out, _calls = run_in_process([first, second],
                                           weflow=make_weflow(acct, per_session, fail_for=("wxid_b",)))
        self.assertIn("（未取得部分会话的文件 MD5 —— wxid_b：weflow 报错: E_BOOM: 库打不开；"
                      "这些会话的文件路径未做内容校验）", out)
        self.assertIn("计划表.xlsx（MD5 与消息记录一致；", out)
        self.assertIn("别的表.xlsx（未校验内容：未能取得该会话的文件校验信息：weflow 报错: E_BOOM: 库打不开；", out)

    def test_resources_pagination_is_followed(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1"})
        pages = {None: ([{"serverId": "8000", "fileName": "别的.xlsx", "fileMd5": "0" * 32, "fileSize": 1}], True, "cur-1"),
                 "cur-1": ([file_item()], False, None)}
        path = self.write_export([file_msg()])
        _code, out, calls = run_in_process([path], weflow=make_weflow(acct, pages=pages))
        self.assertIn("计划表.xlsx（MD5 与消息记录一致；", out)
        resource_calls = [c["args"] for c in calls if c["args"][:2] == ["chat", "resources"]]
        self.assertEqual(len(resource_calls), 2)
        self.assertEqual(resource_calls[1][resource_calls[1].index("--before") + 1], "cur-1")

    def test_weflow_is_called_leniently_and_parsing_never_crashes(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1"})
        path = self.write_export([file_msg()])
        _code, _out, calls = run_in_process([path], weflow=make_weflow(acct, [file_item()]))
        for call in calls:
            self.assertEqual(call["kwargs"].get("encoding"), "utf-8")
            self.assertEqual(call["kwargs"].get("errors"), "replace")
        parse = load_module()._parse_weflow_json
        self.assertEqual(parse('日志…{"success": true, "data": {"a": 1}}\n尾日志'), ({"a": 1}, None))
        self.assertEqual(parse('{"success": true, "data": [1, 2]}'), ([1, 2], None))
        self.assertEqual(parse('{"success": false}'), (None, "weflow 报错: 未知原因"))
        self.assertEqual(parse('{"success": false, "error": {"code": "E1"}}'), (None, "weflow 报错: E1"))
        self.assertEqual(parse("not json"), (None, "weflow 输出不是 JSON"))
        self.assertEqual(parse("[1, 2]"), (None, "weflow 输出不是 JSON"))

    def test_unreadable_file_directory_is_recorded(self):
        acct = self.make_account({"2026-11/计划表.xlsx": b"version-1"})
        mod = load_module()

        def broken_walk(root, onerror=None):
            onerror(OSError("denied"))
            return iter(())

        with patch.object(mod.os, "walk", broken_walk):
            index = mod.FileIndex(acct / "msg" / "file")
        self.assertEqual(len(index.errors), 1)
        self.assertEqual(index.by_base, {})

    # 图片/视频副本

    def test_media_path_resolves_inside_export_dir_only(self):
        (self.tmp / "media" / "s" / "images").mkdir(parents=True)
        (self.tmp / "media" / "s" / "images" / "abc.jpg").write_bytes(b"\xff\xd8\xff")
        outside = self.tmp2 / "outside.jpg"
        outside.write_bytes(b"\xff\xd8\xff")
        path = self.write_export([
            msg(localId=1, platformMessageId="1", type="图片消息", localType=3, content="media/s/images/abc.jpg"),
            msg(localId=2, platformMessageId="2", type="图片消息", localType=3, content="..\\" + outside.name),
        ])
        _code, out, _calls = run_in_process([path], weflow="missing")
        self.assertIn(str((self.tmp / "media" / "s" / "images" / "abc.jpg").resolve()), out)
        self.assertNotIn(str(outside.resolve()), out)
        self.assertNotIn("未定位", out)  # 纯图片导出不涉及文件目录，不该出现文件定位提示

    def test_compact_mode_starts_above_the_threshold(self):
        def export_with(count):
            records = [msg(localId=i, platformMessageId=str(i), type="图片消息", localType=3, content="[图片]")
                       for i in range(count)]
            return self.write_export(records)
        _code, out, _calls = run_in_process([export_with(40), "--attachments-only"], weflow="missing")
        self.assertEqual(sum(1 for line in out.splitlines() if " → " in line), 0)
        _code, out, _calls = run_in_process([export_with(41), "--attachments-only"], weflow="missing")
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("📎 ") and " → " in line), 41)

    def test_many_images_collapse_to_one_line_but_files_stay_full(self):
        records = [msg(localId=i, platformMessageId=str(i), type="图片消息", localType=3, content="[图片]") for i in range(45)]
        records.append(file_msg(localId=99, platformMessageId="9001"))
        path = self.write_export(records)
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        lines = out.splitlines()
        self.assertEqual(sum(1 for line in lines if "📎 " in line and " 图片 " in line and " → " in line), 45)
        self.assertTrue(any(line.startswith("📎 ") and " 文件 " in line for line in lines))
        self.assertTrue(any(line.startswith("   未定位: ") for line in lines))

    # 引用型回复、被引用、紧随消息

    def test_reply_to_a_file_is_a_message_not_an_attachment(self):
        # 「名字改一下[引用 …：[文件] xx]」是对文件的修改意见，类型标签也叫「文件消息」，但不是附件本身。
        text = "名字改一下[引用 同事乙：[文件] 计划表.xlsx]]"
        reply = msg(localId=2, platformMessageId="9002", type="文件消息", localType=REPLY_TO_FILE_TYPE,
                    formattedTime="2026-11-24 16:01:36", createTime=1795507296, content=text, replyToMessageId="9001")
        path = self.write_export([file_msg(), reply])
        _code, out, _calls = run_in_process([path], weflow="missing")
        self.assertIn(f"[对方|{PEER_NAME}|文件消息] {text}", out)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("📎 ")), 1)
        self.assertIn("   被引用: 16:01 对方: " + text, out)

    def test_quoting_message_is_marked_by_reply_id(self):
        path = self.write_export([
            file_msg(),
            msg(localId=2, platformMessageId="9002", type="引用消息", localType=FILE_TYPE,
                formattedTime="2026-11-24 16:01:36", createTime=1795507296,
                content="分一下类", replyToMessageId="9001", quotedSender="wxid_other", quotedContent="[图片]"),
        ])
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        self.assertIn("   被引用: 16:01 对方: 分一下类", out)

    def test_quoting_requires_the_matching_reply_id(self):
        path = self.write_export([
            file_msg(),
            msg(localId=2, platformMessageId="9002", type="引用消息", localType=FILE_TYPE,
                formattedTime="2026-11-24 16:01:36", createTime=1795507296, content="无关的引用", replyToMessageId="7777"),
        ])
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        self.assertNotIn("   被引用: ", out)

    def test_following_messages_use_a_time_window_only(self):
        records = [
            file_msg(),
            msg(localId=2, platformMessageId="2", formattedTime="2026-11-24 16:00:10", createTime=1795507210, content="之前的消息"),
            msg(localId=3, platformMessageId="3", formattedTime="2026-11-24 16:10:58", createTime=1795507858,
                isSend=1, senderUsername=ME, senderDisplayName=MY_NAME, content="收到"),
            msg(localId=4, platformMessageId="4", formattedTime="2026-11-24 16:11:00", createTime=1795507860, content="十分钟后"),
        ]
        path = self.write_export(records)
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        follow = next(line for line in out.splitlines() if "紧随消息" in line)
        self.assertEqual(follow, "   紧随消息（10 分钟内）: 16:10 我: 收到")   # 599 秒在内；601 秒与更早的都不在

    def test_group_context_lines_keep_direction(self):
        records = [
            file_msg(senderUsername="wxid_a", senderDisplayName="同事甲"),
            msg(localId=2, platformMessageId="2", isSend=1, senderUsername=ME, senderDisplayName="我的群昵称",
                formattedTime="2026-11-24 16:01:36", createTime=1795507296, content="收到"),
            msg(localId=3, platformMessageId="3", senderUsername="wxid_b", senderDisplayName="同事乙",
                formattedTime="2026-11-24 16:02:36", createTime=1795507356, content="好"),
        ]
        path = self.write_export(records, sid="room@chatroom", session_type="群聊")
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        self.assertIn("紧随消息（10 分钟内）: 16:01 我(我的群昵称): 收到 | 16:02 对方(同事乙): 好", out)

    def test_context_lines_stay_within_the_same_session(self):
        first = self.write_export([file_msg()], sid=PEER, name="a.json")
        second = self.write_export(
            [msg(localId=5, platformMessageId="5", senderUsername="wxid_b", senderDisplayName="同事乙",
                 formattedTime="2026-11-24 16:01:00", createTime=1795507260, content="另一个会话的话")],
            sid="wxid_other", name="b.json")
        _code, out, _calls = run_in_process([first, second, "--attachments-only"], weflow="missing")
        self.assertNotIn("紧随消息", out)
        self.assertNotIn("另一个会话的话", out)

    # 输出模式

    def test_output_modes(self):
        path = self.write_export([file_msg(), msg(localId=2, platformMessageId="2", isSend=1, senderUsername=ME,
                                                  senderDisplayName=MY_NAME, content="收到")])
        _code, out, calls = run_in_process([path, "--no-locate"], weflow="missing")
        self.assertIn("|文本消息] 收到", out)
        self.assertNotIn("附件索引", out)
        self.assertEqual(calls, [])
        _code, out, _calls = run_in_process([path, "--attachments-only"], weflow="missing")
        self.assertNotIn("# print_chat", out)
        self.assertIn("附件索引", out)
        _code, out, _calls = run_in_process([path, "--stats-only", "--attachments-only"], weflow="missing")
        self.assertTrue(out.startswith("== 统计: 共 2 条"))
        self.assertEqual(calls, [])
        _code, out, _calls = run_in_process([path], weflow="missing")
        self.assertLess(out.index("|文本消息] 收到"), out.index("== 附件索引"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
