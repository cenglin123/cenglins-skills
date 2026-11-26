# Exact targets. Reboot deletion is opt-in and backed up. Works without unprotect.ps1:
# tasks, autostart values and processes are handled here too, so skipping the
# self-protection stage cannot leave persistence behind.
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
function Write-DeletionInventory($Tree, [string]$Target) {
    # Paths/sizes/times only. Files are not backed up automatically; this list is
    # the record of what was removed and is not a restore source.
    $file = Join-Path $script:RunDir 'deletion-inventory.txt'
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('# target: ' + $Target)
    $count = 0
    foreach ($item in $Tree) {
        if ($count -ge 50000) { [void]$sb.AppendLine('# truncated at 50000 entries'); break }
        $length = if ($item.PSIsContainer) { 0 } else { $item.Length }
        $kind = if ($item.PSIsContainer) { 'd' } else { 'f' }
        [void]$sb.AppendLine($kind + '|' + $length + '|' + $item.LastWriteTimeUtc.ToString('o') + '|' + $item.FullName)
        $count++
    }
    [IO.File]::AppendAllText($file, $sb.ToString(), (New-Object Text.UTF8Encoding $false))
}
$pendingCandidates = New-Object 'System.Collections.Generic.List[string]'
$registeredPending = 0
foreach ($entry in @(Get-Field $plan 'tasks' @())) {
    Invoke-Checked "task:$($entry.path)$($entry.name)" { Remove-PlannedTask $entry $DryRun }
}
foreach ($entry in @(Get-Field $plan 'registryValues' @())) {
    Invoke-Checked "registry-value:$($entry.key)::$($entry.name)" { Remove-PlannedValue $entry $DryRun }
}
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
        Write-DeletionInventory $tree $path
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
                $pendingCandidates.Add($item.FullName)
                Write-Host "[locked] $($item.FullName): $($_.Exception.Message)"
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
if ($pendingCandidates.Count) {
    # PENDING only after the rename list is written and read back. An unregistered
    # failure stays ERROR so a reboot is not requested for an operation that does
    # not exist.
    try {
        $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
        Backup-Reg $key
        $item = Get-Item -LiteralPath $key
        $old = @(); if ('PendingFileRenameOperations' -in $item.GetValueNames()) { $old = @($item.GetValue('PendingFileRenameOperations')) }
        if ($old.Count % 2) { throw 'Existing rename list malformed; manual review required' }
        $append = New-Object 'System.Collections.Generic.List[string]'
        foreach ($candidate in $pendingCandidates) { $append.Add('\??\' + $candidate); $append.Add('') }
        $all = [string[]](@($old) + @($append.ToArray()))
        Mark-Mutation
        New-ItemProperty -LiteralPath $key -Name PendingFileRenameOperations -Value $all -PropertyType MultiString -Force | Out-Null
        $actual = @((Get-Item -LiteralPath $key).GetValue('PendingFileRenameOperations'))
        if (($actual -join "`0") -cne ($all -join "`0")) { throw 'Pending rename write verification failed' }
        foreach ($candidate in $pendingCandidates) { Add-Result $candidate 'PENDING' 'Scheduled for deletion after reboot' }
        $registeredPending = $pendingCandidates.Count
    } catch {
        $message = 'Pending deletion registration failed: ' + $_.Exception.Message
        foreach ($candidate in $pendingCandidates) { Add-Result $candidate 'ERROR' $message }
    }
}
exit (Finish-CaseRun 'cleanup' ([bool]$DryRun) ($registeredPending -gt 0))
