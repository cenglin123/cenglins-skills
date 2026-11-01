"""Exercise the real pre-commit hook in an isolated Git repository."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class CatalogHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("scripts/update-readme.py", ".githooks/pre-commit"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, target)
        (self.root / ".githooks/pre-commit").chmod(0o755)
        (self.root / "README.md").write_text(
            "Before\n<!-- skills:start -->\n<!-- skills:end -->\nAfter\n", encoding="utf-8"
        )
        self.skill("alpha", "用途 | example")
        self.git("init")
        self.git("config", "core.hooksPath", ".githooks")
        self.git("config", "user.name", "Catalog Test")
        self.git("config", "user.email", "catalog@example.invalid")
        self.generate()
        self.git("add", ".")
        self.commit()

    def run_command(self, *args, success=True):
        env = dict(os.environ, PYTHONUTF8="1")
        # Keep test repositories isolated from callers using an alternate index.
        for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
            env.pop(key, None)
        result = subprocess.run(args, cwd=self.root, env=env, capture_output=True)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode == 0, success, output)
        return output

    def git(self, *args, success=True):
        return self.run_command("git", *args, success=success)

    def generate(self, *args, success=True):
        import sys

        return self.run_command(sys.executable, "-X", "utf8", "scripts/update-readme.py", *args, success=success)

    def commit(self, success=True):
        return self.git("commit", "-m", "Catalog fixture", success=success)

    def skill(self, name, title):
        path = self.root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: Example\n---\n\n# {title}\n", encoding="utf-8")

    def test_addition_requires_staged_readme(self):
        self.skill("beta", "新增技能")
        self.git("add", "skills")
        self.generate()
        self.assertIn("catalog is stale", self.commit(success=False))
        self.git("add", "README.md")
        self.commit()
        text = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("用途 &#124; example", text)
        self.assertTrue(text.startswith("Before\n"))
        self.assertTrue(text.endswith("After\n"))

    def test_rename_and_heading_change(self):
        self.git("mv", "skills/alpha", "skills/gamma")
        self.skill("gamma", "新用途")
        self.git("add", "skills")
        self.commit(success=False)
        self.generate()
        self.git("add", "README.md")
        self.commit()

    def test_worktree_does_not_override_index(self):
        self.skill("alpha", "尚未暂存")
        self.generate("--check", success=False)
        self.generate("--check", "--staged")

    def test_deletion_removes_catalog_entry(self):
        self.skill("beta", "Example")
        self.generate()
        self.git("add", ".")
        self.commit()
        self.git("rm", "skills/beta/SKILL.md")
        self.commit(success=False)
        self.generate()
        self.git("add", "README.md")
        self.commit()

    def test_missing_heading_and_markers_fail(self):
        path = self.root / "skills/alpha/SKILL.md"
        path.write_text("No heading\n", encoding="utf-8")
        self.generate(success=False)
        self.skill("alpha", "Example")
        (self.root / "README.md").write_text("No markers\n", encoding="utf-8")
        self.generate(success=False)


if __name__ == "__main__":
    unittest.main()
