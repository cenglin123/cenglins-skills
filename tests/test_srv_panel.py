import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "panel" / "srv_panel.py"
SPEC = importlib.util.spec_from_file_location("srv_panel_under_test", SCRIPT)
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)

SS = """LISTEN 0      4096   127.0.0.53%lo:53   0.0.0.0:*  users:(("systemd-resolve",pid=73,fd=15))
LISTEN 0      5            0.0.0.0:3101 0.0.0.0:*  users:(("python3",pid=1059,fd=3))
LISTEN 0      4096         0.0.0.0:22   0.0.0.0:*  users:(("sshd",pid=88,fd=3),("systemd",pid=1,fd=50))
LISTEN 0      4096            [::]:22      [::]:*
LISTEN 0      511        127.0.0.1:9300 0.0.0.0:*
junk line
"""


class SsTests(unittest.TestCase):
    def test_parse(self):
        rows = panel.parse_ss(SS)
        self.assertEqual([r["port"] for r in rows], [53, 3101, 22, 22, 9300])
        self.assertEqual(rows[1]["procs"], [("python3", 1059)])
        self.assertEqual(rows[2]["procs"], [("sshd", 88), ("systemd", 1)])
        self.assertEqual(rows[3]["addr"], "::")
        self.assertEqual(rows[3]["procs"], [])


class RegistryTests(unittest.TestCase):
    def test_parse(self):
        text = "# 项目\t端口\t联系人\t创建时间\nalpha\t3101\t张三\t2026-01-01 10:00:00\n\nbeta\t3102\t\t2026-01-02 10:00:00\n"
        rows = panel.parse_registry(text)
        self.assertEqual([r["name"] for r in rows], ["alpha", "beta"])
        self.assertEqual(rows[0]["contact"], "张三")
        self.assertEqual(rows[1]["contact"], "")
        self.assertEqual(rows[1]["port"], "3102")

    def test_short_row(self):
        self.assertEqual(panel.parse_registry("x\t3000\n")[0]["created"], "")


class KeyLineTests(unittest.TestCase):
    def test_with_from(self):
        k = panel.parse_key_line('restrict,pty,from="10.9.9.9" ssh-ed25519 AAAAC3Nza wj-agent')
        self.assertEqual(k["label"], "wj-agent")
        self.assertEqual(k["from"], "10.9.9.9")
        self.assertEqual(k["type"], "ssh-ed25519")

    def test_plain(self):
        k = panel.parse_key_line("restrict,pty ssh-ed25519 AAAAC3Nza wj-2")
        self.assertEqual(k["label"], "wj-2")
        self.assertEqual(k["from"], "")

    def test_ecdsa_and_blank(self):
        self.assertEqual(panel.parse_key_line("restrict,pty ecdsa-sha2-nistp256 AAAAE2Vj x@y")["label"], "x@y")
        self.assertIsNone(panel.parse_key_line(""))
        self.assertIsNone(panel.parse_key_line("# comment"))
        self.assertIsNone(panel.parse_key_line("not a key"))


class VersionTests(unittest.TestCase):
    def test_ge(self):
        self.assertTrue(panel.version_satisfies(">=18", "v20.11.0"))
        self.assertTrue(panel.version_satisfies(">=18.0.0", "18.0.0"))
        self.assertFalse(panel.version_satisfies(">=20", "v18.19.1"))

    def test_ranges(self):
        self.assertTrue(panel.version_satisfies(">=18 <22", "20.1.0"))
        self.assertFalse(panel.version_satisfies(">=18 <20", "20.1.0"))
        self.assertTrue(panel.version_satisfies("^18 || ^20", "20.1.0"))
        self.assertFalse(panel.version_satisfies("^18", "20.1.0"))
        self.assertTrue(panel.version_satisfies("~18.2", "18.2.9"))
        self.assertFalse(panel.version_satisfies("~18.2", "18.3.0"))
        self.assertTrue(panel.version_satisfies("20.x", "20.5.1"))
        self.assertFalse(panel.version_satisfies("20", "18.5.1"))
        self.assertTrue(panel.version_satisfies(">= 18", "18.1.0"))
        self.assertTrue(panel.version_satisfies("^0.2.3", "0.2.9"))
        self.assertFalse(panel.version_satisfies("^0.2.3", "0.3.0"))

    def test_unknown(self):
        self.assertIsNone(panel.version_satisfies("lts/*", "20.1.0"))
        self.assertIsNone(panel.version_satisfies(">=18", ""))
        self.assertIsNone(panel.version_satisfies("", "20.1.0"))

    def test_python_version_text(self):
        self.assertTrue(panel.version_satisfies(">=3.10", "Python 3.12.3"))
        self.assertFalse(panel.version_satisfies(">=3.13", "Python 3.12.3"))


