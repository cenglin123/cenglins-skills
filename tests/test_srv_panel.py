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


if __name__ == "__main__":
    unittest.main()
