#!/usr/bin/env python3
"""safe_push.py — Safe git-bundle deployment with preflight checks.

Usage:
  python safe_push.py preflight --repo /path/to/repo [--config /path/to/config.json] [--remote] [--json]
  python safe_push.py deploy --repo /path/to/repo [--config /path/to/config.json] [--execute]
  python safe_push.py config-save --repo /path/to/repo --config /path/to/config.json [--replace]
"""

import argparse
import hashlib
import json
import os
import random
import shlex
import string
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
BACKUP_REFS_PREFIX = "refs/deploy/backups/"
INCOMING_REFS_PREFIX = "refs/deploy/incoming/"
MAX_BACKUP_RETENTION = 5
MIN_BACKUP_RETENTION = 1
FORBIDDEN_FIELD_SUBSTRINGS = {"password", "token", "secret", "credential", "private_key"}
DANGEROUS_REMOTE_DIRS = {"/", "~"}
DANGEROUS_REMOTE_DIR_PREFIXES = ("~/", "/home/")

STATUS_PREFLIGHT_FAILED = "preflight_failed"
STATUS_DEPLOY_FAILED = "deploy_failed"
STATUS_CODE_UPDATED_UNVERIFIED = "code_updated_unverified"
STATUS_VERIFIED = "verified"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SafePushError(Exception):
    """Base error — exit code 1."""
    pass


class ConfigError(SafePushError):
    """Configuration validation failure."""
    pass


class PreflightError(SafePushError):
    """Preflight check failed."""
    pass


class DeployError(SafePushError):
    """Deployment step failure."""
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _check_forbidden_fields(obj, path=""):
    """Recursively check for forbidden field names."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            curr = f"{path}.{key}" if path else key
            kl = key.lower()
            if any(f in kl for f in FORBIDDEN_FIELD_SUBSTRINGS):
                raise ConfigError(f"Forbidden field detected: {curr!r}")
            _check_forbidden_fields(val, curr)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _check_forbidden_fields(item, f"{path}[{i}]")


def _validate_remote_dir(d):
    """Reject dangerous or invalid remote directory values."""
    if not isinstance(d, str) or not d:
        raise ConfigError("server.remote_repo_dir must be a non-empty string")
    if d in DANGEROUS_REMOTE_DIRS:
        raise ConfigError(f"Dangerous remote directory: {d!r}")
    if d.startswith(DANGEROUS_REMOTE_DIR_PREFIXES):
        raise ConfigError(f"Remote directory starts with dangerous prefix: {d!r}")
    # Windows-style paths detected first for clearer error
    if len(d) >= 2 and d[1] == ":" and d[0].isalpha():
        raise ConfigError(f"Windows-style path rejected: {d!r}")
    if "\\" in d:
        raise ConfigError(f"Backslash in path (possible Windows path): {d!r}")
    if not d.startswith("/"):
        raise ConfigError(f"Remote directory must be absolute POSIX path: {d!r}")
    if "\0" in d or "\n" in d:
        raise ConfigError("Remote directory contains NUL or newline")


def _validate_runtime_paths(paths):
    """Validate runtime_paths entries."""
    if not isinstance(paths, list):
        raise ConfigError("runtime_paths must be a list")
    for i, rp in enumerate(paths):
        if not isinstance(rp, str) or not rp:
            raise ConfigError(f"runtime_paths[{i}] must be a non-empty string")
        # Reject absolute paths: os.path.isabs covers C:\ on Windows;
        # also explicitly reject /-prefixed paths for cross-platform safety
        if os.path.isabs(rp) or rp.startswith("/"):
            raise ConfigError(f"runtime_paths[{i}] must be relative: {rp!r}")
        p = Path(rp)
        if ".." in p.parts:
            raise ConfigError(f"runtime_paths[{i}] must not contain '..': {rp!r}")


class Config:
    """Validated deployment configuration."""

    def __init__(self, data, repo_root=None):
        self._validate_schema(data)
        self._data = data
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self._load(data)

    @staticmethod
    def _validate_schema(data):
        if not isinstance(data, dict):
            raise ConfigError("Config must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ConfigError(
                f"Expected schema_version={SCHEMA_VERSION}, "
                f"got {data.get('schema_version')}"
            )
        _check_forbidden_fields(data)

        server = data.get("server")
        if not isinstance(server, dict):
            raise ConfigError("Missing 'server' block")
        for key in ("host", "user", "port", "remote_repo_dir"):
            if key not in server:
                raise ConfigError(f"Missing required field: server.{key}")

        port = server["port"]
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ConfigError(f"server.port must be int 1-65535, got {port!r}")

        _validate_remote_dir(server["remote_repo_dir"])
        runtime_paths = data.get("runtime_paths", [])
        _validate_runtime_paths(runtime_paths)

        retention = data.get("backup_retention", 5)
        if not isinstance(retention, int) or retention < MIN_BACKUP_RETENTION or retention > MAX_BACKUP_RETENTION:
            raise ConfigError(
                f"backup_retention must be int "
                f"{MIN_BACKUP_RETENTION}-{MAX_BACKUP_RETENTION}"
            )

        if "expected_branch" in data:
            eb = data["expected_branch"]
            if not isinstance(eb, str) or not eb:
                raise ConfigError("expected_branch must be a non-empty string")

        if "post_sync_script" in data:
            pss = data["post_sync_script"]
            if not isinstance(pss, str) or not pss:
                raise ConfigError("post_sync_script must be a non-empty string")

        health_checks = data.get("health_checks", [])
        if not isinstance(health_checks, list):
            raise ConfigError("health_checks must be a list")
        for hc in health_checks:
            if not isinstance(hc, str) or not hc:
                raise ConfigError("Each health_check must be a non-empty string")

    def _load(self, data):
        s = data["server"]
        self.host = s["host"]
        self.user = s["user"]
        self.port = int(s["port"])
        self.remote_repo_dir = s["remote_repo_dir"]
        self.ssh_key_path = s.get("ssh_key_path")
        self.runtime_paths = data.get("runtime_paths", [])
        self.post_sync_script = data.get("post_sync_script")
        self.health_checks = data.get("health_checks", [])
        self.backup_retention = data.get("backup_retention", 5)
        self.expected_branch = data.get("expected_branch")

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "server": {
                "host": self.host,
                "user": self.user,
                "port": self.port,
                "remote_repo_dir": self.remote_repo_dir,
                "ssh_key_path": self.ssh_key_path,
            },
            "runtime_paths": self.runtime_paths,
            "post_sync_script": self.post_sync_script,
            "health_checks": self.health_checks,
            "backup_retention": self.backup_retention,
            "expected_branch": self.expected_branch,
        }


def load_config(path, repo_root=None):
    """Load and validate config from JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Config(data, repo_root=repo_root)


