import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'WeChat discovery is Windows-only')
class DataDiscoveryTests(unittest.TestCase):
    def test_finds_data_root_directly_under_user_home(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'capture_db_key.py'
        candidates = runpy.run_path(str(script))['bootstrap_candidate_roots']
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / 'xwechat_files'
            root.mkdir()
            with patch('os.path.expanduser', return_value=home):
                self.assertIn(str(root), candidates())

    def test_stale_configured_account_does_not_hide_other_local_accounts(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'capture_db_key.py'
        resolve = runpy.run_path(str(script))['resolve_session_dbs']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'xwechat_files' / 'wxid_previous'
            current = root / 'xwechat_files' / 'wxid_current'
            for account in (old, current):
                db = account / 'db_storage' / 'session' / 'session.db'
                db.parent.mkdir(parents=True)
                db.touch()
            config = root / 'override' / 'config.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'db_path': str(old)}), encoding='utf-8')
            with patch.dict(os.environ, APPDATA=str(root), WEFLOW_HOME=str(config.parent)), \
                 patch.dict(resolve.__globals__, bootstrap_candidate_roots=lambda: [str(old.parent)]):
                matches = resolve(None)
                self.assertEqual([Path(account) for _, account in matches], [old, current])
                self.assertEqual([Path(account) for _, account in resolve(str(current))], [current])
