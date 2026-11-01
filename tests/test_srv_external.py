"""外部服务登记与迁入（EXTERNAL.tsv、create --takeover、takeover-copy）的面板侧逻辑。

srv-project 只透传参数，所有读写、校验、属主判定都在 srv_panel.py，所以这里直接测面板函数；
shell 侧（trap 回滚、delete 静默跳过、5 列表头）由容器端到端覆盖。
路径常量全部指向临时目录（含两把锁文件），系统账号/监听/子进程一律打桩，不碰真实系统：
在 Linux 上跑（含非 root）时，flock 真实生效，锁文件也必须落在沙箱里，不能碰真实 /srv。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

ROOT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server"
SPEC = importlib.util.spec_from_file_location("srv_panel_external", ROOT / "panel" / "srv_panel.py")
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)

# 账号表：名称 → (uid, 是否 srvproj 组成员)
ACCOUNTS = {"appuser": (1001, False), "alice": (1002, False), "root": (0, False), "www-data": (33, False),
            "demo": (1100, True), "legacy": (1101, True)}


def account_info(name):
    return ACCOUNTS.get(name, (None, False))


def listen(port, *owners):
    return {"addr": "0.0.0.0", "port": port, "procs": [("x", 1)], "owners": list(owners)}


class Sandbox(unittest.TestCase):
    """临时目录里的 /srv：PROJECTS.tsv、EXTERNAL.tsv 与账号、监听均可控。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = os.path.join(self.tmp, "srv")
        os.makedirs(self.srv)
        self.ext = os.path.join(self.srv, "EXTERNAL.tsv")
        self.reg = os.path.join(self.srv, "PROJECTS.tsv")
        self.listening = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (("EXTERNAL", self.ext), ("EXTERNAL_LOCK", os.path.join(self.srv, ".lock")),
                            ("REGISTRY", self.reg), ("REGISTRY_LOCK", os.path.join(self.srv, ".PROJECTS.lock")),
                            ("SRV_ROOT", self.srv),
                            ("TAKEOVER_STAGING", os.path.join(self.srv, ".takeover"))):
            stack.enter_context(patch.object(panel, name, value))
        stack.enter_context(patch.object(panel, "account_info", account_info))
        stack.enter_context(patch.object(panel, "_account_exists", lambda n: n in ACCOUNTS))
        stack.enter_context(patch.object(panel, "_uid_of", lambda n: ACCOUNTS.get(n, (None,))[0]))
        stack.enter_context(patch.object(panel, "listeners", lambda: list(self.listening)))
        stack.enter_context(patch.object(panel, "audit"))
        stack.enter_context(patch.dict(os.environ, {"SUDO_USER": "appuser"}))

    def write_registry(self, *rows):
        with open(self.reg, "w", encoding="utf-8", newline="") as f:     # 不让 Windows 把换行翻成 CRLF
            f.write("# 项目\t端口\t联系人\t创建时间\t来源\n" + "".join(r + "\n" for r in rows))

    def rows(self):
        return panel.parse_external(Path(self.ext).read_text(encoding="utf-8")) if os.path.exists(self.ext) else []

    def add(self, name="oldsvc", port=9200, owner="appuser", **kw):
        kw.setdefault("lst", list(self.listening))
        return panel.external_add(name, port, owner, **kw)


class ParseTests(unittest.TestCase):
    def test_roundtrip(self):
        text = panel.EXT_HEADER + "a1\t9200\tappuser\tuser:x.service\t1\t\t说明\t2026-10-31 10:00:00\tappuser\n"
        rows = panel.parse_external(text)
        self.assertEqual(rows[0]["unit"], "user:x.service")
        self.assertEqual(rows[0]["target"], "")
        self.assertEqual(panel.dump_external(rows), text)

    def test_short_lines_padded(self):
        self.assertEqual(panel.parse_external("a1\t9200\tappuser\n")[0]["created"], "")

    def test_registry_four_and_five_columns(self):
        rows = panel.parse_registry("# h\nold\t3000\tc\t2026-01-01 00:00:00\nnew\t3001\t\t2026-01-02 00:00:00\texternal:foo\n")
        self.assertEqual((rows[0]["source"], rows[1]["source"]), ("", "external:foo"))
        self.assertEqual(rows[1]["contact"], "")

    def test_registry_note_column(self):
        """第 6 列 note：6 列行读出注释；旧 4/5 列行按空注释处理（不迁移）。"""
        rows = panel.parse_registry(
            "# 项目\t端口\t联系人\t创建时间\t来源\t注释\n"
            "a\t3000\tc\tt\texternal:x\t备注A\n"
            "b\t3001\t\tt\t\t\n"
            "c\t3002\t\tt\n")
        self.assertEqual([r["note"] for r in rows], ["备注A", "", ""])
        self.assertEqual([r["source"] for r in rows], ["external:x", "", ""])

    def test_light_record_source(self):
        e = {"name": "x1", "port": "3000", "contact": "", "created": "t", "source": "external:foo"}
        self.assertEqual(panel.light_record(e, False, False, [])["source"], "external:foo")
        e["source"] = ""
        self.assertIsNone(panel.light_record(e, False, False, [])["source"])
        e.pop("source")
        self.assertIsNone(panel.light_record(e, False, False, [])["source"])


class AddTests(Sandbox):
    def test_add_default_not_migratable(self):
        row, notes = self.add()
        self.assertEqual((row["migratable"], row["target"]), ("0", ""))
        self.assertEqual(self.rows()[0]["name"], "oldsvc")
        self.assertIn("登记时未监听", notes)
        self.assertIn("登记时未监听", row["desc"])
        self.assertTrue(Path(self.ext).read_text(encoding="utf-8").startswith("# 名称"))

    def test_add_listening_matching_owner(self):
        self.listening = [listen(9200, "appuser")]
        row, notes = self.add(desc="知识库后端", unit="user:kb.service")
        self.assertEqual(notes, [])
        self.assertEqual((row["desc"], row["unit"]), ("知识库后端", "user:kb.service"))

    def test_injection_rejected_and_file_untouched(self):
        for kw in ({"desc": "a\tb"}, {"desc": "a\nb"}, {"desc": "a\rb"}, {"unit": "user:x.service\tz"},
                   {"unit": "user:x.service\n"}):
            with self.assertRaises(panel.ApiExit, msg=kw):
                self.add(**kw)
        self.assertFalse(os.path.exists(self.ext))

    def test_unit_format(self):
        for good in ("", "user:kb.service", "system:nginx.service"):
            self.assertTrue(panel.valid_ext_unit(good), good)
        for bad in ("kb.service", "user:", "user:kb", "root:kb.service", "user:../x.service", "system:a b.service"):
            self.assertFalse(panel.valid_ext_unit(bad), bad)

    def test_desc_length(self):
        self.add(desc="字" * 60)
        with self.assertRaises(panel.ApiExit):
            self.add(name="other1", port=9201, desc="字" * 61)

    def test_bad_name_port_owner(self):
        for kw in ({"name": "X"}, {"name": "a"}, {"port": 0}, {"port": 65536}, {"port": "x"}, {"owner": "ghost"},
                   {"owner": "bad;name"}):
            with self.assertRaises(panel.ApiExit, msg=kw):
                self.add(**kw)

    def test_low_port_allowed_read_only(self):
        self.listening = [listen(22, "root")]
        self.add(name="sshd1", port=22, owner="root")
        self.assertEqual(self.rows()[0]["port"], "22")

    def test_duplicate_external_and_project_name(self):
        self.add()
        with self.assertRaisesRegex(panel.ApiExit, "已登记"):
            self.add(port=9300)
        self.write_registry("proj1\t3000\t\t2026-10-31 10:00:00\t")
        with self.assertRaisesRegex(panel.ApiExit, "重名"):
            self.add(name="proj1", port=9300)

    def test_port_clash_with_project_or_external(self):
        self.write_registry("proj1\t3000\t\t2026-10-31 10:00:00\t")
        with self.assertRaisesRegex(panel.ApiExit, "项目 proj1"):
            self.add(port=3000)
        self.add()
        with self.assertRaisesRegex(panel.ApiExit, "外部服务 oldsvc"):
            self.add(name="other1")

    def test_conflict_check_reads_registry_beyond_1mib(self):
        """展示路径的 1 MiB 截断保护不适用于冲突核对：冲突行在 1 MiB 之后也必须被发现（不能据残缺表放行）。"""
        pad = ["p%05d\t%d\t\t2026-01-01 00:00:00\t" % (i, 20000 + i) for i in range(40000)]
        pad.append("latep\t9200\t\t2026-01-01 00:00:00\t")
        self.write_registry(*pad)
        self.assertGreater(os.path.getsize(self.reg), 1 << 20)
        with self.assertRaisesRegex(panel.ApiExit, "已登记为项目 latep"):
            self.add()
        self.assertFalse(os.path.exists(self.ext))

    def test_lock_order_is_external_then_registry(self):
        """external add 与 create 的 register 共用同一把 EXTERNAL 锁，嵌套方向固定 EXTERNAL → PROJECTS。"""
        order = []
        real = panel._flock
        def rec(path):
            order.append(path)
            return real(path)
        with patch.object(panel, "_flock", rec):
            self.add()
        self.assertEqual(order, [panel.EXTERNAL_LOCK, panel.REGISTRY_LOCK])

    @unittest.skipUnless(HAS_FCNTL, "flock 只在有 fcntl 的平台（Linux）生效；Windows 上无锁，测不出互斥")
    def test_add_waits_for_external_lock_then_rechecks(self):
        """external add 与 create 的 register 的检查-落盘在同一 EXTERNAL 临界区：
        持锁期间 register 追加的项目行，必须等 external add 进锁后重新读到并拒绝（不是 precheck 式的检查完就放锁）。"""
        import threading
        outcome = []
        def worker():
            try:
                panel.external_add("raced", "9200", "appuser", lst=list(self.listening))
                outcome.append("ok")
            except panel.ApiExit as e:
                outcome.append("clash" if "已登记为项目" in str(e) else "other:%s" % e)
        with panel.external_lock():
            self.write_registry("racedp\t9200\t\t2026-01-01 00:00:00\t")   # 模拟并发的 create 已在锁内落盘
            t = threading.Thread(target=worker)
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive())        # 锁被持有时进不来
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcome, ["clash"])     # 进锁后重新读到冲突行
        self.assertFalse(os.path.exists(self.ext))

    def test_project_account_as_owner_rejected(self):
        with self.assertRaisesRegex(panel.ApiExit, "项目账号"):
            self.add(owner="demo")

    def test_listening_by_project_account_rejected(self):
        self.listening = [listen(9200, "demo")]
        with self.assertRaisesRegex(panel.ApiExit, "项目账号 demo"):
            self.add(owner="appuser")
        self.assertFalse(os.path.exists(self.ext))

    def test_listening_owner_mismatch_rejected(self):
        self.listening = [listen(9200, "alice")]
        with self.assertRaisesRegex(panel.ApiExit, "不符"):
            self.add(owner="appuser")

    def test_listening_unknown_owner_rejected(self):
        self.listening = [listen(9200)]
        with self.assertRaisesRegex(panel.ApiExit, "看不到进程属主"):
            self.add()

    def test_mixed_owners_rejected(self):
        self.listening = [listen(9200, "appuser", "alice")]
        with self.assertRaises(panel.ApiExit):
            self.add()


