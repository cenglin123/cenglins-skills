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
2. Locate `.agents/push-to-server.json` in project root for structured config.
3. If missing, ask for server details with **no default values**.

Config schema: `schema_version=1`, `server(host,user,port,remote_repo_dir[,ssh_key_path])`,
`runtime_paths`, `health_checks`, `post_sync_script`, `backup_retention(1..5)`,
`expected_branch`. Forbidden field names: `password`, `token`, `secret`,
`credential`, `private_key` (any nesting, case-insensitive match).

---

## 2. Tool Entrypoints

Replace template variables with project config values. All commands require a
repository path and config path.

```bash
# Preflight: local checks; add --remote to check remote too
python <SKILL_DIR>/scripts/safe_push.py preflight --repo /path/to/repo --config .agents/push-to-server.json

# Preflight with remote checks and JSON output
python <SKILL_DIR>/scripts/safe_push.py preflight --repo /path/to/repo --config .agents/push-to-server.json --remote --json

# Dry-run deploy (no remote writes)
python <SKILL_DIR>/scripts/safe_push.py deploy --repo /path/to/repo --config .agents/push-to-server.json

# Execute deploy (requires --execute flag)
python <SKILL_DIR>/scripts/safe_push.py deploy --repo /path/to/repo --config .agents/push-to-server.json --execute
```

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
