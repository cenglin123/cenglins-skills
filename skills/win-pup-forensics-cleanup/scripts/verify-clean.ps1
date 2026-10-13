# Read-only scope verification. Exit 0=covered/clean, 1=FAIL/ERROR, 2=SKIP.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$CaseDir,
    [ValidateSet('baseline','final')][string]$Phase = 'final'
)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    Assert-Admin
    $plan = Read-Plan $Config
    Assert-CaseLocation $CaseDir $plan
    [void](Start-CaseRun $CaseDir 'verify' $Config)
    $script:VerificationPhase = $Phase
} catch { Write-Error $_; exit 1 }
function Check-Surface([string]$Name, [scriptblock]$Query) {
    try {
        $hits = @(& $Query)
        if ($hits.Count) { Add-Result $Name 'FAIL' ($hits -join '; ') }
        else { Add-Result $Name 'PASS' }
    } catch { Add-Result $Name 'ERROR' $_.Exception.Message }
}
foreach ($entry in @(Get-Field $plan 'paths' @())) {
    Check-Surface "path:$($entry.path)" {
        $path = Resolve-TargetPath $entry.path
        if (Test-Path -LiteralPath $path) { $path }
    }
}
foreach ($key in @(Get-Field $plan 'registryKeys' @())) {
    Check-Surface "registry-key:$key" { if (Test-Path -LiteralPath $key) { $key } }
}
foreach ($entry in @(Get-Field $plan 'registryValues' @())) {
    Check-Surface "registry-value:$($entry.key)::$($entry.name)" {
        if ((Test-Path -LiteralPath $entry.key) -and $entry.name -in (Get-Item -LiteralPath $entry.key).GetValueNames()) { $entry.name }
    }
}
foreach ($entry in @(Get-Field $plan 'services' @()) + @(Get-Field $plan 'drivers' @())) {
    Check-Surface "service/driver:$($entry.name)" {
        $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$($entry.name)"
        if (Test-Path -LiteralPath $key) { $key }
        # A deleted registration does not prove an in-memory driver is unloaded.
        foreach ($className in @('Win32_Service','Win32_SystemDriver')) {
            Get-CimInstance $className | Where-Object { $_.Name -eq $entry.name } |
                ForEach-Object { "$className $($_.Name) $($_.State)" }
        }
    }
}
foreach ($entry in @(Get-Field $plan 'tasks' @())) {
    Check-Surface "task:$($entry.path)$($entry.name)" {
        Get-ScheduledTask | Where-Object { $_.TaskPath -eq $entry.path -and $_.TaskName -eq $entry.name } |
            ForEach-Object { "$($_.TaskPath)$($_.TaskName)" }
    }
}
foreach ($entry in @(Get-Field $plan 'processes' @())) {
    Check-Surface "process:$($entry.path)" {
        Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq (Resolve-TargetPath $entry.path) } |
            ForEach-Object { "$($_.Name):$($_.ProcessId)" }
    }
}
foreach ($entry in @(Get-Field $plan 'svchostGroups' @())) {
    Check-Surface "svchost:$($entry.name)" {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Svchost'
        if ($entry.name -in (Get-Item -LiteralPath $key).GetValueNames()) { $entry.name }
        if (Test-Path -LiteralPath "$key\$($entry.name)") { "$key\$($entry.name)" }
    }
}
Check-Surface 'pending-reboot-deletions' {
    $key = Get-Item -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
    if ('PendingFileRenameOperations' -in $key.GetValueNames()) {
        $pending = @($key.GetValue('PendingFileRenameOperations'))
        if ($pending.Count % 2) { throw 'Malformed pending rename list' }
        foreach ($entry in @(Get-Field $plan 'paths' @())) {
            $path = Resolve-TargetPath $entry.path
            foreach ($p in $pending) {
                $candidate = $p -replace '^\\\?\?\\','' -replace '^!',''
                if ($candidate -eq $path -or (Test-Within $candidate $path)) { $candidate }
            }
        }
    }
}
$pattern = Get-Field $plan 'pupPattern' ''
if ($pattern) {
    Check-Surface 'scan:uninstall' {
        foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')) {
            if (Test-Path -LiteralPath $root) {
                Get-ChildItem -LiteralPath $root | ForEach-Object {
                    $item = Get-ItemProperty -LiteralPath $_.PSPath
                    $text = "$(Get-Field $item 'DisplayName' '') $(Get-Field $item 'InstallLocation' '') $(Get-Field $item 'UninstallString' '')"
                    if ($text -match $pattern) { $text }
                }
            }
        }
    }
    Check-Surface 'scan:directories' {
        foreach ($root in @($env:APPDATA,$env:LOCALAPPDATA,$env:ProgramData,$env:ProgramFiles,${env:ProgramFiles(x86)}) | Where-Object { $_ }) {
            if (Test-Path -LiteralPath $root) {
                Get-ChildItem -LiteralPath $root -Directory -Force | Where-Object { $_.Name -match $pattern } | ForEach-Object FullName
            }
        }
    }
    Check-Surface 'scan:processes' {
        Get-CimInstance Win32_Process | Where-Object { "$($_.Name) $($_.ExecutablePath) $($_.CommandLine)" -match $pattern } |
            ForEach-Object { "$($_.Name):$($_.ProcessId)" }
    }
    Check-Surface 'scan:tasks' {
        Get-ScheduledTask | Where-Object { (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ') -match $pattern } |
            ForEach-Object { "$($_.TaskPath)$($_.TaskName)" }
    }
    foreach ($class in @('Win32_Service','Win32_SystemDriver')) {
        Check-Surface "scan:$class" {
            Get-CimInstance $class | Where-Object { "$($_.Name) $($_.DisplayName) $($_.PathName)" -match $pattern } |
                ForEach-Object { "$($_.Name) $($_.PathName)" }
        }
    }
    Check-Surface 'scan:loaded-user-run' {
        $bases = @('HKLM:\SOFTWARE','HKLM:\SOFTWARE\WOW6432Node')
        $bases += @(Get-ChildItem 'Registry::HKEY_USERS' | Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' } |
            ForEach-Object { "Registry::HKEY_USERS\$($_.PSChildName)\SOFTWARE" })
        foreach ($base in $bases) {
            foreach ($rel in @('Microsoft\Windows\CurrentVersion\Run','Microsoft\Windows\CurrentVersion\RunOnce',
                'Microsoft\Windows\CurrentVersion\Policies\Explorer\Run')) {
                $key = "$base\$rel"
                if (Test-Path -LiteralPath $key) {
                    $item = Get-Item -LiteralPath $key
                    foreach ($name in $item.GetValueNames()) {
                        if ([string]$item.GetValue($name) -match $pattern) { "${key}::$name" }
                    }
                }
            }
        }
    }
    Check-Surface 'scan:shortcuts' {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($root in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('CommonDesktopDirectory'),
            [Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('CommonPrograms'))) {
            if ($root -and (Test-Path -LiteralPath $root)) {
                Get-ChildItem -LiteralPath $root -Recurse -Filter '*.lnk' | ForEach-Object {
                    $link = $shell.CreateShortcut($_.FullName)
                    if ("$($link.TargetPath) $($link.Arguments)" -match $pattern) { $_.FullName }
                }
            }
        }
    }
} else { Add-Result 'discovery-sweep' 'SKIP' 'No pupPattern supplied; exact targets checked only' }
# These surfaces require current-phase evidence; stale records never become PASS.
$threshold = $null
try { $threshold = Get-ReviewThreshold $plan $CaseDir $Phase }
catch { Add-Result 'review-context' 'SKIP' $_.Exception.Message }
foreach ($name in @('browser-policies-extensions','shell-com-bho','injection-wmi-winsock','other-user-profiles','post-reboot-observation','preserved-software')) {
    $review = @(Get-Field $plan 'manualChecks' @() | Where-Object { (Get-Field $_ 'name' '') -eq $name })
    try {
        if ($review.Count -ne 1 -or $null -eq $threshold) { throw 'Current evidence-backed manual review required' }
        Assert-CurrentReview $review[0] $plan.reviewContext $Phase $threshold $CaseDir
        Add-Result "review:$name" $review[0].status ($review[0].evidence + ' @ ' + (Parse-ReviewTime $review[0].reviewedAt).ToString('o'))
    } catch { Add-Result "review:$name" 'SKIP' $_.Exception.Message }
}
$code = Finish-CaseRun 'verify'
if ($code -eq 0) { Write-Host 'No residue found in the declared and reviewed scope.' }
else { Write-Host 'Verification incomplete; see FAIL/ERROR/SKIP results.' }
exit $code
