import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "scripts" / "safe_push.py"
SPEC = importlib.util.spec_from_file_location("safe_push_ssh_run_script", SCRIPT)
safe_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_push)


class FakeConfig:
    """Only the attributes _ssh_base_args / _build_destination read."""

    def __init__(self):
        self.host = "example.test"
        self.user = "deploy"
        self.port = 22
        self.ssh_key_path = None


class SshRunScriptTests(unittest.TestCase):
    def test_script_sent_as_lf_utf8_bytes(self):
        """Remote sh must receive LF line endings even on Windows (text mode would send CRLF)."""
        script = "set -e\necho 中文\n"
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n".encode(), stderr=b"")
        config = FakeConfig()
        with patch.object(safe_push, "_ssh_base_args", return_value=["ssh"]), \
             patch.object(safe_push, "_build_destination", return_value="deploy@example.test"), \
             patch.object(safe_push.subprocess, "run", return_value=completed) as run:
            out, err, rc = safe_push.ssh_run_script(config, script)

        kwargs = run.call_args.kwargs
        self.assertIsInstance(kwargs["input"], bytes)
        self.assertEqual(kwargs["input"], script.encode("utf-8"))
        self.assertNotIn(b"\r", kwargs["input"])
        self.assertFalse(kwargs.get("text", False))
        self.assertEqual((out, err, rc), ("ok\n", "", 0))

    def test_output_decoding_tolerates_invalid_utf8(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"\xff\xfe", stderr=b"err \xff")
        with patch.object(safe_push, "_ssh_base_args", return_value=["ssh"]), \
             patch.object(safe_push, "_build_destination", return_value="deploy@example.test"), \
             patch.object(safe_push.subprocess, "run", return_value=completed):
            out, err, rc = safe_push.ssh_run_script(FakeConfig(), "true\n")
        self.assertEqual(rc, 1)
        self.assertIsInstance(out, str)
        self.assertTrue(err.startswith("err "))


class Utf8DecodingTests(unittest.TestCase):
    def test_text_mode_calls_decode_utf8(self):
        """Remote/git output is UTF-8; locale decoding (e.g. GBK on Chinese Windows) crashes the reader thread."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(safe_push, "_ssh_base_args", return_value=["ssh"]), \
             patch.object(safe_push, "_build_destination", return_value="deploy@example.test"), \
             patch.object(safe_push.subprocess, "run", return_value=completed) as run:
            safe_push.ssh_run_command(FakeConfig(), "true")
            safe_push._git(["status"], check=False)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs.get("encoding"), "utf-8")
            self.assertEqual(call.kwargs.get("errors"), "replace")


class PreflightRemoteDirTests(unittest.TestCase):
    def test_preflight_script_runs_inside_remote_repo_dir(self):
        """SSH starts in the login home, never a valid remote_repo_dir: the script must cd there first."""
        config = FakeConfig()
        config.remote_repo_dir = "/srv/my app"
        seen = {}

        def fake_run_script(cfg, script, timeout=120):
            seen["script"] = script
            return "NOT_A_GIT_REPO\n", "", 1

        with patch.object(safe_push, "ssh_run_script", side_effect=fake_run_script):
            safe_push.preflight_remote(config)
        first = seen["script"].splitlines()[0]
        self.assertTrue(first.startswith("cd '/srv/my app' "), first)
        self.assertIn(safe_push.REMOTE_PREFLIGHT_SCRIPT, seen["script"])


if __name__ == "__main__":
    unittest.main()
