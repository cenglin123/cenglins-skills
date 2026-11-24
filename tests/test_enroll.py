import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server"


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


safe_push = load("safe_push_enroll", "scripts/safe_push.py")
panel = load("srv_panel_enroll", "panel/srv_panel.py")

NOW = 1_800_000_000.0
CODE = "A" * 32
HOSTKEY_ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE0000"
HOSTKEY_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQEXAMPLE"


def payload(**kw):
    p = {"v": 1, "host": "192.0.2.10", "ssh_port": 22, "project": "demo", "panel": "https://192.0.2.10:9300",
         "panel_cert_sha256": "ab" * 32, "ssh_host_keys": [HOSTKEY_ED, HOSTKEY_RSA], "code": CODE,
         "expires": "2027-01-16T08:00:00Z", "label": "ap1"}
    p.update(kw)
    return p


def encode(p):
    raw = json.dumps(p, separators=(",", ":")).encode()
    return "srvinvite1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")




class DecodeInviteTests(unittest.TestCase):
    def test_valid(self):
        inv = safe_push.decode_invite(encode(payload()), now=NOW)
        self.assertEqual(inv["project"], "demo")
        self.assertEqual(inv["panel_port"], 9300)

    def test_server_encoding_is_accepted(self):
        text = panel.encode_invite(payload())
        self.assertNotIn("=", text)
        self.assertEqual(safe_push.decode_invite(text, now=NOW)["code"], CODE)

    def test_bad_prefix_and_version(self):
        for bad in ("", "hello", "srvinvite2." + encode(payload())[11:]):
            with self.assertRaises(safe_push.EnrollError):
                safe_push.decode_invite(bad, now=NOW)
        with self.assertRaises(safe_push.EnrollError):
            safe_push.decode_invite(encode(payload(v=2)), now=NOW)

    def test_garbage_body(self):
        for bad in ("srvinvite1.@@@", "srvinvite1.AAAA", "srvinvite1." + base64.urlsafe_b64encode(b"[1]").decode()):
            with self.assertRaises(safe_push.EnrollError):
                safe_push.decode_invite(bad, now=NOW)

    def test_missing_fields(self):
        p = payload()
        del p["code"]
        with self.assertRaisesRegex(safe_push.EnrollError, "code"):
            safe_push.decode_invite(encode(p), now=NOW)

    def test_expired(self):
        with self.assertRaisesRegex(safe_push.EnrollError, "过期"):
            safe_push.decode_invite(encode(payload(expires="2020-01-01T00:00:00Z")), now=NOW)

    def test_field_validation(self):
        for override in ({"project": "Bad Name"}, {"ssh_port": 0}, {"ssh_port": "22"}, {"panel": "http://192.0.2.10:9300"},
                         {"panel": "https://198.51.100.1:9300"}, {"panel_cert_sha256": "xyz"}, {"ssh_host_keys": []},
                         {"ssh_host_keys": ["no-base64 !!"]}, {"code": "short"}, {"label": "bad label"},
                         {"expires": "tomorrow"}, {"host": "bad host"}):
            with self.assertRaises(safe_push.EnrollError, msg=str(override)):
                safe_push.decode_invite(encode(payload(**override)), now=NOW)

    def test_error_never_contains_code(self):
        try:
            safe_push.decode_invite(encode(payload(expires="2020-01-01T00:00:00Z")), now=NOW)
        except safe_push.EnrollError as e:
            self.assertNotIn(CODE, str(e))


class DeviceNameTests(unittest.TestCase):
    def test_sanitize(self):
        s = safe_push.sanitize_device_name
        self.assertEqual(s("My-Laptop"), "My-Laptop")
        self.assertEqual(s("张三的电脑"), "device")
        self.assertEqual(s("dev;rm -rf /"), "dev-rm -rf")
        self.assertEqual(s("a" * 100), "a" * 64)
        self.assertEqual(s(""), "device")
        self.assertEqual(s("  host.local  "), "host.local")
        self.assertTrue(panel.DEVICE_RE.fullmatch(s("x$y%z")))


KEYGEN_PLAIN = "# Host 192.0.2.10 found: line 3\n192.0.2.10 " + HOSTKEY_ED + "\n"
KEYGEN_HASHED = "# Host 192.0.2.10 found: line 1\n|1|c2FsdA==|aGFzaA== " + HOSTKEY_ED + "\n"


