import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "push-to-server" / "scripts" / "safe_push.py"
SPEC = importlib.util.spec_from_file_location("safe_push_config_store", SCRIPT)
safe_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_push)


def config_data(host="example.test"):
    return {
        "schema_version": 1,
        "server": {
            "host": host,
            "user": "deploy",
            "port": 22,
            "remote_repo_dir": "/srv/app",
            "ssh_key_path": "~/.ssh/deploy",
        },
        "runtime_paths": ["data"],
        "health_checks": ["scripts/health.sh"],
        "custom_metadata": {"keep": True},
    }


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "external"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".agents").mkdir()
        self.env = patch.dict(os.environ, {"PUSH_TO_SERVER_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def write_config(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_save_validates_preserves_fields_and_is_idempotent(self):
        source = self.write_config(self.root / "source.json", config_data())
        destination = safe_push.save_config(self.repo, source)
        saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(saved["custom_metadata"], {"keep": True})
        before = destination.stat().st_mtime_ns
        self.assertEqual(safe_push.save_config(self.repo, source), destination)
        self.assertEqual(destination.stat().st_mtime_ns, before)

        invalid = self.write_config(self.root / "invalid.json", config_data())
        data = config_data()
        data["server"]["remote_repo_dir"] = "/"
        invalid.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(safe_push.ConfigError):
            safe_push.save_config(self.repo, invalid, replace=True)
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_replace_keeps_previous_config_backup(self):
        original = self.write_config(self.root / "original.json", config_data())
        destination = safe_push.save_config(self.repo, original)
        replacement = self.write_config(self.root / "replacement.json", config_data("new.example.test"))
        with self.assertRaises(safe_push.ConfigError):
            safe_push.save_config(self.repo, replacement)
        safe_push.save_config(self.repo, replacement, replace=True)
        self.assertEqual(destination.with_suffix(".json.bak").read_bytes(), original.read_bytes())
        self.assertEqual(json.loads(destination.read_text())["server"]["host"], "new.example.test")

    def test_resolution_precedence_and_corrupt_saved_config(self):
        with self.assertRaises(safe_push.ConfigError):
            safe_push.resolve_config_path(self.repo)
        project = self.write_config(self.repo / ".agents" / "push-to-server.json", config_data("project.test"))
        self.assertEqual(safe_push.resolve_config_path(self.repo), project)
        source = self.write_config(self.root / "saved.json", config_data("saved.test"))
        saved = safe_push.save_config(self.repo, source)
        self.assertEqual(safe_push.resolve_config_path(self.repo), saved)
        explicit = self.write_config(self.root / "explicit.json", config_data("explicit.test"))
        self.assertEqual(safe_push.resolve_config_path(self.repo, explicit), explicit)
        relative = Path("relative-config.json")
        self.assertEqual(safe_push.resolve_config_path(self.repo, relative), Path.cwd() / relative)
        saved.write_text("{broken", encoding="utf-8")
        self.assertEqual(safe_push.resolve_config_path(self.repo), saved)
        with self.assertRaises(json.JSONDecodeError):
            safe_push.load_config(saved, self.repo)
        saved.write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            safe_push.load_config(saved, self.repo)

    def test_storage_survives_skill_copy_and_separates_repositories(self):
        source = self.write_config(self.root / "source.json", config_data())
        saved = safe_push.save_config(self.repo, source)
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        self.assertNotEqual(safe_push.saved_config_path(self.repo), safe_push.saved_config_path(other_repo))

        copied_skill = self.root / "reinstalled-skill" / "scripts"
        copied_skill.mkdir(parents=True)
        copied_script = copied_skill / "safe_push.py"
        shutil.copy2(SCRIPT, copied_script)
        copied_spec = importlib.util.spec_from_file_location("safe_push_reinstalled", copied_script)
        copied_module = importlib.util.module_from_spec(copied_spec)
        copied_spec.loader.exec_module(copied_module)
        self.assertEqual(copied_module.resolve_config_path(self.repo), saved)

    def test_rejects_storage_inside_repo_or_skill_including_symlink(self):
        with patch.dict(os.environ, {"PUSH_TO_SERVER_HOME": str(self.repo / "store")}):
            with self.assertRaises(safe_push.ConfigError):
                safe_push.saved_config_path(self.repo)
        with patch.dict(os.environ, {"PUSH_TO_SERVER_HOME": str(SCRIPT.parents[1] / "store")}):
            with self.assertRaises(safe_push.ConfigError):
                safe_push.saved_config_path(self.repo)
        linked = self.root / "linked-home"
        linked.symlink_to(self.repo, target_is_directory=True)
        with patch.dict(os.environ, {"PUSH_TO_SERVER_HOME": str(linked)}):
            with self.assertRaises(safe_push.ConfigError):
                safe_push.saved_config_path(self.repo)
        external = self.root / "safe-home"
        external.mkdir()
        (external / "configs").symlink_to(self.repo, target_is_directory=True)
        with patch.dict(os.environ, {"PUSH_TO_SERVER_HOME": str(external)}):
            with self.assertRaises(safe_push.ConfigError):
                safe_push.saved_config_path(self.repo)


if __name__ == "__main__":
    unittest.main()