class MigrateInvariantTests(Sandbox):
    """不变量 1：可迁入只允许出现在调用者自己的服务上。"""

    def test_delegate_can_migrate_own_service(self):
        row, _ = self.add(migrate=True, caller="appuser")
        self.assertEqual(row["migratable"], "1")

    def test_delegate_cannot_register_roots_service_as_migratable(self):
        self.listening = [listen(8080, "root")]
        with self.assertRaisesRegex(panel.ApiExit, "只能迁入调用者自己"):
            self.add(name="rootsvc", port=8080, owner="root", migrate=True, caller="appuser")

    def test_delegate_cannot_register_someone_elses_service_as_migratable(self):
        self.listening = [listen(9200, "alice")]
        with self.assertRaisesRegex(panel.ApiExit, "只能迁入调用者自己"):
            self.add(owner="alice", migrate=True, caller="appuser")
        # 只读登记仍然可以
        self.add(owner="alice", migrate=False, caller="appuser")

    def test_root_caller_not_bound_by_caller_rule_but_system_accounts_never_migratable(self):
        self.add(owner="alice", migrate=True, caller="root")
        for owner in ("root", "www-data"):
            with self.assertRaisesRegex(panel.ApiExit, "系统账号"):
                self.add(name="sys-" + owner.replace("-", ""), port=9300, owner=owner, migrate=True, caller="root")

    def test_low_port_not_migratable(self):
        with self.assertRaisesRegex(panel.ApiExit, "低于 1024"):
            self.add(port=900, migrate=True, caller="appuser")
        self.assertFalse(os.path.exists(self.ext))

    def test_caller_comes_from_sudo_user(self):
        with patch.dict(os.environ, {"SUDO_USER": "alice"}):
            self.assertEqual(panel.caller_user(), "alice")
            with self.assertRaises(panel.ApiExit):
                panel.external_add("oldsvc", 9200, "appuser", migrate=True, lst=[])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUDO_USER", None)
            self.assertEqual(panel.caller_user(), "root")


class RecordTests(Sandbox):
    def rec(self, row, reg=(), lst=()):
        return panel.external_record(row, list(reg), list(lst))

    def row(self, **kw):
        base = {"name": "oldsvc", "port": "9200", "owner": "appuser", "unit": "", "migratable": "1", "target": "",
                "desc": "d", "created": "t"}
        base.update(kw)
        return base

    def test_plain(self):
        r = self.rec(self.row(), lst=[listen(9200, "appuser")])
        self.assertEqual((r["listening"], r["listen_owner"], r["owner_match"], r["migrate_state"], r["problems"]),
                         (True, "appuser", True, None, []))
        self.assertTrue(r["migratable"])
        self.assertIsNone(r["unit"])
        json.dumps(r)

    def test_owner_mismatch_is_a_problem(self):
        r = self.rec(self.row(), lst=[listen(9200, "alice")])
        self.assertFalse(r["owner_match"])
        self.assertTrue(r["problems"])

    def test_not_listening_is_a_note(self):
        r = self.rec(self.row())
        self.assertEqual((r["listening"], r["owner_match"], r["problems"]), (False, None, []))
        self.assertIn("当前未监听", r["notes"])

    def test_migrating_then_switched(self):
        reg = [{"name": "newproj", "port": "9200", "contact": "", "created": "t", "source": "external:oldsvc"}]
        old = self.rec(self.row(target="newproj"), reg, [listen(9200, "appuser")])
        self.assertEqual((old["migrate_state"], old["problems"]), (panel.MIGRATING, []))
        self.assertIn("迁移中：旧服务仍在监听", old["notes"])
        gap = self.rec(self.row(target="newproj"), reg, [])
        self.assertEqual((gap["migrate_state"], gap["problems"]), (panel.MIGRATING, []))
        new = self.rec(self.row(target="newproj"), reg, [listen(9200, "newproj")])
        self.assertEqual((new["migrate_state"], new["problems"]), (panel.SWITCHED, []))

    def test_dangling_target_is_a_problem(self):
        r = self.rec(self.row(target="gone1"), [], [])
        self.assertTrue(any("cancel-migration" in p for p in r["problems"]))

    def test_third_party_listener_during_migration_is_a_problem(self):
        reg = [{"name": "newproj", "port": "9200", "contact": "", "created": "t", "source": "external:oldsvc"}]
        r = self.rec(self.row(target="newproj"), reg, [listen(9200, "alice")])
        self.assertTrue(r["problems"])

    def test_project_port_duplicate_flagged(self):
        reg = [{"name": "p1", "port": "9200", "contact": "", "created": "t", "source": ""}]
        self.assertTrue(self.rec(self.row(), reg)["problems"])

    def test_missing_owner_account(self):
        self.assertTrue(self.rec(self.row(owner="ghost"))["problems"])

    def test_project_problems_for_migrating_project(self):
        ext = [self.row(target="newproj")]
        entry = {"name": "newproj", "port": "9200", "contact": "", "created": "t", "source": "external:oldsvc"}
        with patch.object(panel, "account_info", return_value=(1100, True)), \
             patch.object(panel, "project_keys", return_value=[]), patch.object(panel, "project_services", return_value=([], "")), \
             patch.object(panel, "project_head", return_value=""), patch.object(panel, "disk_usage_kb", return_value=1):
            d = panel.collect_project(entry, [listen(9200, "appuser")], keys=False, externals=ext)
            self.assertEqual(d["problems"], [])
            self.assertIn("迁移中：旧服务仍在监听", d["notes"])
            # 没有来源的项目：旧属主占着端口仍是问题
            plain = dict(entry, source="")
            self.assertTrue(panel.collect_project(plain, [listen(9200, "appuser")], keys=False, externals=ext)["problems"])
            # 来源写了但外部服务的迁入目标不是它：不豁免
            other = [self.row(target="another")]
            self.assertTrue(panel.collect_project(entry, [listen(9200, "appuser")], keys=False, externals=other)["problems"])
            # 第三者占端口：即使在迁入也算问题
            self.assertTrue(panel.collect_project(entry, [listen(9200, "alice")], keys=False, externals=ext)["problems"])

    def test_external_port_problems(self):
        entry = {"name": "p1", "port": "9200"}
        self.assertEqual(len(panel.external_port_problems(entry, [self.row()])), 1)
        self.assertEqual(panel.external_port_problems(entry, [self.row(target="p1")]), [])
        self.assertEqual(panel.external_port_problems(entry, [self.row(port="9201")]), [])

    def test_global_record_labels_both(self):
        lst = [listen(9200, "appuser"), listen(22, "root")]
        g = panel.global_record(lst, {"9200": "newproj"}, [], [], True, True,
                                {"9200": {"name": "oldsvc", "migrate_state": "迁移中"}, "22": {"name": "sshd1", "migrate_state": None}})
        by = {x["port"]: x for x in g["listeners"]}
        self.assertEqual((by[9200]["project"], by[9200]["external"], by[9200]["migrate_state"]), ("newproj", "oldsvc", "迁移中"))
        self.assertEqual((by[22]["project"], by[22]["external"], by[22]["migrate_state"]), (None, "sshd1", None))
        plain = panel.global_record(lst, {}, [], [], True, True)
        self.assertTrue(all(x["external"] is None and x["migrate_state"] is None for x in plain["listeners"]))

    def api_status(self, project=None):
        with patch.object(panel, "light_projects", return_value=[]), patch.object(panel, "scan_srv_dirs", return_value=([], [])), \
             patch.object(panel, "collect_all", return_value=([], list(self.listening), [])):
            return panel.api_status(project)

    def test_status_full_has_externals_single_does_not(self):
        self.add()
        data = self.api_status()
        self.assertEqual([e["name"] for e in data["externals"]], ["oldsvc"])
        json.dumps(data)
        self.write_registry("p1\t3000\t\t2026-10-31 10:00:00\t")
        entry = {"name": "p1", "port": "3000", "contact": "", "created": "t", "source": ""}
        light = panel.light_record(entry, True, False, [])
        with patch.object(panel, "light_projects", return_value=[light]), \
             patch.object(panel, "collect_project", return_value={"entry": entry, "problems": [], "notes": []}):
            single = panel.api_status("p1")
        self.assertEqual(list(single), ["projects"])


class TakeoverBeginTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.listening = [listen(9200, "appuser")]
        self.add(migrate=True, caller="appuser")

    def begin(self, **kw):
        a = dict(ext_name="oldsvc", project="newproj", port="9200", caller="appuser", lst=list(self.listening))
        a.update(kw)
        return panel.external_takeover_begin(**a)

    def test_marks_target(self):
        self.begin()
        self.assertEqual(self.rows()[0]["target"], "newproj")

    def test_second_takeover_refused(self):
        self.begin()
        with self.assertRaisesRegex(panel.ApiExit, "已在迁入"):
            self.begin(project="another")

    def test_not_migratable(self):
        self.add(name="ro1", port=9300, owner="appuser")
        with self.assertRaisesRegex(panel.ApiExit, "未标记可迁入"):
            self.begin(ext_name="ro1", port="9300")

    def test_caller_must_be_owner_or_root(self):
        with self.assertRaisesRegex(panel.ApiExit, "属主"):
            self.begin(caller="alice")
        self.begin(caller="root")

    def test_port_must_equal(self):
        with self.assertRaisesRegex(panel.ApiExit, "端口"):
            self.begin(port="9201")

    def test_project_port_conflict_never_waived(self):
        self.write_registry("taken\t9200\t\t2026-10-31 10:00:00\t")
        before = Path(self.ext).read_text(encoding="utf-8")
        with self.assertRaisesRegex(panel.ApiExit, "项目端口冲突不豁免"):
            self.begin()
        self.assertEqual(Path(self.ext).read_text(encoding="utf-8"), before)

    def test_leading_zero_port_cannot_bypass_project_port_rule(self):
        """项目表里写成 09200 的端口，与外部服务的 9200 是同一个端口：takeover 仍然不豁免。"""
        self.write_registry("taken\t09200\t\t2026-10-31 10:00:00\t")
        before = Path(self.ext).read_text(encoding="utf-8")
        with self.assertRaisesRegex(panel.ApiExit, "项目端口冲突不豁免"):
            self.begin()
        with self.assertRaisesRegex(panel.ApiExit, "项目端口冲突不豁免"):
            self.begin(port="09200")
        self.assertEqual(Path(self.ext).read_text(encoding="utf-8"), before)

    def test_same_port_normalizes_everywhere(self):
        self.assertTrue(panel.same_port("09200", 9200))
        self.assertTrue(panel.same_port("9200", "09200"))
        self.assertFalse(panel.same_port("abc", "abc"))
        self.assertFalse(panel.same_port("9200", "9201"))
        entry = {"name": "p1", "port": "09200"}
        row = {"name": "oldsvc", "port": "9200", "target": ""}
        self.assertEqual(len(panel.external_port_problems(entry, [row])), 1)
        with self.assertRaisesRegex(panel.ApiExit, "--takeover"):
            panel.external_precheck("brandnew", "09200")
        self.write_registry("p2\t09300\t\t2026-10-31 10:00:00\t")
        with self.assertRaisesRegex(panel.ApiExit, "项目 p2"):
            self.add(name="viaext", port=9300)
        with self.assertRaisesRegex(panel.ApiExit, "外部服务 oldsvc"):
            self.add(name="viaext", port="09200")

    def test_problem_text_does_not_name_the_external(self):
        entry = {"name": "p1", "port": "9200"}
        row = {"name": "secret_ext", "port": "9200", "target": ""}
        self.assertNotIn("secret_ext", "".join(panel.external_port_problems(entry, [row])))

    def test_listener_not_the_registered_owner_refused(self):
        self.listening = [listen(9200, "alice")]
        with self.assertRaisesRegex(panel.ApiExit, "拒绝迁入"):
            self.begin(lst=list(self.listening))
        self.listening = [listen(9200, "demo")]
        with self.assertRaises(panel.ApiExit):
            self.begin(lst=list(self.listening))
        self.assertEqual(self.rows()[0]["target"], "")

    def test_nobody_listening_is_fine(self):
        self.begin(lst=[])

    def test_project_name_cannot_equal_external_name(self):
        with self.assertRaisesRegex(panel.ApiExit, "重名"):
            self.begin(project="oldsvc")

    def test_unknown_external(self):
        with self.assertRaises(panel.ApiExit):
            self.begin(ext_name="nosuch1")

    def test_owner_turned_system_account_refused(self):
        rows = self.rows()
        rows[0]["owner"] = "www-data"
        Path(self.ext).write_text(panel.dump_external(rows), encoding="utf-8")
        self.listening = [listen(9200, "www-data")]
        with self.assertRaises(panel.ApiExit):
            self.begin(caller="root", lst=list(self.listening))

    def test_abort_rolls_back_only_own_mark(self):
        self.begin()
        self.assertFalse(panel.external_takeover_abort("oldsvc", "someone"))
        self.assertEqual(self.rows()[0]["target"], "newproj")
        self.assertTrue(panel.external_takeover_abort("oldsvc", "newproj"))
        self.assertEqual(self.rows()[0]["target"], "")
        self.assertFalse(panel.external_takeover_abort("nosuch1", "newproj"))

    def test_precheck_for_plain_create(self):
        with self.assertRaisesRegex(panel.ApiExit, "重名"):
            panel.external_precheck("oldsvc", 3000)
        with self.assertRaisesRegex(panel.ApiExit, "--takeover"):
            panel.external_precheck("brandnew", 9200)
        panel.external_precheck("brandnew", 3000)

    def test_precheck_without_file(self):
        os.remove(self.ext)
        panel.external_precheck("brandnew", 9200)