class KnownHostsTests(unittest.TestCase):
    def test_spec(self):
        self.assertEqual(safe_push.known_hosts_spec("192.0.2.10", 22), "192.0.2.10")
        self.assertEqual(safe_push.known_hosts_spec("192.0.2.10", 2222), "[192.0.2.10]:2222")

    def test_parse_find_output(self):
        want = [tuple(HOSTKEY_ED.split(" "))]
        self.assertEqual(safe_push.parse_keygen_find(KEYGEN_PLAIN), want)
        self.assertEqual(safe_push.parse_keygen_find(KEYGEN_HASHED), want)
        self.assertEqual(safe_push.parse_keygen_find(""), [])
        self.assertEqual(safe_push.parse_keygen_find("@revoked 1.2.3.4 ssh-ed25519 AAAA\n"), [])

    def test_classify(self):
        ed, rsa = tuple(HOSTKEY_ED.split(" ")), tuple(HOSTKEY_RSA.split(" "))
        self.assertEqual(safe_push.classify_known_hosts([], [ed, rsa]), ([ed, rsa], []))
        self.assertEqual(safe_push.classify_known_hosts([ed], [ed, rsa]), ([rsa], []))
        other = ("ssh-ed25519", "AAAAOTHERKEY")
        self.assertEqual(safe_push.classify_known_hosts([other], [ed, rsa]), ([rsa], ["ssh-ed25519"]))

    def run_plan(self, find_output, port=22, create_file=True):
        inv = safe_push.decode_invite(encode(payload(ssh_port=port)), now=NOW)
        with tempfile.TemporaryDirectory() as d:
            kh = Path(d) / "known_hosts"
            if create_file:
                kh.write_text("x\n")
            with patch.object(safe_push, "_keygen_find", return_value=find_output) as f:
                result = safe_push.plan_known_hosts(inv, str(kh))
            return result, f

    def test_plan_adds_missing_lines(self):
        (lines, conflicts), f = self.run_plan("")
        self.assertEqual(conflicts, [])
        self.assertEqual(lines, ["192.0.2.10 " + HOSTKEY_ED, "192.0.2.10 " + HOSTKEY_RSA])
        self.assertEqual(f.call_args.args[0], "192.0.2.10")

    def test_plan_skips_identical(self):
        (lines, conflicts), _ = self.run_plan(KEYGEN_HASHED)
        self.assertEqual(lines, ["192.0.2.10 " + HOSTKEY_RSA])

    def test_plan_nonstandard_port(self):
        (lines, _), f = self.run_plan("", port=2222)
        self.assertEqual(f.call_args.args[0], "[192.0.2.10]:2222")
        self.assertTrue(lines[0].startswith("[192.0.2.10]:2222 ssh-ed25519 "))

    def test_plan_conflict(self):
        (lines, conflicts), _ = self.run_plan("192.0.2.10 ssh-ed25519 AAAADIFFERENT\n")
        self.assertEqual(conflicts, ["ssh-ed25519"])

    def test_missing_file_means_no_lookup(self):
        (lines, conflicts), f = self.run_plan("ignored", create_file=False)
        f.assert_not_called()
        self.assertEqual(len(lines), 2)

    def test_write_appends_with_newline_repair(self):
        with tempfile.TemporaryDirectory() as d:
            kh = Path(d) / ".ssh" / "known_hosts"
            safe_push.write_known_hosts(str(kh), ["a ssh-ed25519 K1"])
            self.assertEqual(kh.read_text(), "a ssh-ed25519 K1\n")
            kh.write_text("old line without newline")
            safe_push.write_known_hosts(str(kh), ["b ssh-ed25519 K2"])
            self.assertEqual(kh.read_text(), "old line without newline\nb ssh-ed25519 K2\n")
            safe_push.write_known_hosts(str(kh), [])
            self.assertEqual(kh.read_text().count("\n"), 2)


