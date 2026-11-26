# Collector upgrade tests: file fixtures and mocked registry/CIM/native interfaces only.
# Functions are extracted from scripts/collect-forensics.ps1 by name, then executed
# against fixtures so the production file itself is never modified for tests.

function Get-ScriptFunction([string]$Name,[string]$ScriptFile='collect-forensics.ps1') {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source ('scripts/' + $ScriptFile)),[ref]$tokens,[ref]$errors)
    $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name},$true)
    if (-not $node) { throw "Function not found in $ScriptFile`: $Name" }
    return [scriptblock]::Create($node.Extent.Text)
}
function Initialize-CollectorFixture([string[]]$Names) {
    foreach ($name in @('Get-ConfigValue','Get-CollectorParameters','Add-ItemError','Add-Truncation','Write-TextAtomic') + $Names) { . (Get-ScriptFunction $name) }
    $script:EvidenceBuf=New-Object 'System.Collections.Generic.List[string]'
    $script:ItemErrors=New-Object 'System.Collections.Generic.List[string]'
    $script:AcquisitionErrors=New-Object 'System.Collections.Generic.List[string]'
    $script:ResumeSkip=@(); $script:ResumeActive=$false; $script:HashCache=@{}; $script:SectionTruncated=$false
    $PupPattern='(?!)'; $DnsPattern='(?!)'; $ProbeSeconds=15; $MaxEntries=20000; $MaxRecords=2000
    $Error.Clear()
}

