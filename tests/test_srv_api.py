import base64
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server"
SPEC = importlib.util.spec_from_file_location("srv_panel_api", ROOT / "panel" / "srv_panel.py")
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)

# 计划 §5.1 的字段约定：只能增加，不能删减或改名
LIST_FIELDS = {"name", "port", "contact", "created", "account_exists", "disabled", "keys"}
KEY_FIELDS = {"label", "fingerprint", "from"}
STATUS_EXTRA = {"listening", "listen_owner", "services", "head", "disk_kb", "problems"}
SERVICE_FIELDS = {"unit", "active", "sub"}
GLOBAL_FIELDS = {"sshd_rule", "world_readable", "listeners"}
LISTENER_FIELDS = {"port", "owner", "project"}
ENV_FIELDS = {"project", "disabled", "tools", "declarations", "findings"}
TOOL_FIELDS = {"name", "available", "version", "path"}
INFO_FIELDS = {"project", "host", "ssh_port", "account", "dir", "port", "disabled", "keys", "text"}
INVITE_FIELDS = {"id", "project", "label", "from", "created", "expires", "status", "used"}
USED_FIELDS = {"at", "ip", "device", "fingerprint"}

BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x07" * 32).decode()
KEYLINE = 'restrict,pty,from="10.0.0.5" ssh-ed25519 %s wj-agent' % BLOB
KEYLINE2 = "restrict,pty ssh-ed25519 %s wj-2" % BLOB
ENTRY = {"name": "demo", "port": "3101", "contact": "测试", "created": "2026-10-30 02:43:42"}


def collected(**kw):
    d = {"entry": ENTRY, "uid": 1001, "member": True, "problems": [], "notes": [], "disabled": False, "port": 3101,
         "listening": True, "owners": ["demo"], "owner_ok": True, "keys": [], "services": [
             {"unit": "app.service", "load": "loaded", "active": "active", "sub": "running", "desc": "x"}],
         "services_err": "", "head": "79cef51  2026-10-30 02:45:05 +0000  test: deploy", "disk": "2.3 MB", "disk_kb": 2355}
    d.update(kw)
    return d


class FingerprintTests(unittest.TestCase):
    def test_format_matches_openssh(self):
        fp = panel.ssh_fingerprint(BLOB)
        want = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(BLOB)).digest()).decode().rstrip("=")
        self.assertEqual(fp, want)
        self.assertEqual(len(fp), len("SHA256:") + 43)

    def test_key_entries(self):
        ents = panel.key_entries([KEYLINE, KEYLINE2, "", "# c", "garbage"])
        self.assertEqual([e["label"] for e in ents], ["wj-agent", "wj-2"])
        self.assertEqual(ents[0]["from"], "10.0.0.5")
        self.assertIsNone(ents[1]["from"])
        for e in ents:
            self.assertEqual(set(e), KEY_FIELDS)
            self.assertTrue(e["fingerprint"].startswith("SHA256:"))


class ListRecordTests(unittest.TestCase):
    def test_fields_and_types(self):
        rec = panel.light_record(ENTRY, True, False, [KEYLINE])
        self.assertTrue(LIST_FIELDS <= set(rec))
        self.assertEqual((rec["name"], rec["port"], rec["account_exists"], rec["disabled"]), ("demo", 3101, True, False))
        self.assertEqual(len(rec["keys"]), 1)

    def test_missing_account(self):
        rec = panel.light_record(dict(ENTRY, port="oops"), False, True, [KEYLINE])
        self.assertEqual((rec["account_exists"], rec["disabled"], rec["keys"], rec["port"]), (False, False, [], None))
        json.dumps(rec)

    def test_list_does_not_spawn_processes(self):
        with patch.object(panel, "load_registry", return_value=[ENTRY]), \
             patch.object(panel, "_account_exists", return_value=True), \
             patch.object(panel, "is_disabled", return_value=True), \
             patch.object(panel, "read_text", return_value=KEYLINE + "\n"), \
             patch.object(panel, "run_cmd", side_effect=AssertionError("list 不得起子进程")):
            recs = panel.light_projects()
        self.assertEqual(recs[0]["disabled"], True)
        self.assertEqual(recs[0]["keys"][0]["label"], "wj-agent")


