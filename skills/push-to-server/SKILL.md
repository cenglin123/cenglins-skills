---
name: push-to-server
description: >-
  Safely push the current repository state to a deployment server while
  preserving runtime data and generated artifacts. Use when the user asks to
  push, sync, deploy, upload, or overwrite code on a server for any project.
  Works via SSH (git bundle, rsync, git archive + scp). Preserves runtime data
  (config, uploads, DB) by default unless the user explicitly asks to replace
  them. If the server is unreachable or connection details are missing, stop
  and request them before continuing. Also covers shared /srv servers: the
  server admin's agent creating a project and issuing an invite, an agent
  enrolling with a received `srvinvite1.` invite and doing the first deploy,
  plus the root management panel.
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

### Shared Server Templates

When registering a new project, check the external store's `README.md` and
`templates/` directory for user-maintained server information, if present.
These are optional agent-readable records, not automatic config-discovery
inputs. Reuse confirmed connection values; copy the template to a temporary
file, fill project-specific fields, then run `config-save --repo <project>`
with `--config <temporary-file>`. Do not write project values back into a
shared template. A template with `remote_repo_dir: null` is intentionally
incomplete and must not be saved as an active project config.

### Recommended Project Layout

Deploy projects under `/srv/<project>/`. The script rejects every path under
`/home/` and `~`, so a home-directory layout cannot be deployed:

```text
/srv/
  PROJECTS.tsv              # Registry: project, port, contact, created
  <project>/
    repo/                   # Git code, Compose files, deployment/check scripts
    config/                 # Server-specific configuration and .env
    data/                   # Databases, uploads and other persistent data
    backups/                # Application data backups
```

- Name projects with lowercase letters, digits, `-` and `_`, and set
  `remote_repo_dir` to the absolute `repo/` path. Respect existing layouts and
  deployment accounts; this is a recommendation, not authorization to create
  directories or migrate existing services.
- On a server shared by several people or agents, follow the shared-server
  onboarding below: one Linux account and SSH key per project.
- Keep credentials and persistent data outside tracked code. Reference sibling
  `config/` and `data/` directories explicitly from Compose or service scripts;
  account for their paths and permissions. `runtime_paths` accepts only paths
  relative to the code repository: do not put sibling or absolute paths there.
  For runtime files kept inside `repo/`, ignore them in Git and declare them.
- Record each project's directory, service/container name, port, URL, start/
  stop commands, health check and data-backup procedure in project deployment
  instructions; check for port conflicts before exposing a new service.
  Use Compose or systemd as appropriate for restart behavior and bounded logs.
- First-time code delivery into an empty, existing `repo/` uses `bootstrap`;
  `deploy` only updates an existing Git repository. Neither command creates
  directories, accounts or ports. Once bootstrapped, register the project
  config, run remote preflight, deploy, and verify the application using
  tracked health scripts.
- Code backup refs and rollback cover tracked code only. Back up databases
  with their application-specific tools before migrations; choose data
  retention and an off-server backup destination separately. Never let a
  post-sync script overwrite configuration, delete data, or remove volumes
  merely to update code.

### Shared `/srv` Server Onboarding

Agents run the whole onboarding by invitation; humans only ask for or issue an
invite, forward it once, and change the application password. Read
[`references/project-accounts.md`](references/project-accounts.md) before any
step and act only in your own role:

| Phase | Who | What |
|---|---|---|
| 0. Server setup (once) | Server admin, in their own terminal | Install `srv-project` with `--delegate <admin>`, then the root panel ([`references/srv-panel.md`](references/srv-panel.md)); admin completes the panel's first login. The panel comes before the first project. |
| 1. Invite | Admin, or admin's agent after explicit approval | Create the project if new (`sudo -n srv-project create`), then `sudo -n srv-project invite <project> --label <person-device>`; the admin forwards the one-line `srvinvite1.…` text to the requester. Issuing the invite is the approval. |
| 2. Enroll | Requester's agent | `safe_push.py enroll '<invite>'`: generates the key, pins the panel certificate, registers the public key, writes known_hosts and the SSH alias from the invite, verifies login. |
| 3. First deploy | Requester's agent | `config-save`, `bootstrap`, runtime, `config/.env` with random secrets, systemd user service, post-sync/health scripts, then deliver URL and initial password to its user. |

The SSH host keys written by `enroll` come from the admin's invite, a trusted
channel, so this is not automatic host-key acceptance; a conflicting existing
known_hosts entry still stops. A new device for the same person is a new
invite; revoke the old key afterwards. Never collect or type sudo passwords,
never send private keys or invite codes anywhere but the requester, and never
relay application passwords between the two sides.

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

# Enroll this machine on a shared /srv server from an admin's invite (no --repo)
python <SKILL_DIR>/scripts/safe_push.py enroll 'srvinvite1.…'

# First delivery into an empty remote repo/ directory (dry-run, then --execute)
python <SKILL_DIR>/scripts/safe_push.py bootstrap --repo /path/to/repo
python <SKILL_DIR>/scripts/safe_push.py bootstrap --repo /path/to/repo --execute
```

Use `--config <path>` on `preflight`, `deploy` or `bootstrap` to override discovery. Explicit relative paths resolve from the current working directory. To replace a different saved config, pass `--replace`; the prior file is atomically preserved as `<hash>.json.bak`, replacing the previous backup.

---

## 3. Blocking Gates

Deploy **will not proceed** if any check fails:

| Gate | Check |
|------|-------|
| Local clean | `git status --porcelain` empty |
| Runtime paths | Not tracked by git; covered by `.gitignore` |
| Post-sync script | Exists, tracked, inside repo |
| Health scripts | All exist, tracked, inside repo |
| Remote dir | Absolute POSIX; rejects `/`, `~`, `~/...`, any `/home/...` path, Windows paths |
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
- **No remote init beyond bootstrap**: `deploy` blocks when the remote directory
  is not a git repo. `bootstrap` only clones into an existing, empty directory;
  for anything else point to `references/fallback-sync.md`.
- **No `authorized_keys` or `known_hosts` edits**. Server access for project
  accounts is granted with `srv_project.sh` by the server admin (or the admin's
  agent after explicit approval), outside the deploy tool.

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
