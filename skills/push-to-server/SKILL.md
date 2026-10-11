---
name: push-to-server
description: >-
  Safely push the current repository state to a deployment server while
  preserving runtime data and generated artifacts. Use when the user asks to
  push, sync, deploy, upload, or overwrite code on a server for any project.
  Works via SSH (git bundle, rsync, git archive + scp). Preserves runtime data
  (config, uploads, DB) by default unless the user explicitly asks to replace
  them. If the server is unreachable or connection details are missing, stop
  and request them before continuing.
---

# Push To Server

Deploy local repository code to a remote server via `git bundle` with preflight
checks. **Does not modify `authorized_keys`, `known_hosts`, or accept passwords.**

---

## 1. Discovery Order

1. Read project `AGENTS.md` for deployment instructions.
2. Resolve config in this order: explicit `--config`, saved per-repository config, then `.agents/push-to-server.json`.
3. If all are missing, ask for server details with **no default values**.

`config-save` validates and stores the original JSON outside the skill and repository at `~/.push-to-server/configs/<repo-hash>.json`. Set `PUSH_TO_SERVER_HOME` to choose another external storage root; a root inside the skill directory or repository is rejected. Saved configs survive replacing the skill directory. A present but invalid saved config blocks fallback, so repair it explicitly. After relocating a repository, import its existing saved file with `config-save --repo <new-repo> --config <saved-file>`. To recover the one previous version, explicitly import its `.json.bak` file with `--replace`.

Config schema: `schema_version=1`, `server(host,user,port,remote_repo_dir[,ssh_key_path])`,
`runtime_paths`, `health_checks`, `post_sync_script`, `backup_retention(1..5)`,
`expected_branch`. Forbidden field names: `password`, `token`, `secret`,
`credential`, `private_key` (any nesting, case-insensitive match).

---

## 2. Tool Entrypoints

Replace template variables with project config values. Every command requires a
repository path; `preflight` and `deploy` discover config unless `--config` is set.

```bash
# Save and validate a config once; prints only its path
python <SKILL_DIR>/scripts/safe_push.py config-save --repo /path/to/repo --config /path/to/config.json

# Preflight: local checks; add --remote to check remote too
python <SKILL_DIR>/scripts/safe_push.py preflight --repo /path/to/repo

# Preflight with remote checks and JSON output
python <SKILL_DIR>/scripts/safe_push.py preflight --repo /path/to/repo --remote --json

# Dry-run deploy (no remote writes)
python <SKILL_DIR>/scripts/safe_push.py deploy --repo /path/to/repo

# Execute deploy (requires --execute flag)
python <SKILL_DIR>/scripts/safe_push.py deploy --repo /path/to/repo --execute
```

Use `--config <path>` on `preflight` or `deploy` to override discovery. Explicit relative paths resolve from the current working directory. To replace a different saved config, pass `--replace`; the prior file is atomically preserved as `<hash>.json.bak`, replacing the previous backup.

---

## 3. Blocking Gates

Deploy **will not proceed** if any check fails:

| Gate | Check |
|------|-------|
| Local clean | `git status --porcelain` empty |
| Runtime paths | Not tracked by git; covered by `.gitignore` |
| Post-sync script | Exists, tracked, inside repo |
| Health scripts | All exist, tracked, inside repo |
| Remote dir | Absolute POSIX; rejects `/`, `~`, `~/...`, `/home/<user>`, Windows paths |
| Remote git repo | `git rev-parse --git-dir` succeeds |
| Remote clean | `git status --porcelain` empty |
| Remote runtime | Not tracked on remote |
| Stale incoming | `refs/deploy/incoming/*` empty (local `for-each-ref`) |
| Expected branch | Checked-out branch matches config (if configured) |
| Remote locale | `locale charmap` is UTF-8 (when non-ASCII paths exist) |

---

## 4. Deploy Transaction

1. **Bundle**: `git bundle create` of HEAD → system temp.
2. **Upload**: SCP bundle to `/tmp/push-to-server-<id>.bundle`.
3. **[CRITICAL] Fetch**: `git fetch bundle HEAD:refs/deploy/incoming/<id>`.
4. **[CRITICAL] Backup**: `git update-ref refs/deploy/backups/<ts>-<sha>`.
5. **[CRITICAL] Reset**: `git reset --hard refs/deploy/incoming/<id>`.
6. **[HOUSEKEEPING]**: Delete incoming ref, prune old backups (retain N).
7. **[CRITICAL] Post-sync**: Run configured script (if any).
8. **[CRITICAL] Health**: Run all health check scripts.

**Status outcomes**: `preflight_failed` | `deploy_failed` | `code_updated_unverified` | `verified`

---

## 5. Authorization Boundaries

- **SSH key only**: Uses existing key (`-i`), ssh-agent, or SSH config.
- **No passwords**: Never prompt for, read, or store passwords.
- **No host key auto-accept**: `Permission denied` or `Host key verification failed`
  stops immediately. User handles externally.
- **No remote init**: If remote directory is not a git repo, block and point to
  `references/fallback-sync.md`.
- **No `authorized_keys` or `known_hosts` edits**.

---

## 6. Rollback

On failure (`code_updated_unverified` or `deploy_failed`), the tool outputs a
single SSH command that rolls back tracked code:

```
ssh -p <port> [-i <key>] <user>@<host> 'cd <remote_dir> && git reset --hard <backup_ref>'
```

**Rollback restores tracked code only.** Database migrations, runtime data, and
other side effects must be handled by the project's own recovery procedures.

---

## 7. Reporting

After deploy, report to user:

- **Status**: One of the four deployment statuses.
- **HEAD**: Before → After commit SHAs.
- **Rollback command**: Exact SSH command for manual revert.
- **Backup ref**: `refs/deploy/backups/<ts>-<short-sha>`.
- **Warnings**: HOUSEKEEPING failures, untracked file count.