class StatusRecordTests(unittest.TestCase):
    def light(self, exists=True):
        return panel.light_record(ENTRY, exists, False, [KEYLINE])

    def test_full(self):
        rec = panel.status_record(self.light(), collected())
        self.assertTrue((LIST_FIELDS | STATUS_EXTRA) <= set(rec))
        self.assertEqual((rec["listening"], rec["listen_owner"], rec["disk_kb"]), (True, "demo", 2355))
        self.assertEqual(rec["head"], "79cef51  2026-10-30 02:45:05 +0000  test: deploy")
        self.assertEqual(rec["services"], [{"unit": "app.service", "active": "active", "sub": "running"}])
        for s in rec["services"]:
            self.assertEqual(set(s), SERVICE_FIELDS)

    def test_missing_account_has_same_fields(self):
        full = panel.status_record(self.light(), collected())
        miss = panel.status_record(panel.light_record(ENTRY, False, False, []),
                                   {"entry": ENTRY, "missing": True, "problems": ["账号不存在"], "notes": []})
        self.assertEqual(set(miss), set(full))
        self.assertEqual((miss["listening"], miss["listen_owner"], miss["services"], miss["head"], miss["disk_kb"], miss["keys"]),
                         (False, None, [], None, None, []))
        self.assertEqual(miss["problems"], ["账号不存在"])
        json.dumps(miss)

    def test_head_that_is_an_error_message_becomes_null(self):
        for msg in ("repo/ 下没有 git 仓库", "（无法读取 HEAD：fatal）", ""):
            self.assertIsNone(panel.status_record(self.light(), collected(head=msg))["head"])

    def test_no_collection_at_all(self):
        rec = panel.status_record(self.light(), None)
        self.assertEqual((rec["problems"], rec["services"], rec["head"]), ([], [], None))

    def test_unknown_owner_and_problems(self):
        rec = panel.status_record(self.light(), collected(owners=["root", "x"], problems=["端口被占用"]))
        self.assertEqual(rec["listen_owner"], "root,x")
        self.assertEqual(rec["problems"], ["端口被占用"])


class GlobalRecordTests(unittest.TestCase):
    def test_shape(self):
        lst = [{"addr": "0.0.0.0", "port": 3101, "procs": [("py", 1)], "owners": ["demo"]},
               {"addr": "0.0.0.0", "port": 22, "procs": [], "owners": []}]
        g = panel.global_record(lst, {"3101": "demo"}, ["/srv/x"], ["/srv/y"], True, True)
        self.assertTrue(GLOBAL_FIELDS <= set(g))
        self.assertEqual(g["listeners"], [{"port": 22, "owner": None, "project": None},
                                          {"port": 3101, "owner": "demo", "project": "demo"}])
        for l in g["listeners"]:
            self.assertEqual(set(l), LISTENER_FIELDS)
        self.assertEqual((g["sshd_rule"], g["world_readable"]), (True, ["/srv/x"]))

    def test_scan_skips_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("a", ".archived", "b"):
                (Path(d) / n).mkdir()
            with patch.object(panel, "SRV_ROOT", d):
                world, extra = panel.scan_srv_dirs({"a"})
        self.assertTrue(all(".archived" not in x for x in world + extra))
        self.assertEqual([Path(x).name for x in extra], ["b"])


