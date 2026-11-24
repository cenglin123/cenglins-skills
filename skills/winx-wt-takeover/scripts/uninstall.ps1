<#
.SYNOPSIS
    Undo the Win+X -> Windows Terminal takeover.

.DESCRIPTION
    Restore order of preference:
      1. the pristine backup recorded at the first install (pristine.json),
      2. the most recent backup recorded in install-manifest.json,
      3. a freshly rebuilt stock shortcut pointing at the real powershell.exe.

    Option 3 exists so a machine can always be put back into a working state
    even if the backup folder was lost.

.PARAMETER StateDir
    Where install.ps1 kept its state. Default: %LOCALAPPDATA%\WinXWT

.PARAMETER RemoveLauncher
    Also delete the launcher directory (wrapper exe + backups).

.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -RemoveLauncher
#>
[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'WinXWT'),
    [switch]$RemoveLauncher,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn2{ param([string]$m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Bad  { param([string]$m) Write-Host "[-] $m" -ForegroundColor Red }

function Get-ConsoleHostShortcut {
    try {
        $v = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\PowerShell\3' -Name 'ConsoleHostShortcutTarget' -ErrorAction Stop).ConsoleHostShortcutTarget
        if ($v) {
            $p = [Environment]::ExpandEnvironmentVariables($v)
            if (Test-Path -LiteralPath $p) { return $p }
        }
    } catch { }
    return (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Windows PowerShell\Windows PowerShell.lnk')
}

function Get-LnkTarget {
    param([string]$Path)
    try {
        $sh = New-Object -ComObject WScript.Shell
        return $sh.CreateShortcut((Resolve-Path -LiteralPath $Path).Path).TargetPath
    } catch { return $null }
}

Write-Host ''
Write-Host '=== WinX -> Windows Terminal takeover (uninstall) ===' -ForegroundColor White

$lnk = Get-ConsoleHostShortcut
Write-Step "shortcut: $lnk"
Write-Step ("current target: {0}" -f (Get-LnkTarget $lnk))

# ---- pick a restore source ------------------------------------------------
$source = $null
$why    = $null

foreach ($file in @('pristine.json', 'install-manifest.json')) {
    $jsonPath = Join-Path $StateDir $file
    if (-not (Test-Path $jsonPath)) { continue }
    try { $m = Get-Content $jsonPath -Raw | ConvertFrom-Json } catch { continue }
    if ($m.backup -and (Test-Path -LiteralPath $m.backup)) {
        $source = $m.backup
        $why    = "$file (original target: $($m.originalTarget))"
        break
    }
}

if (-not $source) {
    Write-Warn2 'no usable backup found - rebuilding a stock shortcut instead'
    $stock = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path $stock)) { Write-Bad "system powershell.exe missing: $stock"; exit 1 }
    if ($DryRun) { Write-Warn2 "DryRun: would rebuild $lnk -> $stock"; exit 0 }
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($lnk)
    $sc.TargetPath       = $stock
    $sc.Arguments        = ''
    $sc.WorkingDirectory = '%HOMEDRIVE%%HOMEPATH%'
    $sc.Description      = 'Performs object-based (command-line) functions'
    $sc.IconLocation     = "$stock,0"
    $sc.Save()
} else {
    Write-Step "restore source: $why"
    if ($DryRun) { Write-Warn2 "DryRun: would copy $source -> $lnk"; exit 0 }
    try {
        Copy-Item -LiteralPath $source -Destination $lnk -Force
    } catch {
        Write-Bad ("cannot write the shortcut: {0}" -f $_.Exception.Message)
        Write-Warn2 'If it lives under %ProgramData% (all-users Start Menu), rerun this script elevated.'
        exit 1
    }
}

$now = Get-LnkTarget $lnk
if ($now -like "$env:SystemRoot*") {
    Write-Ok "restored; target is now: $now"
} else {
    Write-Bad "read-back says the target is still: $now"
}

if ($RemoveLauncher) {
    if (Test-Path $StateDir) {
        try {
            Remove-Item $StateDir -Recurse -Force
            Write-Ok "removed $StateDir"
        } catch {
            Write-Warn2 ("could not remove {0}: {1} (the wrapper may still be running)" -f $StateDir, $_.Exception.Message)
        }
    }
}

Write-Host ''
Write-Host 'Check with:  Win+X then I   (should open the legacy PowerShell window again,' -ForegroundColor White
Write-Host '             unless the console handoff to Windows Terminal actually works)' -ForegroundColor White
Write-Host ''