class EnvTests(unittest.TestCase):
    def test_parse_env_output(self):
        t = panel.parse_env_output("noise\nSRVENV|git|/usr/bin/git|git version 2.43.0\nSRVENV|node||\n")
        self.assertEqual(t["git"]["version"], "git version 2.43.0")
        self.assertEqual(t["node"]["path"], "")

    def test_findings(self):
        tools = {"node": {"path": "/x", "version": "v18.1.0"}, "python3": {"path": "/y", "version": "Python 3.12.3"}}
        self.assertEqual(panel.env_findings(tools, [("package.json engines.node", ">=20")])[0][0], "warn")
        self.assertEqual(panel.env_findings({}, [("package.json engines.node", ">=20")])[0][0], "warn")
        self.assertEqual(panel.env_findings(tools, [(".nvmrc", "v18")])[0][0], "ok")
        self.assertEqual(panel.env_findings(tools, [(".nvmrc", "lts/*")])[0][0], "info")


class UnitTests(unittest.TestCase):
    def test_parse_units(self):
        text = "wenjuan.service loaded active running wenjuan.service\ndbus.service loaded active running D-Bus User Message Bus\n"
        u = panel.parse_units(text)
        self.assertEqual(u[0]["active"], "active")
        self.assertEqual(u[1]["desc"], "D-Bus User Message Bus")