class EnvRecordTests(unittest.TestCase):
    def test_with_tools(self):
        tools = {"git": {"path": "/usr/bin/git", "version": "git version 2.43.0"}, "node": {"path": "", "version": ""}}
        rec = panel.env_record("demo", False, tools, [("warn", "需要 node")], [("package.json engines.node", ">=20"), ("requirements.txt", None)])
        self.assertTrue(ENV_FIELDS <= set(rec))
        self.assertEqual([t["name"] for t in rec["tools"]], list(panel.TOOLS))
        for t in rec["tools"]:
            self.assertEqual(set(t), TOOL_FIELDS)
        git = next(t for t in rec["tools"] if t["name"] == "git")
        self.assertEqual((git["available"], git["version"], git["path"]), (True, "git version 2.43.0", "/usr/bin/git"))
        node = next(t for t in rec["tools"] if t["name"] == "node")
        self.assertEqual((node["available"], node["version"], node["path"]), (False, None, None))
        self.assertEqual(rec["findings"], ["需要 node"])
        self.assertEqual(rec["declarations"][1], {"file": "requirements.txt", "requirement": None})

    def test_disabled_has_no_tools(self):
        rec = panel.env_record("demo", True, None, [("info", "项目已停用")], [])
        self.assertEqual((rec["tools"], rec["disabled"], rec["findings"]), ([], True, ["项目已停用"]))
        json.dumps(rec)


class InfoAndInvitesTests(unittest.TestCase):
    def test_info(self):
        rec = panel.info_record("demo", ENTRY, "192.0.2.10", 22, False, panel.key_entries([KEYLINE]), "TEXT")
        self.assertTrue(INFO_FIELDS <= set(rec))
        self.assertEqual((rec["port"], rec["ssh_port"], rec["dir"], rec["account"], rec["text"]), (3101, 22, "/srv/demo", "demo", "TEXT"))

    def test_invites_record(self):
        invs = {
            "a": {"id": "a", "project": "demo", "label": "x", "from": "", "created": "2026-01-01T00:00:00Z",
                  "expires": "2999-01-01T00:00:00Z", "status": "pending"},
            "b": {"id": "b", "project": "demo", "label": "y", "from": "10.0.0.0/24", "created": "2026-01-02T00:00:00Z",
                  "expires": "2999-01-01T00:00:00Z", "status": "used", "used_at": "2026-01-03T00:00:00Z",
                  "used_ip": "10.0.0.7", "used_device": "pc", "used_fingerprint": "SHA256:x"},
            "c": {"id": "c", "project": "demo", "label": "z", "created": "2026-01-03T00:00:00Z",
                  "expires": "2000-01-01T00:00:00Z", "status": "pending"},
            "d": {"id": "d", "project": "demo", "label": "w", "created": "2026-01-04T00:00:00Z",
                  "expires": "2999-01-01T00:00:00Z", "status": "revoked"},
            "e": {"id": "e", "project": "other", "label": "o", "created": "2026-01-05T00:00:00Z",
                  "expires": "2999-01-01T00:00:00Z", "status": "pending"},
        }
        out = panel.invites_record(invs, "demo")["invites"]
        self.assertEqual([i["id"] for i in out], ["a", "b", "c", "d"])
        self.assertEqual([i["status"] for i in out], ["pending", "used", "expired", "revoked"])
        for i in out:
            self.assertTrue(INVITE_FIELDS <= set(i))
        self.assertEqual(set(out[1]["used"]), USED_FIELDS)
        self.assertIsNone(out[0]["used"])
        self.assertEqual((out[0]["from"], out[1]["from"]), (None, "10.0.0.0/24"))
        self.assertNotIn("code_sha256", json.dumps(out))
        self.assertEqual(panel.invites_record(invs, "nobody"), {"invites": []})


