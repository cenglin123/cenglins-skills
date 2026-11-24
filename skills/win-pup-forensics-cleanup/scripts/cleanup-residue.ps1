# Exact targets. Reboot deletion is opt-in and backed up.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$CaseDir,
    [string]$ApprovedPlanHash = '',
    [switch]$ScheduleLockedFiles,
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    Assert-Admin
    $plan = Read-Plan $Config
    Assert-CaseLocation $CaseDir $plan
    $hash = $script:PlanHash
    if (-not $DryRun -and $ApprovedPlanHash -ne $hash) { throw 'Supply SHA256 of the reviewed plan with -ApprovedPlanHash' }
    [void](Start-CaseRun $CaseDir 'cleanup' $Config)
    Write-Host "Plan SHA256: $hash"
} catch { Write-Error $_; exit 1 }
$pending = New-Object 'System.Collections.Generic.List[string]'
foreach ($entry in @(Get-Field $plan 'processes' @())) {
    Invoke-Checked "process:$($entry.path)" { Stop-PlannedProcess $entry $DryRun }
}
foreach ($entry in @(Get-Field $plan 'paths' @())) {
    Invoke-Checked "path:$($entry.path)" {
        $path = Assert-SafeTarget $entry.path @(Get-Field $plan 'allowedRoots' @())
        if (-not (Test-Path -LiteralPath $path)) { Write-Host 'Already absent'; return }
        $tree = @(Get-SafeTree $path)
        if (($entry.kind -eq 'directory') -ne [bool]$tree[0].PSIsContainer) { throw 'Path kind changed' }
        if ($entry.kind -eq 'shortcut') {
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut($path)
            if ("$($link.TargetPath) $($link.Arguments)" -cne (Get-Field $entry 'expectedTarget' '')) { throw 'Shortcut target changed' }
        }
        if ($DryRun) { Write-Host "[would] delete $path ($($tree.Count) items)"; return }
        [void](Assert-SafeTarget $path @(Get-Field $plan 'allowedRoots' @()))
        $ordered = @($tree | Where-Object { -not $_.PSIsContainer }) +
            @($tree | Where-Object { $_.PSIsContainer } | Sort-Object { $_.FullName.Length } -Descending)
        foreach ($item in $ordered) {
            [void](Assert-SafeTarget $item.FullName @(Get-Field $plan 'allowedRoots' @()))
            try {
                Mark-Mutation
                if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName, $false) }
                else { Remove-Item -LiteralPath $item.FullName -Force }
                if (Test-Path -LiteralPath $item.FullName) { throw 'Target remains' }
            } catch {
                if (-not $ScheduleLockedFiles) { throw }
                [void](Assert-SafeTarget $item.FullName @(Get-Field $plan 'allowedRoots' @()))
                $pending.Add('\??\' + $item.FullName); $pending.Add('')
                Add-Result $item.FullName 'PENDING' $_.Exception.Message
            }
        }
    }
}
foreach ($key in @(Get-Field $plan 'registryKeys' @())) {
    Invoke-Checked "registry-key:$key" {
        Assert-RegKey $key
        if (-not (Test-Path -LiteralPath $key)) { Write-Host 'Already absent'; return }
        if ($DryRun) { Write-Host "[would] delete registry key $key"; return }
        Backup-Reg $key
        Mark-Mutation
        Remove-Item -LiteralPath $key -Recurse -Force
        if (Test-Path -LiteralPath $key) { throw 'Registry key remains' }
    }
}
foreach ($entry in @(Get-Field $plan 'registryValues' @())) {
    Invoke-Checked "registry-value:$($entry.key)::$($entry.name)" { Remove-PlannedValue $entry $DryRun }
}
if ($pending.Count) {
    Invoke-Checked 'PendingFileRenameOperations' {
        $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
        Backup-Reg $key
        $item = Get-Item -LiteralPath $key
        $old = @(); if ('PendingFileRenameOperations' -in $item.GetValueNames()) { $old = @($item.GetValue('PendingFileRenameOperations')) }
        if ($old.Count % 2) { throw 'Existing rename list malformed; manual review required' }
        $all = [string[]](@($old) + @($pending.ToArray()))
        Mark-Mutation
        New-ItemProperty -LiteralPath $key -Name PendingFileRenameOperations -Value $all -PropertyType MultiString -Force | Out-Null
        $actual = @((Get-Item -LiteralPath $key).GetValue('PendingFileRenameOperations'))
        if (($actual -join "`0") -cne ($all -join "`0")) { throw 'Pending rename write verification failed' }
    }
}
exit (Finish-CaseRun 'cleanup' ([bool]$DryRun) ($pending.Count -gt 0))
