---
name: crlf-guard
description: >-
  Use in Windows projects when editing, writing, or committing text files to
  Git, or when diagnosing CRLF/LF line-ending warnings. Prevention rules
  (apply these from the description alone, without reading the body): on
  Windows, most tools (Write/Edit/PowerShell Set-Content) produce CRLF by
  default — after editing any text file, check for CRLF with `git ls-files
  --eol | Select-String "w/crlf"` before committing; if any files show
  CRLF, convert them with `python -c "f='path'; open(f,'wb').write(open(f,
  'rb').read().replace(b'\r\n',b'\n'))"` or batch-fix all with `python -c
  "import os; [open(os.path.join(r,f),'wb').write(open(os.path.join(r,f),
  'rb').read().replace(b'\r\n',b'\n')) for r,_,fs in os.walk('.') if
  '.git' not in r for f in fs if f.endswith(('.md','.js','.jsx','.py',
  '.json','.css','.html')) and b'\r\n' in open(os.path.join(r,f),
  'rb').read()]"`; the project should have `.gitattributes` with `*
  text=auto eol=lf` — if missing, add it before editing files; never do
  bulk "normalize to CRLF" commits — always normalize to LF; avoid
  PowerShell `Set-Content` for text files (produces CRLF) — prefer
  `[System.IO.File]::WriteAllText` with UTF-8 no-BOM encoding; after
  `git rm --cached` to untrack files, verify the server's working copy
  still has the files (git reset --hard deletes previously-tracked
  files). Read the body only when deeper troubleshooting is needed.
---

# CRLF Guard

> Related: see `utf8-guard` for encoding/character-set issues, `powershell-guard` for PowerShell-specific syntax issues.

Use this skill to avoid CRLF/LF line-ending issues in Windows Git projects.

## Core Rule

On Windows, most text-writing tools produce CRLF (`\r\n`) by default. Git expects LF (`\n`). After editing any text file, verify line endings before committing.

First distinguish between:

- a CRLF warning (informational, Git will normalize)
- actual content corruption (mixed or wrong line endings)
- a missing `.gitattributes` strategy layer

## Known Environment Pitfalls

### CRLF Warning During Commit

- symptom: `git commit` shows `warning: in the working copy of '...xxx', LF will be replaced by CRLF`
- cause: the file has CRLF on disk, Git will normalize to LF
- fix: convert the file to LF, or let `.gitattributes` handle it (warning is informational, not an error)

### Tools That Produce CRLF on Windows

| Tool | Native EOL | Recommended Alternative |
|------|-----------|------------------------|
| Write/Edit tool (AI agents) | CRLF | Convert to LF after editing |
| PowerShell `Set-Content` | CRLF | `[System.IO.File]::WriteAllText($path, $text, [System.UTF8Encoding]::new($false))` |
| PowerShell `>` redirection | UTF-16 with CRLF | `Out-File -Encoding utf8` or `[System.IO.File]::WriteAllText` |
| Notepad | CRLF | Use VS Code or other LF-capable editor |
| VS Code | LF (if configured) | Ensure `"files.eol": "\n"` in settings |
| Python `open()` with `newline=''` | LF | Use `newline=''` to preserve LF |

### Strategy Layer Missing

- symptom: every commit triggers CRLF warnings, even after manual fixes
- cause: no `.gitattributes` to declare the project's line ending policy
- fix: add `.gitattributes` with `* text=auto eol=lf`, then do full normalization

## Strategy Layer: `.gitattributes`

The `.gitattributes` file is the strategy layer that controls line ending normalization.

### Required Configuration

```gitattributes
* text=auto eol=lf
```

This tells Git:
- Auto-detect text files
- Use LF in the working copy (convert CRLF to LF on checkout)
- Normalize to LF on commit

### Three-Axis Alignment

For CRLF warnings to be fully eliminated, all three axes must be aligned:

| Axis | Meaning | Command to check |
|------|---------|-----------------|
| `i/lf` | Index has LF | `git ls-files --eol` |
| `w/lf` | Working copy has LF | `git ls-files --eol` |
| `attr/eol=lf` | `.gitattributes` declares LF | `git ls-files --eol` |

All three should show `i/lf w/lf attr/text=auto eol=lf`.

### One-Time Full Normalization

After adding or changing `.gitattributes`, do a full normalization:

```powershell
# Step 1: Normalize the index
git add --renormalize .

# Step 2: Normalize the working copy (delete + checkout)
# The delete is required because git checkout won't rewrite files
# that are already "equivalent" by attribute comparison.
git ls-files | Where-Object { $_ -match '\.(md|js|jsx|py|css|html|json|txt|yml|yaml)$' -and $_ -notmatch 'node_modules|dist|package-lock' } | ForEach-Object {
    Remove-Item -LiteralPath $_ -Force -ErrorAction SilentlyContinue
    git checkout HEAD -- $_
}

# Step 3: Verify
git ls-files --eol | Select-String "w/crlf"
# Should output nothing
```

## Troubleshooting: Fixing CRLF After Editing

When a CRLF warning appears after editing files:

### Quick Fix (Single File)

```powershell
python -c "f='path/to/file'; open(f,'wb').write(open(f,'rb').read().replace(b'\r\n',b'\n'))"
```

### Batch Fix (All Text Files)

```powershell
python -c "import os; [open(os.path.join(r,f),'wb').write(open(os.path.join(r,f),'rb').read().replace(b'\r\n',b'\n')) for r,_,fs in os.walk('.') if '.git' not in r and 'node_modules' not in r for f in fs if f.endswith(('.js','.jsx','.md','.css','.py','.json','.html','.txt')) and b'\r\n' in open(os.path.join(r,f),'rb').read()]"
```

### Python Inline vs Script File

When using Python to edit files with Chinese content:
- Inline Python (`python -c "..."`) may have encoding issues with Chinese characters
- Prefer a temporary `.py` script file for complex edits
- Always use `encoding='utf-8'` and `newline=''` when writing

```powershell
# Good: write a temp script
@"
path = 'docs/pitfalls.md'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()
content = content + '\nnew section\n'
with open(path, 'w', encoding='utf-8', newline='') as f:
    f.write(content)
"@ | Out-File -Encoding utf8 fix.py
python fix.py
Remove-Item fix.py
```

## Related Git Pitfall: Tracking Removal And Server Data

> This is not a CRLF issue, but a related Git pitfall that affects line-ending management.

When removing files from Git tracking (e.g., `git rm --cached`), the server's `git reset --hard` will delete those files from the working tree.

### Prevention

Before removing files from tracking:

```powershell
# On server: backup first
ssh server "cp -r energy_data/ /tmp/energy_data_backup/"

# Local: remove from tracking
git rm --cached -r energy_data/
echo "energy_data/" >> .gitignore
git add .gitignore
git commit -m "chore: untrack energy_data/"

# Push to server
# ... push mechanism ...

# On server: restore
ssh server "cp -r /tmp/energy_data_backup/ energy_data/"
```

### Recovery

If files were already deleted:

```powershell
# Find the commit before removal
git log --oneline -- energy_data/

# Restore from that commit
git checkout <commit-before-removal> -- energy_data/
git reset HEAD energy_data/   # Unstage
```

## Operating Rules

- After editing any text file, check for CRLF before committing.
- Never do bulk "normalize to CRLF" commits — always normalize to LF.
- The `.gitattributes` file is the strategy layer — if missing, add it first.
- Avoid PowerShell `Set-Content` for text files — it produces CRLF. Prefer `[System.IO.File]::WriteAllText($path, $text, [System.UTF8Encoding]::new($false))` for BOM-less UTF-8 with LF.
- When writing files with Chinese content, prefer Python script files over inline commands.
- Use `newline=''` in Python `open()` to preserve LF line endings.
- The `git add --renormalize .` command normalizes the index but NOT the working copy.
- After `git rm --cached` to untrack files, verify the server's working copy still has the files.

## Quick Checks

Check for CRLF in working copy:

```powershell
git ls-files --eol | Select-String "w/crlf"
# Should output nothing
```

Check specific file:

```powershell
python -c "f='path/to/file'; data=open(f,'rb').read(); print('CRLF:', data.count(b'\r\n'), 'LF:', data.count(b'\n')-data.count(b'\r\n'))"
```

Verify three-axis alignment (spot check):

```powershell
git ls-files --eol | Select-String "w/crlf"
# If any output, those files need fixing
```

Check `.gitattributes` exists and has correct content:

```powershell
Get-Content .gitattributes
# Should contain: * text=auto eol=lf
```

## Decision Pattern

If a CRLF warning appears:

1. Check whether `.gitattributes` exists with `* text=auto eol=lf`.
2. Check which files have CRLF: `git ls-files --eol | Select-String "w/crlf"`.
3. If only a few files, fix them with Python one-liner.
4. If many files, do full normalization (delete + checkout).
5. If the warning persists after normalization, check if the Write/Edit tool is producing CRLF.
6. Never normalize to CRLF — always normalize to LF.