class SshConfigTests(unittest.TestCase):
    def inv(self):
        return safe_push.decode_invite(encode(payload()), now=NOW)

    def test_block(self):
        b = safe_push.build_ssh_config_block("srv-demo", self.inv(), Path("/home/u/.ssh/id_x"))
        for line in ("Host srv-demo", "HostName 192.0.2.10", "Port 22", "User demo",
                     "IdentityFile /home/u/.ssh/id_x", "IdentitiesOnly yes"):
            self.assertIn(line, b)

    def test_path_with_space_is_quoted(self):
        b = safe_push.build_ssh_config_block("a", self.inv(), "C:\\Users\\Zhang San\\.ssh\\id_x")
        self.assertIn('IdentityFile "C:/Users/Zhang San/.ssh/id_x"', b)

    def test_append_when_absent(self):
        self.assertEqual(safe_push.plan_ssh_config("Host other\n    HostName x\n", "srv-demo", self.inv(), "/k"), "append")
        self.assertEqual(safe_push.plan_ssh_config("", "srv-demo", self.inv(), "/k"), "append")

    def test_same_content_is_skipped(self):
        text = "Host a\n  HostName x\n\n" + safe_push.build_ssh_config_block("srv-demo", self.inv(), "/k") + "\nHost z\n  HostName q\n"
        self.assertEqual(safe_push.plan_ssh_config(text, "srv-demo", self.inv(), "/k"), "same")

    def test_different_content_conflicts(self):
        text = "Host srv-demo\n    HostName 203.0.113.9\n    Port 22\n    User demo\n    IdentityFile /k\n    IdentitiesOnly yes\n"
        with self.assertRaisesRegex(safe_push.EnrollError, "不同"):
            safe_push.plan_ssh_config(text, "srv-demo", self.inv(), "/k")
        text = safe_push.build_ssh_config_block("srv-demo", self.inv(), "/other-key")
        with self.assertRaises(safe_push.EnrollError):
            safe_push.plan_ssh_config(text, "srv-demo", self.inv(), "/k")

    def test_alias_in_multi_host_line(self):
        self.assertIsNotNone(safe_push.parse_ssh_config_host("Host a srv-demo\n  User x\n", "srv-demo"))
        self.assertIsNone(safe_push.parse_ssh_config_host("Host srv-demo2\n  User x\n", "srv-demo"))

    def test_append_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / ".ssh" / "config"
            safe_push.append_ssh_config(str(cfg), "Host a\n    User x\n")
            self.assertEqual(cfg.read_text(), "Host a\n    User x\n")
            safe_push.append_ssh_config(str(cfg), "Host b\n    User y\n")
            self.assertEqual(cfg.read_text(), "Host a\n    User x\n\nHost b\n    User y\n")