def _resolved_path(path):
    return Path(path).expanduser().resolve()


def _is_within(path, parent):
    try:
        _resolved_path(path).relative_to(_resolved_path(parent))
        return True
    except ValueError:
        return False


def config_store_home(repo_root):
    """Return the external store root after rejecting unsafe locations."""
    repo = _resolved_path(repo_root)
    skill_dir = Path(__file__).resolve().parents[1]
    home = _resolved_path(os.environ.get("PUSH_TO_SERVER_HOME", "~/.push-to-server"))
    if _is_within(home, repo) or _is_within(home, skill_dir):
        raise ConfigError("PUSH_TO_SERVER_HOME must be outside the repository and skill directory")
    return home


def saved_config_path(repo_root):
    repo = _resolved_path(repo_root)
    identity = os.path.normcase(str(repo)).encode("utf-8")
    path = config_store_home(repo) / "configs" / (hashlib.sha256(identity).hexdigest() + ".json")
    if _is_within(path, repo) or _is_within(path, Path(__file__).resolve().parents[1]):
        raise ConfigError("Resolved config storage path must be outside the repository and skill directory")
    return path


def resolve_config_path(repo_root, explicit_path=None):
    """Resolve explicit, saved, then project config without guessing values."""
    if explicit_path:
        path = Path(explicit_path).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    saved = saved_config_path(repo_root)
    project = _resolved_path(repo_root) / ".agents" / "push-to-server.json"
    if saved.exists():
        return saved
    if project.exists():
        return project
    raise ConfigError(
        "No deployment config found. Use --config <path> or save one with "
        "config-save --repo <path> --config <path>."
    )


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def save_config(repo_root, source_path, replace=False):
    repo = _resolved_path(repo_root)
    source = Path(source_path).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    try:
        raw = source.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read config JSON: {exc}") from exc
    Config(data, repo_root=repo)
    destination = saved_config_path(repo)
    existing = destination.read_bytes() if destination.exists() else None
    same = False
    if existing is not None:
        try:
            same = json.loads(existing.decode("utf-8")) == data
        except (UnicodeError, json.JSONDecodeError):
            same = False
    if existing is not None and not same and not replace:
        raise ConfigError("A different saved config already exists; use --replace to replace it")
    if not same:
        if existing is not None and replace:
            _atomic_write(destination.with_suffix(destination.suffix + ".bak"), existing)
        _atomic_write(destination, raw)
    return destination


# ---------------------------------------------------------------------------
# Git helpers (local)
# ---------------------------------------------------------------------------
def _git(args, cwd=None, check=True):
    cmd = ["git"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=check)
        return r
    except subprocess.CalledProcessError as e:
        raise SafePushError(f"git command failed: {' '.join(args)}\n{e.stderr.strip()}")


def git_head(repo):
    r = _git(["rev-parse", "HEAD"], cwd=repo)
    return r.stdout.strip()


