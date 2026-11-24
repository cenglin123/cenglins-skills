"""Check the wechat-grep installation or restore assets from the pinned release."""

import argparse
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.request import urlopen
import zipfile


# Keep in sync with install-dist.ps1 when publishing a new distribution.
DIST_SHA256 = '02702a46f1140d43c6cce2150a95c9b224ea79bafe3324b91d2ee0d99f345e8f'
DIST_NAME = 'wechat-grep-dist.zip'
DIST_URL = 'https://github.com/cenglin123/cenglins-skills/releases/download/wechat-grep-v1.0.0/' + DIST_NAME


class DownloadError(Exception):
    pass


def report(code, status, action=None):
    result = {'status': status}
    if action:
        result['action'] = action
    print(json.dumps(result))
    return code


def distribution_ready(skill):
    model = skill / 'models' / 'zipformer-small-ctc-int8'
    return (
        (skill / 'bin' / 'weflow.exe').is_file()
        and (model / 'ctc.int8.onnx').is_file()
        and (model / 'data' / 'tokens.txt').is_file()
        and all(any((skill / 'wheels').glob(prefix + '*.whl'))
                for prefix in ('numpy-', 'sherpa_onnx-', 'sherpa_onnx_core-'))
    )


def install_dist(skill, archive, quiet=False):
    try:
        with archive.open('rb') as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            if digest.hexdigest() != DIST_SHA256:
                return report(6, 'archive_rejected',
                              'Replace the rejected ZIP with the pinned release ZIP, or move it aside '
                              'before retrying online. Stop if neither is available.')
            stream.seek(0)
            # Extract only after the complete archive matches the pinned release.
            with zipfile.ZipFile(stream) as bundle:
                bundle.extractall(skill)
    except (OSError, zipfile.BadZipFile):
        return report(6, 'install_failed', 'Check the archive path and write access to the skill directory.')
    if not distribution_ready(skill):
        return report(6, 'install_failed', 'The release did not restore all required assets.')
    if quiet:
        return 0
    return report(0, 'distribution_installed', 'Run this script again without --install-from to check the profile.')


def check_install(skill):
    exe = skill / 'bin' / 'weflow.exe'
    if not exe.is_file():
        return report(2, 'distribution_missing', 'Run this script with --prepare (or use install-dist.ps1).')
    try:
        completed = subprocess.run(
            [str(exe), 'db', 'test', '--json'], capture_output=True,
            encoding='utf-8', timeout=30,
        )
        result = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return report(4, 'cli_probe_failed', 'Check whether weflow.exe starts and responds to db test.')
    if not isinstance(result, dict):
        return report(4, 'cli_probe_failed', 'Expected a JSON object from weflow db test.')
    if completed.returncode == 0 and result.get('success') is True:
        data = result.get('data')
        if isinstance(data, dict) and data.get('connected') is True:
            return report(0, 'database_opened')
    error = result.get('error')
    if isinstance(error, dict) and error.get('code') == 'config_error':
        return report(3, 'profile_needs_setup',
                      'For a new profile, keep desktop WeChat logged in and obtain explicit permission '
                      'for local credential setup before running scripts/capture_db_key.py. '
                      'For an existing profile, check its configuration without replacing it automatically. '
                      'Run --prepare again after setup.')
    return report(5, 'database_probe_failed', 'Check the existing profile and database; this does not establish a version incompatibility.')


def prepare(skill, archive):
    if archive is not None or not distribution_ready(skill):
        if archive is None and (skill / DIST_NAME).is_file():
            archive = skill / DIST_NAME
        if archive is not None:
            code = install_dist(skill, archive, quiet=True)
        else:
            try:
                with tempfile.TemporaryDirectory(prefix='.wechat-grep-', dir=skill) as temp:
                    download = Path(temp) / DIST_NAME
                    download_dist(download)
                    code = install_dist(skill, download, quiet=True)
            except DownloadError:
                return report(6, 'download_failed',
                              'If the pinned ZIP is already available, rerun --prepare --install-from <ZIP>. '
                              'Otherwise stop until the release is reachable or its ZIP can be supplied.')
            except OSError:
                return report(6, 'install_failed',
                              'Use the installed skill directory and check write access and free space.')
        if code:
            return code
    return check_install(skill)


def download_dist(target):
    deadline = time.monotonic() + 180
    size = 0
    try:
        response = urlopen(DIST_URL, timeout=20)
    except (OSError, HTTPException) as exc:
        raise DownloadError from exc
    with response, target.open('wb') as output:
        while time.monotonic() < deadline:
            try:
                chunk = response.read1(1024 * 1024)
            except (OSError, HTTPException) as exc:
                raise DownloadError from exc
            if not chunk:
                return
            size += len(chunk)
            if size > 160 * 1024 * 1024:
                break
            output.write(chunk)
    raise DownloadError('Release download exceeded its limits')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--prepare', action='store_true', help='restore missing assets and then check the database')
    parser.add_argument('--install-from', type=Path, metavar='ZIP', help='restore assets from the pinned local ZIP')
    args = parser.parse_args()
    skill = args.skill_dir.resolve()
    if args.prepare:
        return prepare(skill, args.install_from)
    if args.install_from:
        return install_dist(skill, args.install_from)
    return check_install(skill)


if __name__ == '__main__':
    raise SystemExit(main())