class FakeTls:
    def __init__(self, der, response=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"):
        self.der = der
        self.sent = []
        self.chunks = [response, b""]
        self.closed = False

    def getpeercert(self, binary_form=False):
        return self.der

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


class PinnedPostTests(unittest.TestCase):
    def inv(self, der):
        return safe_push.decode_invite(encode(payload(panel_cert_sha256=hashlib.sha256(der).hexdigest())), now=NOW)

    def test_mismatch_sends_nothing(self):
        conn = FakeTls(b"real-cert")
        inv = self.inv(b"other-cert")
        with patch.object(safe_push, "_open_tls", return_value=conn):
            with self.assertRaisesRegex(safe_push.EnrollError, "未发送邀请码"):
                safe_push.pinned_post(inv, "/enroll", {"code": CODE})
        self.assertEqual(conn.sent, [])
        self.assertTrue(conn.closed)

    def test_match_posts_json_with_host_header(self):
        conn = FakeTls(b"real-cert", b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
        inv = self.inv(b"real-cert")
        with patch.object(safe_push, "_open_tls", return_value=conn):
            status, data = safe_push.pinned_post(inv, "/enroll", {"code": CODE, "pubkey": "k"})
        self.assertEqual((status, data), (200, {"ok": True}))
        req = conn.sent[0]
        self.assertTrue(req.startswith(b"POST /enroll HTTP/1.1\r\nHost: 192.0.2.10:9300\r\n"))
        self.assertIn(CODE.encode(), req)

    def test_non_json_response(self):
        conn = FakeTls(b"c", b"HTTP/1.1 502 Bad\r\n\r\n<html>")
        with patch.object(safe_push, "_open_tls", return_value=conn):
            with self.assertRaises(safe_push.EnrollError):
                safe_push.pinned_post(self.inv(b"c"), "/enroll", {})

    def test_parse_http_response(self):
        self.assertEqual(safe_push.parse_http_response(b"HTTP/1.1 403 Forbidden\r\nA: b\r\n\r\nbody"), (403, b"body"))
        with self.assertRaises(safe_push.EnrollError):
            safe_push.parse_http_response(b"garbage")


class CmdEnrollTests(unittest.TestCase):
    def args(self, invite, **kw):
        ns = MagicMock()
        ns.invite, ns.device_name, ns.key, ns.alias, ns.json = invite, "laptop", None, None, True
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def run_enroll(self, invite, home, find="", post=None, ssh_ok=(True, "")):
        post = post or (200, {"ok": True, "account": "demo", "fingerprint": "SHA256:x", "info": "INFO"})
        with patch.object(safe_push.Path, "home", return_value=Path(home)), \
             patch.object(safe_push, "_keygen_find", return_value=find), \
             patch.object(safe_push, "ensure_enroll_key", return_value="ssh-ed25519 PUB") as ek, \
             patch.object(safe_push, "pinned_post", return_value=post) as pp, \
             patch.object(safe_push, "verify_enroll_ssh", return_value=ssh_ok):
            with patch("builtins.print") as pr:
                rc = safe_push.cmd_enroll(self.args(invite))
        return rc, pp, ek, pr

    def test_expired_makes_no_request(self):
        with tempfile.TemporaryDirectory() as d:
            rc, pp, ek, _ = self.run_enroll(encode(payload(expires="2020-01-01T00:00:00Z")), d)
        self.assertEqual(rc, 1)
        pp.assert_not_called()
        ek.assert_not_called()

    def test_known_hosts_conflict_stops_before_sending_code(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".ssh").mkdir()
            (Path(d) / ".ssh" / "known_hosts").write_text("x\n")
            rc, pp, ek, pr = self.run_enroll(encode(payload(expires="2999-01-01T00:00:00Z")), d,
                                             find="192.0.2.10 ssh-ed25519 AAAADIFFERENT\n")
            self.assertEqual((Path(d) / ".ssh" / "known_hosts").read_text(), "x\n")
        self.assertEqual(rc, 1)
        pp.assert_not_called()
        ek.assert_not_called()
        self.assertNotIn(CODE, json.dumps([c.args for c in pr.call_args_list]))

    def test_ssh_config_conflict_stops_before_sending_code(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".ssh").mkdir()
            (Path(d) / ".ssh" / "config").write_text("Host srv-demo\n  HostName 203.0.113.1\n")
            rc, pp, _, _ = self.run_enroll(encode(payload(expires="2999-01-01T00:00:00Z")), d)
            self.assertEqual((Path(d) / ".ssh" / "config").read_text(), "Host srv-demo\n  HostName 203.0.113.1\n")
        self.assertEqual(rc, 1)
        pp.assert_not_called()

    def test_success_writes_known_hosts_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            rc, pp, _, pr = self.run_enroll(encode(payload(expires="2999-01-01T00:00:00Z")), d)
            kh = (Path(d) / ".ssh" / "known_hosts").read_text()
            cfg = (Path(d) / ".ssh" / "config").read_text()
            out = pr.call_args_list[-1].args[0]
        self.assertEqual(rc, 0)
        self.assertIn("192.0.2.10 " + HOSTKEY_ED, kh)
        self.assertIn("Host srv-demo", cfg)
        self.assertIn("IdentitiesOnly yes", cfg)
        result = json.loads(out)
        self.assertEqual(result["status"], "enrolled")
        self.assertEqual(result["config_sample"]["server"]["host"], "srv-demo")
        self.assertEqual(result["config_sample"]["server"]["remote_repo_dir"], "/srv/demo/repo")
        self.assertIsNone(result["config_sample"]["server"]["ssh_key_path"])
        self.assertNotIn(CODE, out)
        sent = pp.call_args.args[2]
        self.assertEqual(sent["code"], CODE)
        self.assertEqual(sent["device_name"], "laptop")

    def test_panel_rejection_is_reported_and_nothing_written(self):
        with tempfile.TemporaryDirectory() as d:
            rc, pp, _, pr = self.run_enroll(encode(payload(expires="2999-01-01T00:00:00Z")), d,
                                            post=(403, {"ok": False, "error": "邀请码无效或已失效"}))
            self.assertFalse((Path(d) / ".ssh" / "known_hosts").exists())
            self.assertFalse((Path(d) / ".ssh" / "config").exists())
            out = pr.call_args_list[-1].args[0]
        self.assertEqual(rc, 1)
        self.assertIn("邀请码无效或已失效", out)
        self.assertNotIn(CODE, out)


if __name__ == "__main__":
    unittest.main()
