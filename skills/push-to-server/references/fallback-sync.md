# Fallback Sync Methods

When the remote server is **not a Git repository**, `git bundle` cannot be used.
Use one of these fallback methods.

> **Important**: These methods lack the safety guarantees of `git bundle`:
> no atomic transactions, no built-in rollback, no automatic tracking of
> what was deployed. Use with explicit user consent.

---

## 1. rsync (directory sync)

Use when the remote is a non-git directory and you need fine-grained
include/exclude control.

### Dry-run first (required)

```bash
rsync --dry-run -avz \
  --exclude-from=.rsync-exclude.txt \
  -e "ssh -p <port>" \
  ./ <user>@<host>:/remote/path/
```

### Remove `--dry-run` only after user confirms

```bash
rsync -avz \
  --exclude-from=.rsync-exclude.txt \
  -e "ssh -p <port>" \
  ./ <user>@<host>:/remote/path/
```

### Safety notes

- `--delete` is **not included by default** — it would remove files on the
  remote that don't exist locally. Ask explicitly before adding.
- Runtime paths must be listed in `.rsync-exclude.txt`.
- No automatic rollback: files on the remote are overwritten in place.
- No deployment history or backup refs.

---

## 2. git archive + scp (one-shot tarball)

Use when you want to send only tracked files to a non-git directory.

```bash
git archive --format=tar.gz --prefix=repo/ HEAD > repo.tar.gz
scp -P <port> repo.tar.gz <user>@<host>:/tmp/
ssh -p <port> <user>@<host> \
  "cd /remote/path && tar xzf /tmp/repo.tar.gz --strip-components=1 && rm /tmp/repo.tar.gz"
rm -f repo.tar.gz
```

### Safety notes

- Overwrites all files in the target directory.
- No exclusions: even files listed in `.gitignore` are excluded from `git archive`
  (they are not tracked), so runtime data is naturally preserved *if* it was
  never committed.
- No rollback: consider creating a manual backup before deploying.

---

## 3. Staging / Canary Workflow

For production servers, consider this multi-step process:

1. **Stage**: Sync to a staging directory first.
2. **Verify**: Run smoke tests against the staging deployment.
3. **Promote**: Use `rsync --link-dest` or a symlink swap to move to production.
4. **Rollback**: Keep the previous release directory and swap the symlink back.

---

## Decision Matrix

| Method | Atomic | Rollback | Runtime protection | Auth boundaries |
|--------|--------|----------|--------------------|-----------------|
| git bundle | ✅ | ✅ (backup ref) | ✅ (gitignore) | ✅ (no extra) |
| rsync --dry-run | ❌ | ❌ | Manual exclude | ✅ (no extra) |
| git archive | ❌ | ❌ | Partial (untracked) | ✅ (no extra) |

**Default to `git bundle`** whenever the remote is a Git repository.
