import importlib.util
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "scripts" / "safe_push.py"
SPEC = importlib.util.spec_from_file_location("safe_push_bootstrap", SCRIPT)
safe_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_push)

LOCAL = "a" * 40
OTHER = "b" * 40


class FakeConfig:
    def __init__(self):
        self.repo_root = "/repo"
        self.host = "example.test"
        self.user = "deploy"
        self.port = 22
        self.ssh_key_path = None
        self.remote_repo_dir = "/srv/app/repo"
        self.post_sync_script = None
        self.health_checks = []
        self.expected_branch = None


class PreflightScriptTests(unittest.TestCase):
    def test_script_enters_quoted_dir_and_checks_everything(self):
        script = safe_push.build_bootstrap_preflight_script("/srv/my app/repo")
        self.assertTrue(script.startswith("cd '/srv/my app/repo' "), script.splitlines()[0])
        for marker in ("REMOTE_DIR_MISSING", "GIT_MISSING", "NOT_WRITABLE", "ALREADY_GIT", "NOT_EMPTY", "BOOTSTRAP_OK"):
            self.assertIn(marker, script)
        self.assertIn("ls -A", script)   # hidden entries count as non-empty

    def test_script_has_no_unquoted_dynamic_values(self):
        script = safe_push.build_bootstrap_preflight_script("/srv/a;rm -rf x/repo")
        self.assertTrue(script.startswith("cd '/srv/a;rm -rf x/repo' "))
        self.assertEqual(script.count("rm -rf"), 1)


class ParsePreflightTests(unittest.TestCase):
    def parse(self, out, rc=1, err=""):
        return safe_push.parse_bootstrap_preflight(out, err, rc, "/srv/app/repo")

    def test_ok(self):
        self.assertEqual(self.parse("BOOTSTRAP_OK\n", 0), [])

    def test_missing_dir(self):
        self.assertIn("does not exist", self.parse("REMOTE_DIR_MISSING\n")[0])

    def test_not_writable(self):
        self.assertIn("not writable", self.parse("NOT_WRITABLE\n")[0])

    def test_git_repo_points_to_deploy(self):
        msg = self.parse("ALREADY_GIT\n")[0]
        self.assertIn("already a git repository", msg)
        self.assertIn("deploy", msg)

    def test_not_empty_lists_entries(self):
        msg = self.parse("NOT_EMPTY\n.env\nsrc\n")[0]
        self.assertIn("not empty", msg)
        self.assertIn(".env", msg)

    def test_git_missing(self):
        self.assertIn("git is not installed", self.parse("GIT_MISSING\n")[0])

    def test_unknown_failure_keeps_stderr(self):
        msg = self.parse("", 255, "Permission denied (publickey).")[0]
        self.assertIn("exit=255", msg)
        self.assertIn("Permission denied", msg)

    def test_rc_zero_without_marker_is_failure(self):
        self.assertTrue(self.parse("", 0))


class StepsScriptTests(unittest.TestCase):
    def test_clone_then_remove_origin(self):
        script = safe_push.build_bootstrap_script("/srv/app/repo", "/tmp/push-to-server-x.bundle", "main")
        lines = script.splitlines()
        self.assertEqual(lines[0], "set -e")
        clone = next(l for l in lines if "git clone" in l)
        self.assertIn("--branch main", clone)
        self.assertIn("/tmp/push-to-server-x.bundle", clone)
        self.assertLess(lines.index(clone), next(i for i, l in enumerate(lines) if "remote remove origin" in l))
        self.assertIn("BOOTSTRAP_CLONED", script)

    def test_values_are_shell_quoted(self):
        script = safe_push.build_bootstrap_script("/srv/a b/repo", "/tmp/x.bundle", "feat/x;y")
        self.assertIn(shlex.quote("/srv/a b/repo"), script)
        self.assertIn("--branch " + shlex.quote("feat/x;y"), script)


class LocalIssuesTests(unittest.TestCase):
    def test_detached_head(self):
        self.assertTrue(safe_push.bootstrap_local_issues(FakeConfig(), "HEAD"))

    def test_expected_branch_mismatch(self):
        cfg = FakeConfig()
        cfg.expected_branch = "main"
        self.assertTrue(safe_push.bootstrap_local_issues(cfg, "dev"))
        self.assertEqual(safe_push.bootstrap_local_issues(cfg, "main"), [])

    def test_plain_branch_ok(self):
        self.assertEqual(safe_push.bootstrap_local_issues(FakeConfig(), "main"), [])