class ApiCliTests(unittest.TestCase):
    def run_cli(self, argv, registry=(ENTRY,)):
        with patch.object(panel, "load_registry", return_value=list(registry)):
            with patch("sys.stdout") as out, patch("sys.stderr") as err:
                rc = panel.api_cli(argv)
        return rc, "".join(c.args[0] for c in out.write.call_args_list), "".join(c.args[0] for c in err.write.call_args_list)

    def test_missing_project_exit_2(self):
        with patch.object(panel, "listeners", return_value=[]), patch.object(panel, "light_projects", return_value=[]):
            rc, out, err = self.run_cli(["status", "nosuch"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("不存在", err)
        for argv in (["env", "nosuch"], ["invites", "nosuch"], ["info", "nosuch"]):
            self.assertEqual(self.run_cli(argv)[0], 2, argv)

    def test_bad_project_name_exit_2(self):
        self.assertEqual(self.run_cli(["env", "../etc"])[0], 2)

    def test_logs_validation(self):
        self.assertEqual(self.run_cli(["logs", "demo", "app.service", "--lines", "0"])[0], 1)
        self.assertEqual(self.run_cli(["logs", "demo", "app.service", "--lines", "1001"])[0], 1)
        self.assertEqual(self.run_cli(["logs", "demo", "bad;x.service"])[0], 1)
        with patch.object(panel, "service_log", return_value=(0, "line1\nline2\n", "")) as sl:
            rc, out, _ = self.run_cli(["logs", "demo", "app.service", "--lines", "1000"])
        self.assertEqual((rc, out), (0, "line1\nline2\n"))
        self.assertEqual(sl.call_args.args, ("demo", "app.service", 1000))

    def test_logs_default_is_100(self):
        with patch.object(panel, "service_log", return_value=(0, "", "")) as sl:
            self.run_cli(["logs", "demo", "app.service"])
        self.assertEqual(sl.call_args.args[2], 100)

    def test_svc_validation_and_command(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["svc", "demo", "delete", "app.service"])
        self.assertEqual(self.run_cli(["svc", "demo", "stop", "x;rm.service"])[0], 1)
        with patch.object(panel, "account_info", return_value=(1001, True)), \
             patch.object(panel, "run_cmd", return_value=(0, "", "")) as rc_, patch.object(panel, "audit"):
            rc, out, _ = self.run_cli(["svc", "demo", "restart", "app.service"])
        self.assertEqual(rc, 0)
        self.assertEqual(rc_.call_args.args[0], ["systemctl", "--user", "--machine=demo@", "restart", "app.service"])
        self.assertIn("restart", out)

    def test_svc_failure_exit_1(self):
        with patch.object(panel, "account_info", return_value=(1001, True)), \
             patch.object(panel, "run_cmd", return_value=(5, "", "Unit not found")), patch.object(panel, "audit"):
            rc, out, err = self.run_cli(["svc", "demo", "stop", "app.service"])
        self.assertEqual(rc, 1)
        self.assertIn("Unit not found", err)

    def test_status_all_structure_is_uniform(self):
        entries = [ENTRY, dict(ENTRY, name="gone", port="3102")]
        lights = [panel.light_record(ENTRY, True, False, [KEYLINE]), panel.light_record(entries[1], False, False, [])]
        ds = [collected(), {"entry": entries[1], "missing": True, "problems": ["账号不存在"], "notes": []}]
        lst = [{"addr": "0.0.0.0", "port": 3101, "procs": [], "owners": ["demo"]}]
        with patch.object(panel, "load_registry", return_value=entries), patch.object(panel, "listeners", return_value=lst), \
             patch.object(panel, "light_projects", return_value=lights), \
             patch.object(panel, "collect_all", return_value=(ds, lst, entries)) as ca, \
             patch.object(panel, "scan_srv_dirs", return_value=([], [])):
            data = panel.api_status()
        self.assertEqual(set(data), {"projects", "global"})
        self.assertEqual(set(data["projects"][0]), set(data["projects"][1]))
        self.assertTrue(ca.called)
        json.dumps(data)

    def test_status_single_project_has_no_global(self):
        with patch.object(panel, "load_registry", return_value=[ENTRY]), patch.object(panel, "listeners", return_value=[]), \
             patch.object(panel, "light_projects", return_value=[panel.light_record(ENTRY, True, False, [])]), \
             patch.object(panel, "collect_project", return_value=collected()) as cp:
            data = panel.api_status("demo")
        self.assertEqual(list(data), ["projects"])
        self.assertEqual(len(data["projects"]), 1)
        self.assertFalse(cp.call_args.kwargs.get("keys", True))


class InviteRevokeProjectTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "invites.json")
        self.store = panel.InviteStore(self.path)
        for iid, proj in (("aaaa1111", "demo"), ("bbbb2222", "other")):
            self.store.add({"id": iid, "code_sha256": panel.code_hash(iid), "project": proj, "label": iid, "from": "",
                            "created_by": "t", "created": "2030-01-01T00:00:00Z", "expires": "2999-01-01T00:00:00Z",
                            "ssh_port": 22, "status": "pending"})

    def tearDown(self):
        self.dir.cleanup()

    def test_cross_project_revoke_is_not_found(self):
        with self.assertRaisesRegex(panel.InviteError, "找不到"):
            self.store.revoke("bbbb2222", project="demo")
        self.assertEqual(self.store.snapshot()["bbbb2222"]["status"], "pending")
        self.assertEqual(self.store.revoke("aaaa1111", project="demo")["status"], "revoked")

    def test_cli_revoke_needs_project_and_rejects_cross_project(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            panel.invite_cli(["--invites-file", self.path, "revoke", "demo", "bbbb2222"])
        self.assertEqual(cm.exception.code, 2)   # 与「项目不存在」同口径，后端据此映射 404
        self.assertIn("找不到", err.getvalue())
        with self.assertRaises(SystemExit) as cm:
            panel.invite_cli(["--invites-file", self.path, "revoke", "aaaa1111"])    # 旧的单参数写法：用法错误
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.store.snapshot()["bbbb2222"]["status"], "pending")

    def test_web_path_without_project_still_works(self):
        self.assertEqual(self.store.revoke("bbbb2222")["status"], "revoked")


class PanelConfPersistenceTests(unittest.TestCase):
    def test_merge_keeps_mode_when_not_given(self):
        base = {"lan_ip": "192.0.2.1", "port": 9300, "tls_cert": "/c", "enroll_only": True, "extra": "keep"}
        merged = panel.merge_panel_conf(base, "192.0.2.9", 9300, "/c2")
        self.assertEqual((merged["enroll_only"], merged["lan_ip"], merged["tls_cert"], merged["extra"]), (True, "192.0.2.9", "/c2", "keep"))

    def test_merge_explicit_and_default(self):
        self.assertFalse(panel.merge_panel_conf({}, "192.0.2.1", 9300, "/c")["enroll_only"])
        self.assertFalse(panel.merge_panel_conf(None, "192.0.2.1", 9300, "/c")["enroll_only"])
        self.assertTrue(panel.merge_panel_conf({}, "192.0.2.1", 9300, "/c", True)["enroll_only"])
        self.assertFalse(panel.merge_panel_conf({"enroll_only": True}, "192.0.2.1", 9300, "/c", False)["enroll_only"])

    def test_write_roundtrip_reinstall_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "panel.conf")
            self.assertFalse(panel.write_panel_conf(p, "192.0.2.1", 9300, "/c")["enroll_only"])      # 首次：完整模式
            self.assertTrue(panel.write_panel_conf(p, "192.0.2.1", 9300, "/c", True)["enroll_only"])  # --enroll-only
            self.assertTrue(panel.write_panel_conf(p, "192.0.2.1", 9300, "/c")["enroll_only"])        # 不带参数重装：保持
            self.assertTrue(panel.load_panel_conf(p)["enroll_only"])
            self.assertFalse(panel.write_panel_conf(p, "192.0.2.1", 9300, "/c", False)["enroll_only"])  # --full
            self.assertEqual(json.loads(Path(p).read_text())["port"], 9300)

    def test_write_panel_conf_cli_flags(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "panel.conf")
            base = ["--write-panel-conf", "--panel-conf", p, "--lan-ip", "192.0.2.1", "--port", "9300", "--tls-cert", "/c"]
            with patch.object(panel.os, "geteuid", create=True, return_value=0), patch("builtins.print") as pr:
                panel.main(base + ["--enroll-only"])
                self.assertEqual(pr.call_args.args[0], "enroll_only=true")
                panel.main(base)
                self.assertEqual(pr.call_args.args[0], "enroll_only=true")
                panel.main(base + ["--full"])
                self.assertEqual(pr.call_args.args[0], "enroll_only=false")
                with self.assertRaises(SystemExit):
                    panel.main(base + ["--full", "--enroll-only"])


