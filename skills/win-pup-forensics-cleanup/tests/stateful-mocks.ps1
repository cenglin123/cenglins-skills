# Appended ONLY to fixture copies of common.ps1, never production files.
# Every system interface below is simulated; file IO is confined to the fixture root.
$global:PupMockState = [IO.File]::ReadAllText($env:PUP_STATE_PATH) | ConvertFrom-Json
function Assert-MockPath([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($env:PUP_STATE_ROOT+'\',[StringComparison]::OrdinalIgnoreCase) -and $full -ne $env:PUP_STATE_ROOT) {
        throw "Fixture IO escaped root: $full"
    }
    return $full
}
function Save-MockState([string]$Event) {
    $global:PupMockState.events=@($global:PupMockState.events)+@($Event)
    [IO.File]::WriteAllText($env:PUP_STATE_PATH,($global:PupMockState|ConvertTo-Json -Depth 20),[Text.Encoding]::UTF8)
}
function Mock-Key([string]$Path) {
    return @($global:PupMockState.registry | Where-Object { $_.path -eq $Path }) | Select-Object -First 1
}
function Assert-Admin { }
function Test-Path {
    param([string]$LiteralPath,[string]$Path)
    $p=if($LiteralPath){$LiteralPath}else{$Path}
    if ($p -match '^(HKLM|HKCU|Registry)::?') { return $null -ne (Mock-Key $p) }
    $full=[IO.Path]::GetFullPath($p)
    if ($full -eq $env:PUP_STATE_ROOT -or $full.StartsWith($env:PUP_STATE_ROOT+'\',[StringComparison]::OrdinalIgnoreCase)) {
        return [IO.File]::Exists($full) -or [IO.Directory]::Exists($full)
    }
    return $false
}
function New-MockKey([string]$Path) {
    if ($null -eq (Mock-Key $Path)) { throw "Fixture key missing: $Path" }
    $object=[pscustomobject]@{MockPath=$Path}
    $object | Add-Member ScriptMethod GetValueNames {
        $key=Mock-Key $this.MockPath
        return @($key.values | ForEach-Object { $_.name })
    }
    $object | Add-Member ScriptMethod GetValue {
        param($name,$default=$null,$options=$null)
        $key=Mock-Key $this.MockPath
        $value=@($key.values | Where-Object { $_.name -eq $name })
        if ($value.Count) { return $value[0].data }
        return $default
    }
    $object | Add-Member ScriptMethod DeleteValue {
        param($name,$missingIsError)
        $key=Mock-Key $this.MockPath
        $key.values=@($key.values | Where-Object { $_.name -ne $name })
        Save-MockState "value-delete:$($this.MockPath)::$name"
    }
    $object | Add-Member ScriptMethod Dispose { }
    return $object
}
function Get-Item {
    param([string]$LiteralPath,[switch]$Force)
    if ($LiteralPath -match '^(HKLM|HKCU|Registry)::?') { return New-MockKey $LiteralPath }
    $full=Assert-MockPath $LiteralPath
    return Microsoft.PowerShell.Management\Get-Item -LiteralPath $full -Force:$Force
}
function Open-WritableRegistryKey([string]$Path) { return New-MockKey $Path }
function Get-ItemPropertyValue {
    param([string]$LiteralPath,[string]$Name)
    return (New-MockKey $LiteralPath).GetValue($Name)
}
function Get-ChildItem {
    param([string]$LiteralPath,[string]$Path,[switch]$Directory,[switch]$Force,[switch]$Recurse,[string]$Filter)
    $p=if($LiteralPath){$LiteralPath}else{$Path}
    if ($p -match '^(HKLM|HKCU|Registry)::?') { return @() }
    if ($p -and $p.StartsWith($env:PUP_STATE_ROOT,[StringComparison]::OrdinalIgnoreCase)) {
        $full=Assert-MockPath $p
        $parameters=@{LiteralPath=$full;Force=$Force;Recurse=$Recurse}
        if($Directory){$parameters.Directory=$true}; if($Filter){$parameters.Filter=$Filter}
        return Microsoft.PowerShell.Management\Get-ChildItem @parameters
    }
    return @()
}
function reg.exe {
    $verb=[string]$args[0]; $native=[string]$args[1]; $file=Assert-MockPath ([string]$args[2])
    $key=$native -replace '^(HKLM|HKCU)\\','$1:\'
    if ($verb -ne 'export' -or $null -eq (Mock-Key $key)) { throw 'Unexpected fixture reg.exe call' }
    if ($key -in @($global:PupMockState.failBackup)) {
        Save-MockState "backup-fail:$key"; $global:LASTEXITCODE=5; return 'Simulated denied'
    }
    [IO.File]::WriteAllText($file,'Fixture registry export only')
    Save-MockState "backup:$key"
    $global:LASTEXITCODE=0
    return 'Exported fixture'
}
function sc.exe {
    $verb=[string]$args[0]; $name=[string]$args[1]
    $keyPath="HKLM:\SYSTEM\CurrentControlSet\Services\$name"
    $key=Mock-Key $keyPath
    if ($null -eq $key) { throw 'Unexpected fixture service name' }
    Save-MockState "sc:$verb`:$name"
    if ($verb -eq $global:PupMockState.failSc) { $global:LASTEXITCODE=5; return 'Simulated sc failure' }
    switch ($verb) {
        'config' { ($key.values | Where-Object name -eq 'Start').data=4 }
        'stop' { }
        'delete' { $global:PupMockState.registry=@($global:PupMockState.registry | Where-Object path -ne $keyPath) }
        default { throw 'Unexpected fixture sc.exe verb' }
    }
    Save-MockState "sc-success:$verb`:$name"
    $global:LASTEXITCODE=0
    return 'Fixture success'
}
function Get-CimInstance {
    param([string]$ClassName,[string]$Filter)
    if ($ClassName -eq 'Win32_Process') {
        $items=@($global:PupMockState.processes)
        if($Filter -match '^ProcessId=(\d+)$'){ return $items | Where-Object ProcessId -eq ([int]$matches[1]) }
        return $items
    }
    if ($ClassName -in @('Win32_Service','Win32_SystemDriver')) { return @() }
    throw 'Unexpected fixture CIM query'
}
function Stop-Process {
    param([int]$Id,[switch]$Force)
    if ($Id -notin @($global:PupMockState.processes | ForEach-Object ProcessId)) { throw 'Unexpected fixture PID' }
    $global:PupMockState.processes=@($global:PupMockState.processes | Where-Object ProcessId -ne $Id)
    Save-MockState "process-stop:$Id"
}
function Get-ScheduledTask {
    foreach($task in @($global:PupMockState.tasks)) {
        [pscustomobject]@{TaskPath=$task.path;TaskName=$task.name;
            Actions=@([pscustomobject]@{Execute=$task.execute;Arguments=$task.arguments})}
    }
}
function Export-ScheduledTask {
    param([string]$TaskPath,[string]$TaskName)
    Save-MockState "task-export:$TaskName"
    if($global:PupMockState.failTaskExport){ throw 'Simulated task export failure' }
    return '<Task><Actions><Exec><Command>fixture.exe</Command></Exec></Actions></Task>'
}
function Unregister-ScheduledTask {
    param([string]$TaskPath,[string]$TaskName,[bool]$Confirm)
    $xml=@([IO.Directory]::GetFiles($script:RunDir,'task-*.xml'))
    if (-not $xml.Count -or -not [IO.File]::ReadAllText($xml[0]).Contains('<Task>')) { throw 'Task deletion reached before XML backup' }
    $global:PupMockState.tasks=@($global:PupMockState.tasks | Where-Object { $_.path -ne $TaskPath -or $_.name -ne $TaskName })
    Save-MockState "task-delete:$TaskName"
}
function Remove-ItemProperty {
    param([string]$LiteralPath,[string]$Name)
    (New-MockKey $LiteralPath).DeleteValue($Name,$false)
}
function Remove-Item {
    param([string]$LiteralPath,[switch]$Recurse,[switch]$Force)
    if($LiteralPath -match '^(HKLM|HKCU|Registry)::?') {
        if($null -eq (Mock-Key $LiteralPath)){ throw 'Unexpected fixture key deletion' }
        $global:PupMockState.registry=@($global:PupMockState.registry | Where-Object path -ne $LiteralPath)
        Save-MockState "key-delete:$LiteralPath"
        return
    }
    $full=Assert-MockPath $LiteralPath
    if($full -in @($global:PupMockState.lockedFiles)){ Save-MockState "file-locked:$full"; throw 'Simulated locked file' }
    [IO.File]::Delete($full)
    Save-MockState "file-delete:$full"
}
function New-ItemProperty {
    param([string]$LiteralPath,[string]$Name,$Value,[string]$PropertyType,[switch]$Force)
    if($LiteralPath -ne 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -or
        $Name -ne 'PendingFileRenameOperations' -or $PropertyType -ne 'MultiString') { throw 'Unexpected fixture registry write' }
    $key=Mock-Key $LiteralPath
    $key.values=@([pscustomobject]@{name=$Name;data=@($Value)})
    if($global:PupMockState.dropPendingWrite){ $key.values[0].data=@('broken') }
    Save-MockState 'pending-write'
}
