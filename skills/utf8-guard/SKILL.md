---
name: utf8-guard
description: >-
  Use in Windows projects with Chinese filenames, comments, docs, or terminal
  output when an agent may hit UTF-8 versus GBK/CP936 issues. Prevention rules
  (apply these from the description alone, without reading the body): do not
  assume garbled Chinese means file corruption — first distinguish real
  content corruption from display-layer mis-decoding, wrong default file
  encoding, Git `core.quotepath` escaping of Chinese paths, or a non-UTF-8
  terminal code page; when reading or writing text, prefer UTF-8 explicitly
  (`encoding="utf-8"` or `PYTHONUTF8=1`); write UTF-8 without BOM unless the
  project requires otherwise; before piping non-ASCII text to a native command
  in Windows PowerShell 5.1, set `$OutputEncoding` to UTF-8 (it defaults to
  ASCII and silently corrupts Chinese); only "fix" files after verifying the
  underlying bytes are actually wrong. Read the body only when an actual
  encoding failure needs troubleshooting.
---

# UTF-8 Guard

> Related: see `powershell-guard` for PowerShell-specific syntax and execution issues in Windows.

Use this skill to avoid accidental encoding breakage in Windows environments.

## Core Rule

Do not assume garbled Chinese text means the file itself is corrupted.

First distinguish between:

- real file corruption
- display-layer mis-decoding
- toolchain defaulting to GBK/CP936 instead of UTF-8

Before editing or "fixing" text, verify whether the underlying file bytes are valid UTF-8 and whether the visible problem is only a terminal or tool display issue.

## Known Environment Pitfalls

Example pitfall:

- symptom: Chinese text in Python `print()` output shows as "???" or garbled in the terminal
- cause: the terminal code page is CP936, not UTF-8
- fix: set `$env:PYTHONUTF8='1'` before running, or switch to Windows Terminal

When you encounter new issues, record them using the same structure in the project's `开发日志.md` if one exists.

## UTF-8 Encoding

Projects may contain Chinese filenames, Chinese comments, and Chinese document content. Treat UTF-8 as a full pipeline requirement, not just a file-level preference.

### File Encoding

Symptom:
- Chinese comments or strings display as mojibake
- reading files throws `UnicodeDecodeError`
- Python reports a `gbk` codec error
- diffs show unexpected character damage after small edits

Cause:
- Windows often defaults to GBK/CP936 instead of UTF-8
- newly created files may inherit a local default encoding
- tools may read UTF-8 files using the wrong codec

Fix:
- write source files and docs as UTF-8 without BOM unless the project explicitly requires otherwise
- in Python, always pass `encoding="utf-8"` when reading or writing text files
- do not rely on the system default encoding
- if the project uses `.editorconfig`, prefer `charset = utf-8`
- in VS Code, prefer `"files.encoding": "utf-8"`

### Git And Chinese Paths

Symptom:
- `git status` or `git diff` shows Chinese filenames as octal escape sequences such as `\345\274\200...`

Cause:
- Git quotes non-ASCII paths by default

Fix:

```powershell
git config --global core.quotepath false
```

This improves readability only. It does not change file contents.

### Terminal And Shell

Symptom:
- terminal output containing Chinese appears garbled
- `print()` or command output fails due to encoding
- scripts behave differently across PowerShell, cmd, and other terminals

Cause:
- the terminal code page is not UTF-8
- older Windows shells often default to CP936

Fix:
- prefer Windows Terminal when available
- in classic cmd, run `chcp 65001` (note: some older console programs may crash or misbehave under code page 65001; test before relying on it)
- when needed, set `PYTHONUTF8=1` before running Python tools

Example:

```powershell
$env:PYTHONUTF8='1'
python script.py
```

### Piped Output and $OutputEncoding

Symptom:
- Chinese text passed through a pipe to a native command (e.g. `python`, `findstr`) arrives as garbled
- encoding looks correct in the terminal but wrong when piped

Cause:
- PowerShell's `$OutputEncoding` controls what encoding the receiving native process sees
- it defaults to ASCII, which silently corrupts non-ASCII characters passed through pipes

Fix:
- set output encoding to UTF-8 before piping:

```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
your-command | python -
```

- or use a temp file encoded as UTF-8 instead of piping


## Operating Rules

- When reading text files in scripts, always specify `encoding="utf-8"` unless the file format clearly requires something else.
- When writing generated files, prefer ASCII when possible, but use UTF-8 when the content or filenames include Chinese.
- Before "repairing" garbled text, inspect whether the file is correct when decoded as UTF-8.
- Be cautious when a shell command output looks wrong; the file on disk may still be fine.
- If a validation script fails under GBK but succeeds under UTF-8, prefer fixing the invocation environment before rewriting the file contents.
- When reporting an encoding issue, state whether it is a real file-content problem or a display/decoding problem.

## Quick Checks

Use checks like these before making invasive edits:

```powershell
$env:PYTHONUTF8='1'
@'
from pathlib import Path
path = Path("some-file.txt")
text = path.read_text(encoding="utf-8")
print(text[:500])
'@ | python -
```

```powershell
git config --global core.quotepath false
git status
```

```powershell
# PowerShell native UTF-8 read (no Python dependency)
Get-Content -Path "some-file.txt" -Encoding UTF8 -TotalCount 25
```

```powershell
# Detect and strip UTF-8 BOM (0xEF 0xBB 0xBF) at the byte level.
# Get-Content consumes a BOM as an encoding signature, so checking the first
# char of its output cannot detect a BOM reliably — inspect raw bytes instead.
$path = "some-file.txt"
$bytes = [System.IO.File]::ReadAllBytes($path)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    $rest = New-Object byte[] ($bytes.Length - 3)
    [Array]::Copy($bytes, 3, $rest, 0, $rest.Length)
    [System.IO.File]::WriteAllBytes($path, $rest)
    Write-Host 'BOM detected and stripped'
} else {
    Write-Host 'No BOM'
}
```

## Decision Pattern

If Chinese text looks broken:

1. Check whether the file can be read as UTF-8.
2. Check whether only the terminal or tool display is broken.
3. Check whether Git path quoting is the real complaint.
4. Only then decide whether the file content itself needs repair.