class RouteTests(unittest.TestCase):
    def test_route_allowed(self):
        self.assertTrue(panel.route_allowed(True, "POST", "/enroll"))
        for method, path in (("GET", "/"), ("GET", "/enroll"), ("GET", "/login"), ("POST", "/login"), ("POST", "/action"),
                             ("POST", "/enroll/x"), ("GET", "/invites")):
            self.assertFalse(panel.route_allowed(True, method, path), (method, path))
        for method, path in (("GET", "/"), ("POST", "/login")):
            self.assertTrue(panel.route_allowed(False, method, path))


class LiveServerTests(unittest.TestCase):
    """真实起一个本机 HTTP 服务（不走 TLS），检查登记模式下的路由。"""

    def start(self, enroll_only):
        self.dir = tempfile.TemporaryDirectory()
        invites = panel.InviteStore(str(Path(self.dir.name) / "invites.json"))
        store = None if enroll_only else panel.UserStore(str(Path(self.dir.name) / "users.json"))
        if store is not None:
            store.create("admin", "super")
        state = panel.State(store, 0, log_file=str(Path(self.dir.name) / "a.log"), invites=invites, enroll_only=enroll_only)
        self.servers = panel.make_servers(["127.0.0.1"], 0, state, None)
        self.port = self.servers[0].server_address[1]
        state.port = self.port
        threading.Thread(target=self.servers[0].serve_forever, daemon=True).start()

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_enroll_only_routes(self):
        self.start(True)
        for method, path in (("GET", "/"), ("GET", "/login"), ("GET", "/global"), ("GET", "/invites"), ("GET", "/enroll"),
                             ("POST", "/login"), ("POST", "/action"), ("POST", "/password"), ("PUT", "/enroll"), ("DELETE", "/"),
                             ("HEAD", "/"), ("OPTIONS", "/")):
            self.assertEqual(self.req(method, path, {} if method == "POST" else None)[0], 404, (method, path))
        status, data = self.req("POST", "/enroll", {"code": "short", "pubkey": "x", "device_name": "d"})
        self.assertEqual(status, 403)                       # 到达了 /enroll 处理逻辑（统一的邀请码无效提示）
        self.assertIn("邀请码无效或已失效", json.loads(data)["error"])

    def test_full_mode_still_serves_admin_pages(self):
        self.start(False)
        self.assertEqual(self.req("GET", "/login")[0], 200)
        self.assertEqual(self.req("GET", "/")[0], 303)