def git_branch(repo):
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return r.stdout.strip()


def git_is_clean(repo):
    r = _git(["status", "--porcelain"], cwd=repo)
    return len(r.stdout.strip()) == 0


def git_tracked(paths, repo):
    """Return subset of paths that are tracked by git."""
    tracked = []
    for p in paths:
        r = _git(["ls-files", "--", p], cwd=repo, check=False)
        if r.stdout.strip():
            tracked.append(p)
    return tracked


def git_unignored(paths, repo):
    """Return subset of paths NOT covered by .gitignore."""
    unignored = []
    for p in paths:
        r = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", p],
            cwd=repo,
        )
        if r.returncode != 0:
            unignored.append(p)
    return unignored


def git_create_bundle(repo, output_path):
    _git(["bundle", "create", output_path, "HEAD"], cwd=repo)


def git_for_each_ref(pattern, repo):
    r = _git(["for-each-ref", "--format=%(refname)", pattern], cwd=repo)
    out = r.stdout.strip()
    if not out:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def git_delete_ref(ref, repo):
    _git(["update-ref", "-d", ref], cwd=repo)


def git_short_sha(repo):
    r = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------
def _ssh_base_args(config):
    args = ["ssh"]
    if config.ssh_key_path:
        args += ["-i", config.ssh_key_path]
    args += ["-p", str(config.port)]
    return args


def _build_destination(config):
    return f"{config.user}@{config.host}"


