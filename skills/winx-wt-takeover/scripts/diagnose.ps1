<#
.SYNOPSIS
    Read-only state report for the console handoff / Win+X takeover problem.

.DESCRIPTION
    Executes the layered isolation checks described in docs\troubleshooting.md.
    Nothing is modified. Every check states what it observed, not what it
    assumes - notably:

      * which terminal a NEW console actually lands in is measured by echoing
        %WT_SESSION% from a freshly started console, not by looking at windows;
      * system binaries are identified by SHA256, never by FileVersionInfo
        (which is path-cached and lies for System32 copies);
      * the parent process chain is printed so you know whether killing
        WindowsTerminal would kill your own session.

.PARAMETER OutFile
    Also write the report to this file. Default: %TEMP%\winxwt-report-<stamp>.txt

.PARAMETER SkipLiveTest
    Do not spawn the throwaway console used for the handoff measurement.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\diagnose.ps1
#>
[CmdletBinding()]
param(
    [string]$OutFile,
    [switch]$SkipLiveTest
)

$ErrorActionPreference = 'Continue'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $OutFile) { $OutFile = Join-Path $env:TEMP "winxwt-report-$stamp.txt" }

$lines = New-Object System.Collections.Generic.List[string]
# NOTE: do not name these H / L - `h` is a built-in alias for Get-History and
# aliases win over functions in PowerShell's command precedence.
function L { param([string]$m = '') $lines.Add($m); Write-Host $m }
function Write-Section { param([string]$m) L ''; L ("=== $m ===") }

Write-Section 'environment'
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
L ("host            : {0}  user: {1}" -f $env:COMPUTERNAME, $env:USERNAME)
L ("product         : {0}" -f $cv.ProductName)
L ("build           : {0}.{1}  (DisplayVersion {2})" -f $cv.CurrentBuild, $cv.UBR, $cv.DisplayVersion)
L ("powershell      : {0}" -f $PSVersionTable.PSVersion)

$build = [int]$cv.CurrentBuild
$ubr   = [int]$cv.UBR
if ($build -eq 19045) {
    if ($ubr -ge 3031) { L 'handoff support : Win10 22H2 build is new enough (>= 19045.3031)' }
    else               { L 'handoff support : TOO OLD - "default terminal" needs >= 19045.3031' }
} elseif ($build -ge 22000) {
    L 'handoff support : Windows 11 - supported'
} else {
    L 'handoff support : unknown/older build - "default terminal" may not exist here'
}

Write-Section 'windows terminal'
try {
    $pkgs = Get-AppxPackage -Name 'Microsoft.WindowsTerminal*' -ErrorAction Stop
    if ($pkgs) {
        foreach ($p in $pkgs) {
            L ("package         : {0} {1}" -f $p.Name, $p.Version)
            L ("  install path  : {0}" -f $p.InstallLocation)
            L ("  status        : {0}" -f $p.Status)
        }
        $v = [version](($pkgs | Select-Object -First 1).Version)
        if ($v -lt [version]'1.17.0.0') { L '  NOTE          : handoff needs Windows Terminal >= 1.17' }
    } else {
        L 'package         : NOT INSTALLED'
    }
} catch { L ("package         : query failed - {0}" -f $_.Exception.Message) }

$alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wt.exe'
L ("wt.exe alias    : {0}" -f $(if (Test-Path $alias) { $alias } else { 'MISSING' }))

Write-Section 'handoff registry (HKCU\Console\%%Startup)'
$known = @{
    '{2EACA947-7F5F-4CFA-BA87-8F7FBEEFBE69}' = 'Windows Terminal (OpenConsole)'
    '{E12CFF52-A866-4C77-9A90-F570A7AA2C6B}' = 'Windows Terminal (terminal)'
    '{00000000-0000-0000-0000-000000000000}' = 'let Windows decide (= legacy conhost on Win10)'
}
try {
    $st = Get-ItemProperty -LiteralPath 'HKCU:\Console\%%Startup' -ErrorAction Stop
    foreach ($name in @('DelegationConsole', 'DelegationTerminal')) {
        $val = $st.$name
        if ($null -eq $val) { L ("{0,-18}: <not set>" -f $name); continue }
        $desc = $known[$val.ToUpper()]
        if (-not $desc) { $desc = 'unknown GUID' }
        L ("{0,-18}: {1}  -> {2}" -f $name, $val, $desc)
    }
} catch {
    L 'key not present  : the machine has never had a non-default terminal selected'
}

Write-Section 'Win+X takeover state'
$reg = $null
try { $reg = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\PowerShell\3' -Name 'ConsoleHostShortcutTarget' -ErrorAction Stop).ConsoleHostShortcutTarget } catch { }
if ($reg) {
    $lnk = [Environment]::ExpandEnvironmentVariables($reg)
    L ("ConsoleHostShortcutTarget : {0}" -f $reg)
    L ("  resolved                : {0}  (exists: {1})" -f $lnk, (Test-Path -LiteralPath $lnk))
} else {
    $lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Windows PowerShell\Windows PowerShell.lnk'
    L 'ConsoleHostShortcutTarget : <not set> - using the documented fallback path'
    L ("  fallback                : {0}  (exists: {1})" -f $lnk, (Test-Path -LiteralPath $lnk))
}

$sh = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $lnk) {
    $t = $sh.CreateShortcut((Resolve-Path -LiteralPath $lnk).Path)
    L ("  target                  : {0}" -f $t.TargetPath)
    L ("  arguments               : [{0}]" -f $t.Arguments)
    if ($t.TargetPath -like "$env:SystemRoot*") { L '  state                   : STOCK (not taken over)' }
    else                                        { L '  state                   : TAKEN OVER by a custom launcher' }
}