class StartupWithoutUsersTests(unittest.TestCase):
    def run_main(self, conf, with_users):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        confp = Path(d.name) / "panel.conf"
        confp.write_text(json.dumps(conf), encoding="utf-8")
        argv = ["--panel-conf", str(confp), "--users-file", str(Path(d.name) / "users.json"),
                "--invites-file", str(Path(d.name) / "invites.json"), "--log-file", str(Path(d.name) / "a.log")]
        captured = {}

        class FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt

        def fake_make(binds, port, state, tls):
            captured["state"], captured["port"] = state, port
            return [FakeServer()]

        with patch.object(panel.os, "geteuid", create=True, return_value=0), \
             patch.object(panel, "make_servers", side_effect=fake_make), patch("builtins.print") as pr:
            panel.main(argv)
        return captured, pr

    def test_enroll_only_starts_without_user_store(self):
        captured, pr = self.run_main({"lan_ip": "192.0.2.1", "port": 9311, "enroll_only": True}, False)
        st = captured["state"]
        self.assertTrue(st.enroll_only)
        self.assertIsNone(st.store)
        self.assertEqual(captured["port"], 9311)
        self.assertIn("登记模式", pr.call_args.args[0])

    def test_full_mode_without_users_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main({"lan_ip": "192.0.2.1", "port": 9311}, False)
        self.assertIn("用户库", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
