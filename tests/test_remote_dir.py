import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "scripts" / "safe_push.py"
SPEC = importlib.util.spec_from_file_location("safe_push_remote_dir", SCRIPT)
safe_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_push)


class RemoteDirTests(unittest.TestCase):
    def test_srv_project_paths_are_accepted(self):
        for d in ("/srv/app", "/srv/wenjuan_inner_web/repo", "/opt/app/repo"):
            safe_push._validate_remote_dir(d)

    def test_home_and_dangerous_paths_are_rejected(self):
        for d in ("/", "~", "~/app", "/home/deploy", "/home/deploy/", "/home/deploy/apps/app/repo",
                  "C:/srv/app", "srv/app", "/srv/a\\b"):
            with self.subTest(d=d), self.assertRaises(safe_push.ConfigError):
                safe_push._validate_remote_dir(d)


if __name__ == "__main__":
    unittest.main()
