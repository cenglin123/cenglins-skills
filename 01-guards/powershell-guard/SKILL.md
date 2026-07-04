---
name: powershell-guard
description: >-
  Use in Windows projects when writing or running PowerShell commands, or when
  diagnosing PowerShell command failures. Prevention rules (apply these from
  the description alone, without reading the body): never use `&&` or `||` in
  Windows PowerShell 5.1 — use `cmd1; if ($?) { cmd2 }`; read environment
  variables as `$env:VAR` (never `$VAR` or `%VAR%`); prefer the shell tool's
  workdir parameter over `cd`; `rm`, `cp`, `curl`, `ls`, `cat` are cmdlet
  aliases that reject Unix flags — use `Remove-Item -Recurse -Force`,
  `Copy-Item -Recurse`, `Invoke-WebRequest` or `curl.exe`; use `$LASTEXITCODE`
  for numeric exit codes (`$?` is a boolean); `>` redirection writes UTF-16 in
  PS 5.1 — use `Out-File -Encoding utf8` when other tools must read the file;
  double-quote paths containing spaces. Read the body only when an actual
  PowerShell failure needs troubleshooting.
---

# PowerShell Guard

> Related: see `utf8-guard` for encoding/character-set issues in Windows PowerShell.

Use this skill to avoid accidental shell execution errors in Windows PowerShell environments.

## Core Rule

Do not assume PowerShell syntax is identical to bash or Unix shells.

First distinguish between:

- a real command failure
- a syntax incompatibility (e.g. `&&`, `||`, `>` behavior)
- an alias trap (e.g. `rm -rf`, `cp -r`)
- a quoting or path escaping issue

Before rewriting logic or switching to a different approach, verify whether the command failed because of PowerShell-specific behavior.

## Known Environment Pitfalls

Example pitfall:

- symptom: `cmd1 && cmd2` throws "The token '&&' is not a valid statement separator"
- cause: PowerShell 5.1 does not support `&&` or `||`
- fix: use `cmd1; if ($?) { cmd2 }`

When you encounter new issues, record them using the same structure in the project's `开发日志.md` if one exists.

### Command Chaining

Symptom:
- `cmd1 && cmd2` throws a parser error or unexpected-token error
- `cmd1 || cmd2` is not recognized
- pipeline behavior differs from bash expectations

Cause:
- Windows PowerShell 5.1 does not support `&&` or `||` operators

Fix:
- use `;` for sequential execution (does not check success)
- use `if ($?) { cmd2 }` to run a command only if the previous one succeeded
- avoid single-line chaining; prefer multiple independent shell tool calls

Example:

```powershell
cmd1; if ($?) { cmd2 }
```

### Directory Changes And Workdir

Symptom:
- `cd dir && command` fails
- relative paths resolve to the wrong location
- nested commands do not inherit the expected working directory

Cause:
- PowerShell does not support `&&`
- some toolchains do not persist `cd` across invocations when chained

Fix:
- use the `workdir` parameter of the shell tool instead of `cd`
- if `workdir` is unavailable, run `cd` and the command in the same session without `&&`

Example:

```text
# Good (shell tool call parameters, not PowerShell syntax)
workdir: /foo/bar
command: pytest tests
```

```text
# Bad
cd /foo/bar && pytest tests
```

### Redirection And Streams

Symptom:
- `>` produces Unicode or unexpected encoding
- `2>&1` behavior differs from bash
- binary output is corrupted when redirected

Cause:
- PowerShell `>` and `>>` operate on object streams and default to Unicode
- they are not byte-for-byte equivalents of Unix redirection

Fix:
- for simple text logging, `>` and `>>` usually suffice
- for byte-exact or binary redirection, prefer piping through `Out-File -Encoding utf8` or use `cmd /c`
- note: in Windows PowerShell 5.1, `Out-File -Encoding utf8` writes UTF-8 **with BOM**; for BOM-less UTF-8 use `[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))`
- when cross-platform compatibility matters, invoke `cmd /c "..."` for bash-style redirection

### Environment Variables

Symptom:
- `$VAR` or `%VAR%` expands to nothing
- scripts fail because environment variables are missing

Cause:
- PowerShell uses `$env:VAR` syntax
- `$VAR` refers to a PowerShell variable, not an environment variable
- `%VAR%` is cmd syntax and does not work in PowerShell

Fix:
- always use `$env:VAR` to read environment variables
- use `$env:VAR = 'value'` to set them in the current process

Example:

```powershell
$env:PYTHONUTF8='1'
python script.py
```

### Exit-Status Checks

Symptom:
- checking `$?` returns `$True` or `$False` instead of an integer
- scripts that expect `0` or `1` break

Cause:
- in PowerShell, `$?` is a boolean automatic variable indicating success or failure
- it is not the numeric exit code

