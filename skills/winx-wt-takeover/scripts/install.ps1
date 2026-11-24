<#
.SYNOPSIS
    Make the Win+X "Windows PowerShell (I / A)" entries open Windows Terminal.

.DESCRIPTION
    Minimal-invasive takeover: the Win+X menu does NOT launch the .lnk files in
    the WinX folder for these two entries - TWINUI launches the Start Menu
    shortcut named by

        HKLM\SOFTWARE\Microsoft\PowerShell\3 : ConsoleHostShortcutTarget

    Retargeting that one shortcut keeps the menu text, the I/A accelerators and
    the elevation behaviour of the "(Administrator)" entry, and requires no
    change to the hash-protected WinX folder.

    This script:
      1. locates the shortcut through the registry (with a documented fallback),
      2. backs it up (timestamped) together with a restore manifest,
      3. builds a tiny launcher named powershell.exe (src\wtlaunch.cs) that
         resolves wt.exe at runtime and falls back to the real powershell.exe,
      4. retargets the shortcut - binary in-place patch first (preserves the
         shortcut structure and its property store), automatic rebuild fallback,
      5. reads the result back and reports PASS/FAIL. Nothing is trusted blind.

.PARAMETER Method
    Auto (default) = try Patch, verify, fall back to Rebuild.
    Patch          = binary in-place patch only.
    Rebuild        = rewrite the shortcut with WScript.Shell (loses the
                     property store / AppUserModelID, keeps behaviour).

.PARAMETER LauncherDir
    Where powershell.exe (the wrapper) is written.
    Default: %LOCALAPPDATA%\WinXWT

.PARAMETER DryRun
    Do everything except writing the Start Menu shortcut.

.EXAMPLE
    .\install.ps1 -DryRun
    .\install.ps1
    .\install.ps1 -Method Rebuild -LauncherDir C:\Tools\WinXWT
