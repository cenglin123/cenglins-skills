# Exact approved targets only. Preview first with -DryRun.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$CaseDir,
    [string]$ApprovedPlanHash = '',
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    Assert-Admin
    $plan = Read-Plan $Config
    Assert-CaseLocation $CaseDir $plan
    $hash = $script:PlanHash
    if (-not $DryRun -and $ApprovedPlanHash -ne $hash) { throw 'Supply SHA256 of the reviewed plan with -ApprovedPlanHash' }
    [void](Start-CaseRun $CaseDir 'unprotect' $Config)
    Write-Host "Plan SHA256: $hash"
} catch { Write-Error $_; exit 1 }
$reboot = $false
foreach ($entry in @(Get-Field $plan 'tasks' @())) {
    Invoke-Checked "task:$($entry.path)$($entry.name)" { Remove-PlannedTask $entry $DryRun }
}
foreach ($entry in @(Get-Field $plan 'services' @()) + @(Get-Field $plan 'drivers' @())) {
    Invoke-Checked "service/driver:$($entry.name)" {
        $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$($entry.name)"
        if (-not (Test-Path -LiteralPath $key)) { Write-Host 'Already absent'; return }
        $actual = [string](Get-Item -LiteralPath $key).GetValue('ImagePath', $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($actual -cne $entry.expectedPath) { throw "ImagePath changed: $actual" }
        if ($DryRun) { Write-Host "[would] disable/stop/delete $($entry.name) $actual"; return }
        Backup-Reg $key
        Mark-Mutation
        Invoke-Sc @('config', $entry.name, 'start=', 'disabled')
        Invoke-Sc @('stop', $entry.name) @(0,1052,1062)
        Invoke-Sc @('delete', $entry.name)
        $script:reboot = $true
    }
}
foreach ($entry in @(Get-Field $plan 'svchostGroups' @())) {
    Invoke-Checked "svchost:$($entry.name)" {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Svchost'
        $item = Get-Item -LiteralPath $key
        if ($entry.name -notin $item.GetValueNames()) { Write-Host 'Already absent'; return }
        $members = @($item.GetValue($entry.name))
        if (@(Compare-Object ($members | Sort-Object) (@($entry.expectedMembers) | Sort-Object)).Count) {
            throw 'Svchost members differ from approved plan'
        }
        foreach ($member in $members) {
            $serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$member"
            if (-not $DryRun -and (Test-Path -LiteralPath $serviceKey) -and
                (Get-ItemPropertyValue -LiteralPath $serviceKey -Name Start) -ne 4) { throw "Group member still enabled: $member" }
        }
        if ($DryRun) { Write-Host "[would] remove svchost value $($entry.name)"; return }
        Backup-Reg $key
        Mark-Mutation
        Remove-ItemProperty -LiteralPath $key -Name $entry.name
        if ($entry.name -in (Get-Item -LiteralPath $key).GetValueNames()) { throw 'Svchost value remains' }
    }
}
foreach ($entry in @(Get-Field $plan 'registryValues' @())) {
    Invoke-Checked "registry-value:$($entry.key)::$($entry.name)" { Remove-PlannedValue $entry $DryRun }
}
foreach ($entry in @(Get-Field $plan 'processes' @())) {
    Invoke-Checked "process:$($entry.path)" { Stop-PlannedProcess $entry $DryRun }
}
exit (Finish-CaseRun 'unprotect' ([bool]$DryRun) $reboot)
