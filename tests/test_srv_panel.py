import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

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
        for weak in ("Sh0rt!", "alllower1!", "ALLUPPER1!", "NoDigits!!", "NoSymbol12", "Abc 12345"):
            with self.subTest(weak=weak):
                self.assertIn("至少 8 位", panel.password_problem(weak, "bob"))
        self.assertIn("相同", panel.password_problem("initial-Password-1", "bob", cur))
        self.assertIsNone(panel.password_problem("Ab1!efgh", "bob", cur))
        self.assertIn("用户名", panel.password_problem("Bob-bob-1", "Bob-bob-1"))

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


class DeleteActionTests(unittest.TestCase):
    def test_delete_args(self):
        with patch.object(panel, "registered_names", return_value={"demo"}):
            _, params, args, _ = panel.build_action({"action": "delete", "project": "demo"})
            self.assertEqual(args[1:], ["delete", "demo"])
            self.assertFalse(params["purge"])
            _, params, args, _ = panel.build_action({"action": "delete", "project": "demo", "purge": "1"})
            self.assertEqual(args[1:], ["delete", "demo", "--purge"])
            with self.assertRaises(ValueError):
                panel.build_action({"action": "delete", "project": "other"})

    def test_delete_needs_confirmation(self):
        self.assertIn("delete", panel.CONFIRM_ACTIONS)

    def test_confirm_page_warns_on_purge(self):
        html = panel.render_confirm({"action": "delete", "project": "demo", "purge": "1"}, "tok")
        self.assertIn("彻底删除", html)
        self.assertIn('name="purge" value="1"', html)
        self.assertIn("归档", panel.render_confirm({"action": "delete", "project": "demo"}, "tok"))


def _ssh_str(b):
    return len(b).to_bytes(4, "big") + b