$winx = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\WinX'
L ''
L ("WinX folder     : {0}" -f $winx)
if (Test-Path $winx) {
    Get-ChildItem $winx -Filter '*.lnk' -Recurse | ForEach-Object {
        $target = ''
        try { $target = $sh.CreateShortcut($_.FullName).TargetPath } catch { $target = '<unreadable>' }
        L ("  {0}\{1}  ->  {2}" -f $_.Directory.Name, $_.Name, $target)
    }
    L '  (hash validation of these .lnk files: winxhash.py verify - see the mechanism doc)'
}

Write-Section 'binaries (SHA256 - FileVersionInfo is unreliable for System32 copies)'
$bins = @((Join-Path $env:SystemRoot 'System32\conhost.exe'),
          (Join-Path $env:SystemRoot 'System32\OpenConsole.exe'))
try {
    $wtPkg = Get-AppxPackage -Name 'Microsoft.WindowsTerminal*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($wtPkg) { $bins += (Join-Path $wtPkg.InstallLocation 'OpenConsole.exe') }
} catch { }
foreach ($b in $bins) {
    if (Test-Path -LiteralPath $b) {
        L ("{0}`n  sha256 : {1}" -f $b, (Get-FileHash -LiteralPath $b -Algorithm SHA256).Hash)
    } else {
        L ("{0}`n  sha256 : <absent>" -f $b)
    }
}

Write-Section 'running processes / where am I'
L ("this process    : PID {0}  WT_SESSION={1}" -f $PID, $(if ($env:WT_SESSION) { $env:WT_SESSION } else { '<none - not inside Windows Terminal>' }))
try {
    $cur = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
    $chain = @()
    $guard = 0
    while ($cur -and $guard -lt 8) {
        $chain += ("{0}({1})" -f $cur.Name, $cur.ProcessId)
        if (-not $cur.ParentProcessId) { break }
        $cur = Get-CimInstance Win32_Process -Filter "ProcessId = $($cur.ParentProcessId)" -ErrorAction SilentlyContinue
        $guard++
    }
    L ("parent chain    : {0}" -f ($chain -join ' <- '))
    L '  NOTE: if WindowsTerminal appears above you, do NOT Stop-Process it - you would kill this session.'
} catch { }

foreach ($n in @('WindowsTerminal', 'OpenConsole', 'conhost')) {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name = '$n.exe'" -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) {
        L ("  {0,-18} <none running>" -f "$n.exe")
        continue
    }
    L ("  {0,-18} {1} running" -f "$n.exe", $procs.Count)
    # only the informative ones: -Embedding / --headless / --server reveal handoff attempts
    $show = $procs | Where-Object { $_.CommandLine } | Select-Object -First 4
    foreach ($p in $show) { L ("     PID {0,-7} {1}" -f $p.ProcessId, $p.CommandLine) }
}

Write-Section 'live handoff measurement'
if ($SkipLiveTest) {
    L 'skipped (-SkipLiveTest)'
} else {
    $probe = Join-Path $env:TEMP ("winxwt-probe-{0}.txt" -f [guid]::NewGuid().ToString('N'))
    L 'starting a throwaway console that echoes %WT_SESSION% ...'
    try {
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', "echo %WT_SESSION%>`"$probe`"") | Out-Null
        $deadline = (Get-Date).AddSeconds(10)
        while (-not (Test-Path $probe) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
        Start-Sleep -Milliseconds 300
        if (Test-Path $probe) {
            $raw = (Get-Content $probe -Raw).Trim()
            Remove-Item $probe -Force -ErrorAction SilentlyContinue
            if ($raw -eq '%WT_SESSION%' -or $raw -eq '') {
                L "result          : LEGACY conhost  (a new console does NOT hand off to Windows Terminal)"
            } else {
                L ("result          : Windows Terminal  (WT_SESSION={0})" -f $raw)
            }
        } else {
            L 'result          : probe produced no output (the console may have been blocked)'
        }
    } catch {
        L ("result          : probe failed - {0}" -f $_.Exception.Message)
    }
}

Write-Section 'reading the result'
L 'handoff works        -> no takeover needed; just set Windows Terminal as the default terminal.'
L 'handoff broken       -> install.ps1 gives you Win+X I/A in Windows Terminal without repairing handoff.'
L 'handoff broken AND   -> the machine is likely damaged more deeply (see the troubleshooting doc,'
L '  all parts look OK     "when to stop"): an in-place repair install beats further point fixes.'

$lines -join "`r`n" | Out-File -FilePath $OutFile -Encoding utf8
Write-Host ''
Write-Host "report written to: $OutFile" -ForegroundColor Green