class RegisterTests(Sandbox):
    """create 的权威登记（register）与回滚（unregister）：唯一性检查与落盘在同一临界区（EXTERNAL → PROJECTS 两把锁）。"""

    def test_appends_five_columns_and_preserves_old_rows(self):
        self.write_registry("old4\t3000\tc\t2026-01-01 00:00:00")      # 旧 4 列行原样保留
        row = panel.external_register("newproj", "9200", contact="张三")
        self.assertEqual((row["name"], row["port"], row["source"]), ("newproj", "9200", ""))
        lines = Path(self.reg).read_text(encoding="utf-8").splitlines()
        self.assertIn("old4\t3000\tc\t2026-01-01 00:00:00", lines)
        new = next(l for l in lines if l.startswith("newproj\t"))
        f = new.split("\t")
        self.assertEqual((f[0], f[1], f[2], f[4]), ("newproj", "9200", "张三", ""))
        self.assertEqual(len(f), 5)
        self.assertTrue(lines[0].startswith("# 项目"))

    def test_registry_without_trailing_newline(self):
        with open(self.reg, "w", encoding="utf-8", newline="") as f:
            f.write("# 项目\t端口\t联系人\t创建时间\t来源\nold4\t3000\t\t2026-01-01 00:00:00\t")   # 末行无换行
        panel.external_register("newproj", "9200")
        rows = panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in rows], ["old4", "newproj"])

    def test_name_and_port_clash_rejected(self):
        self.write_registry("taken\t09200\t\t2026-01-01 00:00:00\t")   # 前导零也不能绕过
        with self.assertRaisesRegex(panel.ApiExit, "已被项目使用"):
            panel.external_register("taken", "9300")
        with self.assertRaisesRegex(panel.ApiExit, "已登记为项目 taken"):
            panel.external_register("other1", "9200")
        self.assertNotIn("other1", Path(self.reg).read_text(encoding="utf-8"))
        self.add(port=9400)                                            # oldsvc 9400 外部服务
        with self.assertRaisesRegex(panel.ApiExit, "重名"):
            panel.external_register("oldsvc", "9301")
        with self.assertRaisesRegex(panel.ApiExit, "已登记为外部服务 oldsvc"):
            panel.external_register("other2", "9400")

    def test_bad_input_rejected(self):
        for kw in ({"name": "Bad"}, {"port": "0"}, {"port": "x"}, {"contact": "a\tb"}, {"source": "weird"}):
            with self.assertRaises(panel.ApiExit, msg=kw):
                panel.external_register(kw.get("name", "p1"), kw.get("port", "9200"),
                                        kw.get("contact", ""), kw.get("source", ""))
        self.assertFalse(os.path.exists(self.reg))

    def test_missing_registry_created_with_header(self):
        """登记表还没建（未 install）时按空表起，表头与 install 一致（6 列）；不因此拒绝首个登记。"""
        panel.external_register("p1", "9200")
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 项目\t端口\t联系人\t创建时间\t来源\t注释\n"))
        self.assertIn("p1\t9200\t", text)
        rows = panel.parse_registry(text)
        self.assertEqual(rows[0]["note"], "")      # 普通 create 仍写 5 列，注释为空

    def test_takeover_source_requires_marked_target(self):
        self.listening = [listen(9200, "appuser")]
        self.add(migrate=True, caller="appuser")
        self.write_registry()
        with self.assertRaisesRegex(panel.ApiExit, "不在迁入项目"):     # 跳过 takeover-begin 直接 register：拒
            panel.external_register("newproj", "9200", source="external:oldsvc")
        panel.external_takeover_begin("oldsvc", "newproj", "9200", "appuser", list(self.listening))
        row = panel.external_register("newproj", "9200", source="external:oldsvc")
        self.assertEqual(row["source"], "external:oldsvc")
        self.assertIn("external:oldsvc", Path(self.reg).read_text(encoding="utf-8"))
        panel.external_unregister("newproj")
        panel.external_takeover_abort("oldsvc", "newproj")
        with self.assertRaisesRegex(panel.ApiExit, "不在迁入项目"):     # 标记回滚后再 register：拒
            panel.external_register("newproj", "9200", source="external:oldsvc")
        with self.assertRaisesRegex(panel.ApiExit, "不在迁入项目"):     # 目标改成别的项目：同样拒
            panel.external_takeover_begin("oldsvc", "other9", "9200", "appuser", list(self.listening))
            panel.external_register("newproj", "9200", source="external:oldsvc")

    def test_unregister_removes_only_its_row(self):
        self.assertFalse(panel.external_unregister("ghost1"))           # 表都没有
        self.write_registry("keep1\t3000\t\t2026-01-01 00:00:00", "gone1\t9200\tc\tt\texternal:x1")
        self.assertTrue(panel.external_unregister("gone1"))
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertIn("keep1\t3000\t\t2026-01-01 00:00:00\n", text)     # 其余行逐字节原样
        self.assertNotIn("gone1", text)
        self.assertFalse(panel.external_unregister("gone1"))

    def test_register_conflict_check_reads_beyond_1mib(self):
        pad = ["p%05d\t%d\t\t2026-01-01 00:00:00\t" % (i, 20000 + i) for i in range(40000)]
        pad.append("latep\t9200\t\t2026-01-01 00:00:00\t")
        self.write_registry(*pad)
        self.assertGreater(os.path.getsize(self.reg), 1 << 20)
        with self.assertRaisesRegex(panel.ApiExit, "已登记为项目 latep"):
            panel.external_register("newproj", "9200")
        self.assertNotIn("newproj", Path(self.reg).read_text(encoding="utf-8"))

    def test_lock_order_is_external_then_registry(self):
        order = []
        real = panel._flock
        def rec(path):
            order.append(path)
            return real(path)
        self.write_registry()
        with patch.object(panel, "_flock", rec):
            panel.external_register("p1", "9200")
        self.assertEqual(order, [panel.EXTERNAL_LOCK, panel.REGISTRY_LOCK])

    @unittest.skipUnless(HAS_FCNTL, "flock 只在有 fcntl 的平台（Linux）生效；Windows 上无锁，测不出互斥")
    def test_register_waits_for_lock_then_rechecks(self):
        """确定性同步测试：锁被持有时 register 进不来（不是「检查完放锁」的假把式）；
        锁释放后它读到的是持锁期间落盘的冲突行，必须被拒。"""
        import threading
        outcome = []

        def worker():
            try:
                panel.external_register("p9", "9200")
                outcome.append("ok")
            except panel.ApiExit as e:
                outcome.append("clash" if "已登记为项目" in str(e) else "other:%s" % e)

        with panel.external_lock():
            self.write_registry("taken\t9200\t\t2026-01-01 00:00:00\t")  # 模拟另一个 create 已在锁内落盘
            t = threading.Thread(target=worker)
            t.start()
            t.join(3)
            self.assertEqual(outcome, [])            # 还堵在锁上，没有偷跑过检查
            self.assertTrue(t.is_alive())
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcome, ["clash"])         # 进锁后重新读到冲突行
        self.assertNotIn("p9", Path(self.reg).read_text(encoding="utf-8"))


class ReleaseAndRemoveTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.listening = [listen(9200, "appuser")]
        self.add(migrate=True, caller="appuser")
        panel.external_takeover_begin("oldsvc", "newproj", "9200", "appuser", list(self.listening))
        self.write_registry("old1\t3000\t\t2026-10-30 10:00:00",
                            "newproj\t9200\tc\t2026-10-31 10:00:00\texternal:oldsvc")

    def test_release_clears_and_is_silent_when_missing(self):
        self.assertEqual(panel.external_release("other1"), 0)
        self.assertEqual(panel.external_release("newproj"), 1)
        self.assertEqual(self.rows()[0]["target"], "")
        self.assertEqual(panel.external_release("newproj"), 0)
        os.remove(self.ext)
        self.assertEqual(panel.external_release("newproj"), 0)
        self.assertFalse(os.path.exists(self.ext))

    def test_release_and_unregister_work_without_account(self):
        """register 后、useradd 前中断：登记行存在但账号不存在，delete 只清行；面板 release 仍能清迁入标记。"""
        rows = self.rows()
        rows[0]["target"] = "orphanp"
        panel._save_external(rows)
        self.write_registry("orphanp\t9200\t\t2026-01-01 00:00:00\texternal:oldsvc")
        self.assertEqual(panel.external_release("orphanp"), 1)
        self.assertEqual(self.rows()[0]["target"], "")
        self.assertTrue(panel.external_unregister("orphanp"))
        self.assertEqual(panel.parse_registry(Path(self.reg).read_text(encoding="utf-8")), [])

    def test_remove_refused_while_migrating(self):
        with self.assertRaisesRegex(panel.ApiExit, "cancel-migration"):
            panel.external_remove("oldsvc", False, lst=list(self.listening))
        self.assertEqual(len(self.rows()), 1)

    def test_remove_when_switched_keeps_project_source(self):
        panel.external_remove("oldsvc", False, lst=[listen(9200, "newproj")])
        self.assertEqual(self.rows(), [])
        self.assertIn("external:oldsvc", Path(self.reg).read_text(encoding="utf-8"))

    def test_cancel_migration_clears_source_and_entry(self):
        panel.external_remove("oldsvc", True, lst=list(self.listening))
        self.assertEqual(self.rows(), [])
        reg = panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))
        self.assertEqual([(r["name"], r["source"]) for r in reg], [("old1", ""), ("newproj", "")])
        self.assertEqual(reg[1]["contact"], "c")
        self.assertTrue(Path(self.reg).read_text(encoding="utf-8").startswith("# 项目"))

    def test_cancel_migration_keeps_note(self):
        """取消迁移按列清空第 5 列来源，第 6 列注释原样保留（不物理删列）。"""
        self.write_registry("old1\t3000\t\t2026-10-30 10:00:00",
                            "newproj\t9200\tc\t2026-10-31 10:00:00\texternal:oldsvc\t迁移期间的备注")
        panel.external_remove("oldsvc", True, lst=list(self.listening), caller="appuser")
        raw = next(l for l in Path(self.reg).read_text(encoding="utf-8").split("\n") if l.startswith("newproj\t"))
        self.assertEqual(raw, "newproj\t9200\tc\t2026-10-31 10:00:00\t\t迁移期间的备注")
        reg = {r["name"]: r for r in panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))}
        self.assertEqual((reg["newproj"]["source"], reg["newproj"]["note"]), ("", "迁移期间的备注"))

    def test_remove_when_switched_keeps_source_and_note(self):
        panel.set_project_note("newproj", "迁来的项目")
        panel.external_remove("oldsvc", False, lst=[listen(9200, "newproj")])
        reg = {r["name"]: r for r in panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))}
        self.assertEqual(reg["newproj"]["source"], "external:oldsvc")     # 已切换删登记：来源与注释都保留
        self.assertEqual(reg["newproj"]["note"], "迁来的项目")

    def test_cancel_migration_clear_source_failure_reports_half_done(self):
        """清迁入目标已落盘、清来源失败时：错误必须如实说「已清迁入目标」并给修复步骤，不能报「未改动」。"""
        for boom in (panel.ApiExit("%s 读取失败，未改动：boom" % self.reg), OSError("disk")):
            if not self.rows()[0]["target"]:        # 第一次失败后目标已清，第二轮重新标记
                rows = self.rows()
                rows[0]["target"] = "newproj"
                panel._save_external(rows)
            with patch.object(panel, "clear_project_source", side_effect=boom):
                with self.assertRaisesRegex(panel.ApiExit, "已清迁入目标") as cm:
                    panel.external_remove("oldsvc", True, lst=list(self.listening), caller="appuser")
            self.assertIn("external remove oldsvc", str(cm.exception))        # 带修复命令
            self.assertEqual(self.rows()[0]["target"], "")                    # 目标确实已清
            self.assertIn("external:oldsvc", Path(self.reg).read_text(encoding="utf-8"))   # 来源还在（清失败）
            self.assertEqual([r["name"] for r in self.rows()], ["oldsvc"])    # 条目未删

    def test_lock_order_is_external_then_registry(self):
        """嵌套取锁只有一种方向：EXTERNAL → PROJECTS（反向会被别的持锁路径死锁）。"""
        order = []
        real = panel._flock
        def rec(path):
            order.append(path)
            return real(path)
        with patch.object(panel, "_flock", rec):
            panel.external_remove("oldsvc", True, lst=list(self.listening), caller="appuser")
        self.assertEqual(order, [panel.EXTERNAL_LOCK, panel.REGISTRY_LOCK])

    @unittest.skipUnless(HAS_FCNTL, "flock 只在有 fcntl 的平台（Linux）生效")
    def test_registry_lock_file_lives_in_sandbox(self):
        """Linux 上 flock 真实生效：锁文件必须落在沙箱（路径常量在 setUp 里被打桩），不碰真实 /srv。"""
        panel.external_remove("oldsvc", True, lst=list(self.listening), caller="appuser")
        self.assertTrue(os.path.exists(os.path.join(self.srv, ".PROJECTS.lock")))
        self.assertEqual(panel.REGISTRY_LOCK, os.path.join(self.srv, ".PROJECTS.lock"))

    def test_remove_plain_entry(self):
        self.add(name="plain1", port=9300)
        self.assertIn("只删登记", panel.external_remove("plain1", False, lst=[]))
        self.assertEqual([r["name"] for r in self.rows()], ["oldsvc"])

    def test_remove_unknown_is_exit_2(self):
        with self.assertRaises(panel.ApiExit) as cm:
            panel.external_remove("nosuch1")
        self.assertEqual(cm.exception.code, 2)

    def test_other_delegate_cannot_cancel_or_remove_migrating_entry(self):
        """委托账号越权：别人发起的迁移，既不能取消，也不能删（迁入中和已切换都一样）。"""
        before_ext = Path(self.ext).read_text(encoding="utf-8")
        before_reg = Path(self.reg).read_text(encoding="utf-8")
        for cancel in (True, False):
            for lst in (list(self.listening), [listen(9200, "newproj")]):
                with self.assertRaisesRegex(panel.ApiExit, "只有 root 或它的属主"):
                    panel.external_remove("oldsvc", cancel, lst=lst, caller="alice")
        self.assertEqual(Path(self.ext).read_text(encoding="utf-8"), before_ext)
        self.assertEqual(Path(self.reg).read_text(encoding="utf-8"), before_reg)
        # root 与属主可以
        panel.external_remove("oldsvc", True, lst=list(self.listening), caller="root")
        self.assertEqual(self.rows(), [])

    def test_owner_can_cancel(self):
        panel.external_remove("oldsvc", True, lst=list(self.listening), caller="appuser")
        self.assertEqual(self.rows(), [])

    def test_plain_entry_removal_limited_to_root_owner_creator(self):
        self.add(name="ro1", port=9300, caller="alice")          # alice 登记了 appuser 名下的只读条目
        with self.assertRaisesRegex(panel.ApiExit, "登记人"):
            panel.external_remove("ro1", False, lst=[], caller="demo2")
        panel.external_remove("ro1", False, lst=[], caller="alice")        # 登记人
        self.assertNotIn("ro1", [r["name"] for r in self.rows()])
        self.add(name="ro3", port=9302, caller="alice")
        panel.external_remove("ro3", False, lst=[], caller="appuser")      # 属主
        self.add(name="ro4", port=9303, caller="alice")
        panel.external_remove("ro4", False, lst=[], caller="root")

    def test_created_by_recorded_and_legacy_rows_owner_root_only(self):
        self.add(name="ro5", port=9304, caller="alice")
        self.assertEqual([r for r in self.rows() if r["name"] == "ro5"][0]["created_by"], "alice")
        rows = self.rows()
        rows[-1]["created_by"] = ""        # 旧文件没有登记人列
        Path(self.ext).write_text(panel.dump_external(rows), encoding="utf-8")
        with self.assertRaises(panel.ApiExit):
            panel.external_remove("ro5", False, lst=[], caller="alice")

    def test_cli_remove_uses_sudo_user(self):
        with patch.dict(os.environ, {"SUDO_USER": "alice"}), patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(panel.api_cli(["external", "remove", "oldsvc", "--cancel-migration"]), 1)
        self.assertEqual(self.rows()[0]["target"], "newproj")

    def test_clear_source_keeps_other_rows_and_big_files(self):
        pad = "".join("p%04d\t%d\t\t2026-01-01 00:00:00\n" % (i, 20000 + i) for i in range(40000))      # 远超 1 MiB
        with open(self.reg, "a", encoding="utf-8", newline="") as f:
            f.write(pad)
        before = Path(self.reg).read_text(encoding="utf-8")
        self.assertGreater(len(before.encode("utf-8")), 1 << 20)
        self.assertTrue(panel.clear_project_source("newproj", "oldsvc"))
        after = Path(self.reg).read_text(encoding="utf-8")
        self.assertEqual(after, before.replace("external:oldsvc", ""))   # 按列清空第 5 列：保留空列（第 6 列注释位置不动）
        self.assertTrue(after.endswith("\n"))

    def test_clear_source_missing_registry(self):
        os.remove(self.reg)
        self.assertFalse(panel.clear_project_source("newproj", "oldsvc"))

    def test_clear_source_only_matching(self):
        self.write_registry("newproj\t9200\tc\t2026-10-31 10:00:00\texternal:other1")
        self.assertFalse(panel.clear_project_source("newproj", "oldsvc"))

    def test_corrupt_file_not_overwritten(self):
        with patch.object(panel, "_load_external_strict", side_effect=panel.ApiExit("读取失败")):
            with self.assertRaises(panel.ApiExit):
                self.add(name="another", port=9400)
        self.assertEqual(len(self.rows()), 1)


class TakeoverCopyTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.listening = [listen(9200, "appuser")]
        self.add(migrate=True, caller="appuser")
        panel.external_takeover_begin("oldsvc", "newproj", "9200", "appuser", list(self.listening))
        self.write_registry("newproj\t9200\tc\t2026-10-31 10:00:00\texternal:oldsvc", "plain1\t3000\t\t2026-10-31 10:00:00\t")
        self.data = os.path.join(self.tmp, "home", "appuser", "appdata")
        os.makedirs(self.data)
        Path(self.data, "a.txt").write_text("hello", encoding="utf-8")
        os.makedirs(os.path.join(self.data, "sub"))
        Path(self.data, "sub", "b.bin").write_bytes(b"x" * 10)
        os.makedirs(os.path.join(self.srv, "newproj", "data"))
        # 源目录的 uid 与外部服务属主一致：测试里用当前进程拥有的目录，把属主 uid 打桩成目录的 uid
        self.uid = os.lstat(self.data).st_uid
        self.patch_uid = patch.object(panel, "_uid_of", lambda n: self.uid if n == "appuser" else ACCOUNTS.get(n, (None,))[0])
        self.patch_uid.start()
        self.addCleanup(self.patch_uid.stop)

    def check(self, **kw):
        a = dict(project="newproj", src=self.data, to="data", caller="appuser")
        a.update(kw)
        return panel.takeover_copy_check(**a)

    def test_ok(self):
        ext, real, dest = self.check()
        self.assertEqual(ext["name"], "oldsvc")
        self.assertEqual(Path(dest), Path(self.srv, "newproj", "data", "appdata"))

    def test_non_migrated_project_refused(self):
        with self.assertRaisesRegex(panel.ApiExit, "不是从外部服务迁入"):
            self.check(project="plain1")

    def test_unknown_project_exit_2(self):
        with self.assertRaises(panel.ApiExit) as cm:
            self.check(project="nosuch1")
        self.assertEqual(cm.exception.code, 2)

    def test_after_cancel_refused(self):
        panel.external_takeover_abort("oldsvc", "newproj")
        with self.assertRaisesRegex(panel.ApiExit, "不在迁入中"):
            self.check()

    def test_caller_must_be_owner_or_root(self):
        with self.assertRaisesRegex(panel.ApiExit, "属主"):
            self.check(caller="alice")
        self.check(caller="root")

    def test_bad_to_and_paths(self):
        with self.assertRaises(panel.ApiExit):
            self.check(to="repo")
        for src in ("relative/dir", "", self.data + os.sep + ".." + os.sep + "appdata", self.data + "\n"):
            with self.assertRaises(panel.ApiExit, msg=repr(src)):
                self.check(src=src)

    def test_source_under_srv_refused(self):
        inside = os.path.join(self.srv, "newproj", "data")
        with self.assertRaisesRegex(panel.ApiExit, "不能位于"):
            self.check(src=inside)
        with self.assertRaisesRegex(panel.ApiExit, "不能位于"):
            self.check(src=self.srv)

    def test_wrong_owner_refused(self):
        with patch.object(panel, "_uid_of", lambda n: self.uid + 12345):
            with self.assertRaisesRegex(panel.ApiExit, "属主"):
                self.check()

    def test_not_a_directory_refused(self):
        with self.assertRaisesRegex(panel.ApiExit, "不是目录"):
            self.check(src=os.path.join(self.data, "a.txt"))

    def test_symlink_in_path_refused(self):
        link = os.path.join(self.tmp, "home", "linkdata")
        try:
            os.symlink(self.data, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不能创建符号链接")
        with self.assertRaisesRegex(panel.ApiExit, "符号链接"):
            self.check(src=link)
        linkdir = os.path.join(self.tmp, "home", "linkhome")
        os.symlink(os.path.join(self.tmp, "home", "appuser"), linkdir, target_is_directory=True)
        with self.assertRaisesRegex(panel.ApiExit, "符号链接"):
            self.check(src=os.path.join(linkdir, "appdata"))

    def fake_run(self, calls, dest_exists=False):
        def run(args, stdin_text=None, timeout=30):
            calls.append(list(args))
            cmd = args[args.index("--") + 1:] if args[0] == "runuser" else args
            if cmd[0] == "test":
                target = cmd[-1]
                if cmd[1] == "!" and cmd[2] == "-e":
                    return (1 if dest_exists else 0), "", ""
                if cmd[1] == "!" and cmd[2] == "-L":
                    return 0, "", ""
                return (0 if os.path.isdir(target) else 1), "", ""
            if cmd[0] == "cp":
                shutil.copytree(cmd[-2], cmd[-1])
                return 0, "", ""
            if cmd[0] == "chown":
                return 0, "", ""
            if cmd[0] == "mv":
                os.rename(cmd[-2], cmd[-1])
                return 0, "", ""
            raise AssertionError(args)
        return run

    def test_copy_runs_with_least_privilege_and_reports_counts(self):
        calls = []
        with patch.object(panel, "run_cmd", self.fake_run(calls)), patch.object(panel.os, "chown", create=True):
            out = panel.takeover_copy("newproj", self.data, "data", "appuser")
        self.assertIn("2 个文件", out)
        self.assertIn("15 字节", out)
        dest = Path(self.srv, "newproj", "data", "appdata")
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "hello")
        # 读源目录用属主身份，落地用项目账号身份，root 只 chown
        cp = next(c for c in calls if "cp" in c)
        self.assertEqual(cp[:4], ["runuser", "-u", "appuser", "--"])
        self.assertIn("--no-preserve=ownership", cp)
        self.assertIn("-x", cp)
        mv = next(c for c in calls if "mv" in c)
        self.assertEqual(mv[:4], ["runuser", "-u", "newproj", "--"])
        chown = next(c for c in calls if c[0] == "chown")
        self.assertEqual(chown[1:4], ["--from=appuser", "-hR", "newproj:newproj"])
        self.assertEqual(os.listdir(os.path.join(self.srv, ".takeover")), [])     # 暂存目录已清理

    def test_existing_target_refused_and_nothing_copied(self):
        calls = []
        with patch.object(panel, "run_cmd", self.fake_run(calls, dest_exists=True)), patch.object(panel.os, "chown", create=True):
            with self.assertRaisesRegex(panel.ApiExit, "已存在"):
                panel.takeover_copy("newproj", self.data, "data", "appuser")
        self.assertFalse(any("cp" in c for c in calls))

    def test_copy_failure_cleans_staging(self):
        def run(args, stdin_text=None, timeout=30):
            cmd = args[args.index("--") + 1:] if args[0] == "runuser" else args
            if cmd[0] == "cp":
                return 1, "", "Permission denied"
            return 0, "", ""
        with patch.object(panel, "run_cmd", run), patch.object(panel.os, "chown", create=True):
            with self.assertRaisesRegex(panel.ApiExit, "复制失败"):
                panel.takeover_copy("newproj", self.data, "data", "appuser")
        self.assertEqual(os.listdir(os.path.join(self.srv, ".takeover")), [])

    def test_staging_chmod_only_when_newly_created(self):
        """/srv/.takeover 只在新建时补 0711；管理员后来收紧的权限（如 0700）不被覆盖。"""
        staging = os.path.join(self.srv, ".takeover")
        chmods = []
        real_chmod = os.chmod
        def rec(path, mode, **kw):
            chmods.append((path, mode))
            return real_chmod(path, mode, **kw)
        with patch.object(panel, "run_cmd", self.fake_run([])), patch.object(panel.os, "chown", create=True), \
             patch.object(panel.os, "chmod", rec):
            panel.takeover_copy("newproj", self.data, "data", "appuser")
        self.assertEqual([m for p, m in chmods if p == staging], [0o711])     # 新建：补 umask 吃掉的位
        chmods.clear()
        os.makedirs(os.path.join(self.srv, "newproj", "config"))              # 第二次复制到 config，staging 已存在
        with patch.object(panel, "run_cmd", self.fake_run([])), patch.object(panel.os, "chown", create=True), \
             patch.object(panel.os, "chmod", rec):
            panel.takeover_copy("newproj", self.data, "config", "appuser")
        self.assertEqual([m for p, m in chmods if p == staging], [])          # 已存在：不动权限