class ValidationTests(unittest.TestCase):
    def test_host(self):
        ok = panel.host_ok
        self.assertTrue(ok("127.0.0.1", 9300))
        self.assertTrue(ok("127.0.0.1:9300", 9300))
        self.assertTrue(ok("localhost:9300", 9300))
        self.assertTrue(ok("LOCALHOST", 9300))
        self.assertFalse(ok("127.0.0.1:9999", 9300))
        self.assertFalse(ok("evil.example", 9300))
        self.assertFalse(ok("127.0.0.1.evil.example", 9300))
        self.assertFalse(ok("localhost@evil.example", 9300))
        self.assertFalse(ok("", 9300))
        self.assertFalse(ok(None, 9300))
        self.assertTrue(ok("srv.internal:9300", 9300, ["srv.internal"]))

    def test_project_name(self):
        for good in ("wenjuan_inner_web", "ab", "a-b_c9", "a" * 32):
            self.assertTrue(panel.valid_name(good), good)
        for bad in ("a", "A", "1abc", "a b", "../x", "a/b", "a" * 33, "", "ab\n", "-ab"):
            self.assertFalse(panel.valid_name(bad), repr(bad))

    def test_port_unit(self):
        self.assertTrue(panel.valid_port("3101"))
        self.assertFalse(panel.valid_port("80"))
        self.assertFalse(panel.valid_port("70000"))
        self.assertFalse(panel.valid_port("12a"))
        self.assertTrue(panel.valid_unit("wenjuan.service"))
        self.assertFalse(panel.valid_unit("--user.service"))
        self.assertFalse(panel.valid_unit("a b.service"))
        self.assertFalse(panel.valid_unit("wenjuan"))

    def test_escape(self):
        self.assertEqual(panel.esc('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;")


class CsrfTests(unittest.TestCase):
    def test_csrf(self):
        secret = b"k" * 32
        tok = panel.make_csrf(secret, "sid1")
        self.assertTrue(panel.check_csrf(secret, "sid1", tok))
        self.assertFalse(panel.check_csrf(secret, "sid2", tok))
        self.assertFalse(panel.check_csrf(secret, "sid1", ""))
        self.assertFalse(panel.check_csrf(secret, "", tok))
        self.assertFalse(panel.check_csrf(b"z" * 32, "sid1", tok))

    def test_token_equal(self):
        self.assertTrue(panel.token_equal("abc", "abc"))
        self.assertFalse(panel.token_equal("abc", "abd"))
        self.assertFalse(panel.token_equal("abc", ""))
        self.assertTrue(panel.token_equal("令牌", "令牌"))

    def test_loopback(self):
        self.assertTrue(panel.is_loopback("127.0.0.1"))
        self.assertTrue(panel.is_loopback("::1"))
        self.assertFalse(panel.is_loopback("0.0.0.0"))
        self.assertFalse(panel.is_loopback("192.168.1.2"))


class BuildActionTests(unittest.TestCase):
    def test_create_validation(self):
        with self.assertRaises(ValueError):
            panel.build_action({"action": "create", "project": "Bad", "port": "3000"})
        with self.assertRaises(ValueError):
            panel.build_action({"action": "create", "project": "good", "port": "22"})
        _, _, args, _ = panel.build_action({"action": "create", "project": "good", "port": "3000", "contact": "x"})
        self.assertEqual(args[-2:], ["--contact", "x"])


class PasswordTests(unittest.TestCase):
    def test_hash_verify(self):
        h = panel.hash_password("correct horse battery")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(panel.verify_password("correct horse battery", h))
        self.assertFalse(panel.verify_password("correct horse batterz", h))
        self.assertFalse(panel.verify_password("", h))

    def test_unique_salt(self):
        self.assertNotEqual(panel.hash_password("same-password-1"), panel.hash_password("same-password-1"))

    def test_malformed_hash(self):
        for bad in ("", "x", "scrypt$1$2", "md5$1$2$3$aa$bb", "scrypt$a$b$c$zz$yy", None):
            self.assertFalse(panel.verify_password("x", bad))

    def test_generate(self):
        a, b = panel.generate_password(), panel.generate_password()
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, b)

    def test_password_problem(self):
        cur = panel.hash_password("initial-Password-1")
        self.assertIn("至少", panel.password_problem("short", "bob"))
        self.assertIn("相同", panel.password_problem("initial-Password-1", "bob", cur))
        self.assertIsNone(panel.password_problem("a-brand-new-pass", "bob", cur))
        self.assertIn("用户名", panel.password_problem("bobbobbobbob", "bobbobbobbob"))

    def test_username(self):
        self.assertTrue(panel.valid_username("admin"))
        self.assertTrue(panel.valid_username("li.si-2"))
        for bad in ("A", "a", "Admin", "1ab", "a b", "a/b", "", "x" * 33):
            self.assertFalse(panel.valid_username(bad), bad)

    def test_must_change_redirect(self):
        u = {"must_change": True}
        self.assertTrue(panel.needs_password_change(u, "/"))
        self.assertTrue(panel.needs_password_change(u, "/action"))
        self.assertFalse(panel.needs_password_change(u, "/password"))
        self.assertFalse(panel.needs_password_change(u, "/logout"))
        self.assertFalse(panel.needs_password_change({"must_change": False}, "/"))
        self.assertFalse(panel.needs_password_change(None, "/"))


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.rl = panel.RateLimiter(5, 300, 900, clock=lambda: self.now[0])

    def test_lock_after_five(self):
        for _ in range(4):
            self.assertFalse(self.rl.record_failure("1.1.1.1"))
            self.assertEqual(self.rl.locked_for("1.1.1.1"), 0)
        self.assertTrue(self.rl.record_failure("1.1.1.1"))
        self.assertEqual(self.rl.locked_for("1.1.1.1"), 300)
        self.now[0] += 299
        self.assertGreater(self.rl.locked_for("1.1.1.1"), 0)
        self.now[0] += 2
        self.assertEqual(self.rl.locked_for("1.1.1.1"), 0)

    def test_other_ip_unaffected(self):
        for _ in range(5):
            self.rl.record_failure("1.1.1.1")
        self.assertEqual(self.rl.locked_for("2.2.2.2"), 0)
        self.assertFalse(self.rl.record_failure("2.2.2.2"))

    def test_success_resets(self):
        for _ in range(4):
            self.rl.record_failure("1.1.1.1")
        self.rl.record_success("1.1.1.1")
        self.assertFalse(self.rl.record_failure("1.1.1.1"))

    def test_window_expiry(self):
        for _ in range(4):
            self.rl.record_failure("1.1.1.1")
        self.now[0] += 1000
        self.assertFalse(self.rl.record_failure("1.1.1.1"))


class AllowFromTests(unittest.TestCase):
    def test_empty_means_open(self):
        self.assertTrue(panel.ip_allowed("8.8.8.8", []))

    def test_match(self):
        nets = panel.parse_allow_from(["192.168.1.0/24", "10.0.0.5"])
        self.assertTrue(panel.ip_allowed("192.168.1.77", nets))
        self.assertTrue(panel.ip_allowed("10.0.0.5", nets))
        self.assertFalse(panel.ip_allowed("192.168.2.1", nets))
        self.assertFalse(panel.ip_allowed("10.0.0.6", nets))
        self.assertFalse(panel.ip_allowed("garbage", nets))

    def test_loopback_and_mapped(self):
        nets = panel.parse_allow_from(["192.168.1.0/24"])
        self.assertTrue(panel.ip_allowed("127.0.0.1", nets))
        self.assertTrue(panel.ip_allowed("::1", nets))
        self.assertTrue(panel.ip_allowed("::ffff:192.168.1.9", nets))
        self.assertFalse(panel.ip_allowed("::ffff:172.16.0.1", nets))

    def test_bad_cidr(self):
        with self.assertRaises(ValueError):
            panel.parse_allow_from(["not-a-cidr"])
        with self.assertRaises(ValueError):
            panel.parse_allow_from(["1.2.3.4/33"])


class SanTests(unittest.TestCase):
    def test_lan_ip(self):
        self.assertTrue(panel.valid_lan_ip("192.168.1.10"))
        self.assertTrue(panel.valid_lan_ip("10.0.0.2"))
        for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "169.254.1.1", "255.255.255.255", "1.2.3", "a.b.c.d", "", "::1", "192.168.1.10; rm"):
            self.assertFalse(panel.valid_lan_ip(bad), bad)

    def test_san(self):
        self.assertEqual(panel.build_san("192.168.1.10"), "IP:192.168.1.10,IP:127.0.0.1,DNS:localhost")
        with self.assertRaises(ValueError):
            panel.build_san("127.0.0.1")


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.users = str(Path(self.dir.name) / "users.json")
        self.pw = str(Path(self.dir.name) / "initial-password")

    def tearDown(self):
        self.dir.cleanup()

    def test_bootstrap_and_login(self):
        self.assertIn("admin", panel.bootstrap_store(self.users, self.pw))
        pw = Path(self.pw).read_text(encoding="utf-8").strip()
        store = panel.UserStore(self.users)
        self.assertTrue(store.authenticate("admin", pw))
        self.assertFalse(store.authenticate("admin", pw + "x"))
        self.assertFalse(store.authenticate("nobody", pw))
        u = store.get("admin")
        self.assertEqual((u["role"], u["must_change"]), ("super", True))
        self.assertNotIn(pw, Path(self.users).read_text(encoding="utf-8"))
        self.assertEqual(panel.bootstrap_store(self.users, self.pw), "已有用户库，未改动")

    def test_roundtrip_and_atomic(self):
        store = panel.UserStore(self.users)
        pw = store.create("alice")
        self.assertEqual(len(pw), 16)
        again = panel.UserStore(self.users)
        self.assertTrue(again.authenticate("alice", pw))
        self.assertTrue(again.get("alice")["must_change"])
        again.set_password("alice", "a-much-longer-pw")
        self.assertFalse(again.get("alice")["must_change"])
        import os
        self.assertEqual([n for n in os.listdir(self.dir.name) if ".tmp." in n], [])
        with self.assertRaises(panel.UserError):
            again.create("alice")
        with self.assertRaises(panel.UserError):
            again.create("Bad Name")

    def test_reset_forces_change(self):
        store = panel.UserStore(self.users)
        old = store.create("bob")
        store.set_password("bob", "my-own-password-1")
        new = store.reset("bob")
        self.assertTrue(store.get("bob")["must_change"])
        self.assertFalse(store.authenticate("bob", "my-own-password-1"))
        self.assertTrue(store.authenticate("bob", new))
        self.assertNotEqual(old, new)

    def test_delete_rules(self):
        users = {"admin": {"role": "super"}, "bob": {"role": "admin"}}
        self.assertIn("自己", panel.delete_problem(users, "admin", "admin"))
        self.assertIsNone(panel.delete_problem(users, "admin", "bob"))
        self.assertIn("最后一个超级管理员", panel.delete_problem(users, "bob", "admin"))
        self.assertIn("不存在", panel.delete_problem(users, "admin", "zed"))
        users["root2"] = {"role": "super"}
        self.assertIsNone(panel.delete_problem(users, "root2", "admin"))

    def test_store_delete(self):
        panel.bootstrap_store(self.users, self.pw)
        store = panel.UserStore(self.users)
        store.create("carol")
        with self.assertRaises(panel.UserError):
            store.delete("admin", "admin")
        store.delete("admin", "carol")
        self.assertEqual(store.names(), ["admin"])

    def test_reset_admin(self):
        import json
        panel.bootstrap_store(self.users, self.pw)
        store = panel.UserStore(self.users)
        pw1 = Path(self.pw).read_text(encoding="utf-8").strip()
        store.set_password("admin", "changed-password-1")
        store.create("dave")
        panel.bootstrap_store(self.users, self.pw, reset=True)
        pw2 = Path(self.pw).read_text(encoding="utf-8").strip()
        self.assertNotEqual(pw1, pw2)
        self.assertTrue(store.authenticate("admin", pw2))      # 运行中的 store 会重读被外部改动的文件
        self.assertTrue(store.get("admin")["must_change"])
        self.assertIn("dave", store.names())
        data = json.loads(Path(self.users).read_text(encoding="utf-8"))
        data["users"]["eve"] = data["users"].pop("admin")
        Path(self.users).write_text(json.dumps(data), encoding="utf-8")
        panel.bootstrap_store(self.users, self.pw, reset=True)   # admin 不存在：清空后重建
        self.assertEqual(panel.UserStore(self.users).names(), ["admin"])

    def test_corrupt_file(self):
        Path(self.users).write_text("{not json", encoding="utf-8")
        with self.assertRaises(panel.UserError):
            panel.load_users_file(self.users)
        Path(self.users).write_text('{"users": {"BAD": {"role": "x", "hash": "h"}}}', encoding="utf-8")
        with self.assertRaises(panel.UserError):
            panel.load_users_file(self.users)


if __name__ == "__main__":
    unittest.main()