Test-Case 'collector-persists-status-and-hashes-per-section' {
    . Initialize-CollectorFixture @('W','Save','Get-EvidenceHashList','Write-CollectionState','Complete-Collection')
    $OutDir=Join-Path $run 'incremental'; [void](New-Item -ItemType Directory -Path $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:StartedAt=(Get-Date).ToString('o')
    W 'row-1'; Save 'a.txt'
    $state=[IO.File]::ReadAllText((Join-Path $OutDir 'collection-status.json'))|ConvertFrom-Json
    Assert ($state.complete -eq $false -and @($state.sections).Count -eq 1) 'Status not written after the first section'
    # R1: an empty resumeSkipped must stay a real array (5.1 used to emit {}, 7 null).
    Assert ($null -ne $state.resumeSkipped -and $state.resumeSkipped -is [array] -and @($state.resumeSkipped).Count -eq 0) ('resumeSkipped is not an empty array: ' + [string]$state.resumeSkipped)
    Assert (-not (Test-Path -LiteralPath (Join-Path $OutDir 'collection-done.json'))) 'DONE marker exists before completion'
    $hashes=[IO.File]::ReadAllText((Join-Path $OutDir 'evidence-hashes.json'))|ConvertFrom-Json
    Assert (@($hashes|Where-Object file -eq 'a.txt').Count -eq 1) 'Section artifact hash missing'
    W 'row-2'; Save 'b.txt'
    $state=[IO.File]::ReadAllText((Join-Path $OutDir 'collection-status.json'))|ConvertFrom-Json
    Assert (@($state.sections).Count -eq 2) 'Status not appended per section'
    # A resumed run must list skipped files (and keep the array type).
    $script:ResumeSkip=@('a.txt','b.txt'); $script:ResumeActive=$true
    W 'row-3'; Save 'c.txt'
    $script:ResumeActive=$false
    $state=[IO.File]::ReadAllText((Join-Path $OutDir 'collection-status.json'))|ConvertFrom-Json
    Assert ($state.resumed -eq $true -and $state.resumeSkipped -is [array] -and (@($state.resumeSkipped) -join ',') -eq 'a.txt,b.txt') 'resumeSkipped not serialized from the resume list'
    Complete-Collection
    Assert (Test-Path -LiteralPath (Join-Path $OutDir 'collection-done.json')) 'DONE marker missing after completion'
    $state=[IO.File]::ReadAllText((Join-Path $OutDir 'collection-status.json'))|ConvertFrom-Json
    Assert ($state.complete -eq $true) 'Final status not marked complete'
    Assert ($state.hashErrors -eq 0 -and $null -ne $state.parameters) 'Status contract missing hashErrors/parameters'
    $hashes=[IO.File]::ReadAllText((Join-Path $OutDir 'evidence-hashes.json'))|ConvertFrom-Json
    # DONE is a marker, not evidence; it is written after the hash list on purpose.
    Assert (@($hashes|Where-Object file -eq 'collection-done.json').Count -eq 0) 'DONE marker must not be hashed'
}

Test-Case 'resume-plan-binds-identity-parameters-and-hashes' {
    . Initialize-CollectorFixture @('Get-ResumePlan','Get-CollectorParameters','Get-ConfigValue')
    $dir=Join-Path $run 'resume'; [void](New-Item -ItemType Directory -Path $dir)
    foreach($name in 'a.txt','b.txt','c.txt'){ [IO.File]::WriteAllText((Join-Path $dir $name),('fixture-'+$name)) }
    $hashItems=@(foreach($name in 'a.txt','b.txt','c.txt'){
        [pscustomobject]@{file=$name;sha256=(Get-FileHash -LiteralPath (Join-Path $dir $name) -Algorithm SHA256).Hash}})
    [IO.File]::WriteAllText((Join-Path $dir 'evidence-hashes.json'),($hashItems|ConvertTo-Json -Depth 4),[Text.Encoding]::UTF8)
    $state=[pscustomobject]@{complete=$false;computer=$env:COMPUTERNAME
        userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        parameters=(Get-CollectorParameters)
        sections=@(
            [pscustomobject]@{file='a.txt';status='PASS'},
            [pscustomobject]@{file='b.txt';status='ERROR'},
            [pscustomobject]@{file='c.txt';status='SKIP'},
            [pscustomobject]@{file='d.txt';status='PASS'})}
    $statusPath=Join-Path $dir 'collection-status.json'
    [IO.File]::WriteAllText($statusPath,($state|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
    $plan=Get-ResumePlan $dir
    Assert (-not $plan.refused -and (@($plan.skip) -join ',') -eq 'a.txt,c.txt') ('Unexpected resume skip list: ' + (@($plan.skip) -join ','))
    Assert (@($plan.sections).Count -eq 2) 'Previous section records not restored'
    # A kept artifact whose hash changed must be re-collected, not silently reused.
    [IO.File]::WriteAllText((Join-Path $dir 'c.txt'),'rewritten')
    $plan=Get-ResumePlan $dir
    Assert ((@($plan.skip) -join ',') -eq 'a.txt') 'Changed artifact reused'
    # Identity and parameter changes make it a different batch.
    $state.computer='OTHER-COMPUTER'
    [IO.File]::WriteAllText($statusPath,($state|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
    Assert ((Get-ResumePlan $dir).refused -eq 'identity') 'Identity change not refused'
    $state.computer=$env:COMPUTERNAME
    $originalPup=$state.parameters.pupPattern
    $state.parameters.pupPattern='OtherPattern'
    [IO.File]::WriteAllText($statusPath,($state|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
    Assert ((Get-ResumePlan $dir).refused -eq 'parameters') 'Parameter change not refused'
    $state.parameters.pupPattern=$originalPup
    [IO.File]::WriteAllText($statusPath,($state|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
    Remove-Item -LiteralPath (Join-Path $dir 'evidence-hashes.json')
    Assert ((Get-ResumePlan $dir).refused -eq 'no-hashes') 'Missing hash list not refused'
    [IO.File]::WriteAllText((Join-Path $dir 'evidence-hashes.json'),($hashItems|ConvertTo-Json -Depth 4),[Text.Encoding]::UTF8)
    [IO.File]::WriteAllText((Join-Path $dir 'collection-done.json'),'{"complete":true}')
    Assert ((Get-ResumePlan $dir).refused -eq 'completed') 'Completed collection not refused'
    Remove-Item -LiteralPath $statusPath
    [IO.File]::Delete((Join-Path $dir 'collection-done.json'))
    Assert ((Get-ResumePlan $dir).refused -eq 'no-status') 'Missing status not refused'
}

Test-Case 'service-and-driver-item-failure-does-not-fail-the-section' {
    . Initialize-CollectorFixture @('W','Save','Get-EvidenceHashList','Write-CollectionState','Sig','BinaryPath','Add-ItemError','Get-PropText','Write-ServiceEntry','Write-DriverEntry')
    function Test-Path { param([string]$LiteralPath,[string]$Path) return $false }
    function Get-Item { [CmdletBinding()] param([string]$LiteralPath,[switch]$Force)
        $item=[pscustomobject]@{PSPath=$LiteralPath}
        $item | Add-Member ScriptMethod GetValue { param($name,$default,$options) throw 'Simulated null registry value' }
        return $item }
    function Get-ItemProperty { [CmdletBinding()] param([string]$LiteralPath) return $null }
    function Get-AuthenticodeSignature { param([string]$LiteralPath) return [pscustomobject]@{Status='NotSigned';SignerCertificate=$null} }
    Write-ServiceEntry ([pscustomobject]@{Name='FixtureSvc';State='Stopped';StartMode='Manual';DisplayName=$null;PathName=$null})
    Write-DriverEntry ([pscustomobject]@{Name='FixtureDrv';State='Running';StartMode='Boot';PathName=$null})
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('FixtureSvc') -and $text.Contains('FixtureDrv')) 'Items skipped entirely'
    Assert ($script:ItemErrors.Count -eq 2) ('Per-item errors not recorded: ' + $script:ItemErrors.Count)
    Assert ($text.Contains('# ITEM ERROR')) 'Item error lines missing from evidence'
    $OutDir=Join-Path $run 'item-errors'; [void](New-Item -ItemType Directory -Path $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:StartedAt=(Get-Date).ToString('o')
    Save '04-fixture.txt'
    Assert ($sections[0].status -eq 'PASS' -and @($sections[0].itemErrors).Count -eq 2) 'Item errors failed the whole section'
}

Test-Case 'prefetch-listing-is-bounded-and-newest-first' {
    . Initialize-CollectorFixture @('W','Get-PropText','Collect-Prefetch')
    function Get-Item { [CmdletBinding()] param([string]$LiteralPath,[switch]$Force) return $null }
    $dir=Join-Path $run 'prefetch'; [void](New-Item -ItemType Directory -Path $dir)
    [IO.File]::WriteAllText((Join-Path $dir 'OLD.EXE-00000000.pf'),'old')
    Start-Sleep -Milliseconds 40
    [IO.File]::WriteAllText((Join-Path $dir 'NEW.EXE-11111111.pf'),'new')
    Collect-Prefetch $dir 10
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('NEW.EXE') -and $text.Contains('OLD.EXE')) 'Prefetch entries missing'
    Assert ($text.IndexOf('NEW.EXE') -lt $text.IndexOf('OLD.EXE')) 'Prefetch not sorted newest first'
    $script:EvidenceBuf.Clear()
    Collect-Prefetch $dir 1
    $lines=@($script:EvidenceBuf|Where-Object { $_ -like '*.pf | *' })
    Assert ($lines.Count -eq 1) 'Prefetch limit not applied'
}

Test-Case 'bam-values-decoded-with-filetime' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Get-PropText','Collect-Bam')
    $sid='S-1-5-21-1-2-3-1001'
    $base='HKLM:\SYSTEM\CurrentControlSet\Services\bam\State\UserSettings'
    $keyPath="$base\$sid"
    $when=[DateTime]::Now.AddDays(-1)
    $script:BamValues=@([pscustomobject]@{path=$keyPath;values=@([pscustomobject]@{name='C:\Fixture\app.exe';data=[BitConverter]::GetBytes($when.ToFileTime())})})
    $script:BamBase=$base; $script:BamSid=$sid; $script:BamKeyPath=$keyPath
    function Test-Path { param([string]$LiteralPath,[string]$Path) return $true }
    function Get-ChildItem { [CmdletBinding()] param([string]$LiteralPath,[string]$Path,[switch]$Directory,[switch]$Force,[switch]$Recurse,[string]$Filter)
        if($LiteralPath -eq $script:BamBase){ return @([pscustomobject]@{PSChildName=$script:BamSid;PSPath=$script:BamKeyPath}) }
        return @() }
    function Get-Item { [CmdletBinding()] param([string]$LiteralPath,[switch]$Force)
        $entry=@($script:BamValues|Where-Object path -eq $LiteralPath)|Select-Object -First 1
        if(-not $entry){ return $null }
        $item=[pscustomobject]@{PSChildName=($entry.path -split '\\')[-1];PSPath=$entry.path;Values=$entry.values}
        $item | Add-Member ScriptMethod GetValueNames { param() return @($this.Values|ForEach-Object{$_.name}) }
        $item | Add-Member ScriptMethod GetValue { param($name,$default,$options)
            $hit=@($this.Values|Where-Object name -eq $name)
            if($hit.Count){ return ,$hit[0].data }
            return $default }
        return $item }
    Collect-Bam $base 50
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('C:\Fixture\app.exe')) 'BAM entry missing'
    Assert ($text.Contains($when.ToString('o').Substring(0,10))) 'BAM FILETIME not decoded'
}

Test-Case 'shimcache-raw-preserved-with-hash' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Get-PropText','Collect-ShimCache')
    $script:ShimBytes=[byte[]](0x30,0x00,0x01,0x02)
    function Get-Item { [CmdletBinding()] param([string]$LiteralPath,[switch]$Force)
        $item=[pscustomobject]@{PSPath=$LiteralPath;Blob=$script:ShimBytes}
        $item | Add-Member ScriptMethod GetValue { param($name,$default,$options) if($name -eq 'AppCompatCache'){ return ,$this.Blob } return $default }
        return $item }
    $dest=Join-Path $run 'shim-evidence'; [void](New-Item -ItemType Directory -Path $dest)
    Collect-ShimCache 'HKLM:\Fixture\AppCompatCache' $dest 1024
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('size=4')) 'ShimCache size missing'
    $raw=Join-Path $dest 'execution-artifacts\appcompatcache.bin'
    Assert (Test-Path -LiteralPath $raw) 'ShimCache blob not preserved'
    Assert ((([IO.File]::ReadAllBytes($raw)) -join ',') -eq ($script:ShimBytes -join ',')) 'ShimCache bytes altered'
}

Test-Case 'srudb-copied-within-budget-and-skipped-when-oversized' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Collect-SruDatabase')
    $dir=Join-Path $run 'sru'; [void](New-Item -ItemType Directory -Path $dir)
    $db=Join-Path $dir 'SRUDB.dat'; [IO.File]::WriteAllBytes($db,[byte[]](1,2,3,4,5))
    $dest=Join-Path $run 'sru-evidence'; [void](New-Item -ItemType Directory -Path $dest)
    Collect-SruDatabase $db 1024 $dest
    Assert (Test-Path -LiteralPath (Join-Path $dest 'execution-artifacts\SRUDB.dat')) 'SRUDB not copied within budget'
    $script:EvidenceBuf.Clear()
    Collect-SruDatabase $db 2 $dest
    Assert (($script:EvidenceBuf -join "`n").Contains('not copied')) 'Oversized SRUDB not skipped'
}

Test-Case 'security-status-collects-products-policy-exclusions-and-mppreference' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Get-PropText','Collect-SecurityStatus')
    function Get-CimInstance { [CmdletBinding()] param([Parameter(Position=0)][string]$ClassName,[string]$Namespace,[string]$Filter)
        if($ClassName -eq 'AntiVirusProduct'){ return [pscustomobject]@{displayName='FixtureAV';productState=397568;pathToSignedProductExe='C:\Fixture\av.exe'} }
        if($ClassName -eq 'Win32_Service'){ return [pscustomobject]@{State='Running';StartMode='Auto';PathName='C:\Fixture\defender.exe'} }
        throw 'Unexpected fixture CIM query' }
    $script:PolicyKeys=@{
        'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender'=@{'DisableAntiSpyware'=1}
        'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection'=@{'DisableRealtimeMonitoring'=1}
        'HKLM:\SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths'=@{'C:\Fixture\'=0}}
    function Test-Path { param([string]$LiteralPath,[string]$Path) return $script:PolicyKeys.ContainsKey($LiteralPath) }
    function Get-Item { param([string]$LiteralPath,[switch]$Force)
        if(-not $script:PolicyKeys.ContainsKey($LiteralPath)){ return $null }
        $item=[pscustomobject]@{PSPath=$LiteralPath}
        $item | Add-Member ScriptMethod GetValueNames { param() return @($script:PolicyKeys[$this.PSPath].Keys) }
        $item | Add-Member ScriptMethod GetValue { param($name,$default,$options)
            if($script:PolicyKeys[$this.PSPath].ContainsKey($name)){ return $script:PolicyKeys[$this.PSPath][$name] }
            return $default }
        return $item }
    function Get-MpPreference { [CmdletBinding()] param() return [pscustomobject]@{DisableRealtimeMonitoring=$true;ExclusionPath=@('C:\Fixture\');ExclusionExtension=@();ExclusionProcess=@()} }
    Collect-SecurityStatus
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('FixtureAV')) 'SecurityCenter2 product missing'
    Assert ($text.Contains('WinDefend [Running/Auto]')) 'Defender service missing'
    Assert ($text.Contains('DisableAntiSpyware = 1')) 'Defender policy missing'
    Assert ($text.Contains('EXCLUSION Paths: C:\Fixture\')) 'Defender exclusions missing'
    Assert ($text.Contains('MpPreference DisableRealtimeMonitoring=True')) 'MpPreference missing'
}

Test-Case 'security-status-tolerates-missing-security-center' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Get-PropText','Collect-SecurityStatus')
    function Get-CimInstance { [CmdletBinding()] param([Parameter(Position=0)][string]$ClassName,[string]$Namespace,[string]$Filter) throw 'Simulated SecurityCenter2 unavailable' }
    function Test-Path { param([string]$LiteralPath,[string]$Path) return $false }
    function Get-Item { [CmdletBinding()] param([string]$LiteralPath,[switch]$Force) return $null }
    function Get-ItemProperty { [CmdletBinding()] param([string]$LiteralPath) return $null }
    function Get-MpPreference { [CmdletBinding()] param() throw 'Simulated Defender module unavailable' }
    Collect-SecurityStatus
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('SecurityCenter2 unavailable')) 'Degraded path not recorded'
    Assert ($script:ItemErrors.Count -ge 2) 'Item errors not recorded for degraded interfaces'
}

Test-Case 'filter-driver-state-collected-and-failure-is-item-level' {
    . Initialize-CollectorFixture @('W','Invoke-NativeText','Add-ItemError','Add-FilterDriverState')
    function Invoke-NativeText { param([string]$Command,[string[]]$Arguments)
        return [pscustomobject]@{ok=$true;code=0;lines=@('Filter Name                     Num Instances','FileInfo                       3')} }
    Add-FilterDriverState
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('fltmc filters: Filter Name')) 'fltmc filters missing'
    Assert ($text.Contains('fltmc instances: FileInfo')) 'fltmc instances missing'
    $script:EvidenceBuf.Clear(); $script:AcquisitionErrors.Clear()
    function Invoke-NativeText { param([string]$Command,[string[]]$Arguments)
        return [pscustomobject]@{ok=$false;code=1;lines=@('Access denied')} }
    Add-FilterDriverState
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('fltmc filters: unavailable (exit 1)')) 'fltmc failure not recorded'
    Assert ($script:AcquisitionErrors.Count -eq 0) 'fltmc failure escalated to a section error'
    Assert ($script:ItemErrors.Count -gt 0) 'fltmc failure not recorded as an item error'
}


Test-Case 'verify-task-scan-is-strict-mode-safe-for-comhandler-actions' {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/verify-clean.ps1'),[ref]$tokens,[ref]$errors)
    $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-TaskActionText'},$true)
    Assert ($null -ne $node) 'Get-TaskActionText missing from verify-clean.ps1'
    . ([scriptblock]::Create($node.Extent.Text))
    Set-StrictMode -Version 2.0
    $handler=[pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskComHandlerAction'}}
    $exec=[pscustomobject]@{Execute='cmd.exe';Arguments='/c echo fixture'}
    $task=[pscustomobject]@{Actions=@($handler,$exec);TaskPath='\';TaskName='Fixture'}
    $text=Get-TaskActionText $task
    Assert ($text -match 'no Execute' -and $text -match 'cmd.exe') 'ComHandler action not classified'
    if (Get-Command Get-ScheduledTask -ErrorAction Ignore) {
        $threw=$false; $failure=''
        try { foreach($realTask in @(Get-ScheduledTask)){ [void](Get-TaskActionText $realTask) } }
        catch { $threw=$true; $failure=$_.Exception.Message }
        Assert (-not $threw) ('Strict-mode task scan threw: ' + $failure)
    }
}

Test-Case 'sru-denied-path-is-item-error-not-section-error' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Collect-SruDatabase')
    function Test-Path { [CmdletBinding()] param([string]$LiteralPath,[string]$Path) throw 'ItemExistsUnauthorizedAccessError: Access is denied' }
    $dest=Join-Path $run 'sru-denied'; [void](New-Item -ItemType Directory -Path $dest)
    Collect-SruDatabase 'C:\Windows\System32\sru\SRUDB.dat' 1024 $dest
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text -match 'not accessible') 'Denied SRUDB path not recorded'
    Assert ($script:ItemErrors.Count -eq 1) 'Denied SRUDB path escalated or lost'
    Assert ($text -notmatch 'not found') 'Denied path misreported as absent'
}

Test-Case 'resume-backup-preserves-artifacts-with-unique-names' {
    . Initialize-CollectorFixture @('W','Add-ItemError','Get-ResumeFlag','Backup-EvidenceFile')
    $dir=Join-Path $run 'backup-fixture'; [void](New-Item -ItemType Directory -Path $dir)
    $file=Join-Path $dir 'appcompatcache.bin'
    [IO.File]::WriteAllText($file,'first')
    $script:ResumeActive=$true
    Assert (Backup-EvidenceFile $file) 'Existing artifact not backed up'
    Assert (-not (Test-Path -LiteralPath $file)) 'Original not moved aside'
    [IO.File]::WriteAllText($file,'second')
    Assert (Backup-EvidenceFile $file) 'Second backup in the same second failed'
    $prevs=@(Get-ChildItem -LiteralPath $dir -Filter 'appcompatcache.bin.prev-*')
    Assert ($prevs.Count -eq 2) ('Same-second backups collided: ' + $prevs.Count)
    $contents=@($prevs | ForEach-Object { [IO.File]::ReadAllText($_.FullName) })
    Assert (($contents -contains 'first') -and ($contents -contains 'second')) 'Backed-up contents wrong'
    [IO.File]::WriteAllText($file,'third')
    $script:ResumeActive=$false
    Assert (Backup-EvidenceFile $file) 'Non-resume backup must allow the write'
    Assert ([IO.File]::ReadAllText($file) -eq 'third') 'Non-resume path altered the file'
}

Test-Case 'save-preserves-previous-evidence-on-resume' {
    . Initialize-CollectorFixture @('W','Save','Get-EvidenceHashList','Write-CollectionState','Backup-EvidenceFile','Add-ItemError','Get-ResumeFlag')
    $OutDir=Join-Path $run 'save-resume'; [void](New-Item -ItemType Directory -Path $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:StartedAt=(Get-Date).ToString('o')
    [IO.File]::WriteAllText((Join-Path $OutDir 'a.txt'),'old-evidence')
    $script:ResumeActive=$true
    W 'new-evidence'; Save 'a.txt'
    Assert ([IO.File]::ReadAllText((Join-Path $OutDir 'a.txt')).Contains('new-evidence')) 'New evidence not written'
    $prevs=@(Get-ChildItem -LiteralPath $OutDir -Filter 'a.txt.prev-*')
    Assert ($prevs.Count -eq 1 -and [IO.File]::ReadAllText($prevs[0].FullName) -eq 'old-evidence') 'Previous evidence lost'
}

Test-Case 'save-refuses-overwrite-when-backup-fails' {
    . Initialize-CollectorFixture @('W','Save','Get-EvidenceHashList','Write-CollectionState','Add-ItemError','Get-ResumeFlag')
    function Backup-EvidenceFile { param([string]$Path) return $false }
    $OutDir=Join-Path $run 'save-unpreserved'; [void](New-Item -ItemType Directory -Path $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:StartedAt=(Get-Date).ToString('o')
    [IO.File]::WriteAllText((Join-Path $OutDir 'a.txt'),'old-evidence')
    $script:ResumeActive=$true
    W 'new-evidence'; Save 'a.txt'
    Assert ([IO.File]::ReadAllText((Join-Path $OutDir 'a.txt')) -eq 'old-evidence') 'Original overwritten after backup failure'
    Assert (@(Get-ChildItem -LiteralPath $OutDir -Filter 'a.txt.unpreserved-*').Count -eq 1) 'New evidence not preserved separately'
    Assert ($sections[0].status -eq 'ERROR') 'Backup failure not recorded as a section error'
}

Test-Case 'section-item-errors-are-typed-string-arrays' {
    . Initialize-CollectorFixture @('W','Save','Get-EvidenceHashList','Write-CollectionState')
    $OutDir=Join-Path $run 'item-error-types'; [void](New-Item -ItemType Directory -Path $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:StartedAt=(Get-Date).ToString('o')
    W 'row'; Save 'empty.txt'
    Assert ($sections[0].itemErrors -is [string[]] -and $sections[0].itemErrors.Count -eq 0) 'Empty itemErrors not a string[]'
    $script:ItemErrors.Add('Fixture: one error')
    W 'row2'; Save 'one.txt'
    Assert ($sections[1].itemErrors -is [string[]] -and $sections[1].itemErrors.Count -eq 1) 'Single itemErrors not a string[]'
}

Test-Case 'collector-refuses-existing-outdir-and-invalid-patterns' {
    $shell=(Get-Process -Id $PID).Path
    $scriptPath=Join-Path $source 'scripts/collect-forensics.ps1'
    $existing=Join-Path $run 'existing-outdir'; [void](New-Item -ItemType Directory -Path $existing)
    $savedPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $output=& $shell -NoProfile -File $scriptPath -OutDir $existing 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    Assert ($code -eq 2 -and (($output -join "`n") -match 'OutDir must be new')) 'Existing OutDir not refused'
    $badPup=Join-Path $run 'bad-pup'
    try {
        $ErrorActionPreference='Continue'
        $output=& $shell -NoProfile -File $scriptPath -OutDir $badPup -PupPattern '[' 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    Assert ($code -eq 2 -and (($output -join "`n") -match 'Invalid -PupPattern')) 'Invalid -PupPattern not refused'
    $badDns=Join-Path $run 'bad-dns'
    try {
        $ErrorActionPreference='Continue'
        $output=& $shell -NoProfile -File $scriptPath -OutDir $badDns -DnsPattern '[' 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    Assert ($code -eq 2 -and (($output -join "`n") -match 'Invalid -DnsPattern')) 'Invalid -DnsPattern not refused'
}

Test-Case 'collector-refuses-32-bit-powershell' {
    $wow=Join-Path $env:SystemRoot 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $wow)) { return }
    $scriptPath=Join-Path $source 'scripts/collect-forensics.ps1'
    $fresh=Join-Path $run 'wow64-refused'
    $savedPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $output=& $wow -NoProfile -File $scriptPath -OutDir $fresh 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    Assert ($code -eq 2 -and (($output -join "`n") -match 'Use 64-bit PowerShell')) '32-bit run not refused'
}