def ssh_run_script(config, script, timeout=120):
    """Run a fixed POSIX shell script on the remote via stdin.

    The script body contains no dynamic values — no injection surface.
    Returns (stdout, stderr, returncode).
    """
    args = _ssh_base_args(config) + [_build_destination(config), "sh"]
    try:
        r = subprocess.run(
            args,
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        raise SafePushError("SSH command timed out")
    except FileNotFoundError:
        raise SafePushError("ssh not found — is OpenSSH installed?")


def ssh_run_command(config, command, timeout=120):
    """Run a remote command string with dynamic values.

    The `command` must have all dynamic values pre-encoded with shlex.quote().
    Delivered as a single argv element to preserve quoting.
    """
    args = _ssh_base_args(config) + [_build_destination(config), command]
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        raise SafePushError("SSH command timed out")
    except FileNotFoundError:
        raise SafePushError("ssh not found — is OpenSSH installed?")


def scp_upload(config, local_path, remote_path, timeout=120):
    """Upload a file via SCP."""
    args = ["scp"]
    if config.ssh_key_path:
        args += ["-i", config.ssh_key_path]
    args += ["-P", str(config.port)]
    dest = f"{config.user}@{config.host}:{remote_path}"
    args += [local_path, dest]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise SafePushError(f"SCP upload failed: {r.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise SafePushError("SCP upload timed out")
    except FileNotFoundError:
        raise SafePushError("scp not found")


def build_remote_command(config, shell_command):
    """Build a single argv string for use with ssh.

    The shell_command is a POSIX shell command string whose dynamic values
    have already been shlex.quote()'d. This function wraps it as a single
    argument for ssh (preserving quoting across the argv → SSH → shell chain).
    """
    args = _ssh_base_args(config) + [_build_destination(config), shell_command]
    return shlex.join(args)


# ---------------------------------------------------------------------------
# Preflight (local)
# ---------------------------------------------------------------------------
def _check_config_value_safe(val, name):
    """Ensure a config value does not contain NUL or newline."""
    if isinstance(val, str):
        if "\0" in val:
            raise PreflightError(
                f"Config value {name!r} contains NUL byte (length={len(val)})"
            )
        if "\n" in val:
            raise PreflightError(
                f"Config value {name!r} contains newline (length={len(val)})"
            )


def preflight_local(config):
    """Run local preflight checks. Return list of issues (empty = pass)."""
    issues = []
    repo = config.repo_root

    # 1. Repo exists, HEAD resolvable
    try:
        head = git_head(repo)
        branch = git_branch(repo)
    except SafePushError as e:
        issues.append(f"Repository error: {e}")
        return issues  # cannot continue

    # 2. Clean HEAD
    if not git_is_clean(repo):
        issues.append(
            "Local repository is dirty (git status --porcelain not empty). "
            "Commit or stash changes before deploying."
        )

    # 3. Config value safety
    _check_config_value_safe(config.remote_repo_dir, "server.remote_repo_dir")
    for i, rp in enumerate(config.runtime_paths):
        _check_config_value_safe(rp, f"runtime_paths[{i}]")

    # 4. Runtime paths: not tracked, must be gitignored
    tracked = git_tracked(config.runtime_paths, repo)
    for tp in tracked:
        issues.append(
            f"Runtime path is tracked by git: {tp!r}. "
            "Add to .gitignore and remove from tracking."
        )

    unignored = git_unignored(config.runtime_paths, repo)
    for up in unignored:
        issues.append(
            f"Runtime path not covered by .gitignore: {up!r}. "
            "Add to .gitignore first."
        )

    # 5. Post-sync script exists, tracked, inside repo
    if config.post_sync_script:
        script_rel = Path(config.post_sync_script)
        script_abs = repo / script_rel
        if not script_abs.exists():
            issues.append(
                f"Post-sync script not found: {config.post_sync_script!r}"
            )
        elif not script_abs.is_file():
            issues.append(
                f"Post-sync script is not a regular file: {config.post_sync_script!r}"
            )
        else:
            # Check if tracked
            try:
                r = _git(["ls-files", "--", str(script_rel)], cwd=repo, check=False)
                if not r.stdout.strip():
                    issues.append(
                        f"Post-sync script not tracked by git: {config.post_sync_script!r}"
                    )
            except SafePushError:
                issues.append(
                    f"Cannot check git tracking for post-sync script: {config.post_sync_script!r}"
                )

    # 6. Health check scripts: exist, tracked
    for i, hc in enumerate(config.health_checks):
        hc_path = Path(hc)
        hc_abs = repo / hc_path
        if not hc_abs.exists():
            issues.append(f"Health check script not found: health_checks[{i}] = {hc!r}")
        elif not hc_abs.is_file():
            issues.append(f"Health check not a regular file: health_checks[{i}] = {hc!r}")
        else:
            r = _git(["ls-files", "--", str(hc_path)], cwd=repo, check=False)
            if not r.stdout.strip():
                issues.append(
                    f"Health check script not tracked by git: health_checks[{i}] = {hc!r}"
                )

    return issues, head, branch


# ---------------------------------------------------------------------------
# Preflight (remote — read-only)
# ---------------------------------------------------------------------------
REMOTE_PREFLIGHT_SCRIPT = """\
set -e
# 1. Check it's a git repo, get HEAD and branch
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "NOT_A_GIT_REPO"
    exit 1
fi
echo "HEAD: $(git rev-parse HEAD)"
echo "BRANCH: $(git rev-parse --abbrev-ref HEAD)"
# 2. Check tracked clean
status=$(git status --porcelain 2>/dev/null | head -c 4096)
if [ -n "$status" ]; then
    echo "DIRTY"
else
    echo "CLEAN"
fi
# 3. Count untracked files
untracked=$(git ls-files --others --exclude-standard 2>/dev/null | wc -l)
echo "UNTRACKED_COUNT: $untracked"
# 4. Check runtime paths (passed via args) — handled by caller
# 5. Check incoming refs
incoming=$(git for-each-ref --format='%(refname)' refs/deploy/incoming/ 2>/dev/null || true)
if [ -n "$incoming" ]; then
    echo "STALE_INCOMING"
    echo "$incoming"
fi
"""


def _quote(val):
    """shlex.quote wrapper for clarity."""
    return shlex.quote(val)


def preflight_remote(config):
    """Run remote preflight checks. Return dict of results."""
    result = {
        "is_git": False,
        "head": None,
        "branch": None,
        "clean": False,
        "untracked_count": 0,
        "stale_incoming": [],
        "runtime_tracked": [],
        "locale_ok": True,
        "locale_info": None,
        "errors": [],
    }

    # Pre-check: remote dir safety (already validated by Config)

    # 1. Basic git repo check via stdin script
    stdout, stderr, rc = ssh_run_script(config, REMOTE_PREFLIGHT_SCRIPT)

    if rc != 0:
        if "NOT_A_GIT_REPO" in stdout:
            result["errors"].append(
                "Remote directory is not a Git repository. "
                "See references/fallback-sync.md for non-git deployment options."
            )
        else:
            result["errors"].append(f"Remote preflight failed (exit={rc}): {stderr}")
        return result

    # Parse output
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("HEAD: "):
            result["head"] = line[6:]
        elif line.startswith("BRANCH: "):
            result["branch"] = line[8:]
        elif line == "DIRTY":
            result["clean"] = False
        elif line == "CLEAN":
            result["clean"] = True
        elif line.startswith("UNTRACKED_COUNT: "):
            try:
                result["untracked_count"] = int(line[17:])
            except ValueError:
                pass
        elif line == "STALE_INCOMING":
            pass  # the ref names follow on subsequent lines
        elif line.startswith("refs/deploy/incoming/"):
            result["stale_incoming"].append(line)

    result["is_git"] = True

    # 2. Expected branch check if configured
    if config.expected_branch:
        if result["branch"] != config.expected_branch:
            result["errors"].append(
                f"Remote checked-out branch is {result['branch']!r}, "
                f"but expected_branch is {config.expected_branch!r}. "
                "Deployment blocked."
            )

    # 3. Stale incoming refs
    if result["stale_incoming"]:
        refs_str = ", ".join(result["stale_incoming"])
        result["errors"].append(
            f"Stale incoming refs detected: {refs_str}. "
            "Manual cleanup required. Run on remote:\n"
            f"  git update-ref -d <ref>\n"
            "Do NOT create new incoming refs until cleaned."
        )

    # 4. Runtime path tracking check (via remote command)
    if config.runtime_paths:
        # Build command: check each path
        checks = []
        for rp in config.runtime_paths:
            qrp = _quote(rp)
            checks.append(
                f"if git ls-files -- {qrp} 2>/dev/null | grep -q .; "
                f"then printf '%s\\n' {_quote('TRACKED:' + rp)}; fi"
            )
        remote_cmd = "cd " + _quote(config.remote_repo_dir) + " && " + " && ".join(checks)
        stdout, stderr, rc = ssh_run_command(config, remote_cmd)
        if rc == 0 and stdout.strip():
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("TRACKED:"):
                    tracked_path = line[8:]
                    result["runtime_tracked"].append(tracked_path)
                    result["errors"].append(
                        f"Runtime path tracked on remote: {tracked_path!r}. "
                        "Add to remote .gitignore or remove from tracking."
                    )

    # 5. Post-sync script existence on remote
    if config.post_sync_script:
        qpss = _quote(config.post_sync_script)
        remote_cmd = (
            "cd " + _quote(config.remote_repo_dir)
            + " && test -f " + qpss + " && echo EXISTS || echo NOT_FOUND"
        )
        stdout, stderr, rc = ssh_run_command(config, remote_cmd)
        if stdout.strip() == "NOT_FOUND":
            result["errors"].append(
                f"Post-sync script not found on remote: {config.post_sync_script!r}"
            )

    # 6. Health check scripts existence on remote
    for i, hc in enumerate(config.health_checks):
        qhc = _quote(hc)
        remote_cmd = (
            "cd " + _quote(config.remote_repo_dir)
            + " && test -f " + qhc + " && echo EXISTS || echo NOT_FOUND"
        )
        stdout, stderr, rc = ssh_run_command(config, remote_cmd)
        if stdout.strip() == "NOT_FOUND":
            result["errors"].append(
                f"Health check script not found on remote: health_checks[{i}] = {hc!r}"
            )

    # 7. Locale check (when non-ASCII paths might exist)
    locale_cmd = (
        "echo 'LOCALE_BIN='$(command -v locale 2>/dev/null || echo 'no')"
        " && echo 'CHARMAP='$(locale charmap 2>/dev/null || echo '?')"
        " && echo 'LANG='${LANG:-?}"
        " && echo 'LC_ALL='${LC_ALL:-?}"
        " && echo 'LC_CTYPE='${LC_CTYPE:-?}"
    )
    stdout, stderr, rc = ssh_run_command(config, locale_cmd)
    locale_bin = "?"
    charmap = "?"
    lang = "?"
    lc_all = "?"
    lc_ctype = "?"
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("LOCALE_BIN="):
            locale_bin = line[11:]
        elif line.startswith("CHARMAP="):
            charmap = line[8:]
        elif line.startswith("LANG="):
            lang = line[5:]
        elif line.startswith("LC_ALL="):
            lc_all = line[7:]
        elif line.startswith("LC_CTYPE="):
            lc_ctype = line[9:]
    if locale_bin == "no":
        result["locale_ok"] = False
        result["locale_info"] = "missing_cmd"
        result["errors"].append(
            "Remote has no locale command — cannot verify character encoding. "
            "Install POSIX locale tools or configure remote UTF-8 locale."
        )
    else:
        import re
        if not re.match(r"(?i)UTF-?8", charmap):
            result["locale_ok"] = False
            result["locale_info"] = {
                "charmap": charmap,
                "LANG": lang,
                "LC_ALL": lc_all,
                "LC_CTYPE": lc_ctype,
            }
            result["errors"].append(
                f"Remote locale charmap is {charmap!r} (expected UTF-8). "
                f"LANG={lang} LC_ALL={lc_all} LC_CTYPE={lc_ctype}. "
                "Configure remote UTF-8 locale."
            )

    return result


# ---------------------------------------------------------------------------
# Deploy transaction
# ---------------------------------------------------------------------------
DEPLOY_STEPS_SCRIPT_TEMPLATE = """\
set -e

remote_dir={remote_dir_q}

# [CRITICAL] Fetch bundle to incoming ref
git -C "$remote_dir" fetch /tmp/{bundle_name} HEAD:{incoming_ref_q}

# [CRITICAL] Create backup ref
backup_ref={backup_ref_q}
git -C "$remote_dir" update-ref --no-deref "$backup_ref" HEAD
echo "BACKUP_CREATED:$backup_ref"

# [CRITICAL] Reset to incoming
git -C "$remote_dir" reset --hard {incoming_ref_q}

# [HOUSEKEEPING] Delete incoming ref
git -C "$remote_dir" update-ref -d {incoming_ref_q} || echo "WARNING: incoming ref cleanup failed"

# [HOUSEKEEPING] Prune old backups (keep last N)
backup_count=$(git -C "$remote_dir" for-each-ref --format='%(refname)' {backups_prefix_q} 2>/dev/null | wc -l)
if [ "$backup_count" -gt {retention} ]; then
    git -C "$remote_dir" for-each-ref --format='%(refname)' {backups_prefix_q} \\
        --sort=creatordate | head -n $((backup_count - {retention})) | \\
        while read ref; do
            git -C "$remote_dir" update-ref -d "$ref" || echo "WARNING: could not delete $ref"
        done
fi

echo "DEPLOY_DONE"
"""


def _make_deployment_id():
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{ts}-{rand}"


def _make_backup_ref(deploy_id, short_sha):
    ts_part = deploy_id.split("-")[0]  # YYYYMMDDTHHMMSSZ
    return f"{BACKUP_REFS_PREFIX}{ts_part}-{short_sha}"


def _read_remote_head(config):
    """Read current HEAD on remote."""
    stdout, stderr, rc = ssh_run_command(
        config, "cd " + _quote(config.remote_repo_dir) + " && git rev-parse HEAD"
    )
    if rc != 0:
        raise DeployError(f"Cannot read remote HEAD: {stderr}")
    return stdout.strip()


def _run_post_sync(config, deploy_id, old_head, new_head, backup_ref):
    """Run post-sync script. Returns (ok, error_msg)."""
    qdir = _quote(config.remote_repo_dir)
    qscript = _quote(config.post_sync_script)
    remote_cmd = f"cd {qdir} && ./{qscript}"
    stdout, stderr, rc = ssh_run_command(config, remote_cmd)
    if rc != 0:
        return False, (
            f"Post-sync script failed (exit={rc}): {stderr.strip()}"
        )
    return True, None


def _run_health_checks(config):
    """Run health check scripts. Returns (ok, errors_list)."""
    errors = []
    for i, hc in enumerate(config.health_checks):
        qdir = _quote(config.remote_repo_dir)
        qhc = _quote(hc)
        remote_cmd = f"cd {qdir} && ./{qhc}"
        stdout, stderr, rc = ssh_run_command(config, remote_cmd)
        if rc != 0:
            errors.append(
                f"Health check [{i}] {hc!r} failed (exit={rc}): {stderr.strip()}"
            )
    return len(errors) == 0, errors


def do_deploy(config):
    """Execute the full deploy transaction. Returns status dict."""
    repo = config.repo_root
    deploy_id = _make_deployment_id()
    local_head = git_head(repo)
    local_short = git_short_sha(repo)
    incoming_ref = f"{INCOMING_REFS_PREFIX}{deploy_id}"
    backup_ref = _make_backup_ref(deploy_id, local_short)

    # Bundles go to system temp
    bundle_dir = tempfile.mkdtemp(prefix="push-to-server-")
    bundle_path = os.path.join(bundle_dir, f"{deploy_id}.bundle")
    remote_bundle_path = f"/tmp/{deploy_id}.bundle"

    status = {
        "deployment_id": deploy_id,
        "local_head": local_head,
        "incoming_ref": incoming_ref,
        "backup_ref": backup_ref,
        "status": STATUS_DEPLOY_FAILED,
        "remote_head_before": None,
        "remote_head_after": None,
        "errors": [],
        "warnings": [],
        "rollback_command": None,
    }

    try:
        # Read remote HEAD before
        try:
            status["remote_head_before"] = _read_remote_head(config)
        except DeployError as e:
            status["errors"].append(str(e))
            status["status"] = STATUS_PREFLIGHT_FAILED
            return status

        # Create bundle
        git_create_bundle(repo, bundle_path)

        # Upload bundle
        try:
            scp_upload(config, bundle_path, remote_bundle_path)
        except SafePushError as e:
            status["errors"].append(f"Bundle upload failed: {e}")
            status["status"] = STATUS_DEPLOY_FAILED
            return status

        # Build deploy script
        deploy_script = DEPLOY_STEPS_SCRIPT_TEMPLATE.format(
            remote_dir_q=_quote(config.remote_repo_dir),
            bundle_name=deploy_id,
            backup_ref_q=_quote(backup_ref),
            incoming_ref_q=_quote(incoming_ref),
            backups_prefix_q=_quote(BACKUP_REFS_PREFIX),
            retention=str(config.backup_retention),
        )

        # Execute deploy steps
        stdout, stderr, rc = ssh_run_script(config, deploy_script)

        if rc != 0:
            # CRITICAL step failed — re-read remote HEAD
            try:
                current_head = _read_remote_head(config)
                status["remote_head_after"] = current_head
            except DeployError:
                status["remote_head_after"] = None

            if status["remote_head_after"] == local_head:
                # HEAD actually switched — code_updated_unverified
                status["status"] = STATUS_CODE_UPDATED_UNVERIFIED
                # Check if backup ref was created (search output)
                if "BACKUP_CREATED" in stdout:
                    status["rollback_command"] = _build_rollback_cmd(
                        config, backup_ref
                    )
                else:
                    status["rollback_command"] = None
                status["errors"].append(
                    f"Deploy script failed (exit={rc}) but HEAD switched "
                    f"to expected commit. Status: {STATUS_CODE_UPDATED_UNVERIFIED}."
                )
            else:
                # HEAD not switched — deploy_failed
                status["status"] = STATUS_DEPLOY_FAILED
                if "BACKUP_CREATED" in stdout:
                    status["rollback_command"] = _build_rollback_cmd(
                        config, backup_ref
                    )
                else:
                    status["rollback_command"] = None
                    status["warnings"].append(
                        "Backup ref not created; no code rollback needed "
                        "(remote HEAD unchanged)."
                    )
                err_detail = stderr.strip() or stdout.strip()[-500:]
                status["errors"].append(
                    f"Deploy CRITICAL step failed (exit={rc}): {err_detail}"
                )
            return status

        # All critical steps passed — HEAD is at local_head now
        status["remote_head_after"] = local_head

        # Run post-sync
        if config.post_sync_script:
            ok, err = _run_post_sync(
                config, deploy_id, local_head, local_head, backup_ref
            )
            if not ok:
                status["status"] = STATUS_CODE_UPDATED_UNVERIFIED
                status["rollback_command"] = _build_rollback_cmd(config, backup_ref)
                status["errors"].append(err)
                status["errors"].append(
                    "Code updated but unverified. Run rollback command manually."
                )
                return status

        # Run health checks
        if config.health_checks:
            ok, hc_errors = _run_health_checks(config)
            if not ok:
                status["status"] = STATUS_CODE_UPDATED_UNVERIFIED
                status["rollback_command"] = _build_rollback_cmd(config, backup_ref)
                status["errors"].extend(hc_errors)
                status["errors"].append(
                    "Code updated but health checks failed. Run rollback command manually."
                )
                return status

        # All passed
        status["status"] = STATUS_VERIFIED
        status["rollback_command"] = _build_rollback_cmd(config, backup_ref)
        return status

    finally:
        # Cleanup remote bundle (best-effort)
        try:
            ssh_run_command(config, "rm -f " + _quote(remote_bundle_path))
        except Exception:
            status["warnings"].append(
                "Warning: could not clean up remote bundle "
                f"({remote_bundle_path})"
            )

        # Cleanup remote incoming ref (best-effort)
        try:
            remote_ref_cmd = (
                "cd " + _quote(config.remote_repo_dir)
                + " && git update-ref -d " + _quote(incoming_ref)
            )
            ssh_run_command(config, remote_ref_cmd)
        except Exception:
            status["warnings"].append(
                "Warning: could not clean up remote incoming ref "
                f"({incoming_ref})"
            )

        # Cleanup local bundle
        try:
            os.unlink(bundle_path)
            os.rmdir(bundle_dir)
        except OSError:
            pass


def _build_rollback_cmd(config, backup_ref):
    """Build the double-layer-quoted rollback command string.

    Layer 1: remote_command = "cd <dir> && git reset --hard <backup_ref>"
    Layer 2: shlex.join([ssh, ...opts, destination, remote_command])
    """
    remote_command = (
        "cd " + _quote(config.remote_repo_dir)
        + " && git reset --hard " + _quote(backup_ref)
    )
    return build_remote_command(config, remote_command)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_preflight(args):
    """Handle 'preflight' subcommand."""
    try:
        config_path = resolve_config_path(args.repo, args.config)
        config = load_config(config_path, repo_root=args.repo)
    except (ConfigError, OSError, UnicodeError, json.JSONDecodeError) as e:
        result = {"status": STATUS_PREFLIGHT_FAILED, "errors": [str(e)]}
        _output(result, args.json)
        return 1

    # Local preflight
    try:
        local_result = preflight_local(config)
    except PreflightError as e:
        result = {"status": STATUS_PREFLIGHT_FAILED, "errors": [str(e)]}
        _output(result, args.json)
        return 1

    if isinstance(local_result, tuple):
        issues, head, branch = local_result
    else:
        issues = local_result
        head, branch = None, None

    result = {
        "status": STATUS_PREFLIGHT_FAILED if issues else "preflight_local_pass",
        "repo_head": head,
        "repo_branch": branch,
        "errors": issues,
    }

    if args.remote and not issues:
        # Remote preflight
        try:
            remote_result = preflight_remote(config)
        except (SafePushError, OSError) as e:
            result["status"] = STATUS_PREFLIGHT_FAILED
            result["errors"].append(f"Remote preflight error: {e}")
            _output(result, args.json)
            return 1

        result["remote"] = remote_result
        if remote_result["errors"]:
            result["status"] = STATUS_PREFLIGHT_FAILED
            result["errors"].extend(remote_result["errors"])
        else:
            result["status"] = "preflight_pass"

    _output(result, args.json)
    return 0 if not result["errors"] else 1


def cmd_deploy(args):
    """Handle 'deploy' subcommand."""
    try:
        config_path = resolve_config_path(args.repo, args.config)
        config = load_config(config_path, repo_root=args.repo)
    except (ConfigError, OSError, UnicodeError, json.JSONDecodeError) as e:
        result = {"status": STATUS_PREFLIGHT_FAILED, "errors": [str(e)]}
        _output(result, args.json)
        return 1

    # Run full preflight first
    try:
        local_result = preflight_local(config)
    except PreflightError as e:
        result = {"status": STATUS_PREFLIGHT_FAILED, "errors": [str(e)]}
        _output(result, args.json)
        return 1

    if isinstance(local_result, tuple):
        issues, local_head, _ = local_result
    else:
        issues, local_head = local_result, None

    if issues:
        result = {
            "status": STATUS_PREFLIGHT_FAILED,
            "errors": issues,
        }
        _output(result, args.json)
        return 1

    # Remote preflight
    try:
        remote_result = preflight_remote(config)
    except (SafePushError, OSError) as e:
        result = {
            "status": STATUS_PREFLIGHT_FAILED,
            "errors": [f"Remote preflight error: {e}"],
        }
        _output(result, args.json)
        return 1

    if remote_result["errors"]:
        result = {
            "status": STATUS_PREFLIGHT_FAILED,
            "errors": remote_result["errors"],
            "remote": remote_result,
        }
        _output(result, args.json)
        return 1

    if not args.execute:
        # Dry-run: show plan
        result = {
            "status": "dry_run",
            "message": "Preflight passed. Use --execute to deploy.",
            "local_head": local_head,
            "remote": remote_result,
        }
        _output(result, args.json)
        return 0

    # Execute deploy
    try:
        deploy_result = do_deploy(config)
    except (SafePushError, OSError) as e:
        result = {
            "status": STATUS_DEPLOY_FAILED,
            "errors": [f"Deploy error: {e}"],
        }
        _output(result, args.json)
        return 1

    _output(deploy_result, args.json)
    # Exit code: 0 only if verified
    return 0 if deploy_result["status"] == STATUS_VERIFIED else 1


def _output(data, use_json):
    if use_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        status = data.get("status", "?")
        print(f"Status: {status}")
        errors = data.get("errors", [])
        if errors:
            print(f"Errors ({len(errors)}):")
            for e in errors:
                print(f"  - {e}")
        if "rollback_command" in data and data["rollback_command"]:
            print(f"Rollback: {data['rollback_command']}")
        if "local_head" in data and data["local_head"]:
            print(f"Local HEAD: {data['local_head']}")
        if "remote_head_before" in data:
            print(f"Remote HEAD (before): {data['remote_head_before']}")
        if "remote_head_after" in data:
            print(f"Remote HEAD (after): {data['remote_head_after']}")


def main():
    parser = argparse.ArgumentParser(
        description="Safe git-bundle deployment with preflight checks"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # preflight
    p_pre = sub.add_parser("preflight", help="Run preflight checks")
    p_pre.add_argument("--repo", required=True, help="Local repository path")
    p_pre.add_argument("--config", help="Config JSON path (defaults to saved or project config)")
    p_pre.add_argument("--remote", action="store_true", help="Also run remote preflight")
    p_pre.add_argument("--json", action="store_true", help="JSON output")

    # deploy
    p_dep = sub.add_parser("deploy", help="Execute deployment")
    p_dep.add_argument("--repo", required=True, help="Local repository path")
    p_dep.add_argument("--config", help="Config JSON path (defaults to saved or project config)")
    p_dep.add_argument("--execute", action="store_true", help="Actually write to remote")
    p_dep.add_argument("--json", action="store_true", help="JSON output")

    p_save = sub.add_parser("config-save", help="Validate and save a per-repository config")
    p_save.add_argument("--repo", required=True, help="Local repository path")
    p_save.add_argument("--config", required=True, help="Source config JSON path")
    p_save.add_argument("--replace", action="store_true", help="Replace a different saved config and back it up")

    parsed = parser.parse_args()

    try:
        if parsed.command == "preflight":
            sys.exit(cmd_preflight(parsed))
        elif parsed.command == "deploy":
            sys.exit(cmd_deploy(parsed))
        elif parsed.command == "config-save":
            path = save_config(parsed.repo, parsed.config, replace=parsed.replace)
            print(f"Saved config: {path}")
            sys.exit(0)
    except (SafePushError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