#>
[CmdletBinding()]
param(
    [ValidateSet('Auto', 'Patch', 'Rebuild')]
    [string]$Method = 'Auto',
    [string]$LauncherDir = (Join-Path $env:LOCALAPPDATA 'WinXWT'),
    [string]$LauncherPath,
    [switch]$SkipBuild,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$script:Fail = $false

function Write-Step { param([string]$m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn2{ param([string]$m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Bad  { param([string]$m) Write-Host "[-] $m" -ForegroundColor Red; $script:Fail = $true }

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

Add-Type -Namespace WinXWT -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern uint GetShortPathName(string lpszLongPath, System.Text.StringBuilder lpszShortPath, uint cchBuffer);
'@ -ErrorAction SilentlyContinue

function Get-ShortPath {
    param([string]$Path)
    try {
        $sb = New-Object System.Text.StringBuilder 512
        $n = [WinXWT.Native]::GetShortPathName($Path, $sb, 512)
        if ($n -gt 0) { return $sb.ToString() }
    } catch { }
    return $Path
}

function Get-ConsoleHostShortcut {
    # authoritative source: the registry value TWINUI actually consults
    $key = 'HKLM:\SOFTWARE\Microsoft\PowerShell\3'
    $fromReg = $null
    try {
        $v = (Get-ItemProperty -Path $key -Name 'ConsoleHostShortcutTarget' -ErrorAction Stop).ConsoleHostShortcutTarget
        if ($v) { $fromReg = [Environment]::ExpandEnvironmentVariables($v) }
    } catch { }

    $fallback = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Windows PowerShell\Windows PowerShell.lnk'

    if ($fromReg -and (Test-Path -LiteralPath $fromReg)) {
        return [pscustomobject]@{ Path = $fromReg; Source = 'registry' }
    }
    if ($fromReg) {
        Write-Warn2 "registry points to a missing file: $fromReg"
    } else {
        Write-Warn2 'ConsoleHostShortcutTarget not found in registry (non-standard build?)'
    }
    return [pscustomobject]@{ Path = $fallback; Source = 'fallback' }
}

function Get-LnkTarget {
    param([string]$Path)
    try {
        $sh = New-Object -ComObject WScript.Shell
        $sc = $sh.CreateShortcut((Resolve-Path -LiteralPath $Path).Path)
        return [pscustomobject]@{
            TargetPath       = $sc.TargetPath
            Arguments        = $sc.Arguments
            WorkingDirectory = $sc.WorkingDirectory
            Description      = $sc.Description
            IconLocation     = $sc.IconLocation
            WindowStyle      = $sc.WindowStyle
        }
    } catch {
        return $null
    }
}

function Find-ByteRuns {
    param([byte[]]$Haystack, [byte[]]$Needle)
    $hits = New-Object System.Collections.Generic.List[int]
    if ($Needle.Length -eq 0 -or $Haystack.Length -lt $Needle.Length) { return $hits }
    $last = $Haystack.Length - $Needle.Length
    for ($i = 0; $i -le $last; $i++) {
        $match = $true
        for ($j = 0; $j -lt $Needle.Length; $j++) {
            if ($Haystack[$i + $j] -ne $Needle[$j]) { $match = $false; break }
        }
        if ($match) { $hits.Add($i) }
    }
    return $hits
}

function Invoke-LnkBinaryPatch {
    <#
      In-place replacement of the embedded target string, NUL-padded so every
      offset in the file stays valid (the strings live in fixed-size, NUL-
      terminated fields). Returns the patched byte array plus a replacement
      count; 0 means "nothing matched - do not use this result".
    #>
    param([byte[]]$Data, [string[]]$OldPaths, [string]$NewPath)

    $ansi = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
    $encodings = @([Text.Encoding]::Unicode, $ansi)
    $count = 0

    foreach ($enc in $encodings) {
        $newB = $enc.GetBytes($NewPath)
        foreach ($old in $OldPaths) {
            if (-not $old) { continue }
            $oldB = $enc.GetBytes($old)
            if ($newB.Length -gt $oldB.Length) { continue }   # would not fit
            $hits = Find-ByteRuns -Haystack $Data -Needle $oldB
            foreach ($at in $hits) {
                for ($k = 0; $k -lt $oldB.Length; $k++) {
                    if ($k -lt $newB.Length) { $Data[$at + $k] = $newB[$k] }
                    else                     { $Data[$at + $k] = 0 }
                }
                $count++
            }
        }
    }
    return [pscustomobject]@{ Data = $Data; Count = $count }
}

function New-RebuiltShortcut {
    param([string]$TemplatePath, [string]$OutPath, [string]$Target)
    $orig = Get-LnkTarget -Path $TemplatePath
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($OutPath)
    $sc.TargetPath = $Target
    if ($orig) {
        $desc = $orig.Description
        if (-not $desc) { $desc = 'Windows PowerShell' }
        $icon = $orig.IconLocation
        if (-not $icon -or $icon -eq ',0') {
            $icon = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') + ',0'
        }
        $sc.Arguments        = $orig.Arguments
        $sc.WorkingDirectory = $orig.WorkingDirectory
        $sc.Description      = $desc
        $sc.IconLocation     = $icon
        $sc.WindowStyle      = $orig.WindowStyle
    }
    $sc.Save()
}

# --------------------------------------------------------------------------
# 0. preflight
# --------------------------------------------------------------------------

Write-Host ''
Write-Host '=== WinX -> Windows Terminal takeover (install) ===' -ForegroundColor White
Write-Step ("host {0} / user {1} / OS build {2}" -f $env:COMPUTERNAME, $env:USERNAME,
    (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuild)

$wtAlias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wt.exe'
if (Test-Path $wtAlias) {
    Write-Ok "wt.exe alias present: $wtAlias"
} else {
    Write-Warn2 'wt.exe alias not found - install Windows Terminal first, or the launcher will fall back to the legacy console.'
}

# --------------------------------------------------------------------------
# 1. locate the shortcut TWINUI launches
# --------------------------------------------------------------------------

$sc = Get-ConsoleHostShortcut
Write-Step ("shortcut ({0}): {1}" -f $sc.Source, $sc.Path)
if (-not (Test-Path -LiteralPath $sc.Path)) {
    Write-Bad 'shortcut file does not exist - nothing to retarget. Aborting.'
    exit 1
}

$before = Get-LnkTarget -Path $sc.Path
if (-not $before) {
    Write-Bad 'cannot read the shortcut (WScript.Shell failed) - aborting.'
    exit 1
}
Write-Step ("current target: {0}" -f $before.TargetPath)

$isPristine = ($before.TargetPath -like "$env:SystemRoot*")
if (-not $isPristine) {
    Write-Warn2 'the shortcut already points outside System32 - it looks retargeted already. Rerunning is safe: the first pristine backup is kept separately for uninstall.'
}

# --------------------------------------------------------------------------
# 2. backup
# --------------------------------------------------------------------------

$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
$stateDir = $LauncherDir
$backupDir = Join-Path $stateDir "backup\$stamp"
if (-not $DryRun) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }
$backupLnk = Join-Path $backupDir (Split-Path $sc.Path -Leaf)
if (-not $DryRun) {
    Copy-Item -LiteralPath $sc.Path -Destination $backupLnk -Force
    $manifest = [pscustomobject]@{
        installedAt    = (Get-Date).ToString('o')
        computer       = $env:COMPUTERNAME
        user           = $env:USERNAME
        shortcutPath   = $sc.Path
        shortcutSource = $sc.Source
        originalTarget = $before.TargetPath
        pristine       = [bool]$isPristine
        backup         = $backupLnk
        backupSha256   = (Get-FileHash $backupLnk -Algorithm SHA256).Hash
    }
    $manifest | ConvertTo-Json | Out-File (Join-Path $stateDir 'install-manifest.json') -Encoding utf8
    # the pristine copy is written once and never overwritten -> uninstall always
    # has a stock shortcut to restore, no matter how often install is rerun
    $pristineJson = Join-Path $stateDir 'pristine.json'
    if ($isPristine -and -not (Test-Path $pristineJson)) {
        $manifest | ConvertTo-Json | Out-File $pristineJson -Encoding utf8
        Write-Ok 'recorded this backup as the pristine (stock) shortcut'
    }
    Write-Ok "backup: $backupLnk"
} else {
    Write-Warn2 "DryRun: would back up to $backupLnk"
}

# --------------------------------------------------------------------------
# 3. launcher
# --------------------------------------------------------------------------

if (-not $LauncherPath) { $LauncherPath = Join-Path $LauncherDir 'powershell.exe' }

if ($SkipBuild -or (Test-Path $LauncherPath)) {
    if (Test-Path $LauncherPath) { Write-Ok "launcher present: $LauncherPath" }
    else { Write-Bad "launcher missing and -SkipBuild given: $LauncherPath"; exit 1 }
} else {
    Write-Step 'building launcher (csc.exe)...'
    & (Join-Path $PSScriptRoot 'build-launcher.ps1') -OutDir (Split-Path $LauncherPath -Parent) | Out-Null
    if (-not (Test-Path $LauncherPath)) { Write-Bad 'build failed'; exit 1 }
    Write-Ok "launcher built: $LauncherPath"
}

# --------------------------------------------------------------------------
# 4. retarget (patch -> verify -> rebuild fallback)
# --------------------------------------------------------------------------

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("winxwt-$stamp.lnk")
$applied = $null

function Test-Retarget {
    param([string]$LnkPath, [string]$Expect)
    $t = Get-LnkTarget -Path $LnkPath
    if (-not $t) { return $false }
    $a = $t.TargetPath.TrimEnd('\')
    $b = $Expect.TrimEnd('\')
    if ($a -ieq $b) { return $true }
    # a short (8.3) path resolves to the same file
    try { return ((Resolve-Path -LiteralPath $a -ErrorAction Stop).Path -ieq (Resolve-Path -LiteralPath $b).Path) }
    catch { return $false }
}

if ($Method -in @('Auto', 'Patch')) {
    Write-Step 'method: binary in-place patch'
    $oldCandidates = @(
        $before.TargetPath,
        '%windir%\system32\WindowsPowerShell\v1.0\powershell.exe',
        '%SystemRoot%\system32\WindowsPowerShell\v1.0\powershell.exe'
    ) | Where-Object { $_ } | Select-Object -Unique

    # the replacement must fit inside the original string; try the 8.3 form if not
    $newCandidates = @($LauncherPath, (Get-ShortPath $LauncherPath)) | Select-Object -Unique
    $bytes = [IO.File]::ReadAllBytes($sc.Path)

    foreach ($cand in $newCandidates) {
        $res = Invoke-LnkBinaryPatch -Data ([byte[]]$bytes.Clone()) -OldPaths $oldCandidates -NewPath $cand
        if ($res.Count -eq 0) { Write-Warn2 ("no string match for target form: {0}" -f $cand); continue }
        [IO.File]::WriteAllBytes($tmp, $res.Data)
        if (Test-Retarget -LnkPath $tmp -Expect $cand) {
            Write-Ok ("patch OK ({0} replacement(s), target form: {1})" -f $res.Count, $cand)
            $applied = 'Patch'
            break
        }
        Write-Warn2 ("patched {0} string(s) but the shell still resolves the old target (form: {1})" -f $res.Count, $cand)
    }
}

if (-not $applied -and $Method -in @('Auto', 'Rebuild')) {
    Write-Step 'method: rebuild shortcut (WScript.Shell)'
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
    New-RebuiltShortcut -TemplatePath $sc.Path -OutPath $tmp -Target $LauncherPath
    if (Test-Retarget -LnkPath $tmp -Expect $LauncherPath) {
        Write-Ok 'rebuild OK'
        $applied = 'Rebuild'
    } else {
        Write-Bad 'rebuild produced a shortcut that does not point at the launcher'
    }
}

if (-not $applied) {
    Write-Bad 'no method succeeded - the Start Menu shortcut was NOT modified.'
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
    exit 1
}

# --------------------------------------------------------------------------
# 5. install + read-back verification
# --------------------------------------------------------------------------

if ($DryRun) {
    Write-Warn2 "DryRun: prepared shortcut left at $tmp (nothing installed)"
    exit 0
}

try {
    Copy-Item -LiteralPath $tmp -Destination $sc.Path -Force
} catch {
    Write-Bad ("cannot write the shortcut: {0}" -f $_.Exception.Message)
    Write-Warn2 'If it lives under %ProgramData% (all-users Start Menu), rerun this script elevated.'
    exit 1
}
Remove-Item $tmp -Force -ErrorAction SilentlyContinue

$after = Get-LnkTarget -Path $sc.Path
if (Test-Retarget -LnkPath $sc.Path -Expect $LauncherPath) {
    Write-Ok ("installed via {0}; target is now: {1}" -f $applied, $after.TargetPath)
} else {
    Write-Bad ("read-back mismatch - target is: {0}" -f $after.TargetPath)
}

$testScript = @((Join-Path $PSScriptRoot 'tools\test-winx-key.ps1'), (Join-Path $PSScriptRoot 'test-winx-key.ps1')) |
              Where-Object { Test-Path $_ } | Select-Object -First 1

Write-Host ''
Write-Host 'Verify:' -ForegroundColor White
Write-Host '  1. Press Win+X then I  -> Windows Terminal should open.'
Write-Host '  2. Press Win+X then A  -> Windows Terminal, elevated (title shows Administrator).'
if ($testScript) {
    Write-Host ("  3. Unattended check:  powershell -ExecutionPolicy Bypass -File `"{0}`" -Key I" -f $testScript)
}
Write-Host ("  4. Full state report: powershell -ExecutionPolicy Bypass -File `"{0}`"" -f (Join-Path $PSScriptRoot 'diagnose.ps1'))
Write-Host ''
Write-Host ("Undo:  powershell -ExecutionPolicy Bypass -File `"{0}`"" -f (Join-Path $PSScriptRoot 'uninstall.ps1')) -ForegroundColor White
Write-Host ''

if ($script:Fail) { exit 1 } else { exit 0 }