Fix:
- use `$?` for boolean success checks
- use `$LASTEXITCODE` for the numeric exit code of the last native command

Example:

```powershell
python script.py
if ($LASTEXITCODE -ne 0) { Write-Error "failed" }
```

### Alias Traps

Symptom:
- `rm -rf dir` fails or behaves unexpectedly
- `cp -r src dst` fails
- `curl` options are not recognized
- `cat`, `ls`, `ps` output format differs from Unix tools

Cause:
- PowerShell provides aliases (`rm`, `cp`, `curl`, `cat`, `ls`) that map to cmdlets, not native Unix binaries
- aliases often do not accept Unix-style short flags

Fix:

| Unix habit | PowerShell equivalent |
|------------|----------------------|
| `rm -rf dir` | `Remove-Item -Recurse -Force dir` |
| `cp -r src dst` | `Copy-Item -Recurse src dst` |
| `mkdir -p dir` | `New-Item -ItemType Directory -Path dir -Force` |
| `cat file` | `Get-Content file` (or `type file`) |
| `ls` | `Get-ChildItem` (or `dir`) |
| `curl` | `Invoke-WebRequest`. `curl.exe` ships in Windows 10 17063+ / Server 2019+; on older Windows, use `Invoke-WebRequest`. |

### Quoting And Paths

Symptom:
- paths with spaces split into multiple arguments
- variables inside strings are not expanded (or expanded unexpectedly)
- nested quotes are parsed incorrectly

Cause:
- PowerShell uses double quotes `"` for expandable strings and single quotes `'` for literal strings
- nested quoting can be tricky when calling external programs

Fix:
- for paths with spaces, use double quotes: `"C:\Program Files\app.exe"`
- when calling external tools, prefer `--%` (stop-parsing symbol) after the program name to pass raw arguments
- use single quotes only when variable expansion must be suppressed

Example:

```powershell
myapp.exe --% --arg="C:\Program Files\data"
```

> **Warning**: after `--%`, PowerShell expansion is disabled. `$env:VAR` references placed after `--%` will be passed literally, not expanded. Keep `$env:` variable references before `--%`.

### Execution Policy

Symptom:
- running `.ps1` scripts fails with an execution-policy error
- `Cannot run scripts because execution of scripts is disabled`

Cause:
- default PowerShell execution policy may be `Restricted` or `AllSigned`

Fix:
- set execution policy for the current process only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
- if blocked by machine-level GPO, launch with bypass inline:

```powershell
powershell -ExecutionPolicy Bypass -File script.ps1
```

### Line Endings (CRLF vs LF)

Symptom:
- scripts, here-strings (`@'...'@`), or piped output behave differently between Windows and Unix
- Git shows spurious `\r` diffs
- native Linux tools fail to parse PowerShell-generated files

Cause:
- Windows PowerShell defaults to CRLF (\r\n). Unix tools expect LF (\n).

Fix:
- configure Git: `git config --global core.autocrlf true`
- when a Unix tool must consume PowerShell-generated text, normalize line endings explicitly before writing:

```powershell
$text = $text -replace "`r`n", "`n"
```

- use `.editorconfig` with `end_of_line = lf` for cross-platform projects


## Operating Rules

- When chaining commands, never use `&&` or `||` in PowerShell 5.1.
- When running a command in a specific directory, prefer the `workdir` parameter over `cd`.
- When reading environment variables, use `$env:VAR`.
- When checking command success, use `$?` for boolean status and `$LASTEXITCODE` for numeric exit codes.
- When using common Unix commands, verify whether the PowerShell alias supports the flags you are using; prefer full cmdlet names for scripts.
- When passing paths with spaces, double-quote them.
- When a command behaves differently from bash, consider invoking `cmd /c "..."` for bash-style behavior, or split the work into multiple independent shell tool calls.
- When a shell command output looks wrong, verify whether the issue is PowerShell syntax, display encoding, or the command itself.

## Quick Checks

Verify PowerShell version and behavior:

```powershell
$PSVersionTable.PSVersion
```

Test whether `&&` is supported:

```powershell
# This will fail on PowerShell 5.1
echo "a" && echo "b"
```

Test command resolution (prefer Get-Command over Get-Alias for full parse chain):

```powershell
Get-Command rm
Get-Command cp
Get-Command curl
```

## Decision Pattern

If a PowerShell command fails:

1. Check whether `&&` or `||` was used.
2. Check whether a Unix-style flag was passed to a PowerShell alias.
3. Check whether the working directory was set correctly (prefer `workdir`).
4. Check whether environment variables use `$env:` syntax.
5. Check whether quoting or path escaping is correct.
6. Only then decide whether the command logic itself needs to change.
