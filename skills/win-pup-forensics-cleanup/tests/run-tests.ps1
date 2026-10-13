# Offline regression tests: generated fixtures and mocked Windows interfaces only.
[CmdletBinding()]
param([string]$OutputRoot = (Join-Path $env:TEMP 'win-pup-skill-tests'))
$ErrorActionPreference = 'Stop'
$source = Split-Path $PSScriptRoot -Parent
$run = [IO.Path]::GetFullPath((Join-Path $OutputRoot ([guid]::NewGuid().ToString('N'))))
[void](New-Item -ItemType Directory -Path $run -Force)
$results = New-Object 'System.Collections.Generic.List[object]'
function Test-Case([string]$Name, [scriptblock]$Action) {
    try { & $Action; $results.Add([pscustomobject]@{name=$Name;passed=$true}); Write-Host "PASS $Name" }
    catch { $results.Add([pscustomobject]@{name=$Name;passed=$false;error=$_.Exception.Message}); Write-Host "FAIL $Name $_" }
}
function Assert($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Reject([scriptblock]$Action) { $rejected=$false; try { & $Action | Out-Null } catch { $rejected=$true }; Assert $rejected 'Expected rejection' }
. (Join-Path $source 'scripts/common.ps1')
Test-Case 'all-scripts-parse' {
    foreach ($file in @(Get-ChildItem (Join-Path $source 'scripts') -Filter '*.ps1')) {
        $tokens=$null; $errors=$null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors)
        Assert (@($errors).Count -eq 0) ($errors -join '; ')
        Assert (-not ([IO.File]::ReadAllText($file.FullName) -match '[^\x00-\x7F]')) 'Scripts must be ASCII'
    }
}
Test-Case 'dangerous-root-and-ancestor-rejected' {
    foreach ($path in @(($env:SystemDrive+'\'),$env:USERPROFILE,$env:APPDATA,$env:ProgramFiles,$env:SystemRoot,(Join-Path $env:SystemRoot 'Temp'))) {
        Reject { Assert-SafeTarget $path @(($env:SystemDrive+'\')) }
    }
}
Test-Case 'literal-path-boundary-and-normalization' {
    $allowed=Join-Path $run 'allowed'; [void](New-Item -ItemType Directory $allowed)
    $ok=Join-Path $allowed 'Vendor[1]'
    Assert ((Assert-SafeTarget $ok @($allowed)) -eq $ok) 'Literal bracket path rejected'
    Reject { Assert-SafeTarget (Join-Path $run 'allowed-other\Vendor') @($allowed) }
    Reject { Assert-SafeTarget (Join-Path $allowed '..\outside') @($allowed) }
    Reject { Assert-SafeTarget $allowed @($allowed) }
    Reject { Assert-SafeTarget '%UNDEFINED_TEST_ENV_945%\Vendor' @($allowed) }
    Reject { Assert-SafeTarget (Join-Path $allowed 'Vendor. \child') @($allowed) }
    Reject { Assert-SafeTarget (Join-Path $allowed 'Vendor:stream') @($allowed) }
    Reject { Assert-SafeTarget (Join-Path $allowed 'NUL.txt') @($allowed) }
}
Test-Case 'reparse-point-never-traversed' {
    $allowed=Join-Path $run 'link-root'; $outside=Join-Path $run 'outside'
    [void](New-Item -ItemType Directory $allowed); [void](New-Item -ItemType Directory $outside)
    $link=Join-Path $allowed 'link'
    [void](New-Item -ItemType Junction -Path $link -Target $outside)
    Reject { Assert-SafeTarget (Join-Path $link 'child') @($allowed) }
    Reject { Get-SafeTree $allowed }
}
Test-Case 'registry-containers-rejected-leaves-supported' {
    foreach ($key in @('HKLM:\SOFTWARE','HKLM:\SOFTWARE\Microsoft','HKCU:\SOFTWARE\Classes\CLSID',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer','HKCU:\SOFTWARE\Vendor\..\Classes',
        'HKLM:\SOFTWARE\WOW6432Node','HKLM:\SOFTWARE\WOW6432Node\','HKLM:\SOFTWARE\Policies',
        'HKLM:\SOFTWARE\Clients','HKLM:\SOFTWARE\WOW6432Node\Policies','HKCU:\SOFTWARE\WOW6432Node\Clients\Vendor',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft','HKLM:\SOFTWARE\RegisteredApplications',
        'HKCU:\SOFTWARE\Classes\WOW6432Node','HKCU:\SOFTWARE\Classes\WOW6432Node\CLSID')) {
        Reject { Assert-RegKey $key }
    }
    Assert-RegKey 'HKCU:\SOFTWARE\Vendor'
    Assert-RegKey 'HKLM:\SOFTWARE\WOW6432Node\Vendor'
    Assert-RegKey 'HKCU:\SOFTWARE\Classes\CLSID\{0002DF01-0000-0000-C000-000000000046}\LocalServer32'
    Assert-RegKey 'HKCU:\SOFTWARE\Classes\WOW6432Node\CLSID\{0002DF01-0000-0000-C000-000000000046}\LocalServer32'
    Assert-RegKey 'HKLM:\SOFTWARE\Classes\*\shell\VendorAction'
}
Test-Case 'same-timestamp-directory-evidence-retained' {
    $items=@([pscustomobject]@{FullName='A';CreationTime=[datetime]'2026-10-08'},[pscustomobject]@{FullName='B';CreationTime=[datetime]'2026-10-08'})
    $kept=@($items | Sort-Object FullName -Unique | Sort-Object CreationTime,FullName)
    Assert ($kept.Count -eq 2) 'Same-time directories lost'
}
Test-Case 'collection-query-error-is-recorded' {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/collect-forensics.ps1'),[ref]$tokens,[ref]$errors)
    foreach ($name in @('W','Save')) {
        $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
        . ([scriptblock]::Create($node.Extent.Text))
    }
    $OutDir=Join-Path $run 'collector'; [void](New-Item -ItemType Directory $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:EvidenceBuf=New-Object 'System.Collections.Generic.List[string]'
    $script:AcquisitionErrors=New-Object 'System.Collections.Generic.List[string]'
    $Error.Clear()
    Write-Error 'Simulated collector permission denial' -ErrorAction SilentlyContinue
    Save 'mock-evidence.txt'
    Assert ($sections[0].status -eq 'ERROR') 'Collection failure swallowed'
    Assert ([IO.File]::ReadAllText((Join-Path $OutDir 'mock-evidence.txt')).Contains('Simulated collector permission denial')) 'Error evidence missing'
}
Test-Case 'service-and-driver-binary-path-normalized' {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/collect-forensics.ps1'),[ref]$tokens,[ref]$errors)
    $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'BinaryPath'},$true)
    . ([scriptblock]::Create($node.Extent.Text))
    Assert ((BinaryPath '"C:\Program Files\Vendor\app.exe" --service') -eq 'C:\Program Files\Vendor\app.exe') 'Quoted service path wrong'
    Assert ((BinaryPath '%SystemRoot%\SysWOW64\svchost.exe -k FixtureGp') -eq (Join-Path $env:SystemRoot 'SysWOW64\svchost.exe')) 'Hosted service path wrong'
    Assert ((BinaryPath '\SystemRoot\System32\drivers\guard.sys') -eq (Join-Path $env:SystemRoot 'System32\drivers\guard.sys')) 'Driver path wrong'
}
Test-Case 'native-acquisition-nonzero-exit-is-recorded-without-exception' {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/collect-forensics.ps1'),[ref]$tokens,[ref]$errors)
    foreach($name in @('W','Save','Invoke-EvidenceNative')){
        $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
        . ([scriptblock]::Create($node.Extent.Text))
    }
    $OutDir=Join-Path $run 'native-collector'; [void](New-Item -ItemType Directory $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:EvidenceBuf=New-Object 'System.Collections.Generic.List[string]'
    $script:AcquisitionErrors=New-Object 'System.Collections.Generic.List[string]'
    $Error.Clear()
    # This native process only exits 5; it performs no system changes.
    Invoke-EvidenceNative (Join-Path $env:WINDIR 'System32\cmd.exe') @('/d','/c','exit 5') | Out-Null
    Save 'native-failure.txt'
    Assert ($sections[0].status -eq 'ERROR' -and ($sections[0].errors -join '; ').Contains('Native command failed (5)')) 'Native exit failure misreported as PASS'
    Assert ($script:AcquisitionErrors.Count -eq 0) 'Errors leaked into the next section'
    Invoke-EvidenceNative (Join-Path $env:WINDIR 'System32\cmd.exe') @('/d','/c','exit 0') | Out-Null
    Save 'native-success.txt'
    Assert ($sections[1].status -eq 'PASS') 'Successful native exit misreported'
}
. (Join-Path $PSScriptRoot 'shell-collection-tests.ps1')
. (Join-Path $PSScriptRoot 'bounded-probe-tests.ps1')

$toolDir=Join-Path $run 'mock-tools'; [void](New-Item -ItemType Directory $toolDir)
foreach ($name in @('common.ps1','unprotect.ps1','cleanup-residue.ps1','verify-clean.ps1')) {
    Copy-Item -LiteralPath (Join-Path $source "scripts/$name") -Destination $toolDir
}
# Mocks are appended to the fixture copy, never to production scripts.
$mocks=@'
function Assert-Admin { }
function Test-Path {
    param([string]$LiteralPath,[string]$Path)
    $p=if($LiteralPath){$LiteralPath}else{$Path}
    if($p -eq 'HKCU:\SOFTWARE\FixtureOnly' -and $env:PUP_TEST_SCENARIO -eq 'backup-block') { return $true }
    if($p -match '^(HKLM|HKCU|Registry)::?') { return $false }
    if($p -like "$env:PUP_TEST_RUN*") { return Microsoft.PowerShell.Management\Test-Path -LiteralPath $p }
    return $false
}
function Get-Item {
    param([string]$LiteralPath,[switch]$Force)
    if($LiteralPath -like 'HKLM:*') {
        $item=[pscustomobject]@{}
        $item | Add-Member ScriptMethod GetValueNames { return @() }
        return $item
    }
    Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force
}
function Get-ChildItem {
    param([string]$LiteralPath,[string]$Path,[switch]$Directory,[switch]$Force,[switch]$Recurse,[string]$Filter)
    # Verification discovery queries return fixture data only.
    return @()
}
function Get-CimInstance {
    param([string]$ClassName,[string]$Filter)
    if($env:PUP_TEST_SCENARIO -eq 'query-error') { throw 'Simulated access denied' }
    if($env:PUP_TEST_SCENARIO -eq 'loaded-driver' -and $ClassName -eq 'Win32_SystemDriver') {
        return [pscustomobject]@{Name='FixtureProtect';State='Running';DisplayName='Fixture';PathName='C:\Fixture\guard.sys'}
    }
    return @()
}
function Get-ScheduledTask { return @() }
function reg.exe { $global:LASTEXITCODE=5; 'Simulated backup denial' }
function sc.exe { throw 'Mutation must never be reached in these tests' }
function Stop-Process { throw 'Mutation must never be reached in these tests' }
function Remove-Item { throw 'Mutation must never be reached in these tests' }
'@
[IO.File]::AppendAllText((Join-Path $toolDir 'common.ps1'), "`r`n"+$mocks,[Text.Encoding]::ASCII)
$env:PUP_TEST_RUN=$run
$shell=(Get-Process -Id $PID).Path
function Make-Plan {
    $contextId=[guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText((Join-Path $run 'manual-evidence.txt'),'Generated fixture, not system evidence')
    $reviews=@('browser-policies-extensions','shell-com-bho','injection-wmi-winsock','other-user-profiles','post-reboot-observation','preserved-software') | ForEach-Object {
        [pscustomobject]@{name=$_;status='PASS';evidence='manual-evidence.txt';reviewedAt=[DateTimeOffset]::UtcNow.ToString('o');phase='final';contextId=$contextId}
    }
    return [pscustomobject]@{schemaVersion=1;caseId='fixture';computer=$env:COMPUTERNAME;
        userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;pupPattern='(?!)';
        allowedRoots=@($run);paths=@();registryKeys=@();registryValues=@();services=@();drivers=@();tasks=@();
        processes=@();svchostGroups=@();manualChecks=@($reviews);
        reviewContext=[pscustomobject]@{id=$contextId;phase='final';notBefore=[DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o')}}
}
function Run-Fixture($Plan,[string]$Script,[string]$Scenario,[string[]]$Extra=@()) {
    $planPath=Join-Path $run ([guid]::NewGuid().ToString('N')+'.json')
    [IO.File]::WriteAllText($planPath,($Plan|ConvertTo-Json -Depth 12),[Text.Encoding]::UTF8)
    $env:PUP_TEST_SCENARIO=$Scenario
    # PS 5.1 surfaces native stderr as error records; capture expected rejections.
    $savedPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $output=& $shell -NoProfile -File (Join-Path $toolDir $Script) -Config $planPath -CaseDir $run @Extra 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    [IO.File]::WriteAllText((Join-Path $run ($Scenario+'-'+$Script+'.txt')),($output -join "`r`n"),[Text.Encoding]::UTF8)
    return $code
}
Test-Case 'verification-empty-successful-query-is-pass' {
    Assert ((Run-Fixture (Make-Plan) 'verify-clean.ps1' 'clean') -eq 0) 'Expected complete scoped verification'
}
Test-Case 'verification-query-error-is-not-pass' {
    Assert ((Run-Fixture (Make-Plan) 'verify-clean.ps1' 'query-error') -eq 1) 'Query failure reported as clean'
}
Test-Case 'verification-missing-manual-review-is-skip' {
    $plan=Make-Plan; $plan.manualChecks=@()
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'skip') -eq 2) 'Missing coverage reported as clean'
}
Test-Case 'baseline-pass-cannot-be-reused-for-final' {
    $plan=Make-Plan
    foreach ($review in $plan.manualChecks) { $review.phase='baseline' }
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'stale-phase') -eq 2) 'Baseline PASS reused for final'
}
Test-Case 'current-baseline-context-is-accepted-only-as-baseline' {
    $plan=Make-Plan; $plan.reviewContext.phase='baseline'
    foreach($review in $plan.manualChecks){$review.phase='baseline'}
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'baseline-context' @('-Phase','baseline')) -eq 0) 'Valid baseline context rejected'
}
Test-Case 'invalid-or-future-review-time-is-skip' {
    $plan=Make-Plan; $plan.manualChecks[0].reviewedAt='not-a-time'
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'invalid-review-time') -eq 2) 'Invalid time accepted'
    $plan=Make-Plan; $plan.manualChecks[0].reviewedAt=[DateTimeOffset]::UtcNow.AddDays(1).ToString('o')
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'future-review-time') -eq 2) 'Future time accepted'
}
Test-Case 'missing-evidence-or-wrong-context-is-skip' {
    $plan=Make-Plan; $plan.manualChecks[0].evidence='absent-evidence.txt'
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'missing-evidence') -eq 2) 'Absent evidence accepted'
    $plan=Make-Plan; $plan.manualChecks[0].contextId='old-context'
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'wrong-context') -eq 2) 'Stale context accepted'
}
Test-Case 'review-before-last-mutation-is-skip' {
    $plan=Make-Plan
    $marker=[pscustomobject]@{caseId=$plan.caseId;computer=$plan.computer;userSid=$plan.userSid;updatedAt=[DateTimeOffset]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText((Join-Path $run 'last-mutation.json'),($marker|ConvertTo-Json))
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'stale-mutation') -eq 2) 'Pre-mutation review accepted'
}
Test-Case 'verification-existing-target-is-fail' {
    $target=Join-Path $run 'residue.txt'; [IO.File]::WriteAllText($target,'fixture')
    $plan=Make-Plan; $plan.paths=@([pscustomobject]@{path=$target;kind='file'})
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'residue') -eq 1) 'Existing target missed'
}
Test-Case 'deleted-registration-but-loaded-driver-is-fail' {
    $plan=Make-Plan; $plan.drivers=@([pscustomobject]@{name='FixtureProtect';expectedPath='C:\Fixture\guard.sys'})
    Assert ((Run-Fixture $plan 'verify-clean.ps1' 'loaded-driver') -eq 1) 'Loaded driver missed after registration deletion'
}
Test-Case 'changed-plan-hash-blocks-execution' {
    Assert ((Run-Fixture (Make-Plan) 'unprotect.ps1' 'bad-hash' @('-ApprovedPlanHash','invalid')) -eq 1) 'Hash mismatch accepted'
}
Test-Case 'wrong-machine-blocks-execution' {
    $plan=Make-Plan; $plan.computer='OTHER-COMPUTER'
    Assert ((Run-Fixture $plan 'unprotect.ps1' 'wrong-machine' @('-DryRun')) -eq 1) 'Wrong machine accepted'
}
Test-Case 'legacy-config-is-rejected' {
    $plan=Make-Plan; $plan | Add-Member NoteProperty killPattern '.*'
    Assert ((Run-Fixture $plan 'cleanup-residue.ps1' 'legacy' @('-DryRun')) -eq 1) 'Legacy destructive pattern accepted'
}
Test-Case 'dry-run-does-not-delete-a-file' {
    $target=Join-Path $run 'dry-run.txt'; [IO.File]::WriteAllText($target,'keep')
    $plan=Make-Plan; $plan.paths=@([pscustomobject]@{path=$target;kind='file'})
    Assert ((Run-Fixture $plan 'cleanup-residue.ps1' 'dry-run' @('-DryRun')) -eq 0) 'Dry run failed'
    Assert (Test-Path -LiteralPath $target) 'Dry run deleted the target'
}
Test-Case 'svchost-unapproved-member-rejected' {
    $plan=Make-Plan; $plan.svchostGroups=@([pscustomobject]@{name='FixtureGp';expectedMembers=@('NotApproved')})
    Assert ((Run-Fixture $plan 'unprotect.ps1' 'shared-group' @('-DryRun')) -eq 1) 'Shared group accepted'
}
Test-Case 'plan-bytes-and-hash-are-bound' {
    $plan=Make-Plan; $file=Join-Path $run 'hash-binding.json'
    [IO.File]::WriteAllText($file,($plan|ConvertTo-Json -Depth 10),[Text.Encoding]::UTF8)
    $loaded=Read-Plan $file
    Assert ($script:PlanHash -eq (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash) 'Parsed bytes hash differs'
    $before=[Text.Encoding]::UTF8.GetString($script:PlanBytes)
    [IO.File]::WriteAllText($file,'changed')
    Assert ([Text.Encoding]::UTF8.GetString($script:PlanBytes) -eq $before) 'Plan snapshot changed with disk file'
}
Test-Case 'backup-failure-blocks-service-mutation' {
    # Direct helper test with mocked reg.exe; no Windows service interface is called.
    $script:RunDir=$run; $script:BackupIndex=0
    function reg.exe { $global:LASTEXITCODE=5; return 'Denied' }
    Reject { Backup-Reg 'HKLM:\SYSTEM\CurrentControlSet\Services\FixtureOnly' }
}
Test-Case 'cleanup-backup-failure-prevents-registry-delete' {
    $plan=Make-Plan; $plan.registryKeys=@('HKCU:\SOFTWARE\FixtureOnly')
    $file=Join-Path $run 'cleanup-backup-plan.json'
    [IO.File]::WriteAllText($file,($plan|ConvertTo-Json -Depth 10),[Text.Encoding]::UTF8)
    $hash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $env:PUP_TEST_SCENARIO='backup-block'
    $output=& $shell -NoProfile -File (Join-Path $toolDir 'cleanup-residue.ps1') -Config $file -CaseDir $run -ApprovedPlanHash $hash 2>&1
    Assert ($LASTEXITCODE -eq 1) 'Backup failure not reported'
    $text=$output -join "`n"
    Assert ($text.Contains('Backup failed; mutation blocked')) 'Backup gate not reached'
    Assert (-not $text.Contains('Mutation must never be reached')) 'Deletion attempted after backup failure'
}

# Success-chain fixtures override system IO only, preserving production control flow.
$stateToolDir=Join-Path $run 'stateful-tools'; [void](New-Item -ItemType Directory $stateToolDir)
foreach ($name in @('common.ps1','unprotect.ps1','cleanup-residue.ps1','verify-clean.ps1')) {
    Copy-Item -LiteralPath (Join-Path $source "scripts/$name") -Destination $stateToolDir
}
[IO.File]::AppendAllText((Join-Path $stateToolDir 'common.ps1'), "`r`n"+[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'stateful-mocks.ps1')),[Text.Encoding]::ASCII)
function New-State {
    return [pscustomobject]@{events=@();failBackup=@();failSc='';failTaskExport=$false;dropPendingWrite=$false;lockedFiles=@();processes=@();
        registry=@(
            [pscustomobject]@{path='HKLM:\SYSTEM\CurrentControlSet\Services\FixtureGuard';values=@(
                [pscustomobject]@{name='ImagePath';data='C:\Fixture\guard.exe'},[pscustomobject]@{name='Start';data=2})},
            [pscustomobject]@{path='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Svchost';values=@(
                [pscustomobject]@{name='FixtureGp';data=@('FixtureGuard')})},
            [pscustomobject]@{path='HKCU:\SOFTWARE\FixtureProduct';values=@([pscustomobject]@{name='';data='old-default'})},
            [pscustomobject]@{path='HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run';values=@([pscustomobject]@{name='FixtureRun';data='fixture.exe /autorun'})},
            [pscustomobject]@{path='HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager';values=@(
                [pscustomobject]@{name='PendingFileRenameOperations';data=@('\??\C:\Existing\old.tmp','')})}
        );tasks=@([pscustomobject]@{path='\';name='FixtureTask';execute='fixture.exe';arguments='/autorun'})}
}
function Run-Stateful($Plan,$State,[string]$Script,[string]$Scenario,[string[]]$Extra=@()) {
    $caseDir=Join-Path $run ('stateful-'+$Scenario); [void](New-Item -ItemType Directory $caseDir -Force)
    $planPath=Join-Path $caseDir 'plan.json'; $statePath=Join-Path $caseDir 'state.json'
    [IO.File]::WriteAllText($planPath,($Plan|ConvertTo-Json -Depth 20),[Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($statePath,($State|ConvertTo-Json -Depth 20),[Text.Encoding]::UTF8)
    $env:PUP_STATE_PATH=$statePath; $env:PUP_STATE_ROOT=$run
    $hash=(Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash
    $savedPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $output=& $shell -NoProfile -File (Join-Path $stateToolDir $Script) -Config $planPath -CaseDir $caseDir -ApprovedPlanHash $hash @Extra 2>&1
        $code=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    [IO.File]::WriteAllText((Join-Path $caseDir 'output.txt'),($output -join "`r`n"),[Text.Encoding]::UTF8)
    $changed=[IO.File]::ReadAllText($statePath)|ConvertFrom-Json
    $summaryFiles=@(Get-ChildItem -LiteralPath $caseDir -Filter 'results.json' -Recurse)
    $summary=$null; if($summaryFiles.Count){$summary=[IO.File]::ReadAllText($summaryFiles[0].FullName)|ConvertFrom-Json}
    return [pscustomobject]@{code=$code;state=$changed;summary=$summary;caseDir=$caseDir;output=($output -join "`n")}
}
Test-Case 'service-backup-disable-stop-delete-success-order' {
    $plan=Make-Plan; $state=New-State
    $plan.services=@([pscustomobject]@{name='FixtureGuard';expectedPath='C:\Fixture\guard.exe'})
    $plan.svchostGroups=@([pscustomobject]@{name='FixtureGp';expectedMembers=@('FixtureGuard')})
    $result=Run-Stateful $plan $state 'unprotect.ps1' 'service-success'
    Assert ($result.code -eq 2 -and $result.summary.rebootRequired) 'Service removal did not request reboot'
    $events=@($result.state.events)
    Assert (($events[0..6] -join '|') -eq 'backup:HKLM:\SYSTEM\CurrentControlSet\Services\FixtureGuard|sc:config:FixtureGuard|sc-success:config:FixtureGuard|sc:stop:FixtureGuard|sc-success:stop:FixtureGuard|sc:delete:FixtureGuard|sc-success:delete:FixtureGuard') 'Service call order incorrect'
    Assert (-not @($result.state.registry | Where-Object path -eq 'HKLM:\SYSTEM\CurrentControlSet\Services\FixtureGuard').Count) 'Service registration remains'
    Assert (@(($result.state.registry | Where-Object path -like '*Svchost').values).Count -eq 0) 'Svchost group remains'
    Assert (Test-Path -LiteralPath (Join-Path $result.caseDir 'last-mutation.json')) 'Mutation marker missing'
}
Test-Case 'shared-registry-container-blocked-before-backup-or-delete' {
    foreach($key in @('HKLM:\SOFTWARE\WOW6432Node','HKLM:\SOFTWARE\Policies','HKLM:\SOFTWARE\Clients')) {
        $plan=Make-Plan; $state=New-State; $plan.registryKeys=@($key)
        $result=Run-Stateful $plan $state 'cleanup-residue.ps1' ('shared-'+($key -split '\\')[-1])
        Assert ($result.code -eq 1 -and @($result.state.events).Count -eq 0) 'Shared container reached mutation interface'
    }
}
Test-Case 'service-backup-or-stop-failure-blocks-delete' {
    foreach ($mode in @('backup','stop')) {
        $plan=Make-Plan; $state=New-State
        $plan.services=@([pscustomobject]@{name='FixtureGuard';expectedPath='C:\Fixture\guard.exe'})
        if($mode -eq 'backup'){$state.failBackup=@('HKLM:\SYSTEM\CurrentControlSet\Services\FixtureGuard')}else{$state.failSc='stop'}
        $result=Run-Stateful $plan $state 'unprotect.ps1' ('service-failure-'+$mode)
        Assert ($result.code -eq 1) 'Service failure not reported'
        Assert ('sc:delete:FixtureGuard' -notin @($result.state.events)) 'Service deleted after failure'
        if($mode -eq 'backup'){ Assert (-not @($result.state.events | Where-Object { $_ -like 'sc:*' }).Count) 'Service mutated after failed backup' }
    }
}
Test-Case 'task-exported-xml-precedes-deletion' {
    $plan=Make-Plan; $state=New-State
    $plan.tasks=@([pscustomobject]@{path='\';name='FixtureTask';expectedActions='fixture.exe /autorun'})
    $result=Run-Stateful $plan $state 'unprotect.ps1' 'task-success'
    Assert ($result.code -eq 0 -and @($result.state.tasks).Count -eq 0) 'Task not removed'
    Assert (($result.state.events -join '|') -eq 'task-export:FixtureTask|task-delete:FixtureTask') 'Task backup/delete order wrong'
    Assert (@(Get-ChildItem -LiteralPath $result.caseDir -Filter 'task-*.xml' -Recurse).Count -eq 1) 'Task XML backup missing'
}
Test-Case 'task-export-failure-blocks-deletion' {
    $plan=Make-Plan; $state=New-State; $state.failTaskExport=$true
    $plan.tasks=@([pscustomobject]@{path='\';name='FixtureTask';expectedActions='fixture.exe /autorun'})
    $result=Run-Stateful $plan $state 'unprotect.ps1' 'task-failure'
    Assert ($result.code -eq 1 -and @($result.state.tasks).Count -eq 1) 'Task deleted without XML backup'
}
Test-Case 'registry-key-backup-precedes-successful-delete' {
    $plan=Make-Plan; $state=New-State; $plan.registryKeys=@('HKCU:\SOFTWARE\FixtureProduct')
    $result=Run-Stateful $plan $state 'cleanup-residue.ps1' 'key-success'
    Assert ($result.code -eq 0) $result.output
    Assert (($result.state.events -join '|') -eq 'backup:HKCU:\SOFTWARE\FixtureProduct|key-delete:HKCU:\SOFTWARE\FixtureProduct') 'Registry delete order wrong'
}
Test-Case 'named-and-default-registry-value-success-chain' {
    foreach($mode in @('named','default')) {
        $plan=Make-Plan; $state=New-State
        $entry=if($mode -eq 'named'){[pscustomobject]@{key='HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run';name='FixtureRun';expectedData='fixture.exe /autorun'}}
            else{[pscustomobject]@{key='HKCU:\SOFTWARE\FixtureProduct';name='';expectedData='old-default'}}
        $plan.registryValues=@($entry)
        $result=Run-Stateful $plan $state 'cleanup-residue.ps1' ('value-'+$mode)
        Assert ($result.code -eq 0) $result.output
        Assert ($result.state.events[0] -eq "backup:$($entry.key)") 'Value not backed up first'
        Assert (@(($result.state.registry | Where-Object path -eq $entry.key).values).Count -eq 0) 'Value remains'
    }
}
Test-Case 'process-identity-success-stops-only-approved-process' {
    $plan=Make-Plan; $state=New-State; $path=Join-Path $run 'fixture-process.exe'
    $state.processes=@([pscustomobject]@{Name='fixture-process.exe';ProcessId=321001;ExecutablePath=$path;CreationDate='2026-10-08T00:00:00Z'})
    $plan.processes=@([pscustomobject]@{path=$path})
    $result=Run-Stateful $plan $state 'unprotect.ps1' 'process-success'
    Assert ($result.code -eq 0 -and @($result.state.processes).Count -eq 0) 'Approved process not stopped'
    Assert (($result.state.events -join '|') -eq 'process-stop:321001') 'Unexpected process stopped'
}
Test-Case 'ordinary-file-delete-success-clears-target-and-marks-mutation' {
    $plan=Make-Plan; $state=New-State; $path=Join-Path $run 'ordinary-fixture.txt'
    [IO.File]::WriteAllText($path,'fixture'); $plan.paths=@([pscustomobject]@{path=$path;kind='file'})
    $result=Run-Stateful $plan $state 'cleanup-residue.ps1' 'file-success'
    Assert ($result.code -eq 0 -and -not (Test-Path -LiteralPath $path)) 'Fixture file remains'
    Assert (($result.state.events -join '|') -eq "file-delete:$path") 'Unexpected deletion'
    $marker=[IO.File]::ReadAllText((Join-Path $result.caseDir 'last-mutation.json'))|ConvertFrom-Json
    Assert ($marker.caseId -eq $plan.caseId -and (Parse-ReviewTime $marker.updatedAt) -le (Parse-ReviewTime $result.summary.time)) 'End-of-stage mutation marker invalid'
}
Test-Case 'locked-file-pending-append-preserves-existing-pairs' {
    $plan=Make-Plan; $state=New-State; $path=Join-Path $run 'locked-fixture.txt'
    [IO.File]::WriteAllText($path,'fixture'); $state.lockedFiles=@($path)
    $plan.paths=@([pscustomobject]@{path=$path;kind='file'})
    $result=Run-Stateful $plan $state 'cleanup-residue.ps1' 'pending-success' @('-ScheduleLockedFiles')
    Assert ($result.code -eq 2 -and $result.summary.rebootRequired) $result.output
    $pending=@(($result.state.registry | Where-Object path -like '*Session Manager').values[0].data)
    Assert ($pending.Count -eq 4 -and $pending[0] -eq '\??\C:\Existing\old.tmp' -and $pending[1] -eq '' -and
        $pending[2] -eq ('\??\'+$path) -and $pending[3] -eq '') 'Existing pending pairs lost or append malformed'
    Assert (@($result.summary.results | Where-Object status -eq 'PENDING').Count -eq 1) 'Pending state not recorded'
    Assert ($result.state.events[-2] -eq 'backup:HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -and
        $result.state.events[-1] -eq 'pending-write') 'Pending write not backed up'
}
Test-Case 'pending-write-mismatch-is-error' {
    $plan=Make-Plan; $state=New-State; $path=Join-Path $run 'locked-mismatch.txt'
    [IO.File]::WriteAllText($path,'fixture'); $state.lockedFiles=@($path); $state.dropPendingWrite=$true
    $plan.paths=@([pscustomobject]@{path=$path;kind='file'})
    $result=Run-Stateful $plan $state 'cleanup-residue.ps1' 'pending-mismatch' @('-ScheduleLockedFiles')
    Assert ($result.code -eq 1 -and $result.output.Contains('Pending rename write verification failed')) 'Pending mismatch missed'
}
[IO.File]::WriteAllText((Join-Path $run 'test-results.json'),(@($results.ToArray())|ConvertTo-Json -Depth 6),[Text.Encoding]::UTF8)
Write-Host "Test results: $run"
if (@($results | Where-Object { -not $_.passed }).Count) { exit 1 }
