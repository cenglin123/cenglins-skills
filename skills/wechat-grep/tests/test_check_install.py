import json
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]


class CheckInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.skill = Path(self.tmp.name) / 'skill with spaces'
        (self.skill / 'scripts').mkdir(parents=True)
        self.script = SKILL / 'scripts' / 'check-install.py'
        if self.script.exists():
            self.script = Path(shutil.copy(self.script, self.skill / 'scripts'))
        self.env = dict(os.environ, WEFLOW_HOME=str(Path(self.tmp.name) / 'weflow'))

    def check(self, *args):
        result = subprocess.run(
            [sys.executable, str(self.script), *args], env=self.env,
            capture_output=True, encoding='utf-8', timeout=60,
        )
        self.assertFalse(result.stderr, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_missing_cli_points_to_installer(self):
        code, report = self.check()
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'distribution_missing')
        self.assertIn('install-dist.ps1', report['action'])

    def test_installed_cli_with_no_profile_reports_config_error(self):
        source = os.environ.get('WECHAT_GREP_TEST_EXE')
        if not source:
            self.skipTest('WECHAT_GREP_TEST_EXE must point to the verified release binary')
        (self.skill / 'bin').mkdir()
        shutil.copy(source, self.skill / 'bin' / 'weflow.exe')
        code, report = self.check()
        self.assertEqual(code, 3)
        self.assertEqual(report['status'], 'profile_needs_setup')
        self.assertFalse((Path(self.env['WEFLOW_HOME']) / 'config.json').exists())

    def test_unverified_archive_is_rejected_before_installing(self):
        bundle = self.skill / 'unverified.zip'
        bundle.write_bytes(b'not the pinned release')
        code, report = self.check('--install-from', str(bundle))
        self.assertEqual(code, 6)
        self.assertEqual(report['status'], 'archive_rejected')
        self.assertFalse((self.skill / 'bin').exists())

    def test_pinned_archive_restores_cli_and_support_assets(self):
        bundle = os.environ.get('WECHAT_GREP_TEST_ZIP')
        if not bundle:
            self.skipTest('WECHAT_GREP_TEST_ZIP must point to the release archive')
        code, report = self.check('--install-from', bundle)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'distribution_installed')
        self.assertTrue((self.skill / 'bin' / 'weflow.exe').is_file())
        self.assertTrue((self.skill / 'models' / 'zipformer-small-ctc-int8' / 'ctc.int8.onnx').is_file())
        self.assertTrue(any((self.skill / 'wheels').glob('*.whl')))

    def test_prepare_from_local_archive_installs_and_checks_profile(self):
        bundle = os.environ.get('WECHAT_GREP_TEST_ZIP')
        if not bundle:
            self.skipTest('WECHAT_GREP_TEST_ZIP must point to the release archive')
        shutil.copy(bundle, self.skill / 'wechat-grep-dist.zip')
        code, report = self.check('--prepare')
        self.assertEqual(code, 3)
        self.assertEqual(report['status'], 'profile_needs_setup')
        self.assertIn('capture_db_key.py', report['action'])
        self.assertTrue((self.skill / 'bin' / 'weflow.exe').is_file())
        self.assertFalse((Path(self.env['WEFLOW_HOME']) / 'config.json').exists())
        (self.skill / 'wechat-grep-dist.zip').unlink()
        self.assertEqual(self.check('--prepare'), (code, report))

    def test_prepare_distinguishes_local_directory_failure_from_offline(self):
        code, report = self.check('--prepare', '--skill-dir', str(self.skill / 'nonexistent'))
        self.assertEqual(code, 6)
        self.assertEqual(report['status'], 'install_failed')

    def test_prepare_offline_without_zip_has_a_terminal_status(self):
        prepare = runpy.run_path(str(self.script))['prepare']
        out = StringIO()
        with patch.dict(prepare.__globals__, urlopen=lambda *a, **kw: self.offline()), redirect_stdout(out):
            self.assertEqual(prepare(self.skill, None), 6)
        report = json.loads(out.getvalue())
        self.assertEqual(report['status'], 'download_failed')
        self.assertIn('stop', report['action'].lower())
        self.assertFalse(list(self.skill.glob('.wechat-grep-*')))

    @staticmethod
    def offline():
        raise OSError('simulated network outage')


if __name__ == '__main__':
    unittest.main()