def fake_pubkey(kind="ed25519", bits=2048):
    import base64
    if kind == "ed25519":
        blob = _ssh_str(b"ssh-ed25519") + _ssh_str(b"\x01" * 32)
        return "ssh-ed25519 " + base64.b64encode(blob).decode() + " user@host"
    n = (1 << (bits - 1)) | 1
    blob = _ssh_str(b"ssh-rsa") + _ssh_str(b"\x01\x00\x01") + _ssh_str(b"\x00" + n.to_bytes(bits // 8, "big"))
    return "ssh-rsa " + base64.b64encode(blob).decode() + " user@host"


class InviteHelpersTests(unittest.TestCase):
    def test_code_and_hash(self):
        a, b = panel.new_code(), panel.new_code()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)                  # >=192 bit in base64url
        self.assertEqual(len(panel.code_hash(a)), 64)
        self.assertNotIn(a, panel.code_hash(a))

    def test_encode_roundtrip_has_no_padding(self):
        import base64
        text = panel.encode_invite({"v": 1, "label": "中文 ok", "n": "x" * 7})
        self.assertTrue(text.startswith("srvinvite1."))
        body = text[len("srvinvite1."):]
        self.assertNotIn("=", body)
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        self.assertEqual(data["label"], "中文 ok")

    def test_status_transitions(self):
        rec = {"status": "pending", "expires": "2030-01-01T00:00:00Z"}
        before = panel.parse_iso_utc("2029-12-31T23:59:59Z")
        after = panel.parse_iso_utc("2030-01-01T00:00:00Z")
        self.assertEqual(panel.invite_status(rec, before), "pending")
        self.assertEqual(panel.invite_status(rec, after), "expired")
        self.assertEqual(panel.invite_status(dict(rec, status="used"), after), "used")
        self.assertEqual(panel.invite_status(dict(rec, status="revoked"), before), "revoked")
        self.assertEqual(panel.invite_status({"status": "pending", "expires": "garbage"}, before), "expired")

    def test_host_keys_and_cert_hash(self):
        keys = panel.parse_host_keys(["ssh-ed25519 AAAA root@x\n", "ssh-rsa BBBB\n", "ssh-ed25519 AAAA other\n", "junk\n", ""])
        self.assertEqual(keys, ["ssh-ed25519 AAAA", "ssh-rsa BBBB"])
        import hashlib
        import ssl
        pem = ssl.DER_cert_to_PEM_cert(b"abc-der-bytes")
        self.assertEqual(panel.cert_sha256_hex(pem), hashlib.sha256(b"abc-der-bytes").hexdigest())

    def test_from_spec(self):
        self.assertEqual(panel.normalize_from_spec(""), "")
        self.assertEqual(panel.normalize_from_spec("10.0.0.5, 192.168.1.0/24"), "10.0.0.5,192.168.1.0/24")
        for bad in ("nonsense", "10.0.0.0/99", "10.0.0.5;ls", "a b"):
            with self.assertRaises(panel.InviteError):
                panel.normalize_from_spec(bad)
        self.assertTrue(panel.invite_from_allows("", "8.8.8.8"))
        self.assertTrue(panel.invite_from_allows("192.168.1.0/24,10.0.0.5", "10.0.0.5"))
        self.assertFalse(panel.invite_from_allows("192.168.1.0/24", "192.168.2.9"))
        self.assertFalse(panel.invite_from_allows("192.168.1.0/24", "127.0.0.1"))   # 回环不豁免

    def test_label_conflict(self):
        self.assertIsNone(panel.label_conflict("a", {"b"}, {"c"}))
        self.assertIn("已授权", panel.label_conflict("b", {"b"}, set()))
        self.assertIn("待使用", panel.label_conflict("c", set(), {"c"}))


class PubkeyValidationTests(unittest.TestCase):
    def test_accepts(self):
        self.assertEqual(panel.validate_pubkey(fake_pubkey()).split()[0], "ssh-ed25519")
        self.assertEqual(panel.validate_pubkey(fake_pubkey("rsa", 3072)).split()[0], "ssh-rsa")
        self.assertEqual(panel.validate_pubkey(fake_pubkey("rsa", 4096)).split()[0], "ssh-rsa")

    def test_normalizes_comment_away(self):
        self.assertEqual(len(panel.validate_pubkey(fake_pubkey()).split()), 2)

    def test_rejects(self):
        for bad in (fake_pubkey("rsa", 2048), "", "ssh-dss AAAA", "-----BEGIN OPENSSH PRIVATE KEY-----",
                    fake_pubkey() + "\n" + fake_pubkey(), "ssh-ed25519 !!!notbase64", "ssh-ed25519",
                    "sk-ssh-ed25519@openssh.com AAAA", "ssh-ed25519 " + "A" * 5000):
            with self.assertRaises(ValueError, msg=bad[:30]):
                panel.validate_pubkey(bad)

    def test_rejects_type_mismatch(self):
        wrong = fake_pubkey().replace("ssh-ed25519 ", "ecdsa-sha2-nistp256 ", 1)
        with self.assertRaises(ValueError):
            panel.validate_pubkey(wrong)


class EnrollBodyTests(unittest.TestCase):
    CODE = "c" * 32

    def body(self, **kw):
        d = {"code": self.CODE, "pubkey": fake_pubkey(), "device_name": "my laptop-1.local"}
        d.update(kw)
        return json.dumps(d).encode()

    def test_ok(self):
        code, pub, dev = panel.parse_enroll_body(self.body())
        self.assertEqual((code, dev), (self.CODE, "my laptop-1.local"))

    def test_rejections(self):
        cases = [(b"x" * 9000, 413), (b"not json", 400), (b"[]", 400), (self.body(device_name="bad;name"), 400),
                 (self.body(device_name="x" * 65), 400), (self.body(device_name=""), 400), (self.body(pubkey=5), 400),
                 (self.body(code="short"), 403), (self.body(code="a b" * 12), 403), (self.body(code=None), 403)]
        for raw, status in cases:
            with self.assertRaises(panel.EnrollError, msg=raw[:30]) as cm:
                panel.parse_enroll_body(raw)
            self.assertEqual(cm.exception.status, status, raw[:30])

    def test_size_limit_is_8kb(self):
        self.assertEqual(panel.ENROLL_MAX_BODY, 8192)


class InviteStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "invites.json")
        self.store = panel.InviteStore(self.path)
        self.code = panel.new_code()
        self.rec = {"id": "deadbeef", "code_sha256": panel.code_hash(self.code), "project": "demo", "label": "ap1",
                    "from": "", "created_by": "cli:root", "created": "2030-01-01T00:00:00Z",
                    "expires": "2999-01-01T00:00:00Z", "ssh_port": 22, "status": "pending"}

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_store_is_created(self):
        self.assertEqual(json.loads(Path(self.path).read_text())["invites"], {})

    def test_roundtrip_and_no_plaintext_code(self):
        self.store.add(self.rec)
        again = panel.InviteStore(self.path)
        self.assertEqual(again.find_by_code(self.code)["id"], "deadbeef")
        self.assertIsNone(again.find_by_code(self.code + "x"))
        self.assertNotIn(self.code, Path(self.path).read_text())

    def test_used_and_revoke_flow(self):
        self.store.add(self.rec)
        self.store.mark_used("deadbeef", "10.0.0.5", "laptop", "SHA256:fp")
        rec = self.store.snapshot()["deadbeef"]
        self.assertEqual((rec["status"], rec["used_ip"], rec["used_device"], rec["used_fingerprint"]),
                         ("used", "10.0.0.5", "laptop", "SHA256:fp"))
        self.assertIn("used_at", rec)
        with self.assertRaises(panel.InviteError):
            self.store.mark_used("deadbeef", "10.0.0.5", "laptop", "SHA256:fp")
        with self.assertRaises(panel.InviteError):
            self.store.revoke("deadbeef")

    def test_revoke(self):
        self.store.add(self.rec)
        self.assertEqual(self.store.revoke("deadbeef")["status"], "revoked")
        with self.assertRaises(panel.InviteError):
            self.store.mark_used("deadbeef", "1.1.1.1", "d", "fp")
        with self.assertRaises(panel.InviteError):
            self.store.revoke("nosuch")

    def test_expired_cannot_be_used(self):
        self.store.add(dict(self.rec, expires="2000-01-01T00:00:00Z"))
        with self.assertRaises(panel.InviteError):
            self.store.mark_used("deadbeef", "1.1.1.1", "d", "fp")

    def test_external_change_is_picked_up(self):
        other = panel.InviteStore(self.path)
        other.add(self.rec)
        self.assertIsNotNone(self.store.find_by_code(self.code))


class ProcessEnrollTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.store = panel.InviteStore(str(Path(self.dir.name) / "invites.json"))
        self.log = str(Path(self.dir.name) / "audit.log")
        self.state = panel.State(None, 9300, log_file=self.log, lan_ip="192.0.2.10", invites=self.store)
        self.code = panel.new_code()
        self.add_invite()
        self.grants = []
        self.patches = [
            patch.object(panel, "registered_names", return_value={"demo"}),
            patch.object(panel, "account_info", return_value=(1001, True)),
            patch.object(panel, "is_disabled", return_value=False),
            patch.object(panel, "key_fingerprint", return_value="SHA256:testfp"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def add_invite(self, **kw):
        rec = {"id": "deadbeef", "code_sha256": panel.code_hash(self.code), "project": "demo", "label": "ap1", "from": "",
               "created_by": "cli:root", "created": "2030-01-01T00:00:00Z", "expires": "2999-01-01T00:00:00Z",
               "ssh_port": 22, "status": "pending"}
        rec.update(kw)
        self.store.add(rec)

    def grant_ok(self, args, stdin_text=None, timeout=0):
        self.grants.append((args, stdin_text))
        return 0, "已授权", ""

    def body(self, code=None, pubkey=None, device="laptop"):
        return json.dumps({"code": code or self.code, "pubkey": pubkey or fake_pubkey(), "device_name": device}).encode()

    def enroll(self, raw=None, ip="10.0.0.5", grant=None):
        return panel.process_enroll(self.state, raw or self.body(), ip, grant=grant or self.grant_ok, info_fn=lambda p, h: "INFO " + p)

    def test_success(self):
        status, resp = self.enroll()
        self.assertEqual(status, 200)
        self.assertEqual((resp["ok"], resp["project"], resp["account"], resp["ssh_port"], resp["host"], resp["fingerprint"], resp["info"]),
                         (True, "demo", "demo", 22, "192.0.2.10", "SHA256:testfp", "INFO demo"))
        args, stdin = self.grants[0]
        self.assertEqual(args[1:6], ["grant", "demo", "-", "--label", "ap1"])
        self.assertTrue(stdin.startswith("ssh-ed25519 ") and stdin.endswith("\n"))
        rec = self.store.snapshot()["deadbeef"]
        self.assertEqual((rec["status"], rec["used_ip"], rec["used_device"]), ("used", "10.0.0.5", "laptop"))

    def test_from_is_passed_to_grant(self):
        code2 = "q" * 32
        self.add_invite(id="feedface", code_sha256=panel.code_hash(code2), **{"from": "10.0.0.0/24"})
        status, resp = self.enroll(self.body(code=code2))
        self.assertEqual(status, 200)
        self.assertEqual(self.grants[0][0][-2:], ["--from", "10.0.0.0/24"])

    def test_wrong_used_expired_revoked_all_look_the_same(self):
        msgs = set()
        status, resp = self.enroll(self.body(code="z" * 32)); msgs.add(resp["error"]); self.assertEqual(status, 403)
        self.enroll()                                   # 用掉
        status, resp = self.enroll(); msgs.add(resp["error"]); self.assertEqual(status, 403)
        self.add_invite(id="aaaa0001", code_sha256=panel.code_hash("e" * 32), expires="2000-01-01T00:00:00Z")
        status, resp = self.enroll(self.body(code="e" * 32)); msgs.add(resp["error"])
        self.add_invite(id="aaaa0002", code_sha256=panel.code_hash("r" * 32))
        self.store.revoke("aaaa0002")
        status, resp = self.enroll(self.body(code="r" * 32)); msgs.add(resp["error"])
        self.assertEqual(msgs, {panel.GENERIC_INVALID})
        self.assertEqual(len(self.grants), 1)

    def test_from_restriction(self):
        code2 = "q" * 32
        self.add_invite(id="feedface", code_sha256=panel.code_hash(code2), **{"from": "192.168.1.0/24"})
        status, resp = self.enroll(self.body(code=code2), ip="10.0.0.9")
        self.assertEqual(status, 403)
        self.assertEqual(self.grants, [])
        self.assertEqual(self.store.snapshot()["feedface"]["status"], "pending")
        self.assertEqual(self.enroll(self.body(code=code2), ip="192.168.1.7")[0], 200)

    def test_rsa_2048_rejected_and_invite_stays_pending(self):
        status, resp = self.enroll(self.body(pubkey=fake_pubkey("rsa", 2048)))
        self.assertEqual(status, 400)
        self.assertIn("RSA", resp["error"])
        self.assertEqual(self.store.snapshot()["deadbeef"]["status"], "pending")

    def test_grant_failure_keeps_invite_pending(self):
        status, resp = self.enroll(grant=lambda args, stdin_text=None, timeout=0: (1, "", "错误：标签 ap1 已被另一把公钥使用"))
        self.assertEqual(status, 409)
        self.assertFalse(resp["ok"])
        self.assertIn("标签 ap1", resp["error"])
        self.assertEqual(self.store.snapshot()["deadbeef"]["status"], "pending")
        self.assertEqual(self.enroll()[0], 200)         # 修好后同一个码还能用

    def test_disabled_project(self):
        with patch.object(panel, "is_disabled", return_value=True):
            status, resp = self.enroll()
        self.assertEqual(status, 409)
        self.assertEqual(self.grants, [])

    def test_lockout_after_five_failures_per_ip(self):
        for _ in range(5):
            self.assertEqual(self.enroll(self.body(code="z" * 32), ip="10.9.9.9")[0], 403)
        status, resp = self.enroll(ip="10.9.9.9")                      # 正确的码也被拒
        self.assertEqual(status, 429)
        self.assertEqual(self.grants, [])
        self.assertEqual(self.enroll(ip="10.9.9.8")[0], 200)           # 别的 IP 不受影响
        self.assertEqual(self.state.limiter.locked_for("10.9.9.9"), 0)  # 登录限流是另一份计数
        self.assertEqual(self.state.enroll_limiter.lock_seconds, 900)

    def test_audit_never_contains_code(self):
        self.enroll(self.body(code="z" * 32))
        self.enroll()
        log = Path(self.log).read_text(encoding="utf-8")
        self.assertIn("enroll", log)
        self.assertNotIn(self.code, log)
        self.assertNotIn("z" * 32, log)
        self.assertIn("invalid_code", log)
        self.assertIn("SHA256:testfp", log)

    def test_concurrent_same_code_succeeds_once(self):
        import threading
        import time as _t
        results = []

        def slow_grant(args, stdin_text=None, timeout=0):
            _t.sleep(0.2)
            self.grants.append(args)
            return 0, "ok", ""

        def worker(ip):
            results.append(self.enroll(ip=ip, grant=slow_grant)[0])

        threads = [threading.Thread(target=worker, args=("10.0.0.%d" % i,)) for i in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 403, 403])
        self.assertEqual(len(self.grants), 1)


class MakeInviteTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.store = panel.InviteStore(str(Path(self.dir.name) / "invites.json"))
        cert = Path(self.dir.name) / "tls.crt"
        import ssl
        cert.write_text(ssl.DER_cert_to_PEM_cert(b"cert-der"))
        self.conf = {"lan_ip": "192.0.2.10", "port": 9300, "tls_cert": str(cert)}
        self.keys = Path(self.dir.name) / "keys"
        self.keys.mkdir()
        (self.keys / "demo").write_text("restrict,pty ssh-ed25519 AAAA existing-key\n")
        self.patches = [
            patch.object(panel, "registered_names", return_value={"demo", "off"}),
            patch.object(panel, "account_info", return_value=(1001, True)),
            patch.object(panel, "is_disabled", side_effect=lambda n: n == "off"),
            patch.object(panel, "KEYS_DIR", str(self.keys)),
            patch.object(panel, "read_text", side_effect=lambda p, limit=0: open(p, encoding="utf-8").read() if p.startswith(self.dir.name) else "ssh-ed25519 HOSTKEY root@x"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def make(self, project="demo", label="ap1", hours=24, frm="", now=1_800_000_000.0):
        return panel.make_invite(self.store, self.conf, project, label, hours, frm, "cli:root", now=now, sshd_port_fn=lambda: 2222)

    def test_payload_contents(self):
        import base64
        import hashlib
        import ssl
        with patch("glob.glob", return_value=["/etc/ssh/ssh_host_ed25519_key.pub"]):
            text, rec = self.make()
        body = text[len("srvinvite1."):]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.assertEqual((data["v"], data["host"], data["ssh_port"], data["project"], data["panel"], data["label"]),
                         (1, "192.0.2.10", 2222, "demo", "https://192.0.2.10:9300", "ap1"))
        self.assertEqual(data["panel_cert_sha256"], hashlib.sha256(b"cert-der").hexdigest())
        self.assertEqual(data["ssh_host_keys"], ["ssh-ed25519 HOSTKEY"])
        self.assertEqual(data["expires"], "2027-01-16T08:00:00Z")
        self.assertGreaterEqual(len(data["code"]), 32)
        self.assertEqual(rec["code_sha256"], panel.code_hash(data["code"]))
        self.assertNotIn(data["code"], Path(self.store.path).read_text())
        self.assertNotIn("PRIVATE", text)

    def test_rules(self):
        with patch("glob.glob", return_value=["x"]):
            self.make()
            for kwargs in ({"label": "ap1"}, {"label": "existing-key"}, {"label": "bad label"}, {"hours": 0}, {"hours": 169},
                           {"hours": "x"}, {"frm": "nonsense"}, {"project": "nosuch"}, {"project": "off"}):
                with self.assertRaises(panel.InviteError, msg=str(kwargs)):
                    self.make(**kwargs)
            self.make(label="ap2", hours=168, frm="10.0.0.0/24")




if __name__ == "__main__":
    unittest.main()