class DoBootstrapTests(unittest.TestCase):
    def run_bootstrap(self, script_result=("BOOTSTRAP_CLONED\n", "", 0), remote_head=LOCAL, config=None,
                      post_sync=(True, None), health=(True, [])):
        seen = {"commands": []}

        def fake_upload(cfg, local, remote, timeout=120):
            seen["uploaded"] = remote

        def fake_run_script(cfg, script, timeout=120):
            seen["script"] = script
            return script_result

        def fake_run_command(cfg, command, timeout=120):
            seen["commands"].append(command)
            return "", "", 0

        def fake_bundle(repo, path, ref="HEAD"):
            seen["bundle_ref"] = ref

        with patch.object(safe_push, "git_head", return_value=LOCAL), \
             patch.object(safe_push, "git_create_bundle", side_effect=fake_bundle), \
             patch.object(safe_push, "scp_upload", side_effect=fake_upload), \
             patch.object(safe_push, "ssh_run_script", side_effect=fake_run_script), \
             patch.object(safe_push, "ssh_run_command", side_effect=fake_run_command), \
             patch.object(safe_push, "_read_remote_head", return_value=remote_head), \
             patch.object(safe_push, "_run_post_sync", return_value=post_sync), \
             patch.object(safe_push, "_run_health_checks", return_value=health):
            status = safe_push.do_bootstrap(config or FakeConfig(), "main")
        return status, seen

    def test_verified(self):
        status, seen = self.run_bootstrap()
        self.assertEqual(status["status"], safe_push.STATUS_VERIFIED)
        self.assertEqual(status["remote_head_after"], LOCAL)
        self.assertIn("deploy", status["next_step"])
        self.assertEqual(seen["bundle_ref"], "main")

    def test_clone_reads_the_uploaded_bundle(self):
        _, seen = self.run_bootstrap()
        uploaded = seen["uploaded"]
        self.assertTrue(uploaded.startswith("/tmp/push-to-server-") and uploaded.endswith(".bundle"))
        clone = next(l for l in seen["script"].splitlines() if "git clone" in l)
        self.assertIn(uploaded, clone)

    def test_remote_bundle_removed_afterwards(self):
        _, seen = self.run_bootstrap()
        self.assertTrue(any(c.startswith("rm -f ") and seen["uploaded"] in c for c in seen["commands"]))

    def test_clone_failure_is_deploy_failed_with_detail(self):
        status, _ = self.run_bootstrap(("", "fatal: destination path exists\n", 128))
        self.assertEqual(status["status"], safe_push.STATUS_DEPLOY_FAILED)
        self.assertIn("destination path exists", " ".join(status["errors"]))

    def test_head_mismatch_is_deploy_failed(self):
        status, _ = self.run_bootstrap(remote_head=OTHER)
        self.assertEqual(status["status"], safe_push.STATUS_DEPLOY_FAILED)
        self.assertIn("does not match", " ".join(status["errors"]))

    def test_post_sync_failure_is_unverified(self):
        cfg = FakeConfig()
        cfg.post_sync_script = "deploy/post.sh"
        status, _ = self.run_bootstrap(config=cfg, post_sync=(False, "post-sync boom"))
        self.assertEqual(status["status"], safe_push.STATUS_CODE_UPDATED_UNVERIFIED)
        self.assertIn("post-sync boom", " ".join(status["errors"]))

    def test_health_failure_is_unverified(self):
        cfg = FakeConfig()
        cfg.health_checks = ["deploy/health.sh"]
        status, _ = self.run_bootstrap(config=cfg, health=(False, ["Health check [0] failed"]))
        self.assertEqual(status["status"], safe_push.STATUS_CODE_UPDATED_UNVERIFIED)


class CliTests(unittest.TestCase):
    def test_parser_accepts_bootstrap(self):
        import sys
        argv = ["safe_push.py", "bootstrap", "--repo", "/r", "--execute", "--json"]
        with patch.object(sys, "argv", argv), patch.object(safe_push, "cmd_bootstrap", return_value=0) as cmd:
            with self.assertRaises(SystemExit) as cm:
                safe_push.main()
        self.assertEqual(cm.exception.code, 0)
        parsed = cmd.call_args.args[0]
        self.assertTrue(parsed.execute and parsed.json)

    def test_git_create_bundle_takes_branch(self):
        with patch.object(safe_push, "_git") as g:
            safe_push.git_create_bundle("/r", "/tmp/x.bundle", "main")
        self.assertEqual(g.call_args.args[0], ["bundle", "create", "/tmp/x.bundle", "main"])


if __name__ == "__main__":
    unittest.main()