class CliTests(Sandbox):
    def run_cli(self, argv):
        with patch("sys.stdout") as out, patch("sys.stderr") as err:
            rc = panel.api_cli(argv)
        return rc, "".join(c.args[0] for c in out.write.call_args_list), "".join(c.args[0] for c in err.write.call_args_list)

    def test_add_list_remove(self):
        rc, out, _ = self.run_cli(["external", "add", "oldsvc", "--port", "9200", "--owner", "appuser", "--desc", "x", "--migrate"])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertTrue(data["external"]["migratable"])
        rc, out, _ = self.run_cli(["external", "list"])
        self.assertEqual([e["name"] for e in json.loads(out)["externals"]], ["oldsvc"])
        rc, out, _ = self.run_cli(["external", "remove", "oldsvc"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.rows(), [])

    def test_failures_exit_1_reason_on_stderr(self):
        rc, out, err = self.run_cli(["external", "add", "oldsvc", "--port", "9200", "--owner", "ghost"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("不存在", err)

    def test_remove_missing_exit_2(self):
        self.assertEqual(self.run_cli(["external", "remove", "nosuch1"])[0], 2)

    def test_migrate_by_other_caller_refused_via_cli(self):
        with patch.dict(os.environ, {"SUDO_USER": "alice"}):
            rc, _, err = self.run_cli(["external", "add", "oldsvc", "--port", "9200", "--owner", "appuser", "--migrate"])
        self.assertEqual(rc, 1)
        self.assertIn("只能迁入调用者自己", err)

    def test_internal_commands(self):
        self.listening = [listen(9200, "appuser")]
        self.run_cli(["external", "add", "oldsvc", "--port", "9200", "--owner", "appuser", "--migrate"])
        self.assertEqual(self.run_cli(["external", "precheck", "brandnew", "3000"])[0], 0)
        self.assertEqual(self.run_cli(["external", "precheck", "brandnew", "9200"])[0], 1)
        self.assertEqual(self.run_cli(["external", "takeover-begin", "oldsvc", "--project", "newproj", "--port", "9200"])[0], 0)
        self.assertEqual(self.rows()[0]["target"], "newproj")
        self.assertEqual(self.run_cli(["external", "register", "newproj", "--port", "9200",
                                       "--contact=张三", "--source=external:oldsvc"])[0], 0)
        reg = panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))
        self.assertEqual([(r["name"], r["source"]) for r in reg], [("newproj", "external:oldsvc")])
        self.assertEqual(self.run_cli(["external", "unregister", "newproj"])[0], 0)
        self.assertEqual(panel.parse_registry(Path(self.reg).read_text(encoding="utf-8")), [])
        self.assertEqual(self.run_cli(["external", "takeover-abort", "oldsvc", "--project", "newproj"])[0], 0)
        self.assertEqual(self.rows()[0]["target"], "")
        self.assertEqual(self.run_cli(["external", "release", "newproj"])[0], 0)

    def test_failed_add_and_remove_are_audited(self):
        """external add/remove 失败（含越权）也写审计：动作名 + rc + 调用者；desc 等自由文本不入审计。"""
        self.assertEqual(self.run_cli(["external", "add", "oldsvc", "--port", "9200",
                                       "--owner", "appuser", "--desc=秘密描述"])[0], 0)
        panel.audit.reset_mock()
        rc, _, err = self.run_cli(["external", "add", "oldsvc", "--port", "9201", "--owner", "appuser"])   # 重复登记
        self.assertEqual(rc, 1)
        adds = [c for c in panel.audit.call_args_list if c.args[1] == "api_external_add"]
        self.assertTrue(any(c.args[3] == 1 and c.args[4] == "cli:appuser" for c in adds), adds)
        self.assertTrue(all("desc" not in c.args[2] for c in adds))
        panel.audit.reset_mock()
        with patch.dict(os.environ, {"SUDO_USER": "alice"}):
            rc, _, _ = self.run_cli(["external", "remove", "oldsvc"])                                       # 越权删除
        self.assertEqual(rc, 1)
        rems = [c for c in panel.audit.call_args_list if c.args[1] == "api_external_remove"]
        self.assertTrue(any(c.args[3] == 1 and c.args[4] == "cli:alice"
                            and c.args[2] == {"name": "oldsvc", "cancel_migration": False} for c in rems), rems)
        panel.audit.reset_mock()
        self.assertEqual(self.run_cli(["external", "remove", "nosuch1"])[0], 2)                             # 不存在
        rems = [c for c in panel.audit.call_args_list if c.args[1] == "api_external_remove"]
        self.assertTrue(any(c.args[3] == 2 for c in rems), rems)

    def test_non_apiexit_io_failures_audited_and_readable(self):
        """OSError/ENOSPC 也写失败审计；CLI 给 stage/路径，不打印 traceback，不改授权结果。"""
        panel.audit.reset_mock()
        with patch.object(panel, "_save_external", side_effect=OSError(28, "No space left on device")):
            rc, out, err = self.run_cli(["external", "add", "oldsvc", "--port", "9200",
                                         "--owner", "appuser", "--desc=秘密"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn(panel.EXTERNAL, err)
        self.assertIn("No space left on device", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.ext))
        adds = [c for c in panel.audit.call_args_list if c.args[1] == "api_external_add"]
        self.assertTrue(any(c.args[3] == 28 and c.args[4] == "cli:appuser" for c in adds), adds)
        self.assertTrue(all("desc" not in c.args[2] for c in adds))
        self.add(name="plain1", port=9300)                                  # 先正常登记，再让删除时写失败
        panel.audit.reset_mock()
        with patch.object(panel, "_save_external", side_effect=OSError(28, "No space left on device")):
            rc, out, err = self.run_cli(["external", "remove", "plain1"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn(panel.EXTERNAL, err)
        self.assertIn("No space left on device", err)
        self.assertNotIn("Traceback", err)
        rems = [c for c in panel.audit.call_args_list if c.args[1] == "api_external_remove"]
        self.assertTrue(any(c.args[3] == 28 and c.args[4] == "cli:appuser" for c in rems), rems)


class NoteTests(Sandbox):
    """项目注释（PROJECTS.tsv 第 6 列）：兼容 4/5/6 列、set/clear、幂等、fail-closed 与并发。"""

    def reg_text(self):
        return Path(self.reg).read_text(encoding="utf-8")

    def one_row(self):
        return next(l for l in self.reg_text().split("\n") if l.startswith("p1\t"))

    def test_set_expands_5_columns_to_6_and_preserves_other_rows(self):
        self.write_registry("old4\t3000\tc\t2026-01-01 00:00:00",
                            "p1\t3100\t张三\t2026-01-02 00:00:00\t",
                            "p2\t3200\t\t2026-01-03 00:00:00\texternal:foo")
        before = self.reg_text().split("\n")
        self.assertEqual(panel.set_project_note("p1", "运维备注：内部使用"), "运维备注：内部使用")
        lines = self.reg_text().split("\n")
        self.assertEqual(lines[0], before[0])            # 旧表头原样（不迁移）
        self.assertEqual(lines[1], before[1])            # 其他行逐字节原样
        self.assertEqual(lines[3], before[3])
        self.assertEqual(lines[2], "p1\t3100\t张三\t2026-01-02 00:00:00\t\t运维备注：内部使用")

    def test_set_on_4_column_row(self):
        """旧 4 列行：补齐空的第 5 列，注释落到第 6 列（不能让注释挤进来源列）。"""
        self.write_registry("old4\t3000\tc\t2026-01-01 00:00:00")
        panel.set_project_note("old4", "补一条")
        self.assertIn("old4\t3000\tc\t2026-01-01 00:00:00\t\t补一条", self.reg_text())
        row = panel.parse_registry(self.reg_text())[0]
        self.assertEqual((row["source"], row["note"]), ("", "补一条"))

    def test_set_replaces_existing_note(self):
        self.write_registry("p1\t3000\t\tt\texternal:x\t旧注")
        panel.set_project_note("p1", "新注")
        self.assertNotIn("旧注", self.reg_text())
        self.assertEqual(self.one_row(), "p1\t3000\t\tt\texternal:x\t新注")

    def test_clear_removes_sixth_field_without_trailing_tab(self):
        self.write_registry("p1\t3000\t\tt\texternal:x\t有注")
        self.assertIsNone(panel.set_project_note("p1", "", clear=True))
        self.assertEqual(self.one_row(), "p1\t3000\t\tt\texternal:x")     # 回到 5 列、无尾随 tab

    def test_clear_normalizes_stray_empty_sixth_field(self):
        with open(self.reg, "w", encoding="utf-8", newline="") as f:
            f.write("# 项目\t端口\t联系人\t创建时间\t来源\np1\t3000\t\tt\t\t\n")   # 手工编辑留下的 6 字段空注释
        self.assertIsNone(panel.set_project_note("p1", "", clear=True))
        self.assertEqual(self.one_row(), "p1\t3000\t\tt\t")               # 只删第 6 字段；空的第 5 列（尾随 tab）属于来源列，保留
        with patch.object(panel, "write_file_atomic") as w:
            self.assertIsNone(panel.set_project_note("p1", "", clear=True))   # 之后幂等无写
            w.assert_not_called()

    def test_unicode_roundtrip_without_strip(self):
        note = "中文与空格 保留 🎉 π --不是选项  "
        self.write_registry("p1\t3000\t\tt\t")
        panel.set_project_note("p1", note)
        self.assertEqual(panel.parse_registry(self.reg_text())[0]["note"], note)   # 保存不 strip，原样读回

    def test_empty_set_is_rejected_not_treated_as_clear(self):
        self.write_registry("p1\t3000\t\tt\t")
        with self.assertRaisesRegex(panel.ApiExit, "空白"):
            panel.set_project_note("p1", "")
        self.assertEqual(panel.parse_registry(self.reg_text())[0]["note"], "")

    def test_length_and_content_limits(self):
        self.write_registry("p1\t3000\t\tt\t")
        for bad in ("   ", "a\tb", "a\nb", "字" * 201, "🎉" * 201, "a\x7fb", "a\x85b", "a\u2028b", "a\u2029b"):
            with self.assertRaisesRegex(panel.ApiExit, "注释") as cm:
                panel.set_project_note("p1", bad)
            self.assertNotIn(bad, str(cm.exception))     # 错误不回显正文
        self.assertEqual(self.one_row(), "p1\t3000\t\tt\t")                   # 失败不写表
        panel.set_project_note("p1", "字" * 200)                              # 恰好 200 合格
        panel.set_project_note("p1", "🎉" * 200)

    def test_unknown_project_exit_2_no_append(self):
        self.write_registry("p1\t3000\t\tt\t")
        before = self.reg_text()
        with self.assertRaises(panel.ApiExit) as cm:
            panel.set_project_note("ghost1", "x")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.reg_text(), before)        # 绝不追加

    def test_missing_registry_is_exit_2(self):
        with self.assertRaises(panel.ApiExit) as cm:
            panel.set_project_note("p1", "x")
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(self.reg))

    def test_invalid_name_exit_2(self):
        for name in ("__probe__", "Bad", "../x", "a", ""):
            with self.assertRaises(panel.ApiExit) as cm:
                panel.set_project_note(name, "x", clear=True)
            self.assertEqual(cm.exception.code, 2, name)
            self.assertIn("项目名不合法", str(cm.exception))
        self.assertFalse(os.path.exists(self.reg))       # 探测不写表

    def test_duplicate_rows_fail_closed(self):
        self.write_registry("dup\t3000\t\tt", "dup\t3001\t\tt")
        before = self.reg_text()
        with self.assertRaisesRegex(panel.ApiExit, "重复"):
            panel.set_project_note("dup", "x")
        self.assertEqual(self.reg_text(), before)

    def test_crlf_rejected(self):
        with open(self.reg, "w", encoding="utf-8", newline="") as f:
            f.write("# 项目\t端口\t联系人\t创建时间\t来源\r\np1\t3000\t\tt\texternal:x\r\n")
        before = Path(self.reg).read_bytes()
        with self.assertRaisesRegex(panel.ApiExit, "CR"):
            panel.set_project_note("p1", "x")
        self.assertEqual(Path(self.reg).read_bytes(), before)

    def test_malformed_short_row_refused(self):
        self.write_registry("p1")
        before = self.reg_text()
        with self.assertRaisesRegex(panel.ApiExit, "格式异常"):
            panel.set_project_note("p1", "x")
        self.assertEqual(self.reg_text(), before)

    def test_idempotent_set_and_clear_do_not_write(self):
        self.write_registry("p1\t3000\t\tt\t")
        panel.set_project_note("p1", "备注")              # 首次设置：真实写入
        with patch.object(panel, "write_file_atomic") as w:
            self.assertEqual(panel.set_project_note("p1", "备注"), "备注")     # 同值 set：幂等无写
            w.assert_not_called()
        panel.set_project_note("p1", "", clear=True)     # 清除：回到 5 列
        with patch.object(panel, "write_file_atomic") as w:
            self.assertIsNone(panel.set_project_note("p1", "", clear=True))    # 已空 clear：幂等无写
            w.assert_not_called()

    def test_tail_row_beyond_1mib(self):
        pad = ["p%05d\t%d\t\t2026-01-01 00:00:00\t" % (i, 20000 + i) for i in range(40000)]
        pad.append("tailp\t9300\t\t2026-01-01 00:00:00")
        self.write_registry(*pad)
        self.assertGreater(os.path.getsize(self.reg), 1 << 20)
        panel.set_project_note("tailp", "表尾")
        rows = panel.parse_registry(self.reg_text())
        self.assertEqual(len(rows), len(pad))
        self.assertEqual((rows[-1]["name"], rows[-1]["note"]), ("tailp", "表尾"))
        self.assertEqual(rows[0]["name"], "p00000")      # 大表其余行未动

    def test_note_only_takes_registry_lock(self):
        """note 只持 PROJECTS 一把锁，不取 EXTERNAL 锁，无反向锁序。"""
        self.write_registry("p1\t3000\t\tt\t")
        order = []
        real = panel._flock
        def rec(path):
            order.append(path)
            return real(path)
        with patch.object(panel, "_flock", rec):
            panel.set_project_note("p1", "x")
        self.assertEqual(order, [panel.REGISTRY_LOCK])

    @unittest.skipUnless(HAS_FCNTL, "flock 只在有 fcntl 的平台（Linux）生效")
    def test_concurrent_delete_wins_note_never_revives(self):
        """锁内的 delete 先删行：note 进锁后按同一次完整读取定位不到目标，报不存在且不把行写回。"""
        import threading
        self.write_registry("gone1\t3000\t\t2026-01-01 00:00:00")
        outcome = []
        def worker():
            try:
                panel.set_project_note("gone1", "x")
                outcome.append("ok")
            except panel.ApiExit as e:
                outcome.append("missing:%d" % e.code)
        with panel.registry_lock():
            lines = [l for l in self.reg_text().split("\n") if not l.startswith("gone1\t")]
            with open(self.reg, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(lines))                # 模拟并发的 delete 已在锁内落盘
            t = threading.Thread(target=worker)
            t.start()
            t.join(0.5)
            self.assertTrue(t.is_alive())                # 堵在锁上，没有偷跑
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcome, ["missing:2"])
        self.assertNotIn("gone1", self.reg_text())

    @unittest.skipUnless(HAS_FCNTL, "flock 只在有 fcntl 的平台（Linux）生效")
    def test_concurrent_note_and_register_keep_both_rows(self):
        import threading
        self.write_registry("p1\t3000\t\t2026-01-01 00:00:00")
        note_result, reg_result = [], []
        def note_worker():
            note_result.append(panel.set_project_note("p1", "并发备注"))
        def reg_worker():
            try:
                panel.external_register("p2", "3001")
                reg_result.append("ok")
            except panel.ApiExit as e:
                reg_result.append("clash:%s" % e)
        with panel.registry_lock():
            t1 = threading.Thread(target=note_worker)
            t2 = threading.Thread(target=reg_worker)
            t1.start()
            t2.start()
            t1.join(0.5)
            t2.join(0.5)
            self.assertTrue(t1.is_alive() and t2.is_alive())
        t1.join(5)
        t2.join(5)
        rows = {r["name"]: r for r in panel.parse_registry(self.reg_text())}
        self.assertEqual(set(rows), {"p1", "p2"})        # 并发不丢行
        self.assertEqual((rows["p1"]["note"], note_result), ("并发备注", ["并发备注"]))
        self.assertEqual(reg_result, ["ok"])


class NoteCliTests(Sandbox):
    """srv-panel.py api note 的 CLI 形态：stdout 仅 JSON、互斥必选、审计与错误不含正文、写失败口径。"""

    def run_cli(self, argv, stdin_text=""):
        with patch("sys.stdin", io.StringIO(stdin_text)), patch("sys.stdout") as out, patch("sys.stderr") as err:
            rc = panel.api_cli(argv)
        return rc, "".join(c.args[0] for c in out.write.call_args_list), "".join(c.args[0] for c in err.write.call_args_list)

    def note_row(self):
        self.write_registry("demo\t3100\t测试\t2026-01-01 00:00:00\t")

    def test_set_stdout_is_json_only(self):
        self.note_row()
        rc, out, err = self.run_cli(["note", "demo", "--set=中文 备注 🎉"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), {"name": "demo", "note": "中文 备注 🎉"})    # stdout 只有这一个 JSON

    def test_stdin_form(self):
        self.note_row()
        rc, out, _ = self.run_cli(["note", "demo", "--set=-"], stdin_text="来自 stdin 的备注")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["note"], "来自 stdin 的备注")

    def test_empty_stdin_is_rejected_not_clear(self):
        self.note_row()
        rc, out, err = self.run_cli(["note", "demo", "--set=-"], stdin_text="")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("空白", err)
        self.assertEqual(panel.parse_registry(Path(self.reg).read_text(encoding="utf-8"))[0]["note"], "")

    def test_clear_stdout_note_null_and_file_back_to_5(self):
        self.note_row()
        self.run_cli(["note", "demo", "--set=x"])
        rc, out, _ = self.run_cli(["note", "demo", "--clear"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"name": "demo", "note": None})
        line = next(l for l in Path(self.reg).read_text(encoding="utf-8").split("\n") if l.startswith("demo\t"))
        self.assertEqual(line, "demo\t3100\t测试\t2026-01-01 00:00:00\t")   # 回到 5 列（空的第 5 列是来源列）

    def test_dashed_values_are_plain_text(self):
        """--set= 的 = 形态消除以 - 开头文本的 argparse 歧义：-x/--clear/-- 都是普通注释文本。"""
        self.note_row()
        for text in ("-x", "--clear", "--"):
            rc, out, _ = self.run_cli(["note", "demo", "--set=" + text])
            self.assertEqual((rc, json.loads(out)["note"]), (0, text), text)

    def test_mutex_and_missing_options_exit_2(self):
        self.note_row()
        for argv in (["note"], ["note", "demo"], ["note", "demo", "--set=a", "--clear"]):
            with self.assertRaises(SystemExit) as cm:
                self.run_cli(argv)
            self.assertEqual(cm.exception.code, 2, argv)

    def test_unknown_project_exit_2_stdout_empty(self):
        self.note_row()
        rc, out, err = self.run_cli(["note", "nosuch1", "--clear"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("不存在", err)

    def test_invalid_name_reports_and_writes_nothing(self):
        self.note_row()
        rc, out, err = self.run_cli(["note", "__probe__", "--clear"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("项目名不合法", err)
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertIn("demo\t3100", text)                # 表未动
        self.assertNotIn("__probe__", text)              # 未追加

    def test_validation_failure_exit_1_without_echo(self):
        self.note_row()
        rc, out, err = self.run_cli(["note", "demo", "--set=坏\t的"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("控制字符", err)
        self.assertNotIn("坏\t的", err)                  # 错误不回显正文
        rc, out, err = self.run_cli(["note", "demo", "--set=   "])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("空白", err)
        rc, _, err = self.run_cli(["note", "demo", "--set=" + "字" * 201])
        self.assertIn("200", err)

    def test_audit_records_op_without_body(self):
        self.note_row()
        self.run_cli(["note", "demo", "--set=机密正文保密"])
        sets = [c for c in panel.audit.call_args_list if c.args[1] == "api_project_note"]
        self.assertTrue(sets)
        self.assertEqual(sets[-1].args[2], {"project": "demo", "op": "set"})
        self.assertEqual(sets[-1].args[3], 0)
        self.assertNotIn("机密正文保密", json.dumps(sets[-1].args[2], ensure_ascii=False))
        panel.audit.reset_mock()
        self.run_cli(["note", "demo", "--clear"])
        clears = [c for c in panel.audit.call_args_list if c.args[1] == "api_project_note"]
        self.assertEqual(clears[-1].args[2], {"project": "demo", "op": "clear"})

    def test_write_failure_exit_1_readable_and_table_intact(self):
        """ENOSPC：非零退出、错误可读、无 traceback、无半截表、失败也留审计。"""
        self.note_row()
        before = Path(self.reg).read_text(encoding="utf-8")
        with patch.object(panel, "write_file_atomic", side_effect=OSError(28, "No space left on device")):
            rc, out, err = self.run_cli(["note", "demo", "--set=x"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("No space left on device", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(Path(self.reg).read_text(encoding="utf-8"), before)
        fails = [c for c in panel.audit.call_args_list if c.args[1] == "api_project_note"]
        self.assertTrue(any(c.args[3] == 28 for c in fails), fails)


if __name__ == "__main__":
    unittest.main()
