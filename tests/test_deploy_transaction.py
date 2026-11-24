import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "scripts" / "safe_push.py"
SPEC = importlib.util.spec_from_file_location("safe_push_deploy_transaction", SCRIPT)
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
        self.remote_repo_dir = "/srv/app"
        self.backup_retention = 3
        self.post_sync_script = None
        self.health_checks = []


class DeployTransactionTests(unittest.TestCase):
    def run_deploy(self, remote_before, script_result, remote_after):
        seen = {}

        def fake_upload(cfg, local, remote, timeout=120):
            seen["uploaded"] = remote

        def fake_run_script(cfg, script, timeout=120):
            seen["script"] = script
            return script_result

        heads = iter([remote_before, remote_after, remote_after])
        with patch.object(safe_push, "git_head", return_value=LOCAL), \
             patch.object(safe_push, "git_short_sha", return_value=LOCAL[:7]), \
             patch.object(safe_push, "git_create_bundle"), \
             patch.object(safe_push, "scp_upload", side_effect=fake_upload), \
             patch.object(safe_push, "ssh_run_script", side_effect=fake_run_script), \
             patch.object(safe_push, "ssh_run_command", return_value=("", "", 0)), \
             patch.object(safe_push, "_read_remote_head", side_effect=lambda cfg: next(heads)):
            status = safe_push.do_deploy(FakeConfig())
        return status, seen

    def test_fetch_uses_the_uploaded_bundle_path(self):
        """The remote fetch must read exactly the file scp uploaded (it once dropped the .bundle suffix)."""
        _, seen = self.run_deploy(OTHER, ("BACKUP_CREATED:x\nDEPLOY_DONE\n", "", 0), LOCAL)
        uploaded = seen["uploaded"]
        self.assertTrue(uploaded.startswith("/tmp/push-to-server-") and uploaded.endswith(".bundle"), uploaded)
        fetch = next(line for line in seen["script"].splitlines() if " fetch " in line)
        self.assertIn(" fetch " + uploaded + " ", fetch)

    def test_failure_on_unchanged_head_is_deploy_failed_with_detail(self):
        """Remote already on the target commit + early failure: nothing changed, so not 'code_updated_unverified'."""
        status, _ = self.run_deploy(LOCAL, ("", "fatal: could not open bundle\n", 128), LOCAL)
        self.assertEqual(status["status"], safe_push.STATUS_DEPLOY_FAILED)
        self.assertIn("could not open bundle", " ".join(status["errors"]))

    def test_failure_after_head_switched_is_unverified_with_detail(self):
        status, _ = self.run_deploy(OTHER, ("BACKUP_CREATED:x\n", "error: housekeeping\n", 1), LOCAL)
        self.assertEqual(status["status"], safe_push.STATUS_CODE_UPDATED_UNVERIFIED)
        self.assertIn("housekeeping", " ".join(status["errors"]))


if __name__ == "__main__":
    unittest.main()
