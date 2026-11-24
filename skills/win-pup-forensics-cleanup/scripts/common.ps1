# Shared helpers. ASCII-only for Windows PowerShell 5.1.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-Field($Object, [string]$Name, $Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

function Resolve-TargetPath([string]$Path) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    if ([string]::IsNullOrWhiteSpace($expanded) -or $expanded -match '%[^%]+%' -or
        $expanded -notmatch '^[A-Za-z]:\\' -or $expanded -match '[*?]' -or $expanded.Substring(2).Contains(':')) {
        throw "Expected a local absolute literal path: $Path"
    }
    foreach ($segment in ($expanded.Substring(3) -split '\\')) {
        if ($segment -in @('.','..','')) { continue }
        if ($segment -match '[. ]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
            throw "Ambiguous Windows path segment: $segment"
        }
    }
    return [IO.Path]::GetFullPath($expanded).TrimEnd('\')
}

function Test-Within([string]$Child, [string]$Parent) {
    return $Child.StartsWith($Parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafeTarget([string]$Path, [string[]]$AllowedRoots) {
    $full = Resolve-TargetPath $Path
    $protected = @($env:SystemRoot, $env:USERPROFILE, $env:APPDATA, $env:LOCALAPPDATA,
        $env:ProgramData, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:TEMP,
        (Join-Path $env:SystemDrive 'Users')) | Where-Object { $_ }
    if ($full -match '^[A-Za-z]:$') { throw "Drive root forbidden: $full" }
    foreach ($base in $protected) {
        $base = Resolve-TargetPath $base
        if ($full -eq $base -or (Test-Within $base $full)) { throw "Protected root/ancestor: $full" }
    }
    if (Test-Within $full (Resolve-TargetPath $env:SystemRoot)) { throw "Windows directory forbidden: $full" }
    $allowed = $false
    foreach ($root in $AllowedRoots) {
        if (Test-Within $full (Resolve-TargetPath $root)) { $allowed = $true }
    }
    if (-not $allowed) { throw "Outside allowedRoots (must be a strict descendant): $full" }
    # Check existing ancestors without following junctions/symlinks.
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point forbidden: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

function Get-SafeTree([string]$Path) {
    # Enumerate one level at a time; never traverse a reparse point.
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point forbidden: $Path" }
    $item
    if ($item.PSIsContainer) {
        foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) { Get-SafeTree $child.FullName }
    }
}

function Assert-RegKey([string]$Path) {
    # Only product keys / explicit persistence leaves are accepted.
    if ($Path -notmatch '^(HKLM|HKCU):\\SOFTWARE\\.+|^Registry::HKEY_USERS\\S-1-5-21-[\d-]+\\SOFTWARE\\.+') {
        throw "Registry path not allowed: $Path"
    }
    $tail = ($Path -replace '^.*?SOFTWARE\\', '').TrimEnd('\')
    # WOW6432Node selects a view, not a product. Check shared roots after stripping it.
    if ($tail -eq 'WOW6432Node') { throw "Registry view root forbidden: $Path" }
    $tail = $tail -replace '^WOW6432Node\\', ''
    # CLSID redirection also uses Classes\WOW6432Node, including the user hive.
    if ($tail -eq 'Classes\WOW6432Node') { throw "Classes view root forbidden: $Path" }
    $tail = $tail -replace '^Classes\\WOW6432Node\\', 'Classes\'
    $root = ($tail -split '\\')[0]
    if (-not $tail -or $tail -match '\\\\|/' -or $root -in @('WOW6432Node','Policies','Clients',
        'RegisteredApplications','App Paths','Environment','ODBC','Description','Windows','Windows NT')) {
        throw "Shared registry branch forbidden: $Path"
    }
    $literalStarLeaf = $tail -match '^(WOW6432Node\\)?Classes\\\*\\(shell|shellex)\\[^\\]+(\\.+)?$'
    if (($tail -match '[*?]' -and -not $literalStarLeaf) -or $tail -match '(^|\\)\.\.?($|\\)' -or
        $tail -match '^(WOW6432Node\\)?(Microsoft|Classes)(\\)?$' -or
        $tail -match '^(WOW6432Node\\)?Microsoft\\(Windows|Windows NT)(\\CurrentVersion)?(\\)?$' -or
        $tail -match '(?i)\\(CLSID|Browser Helper Objects|Run|RunOnce|Policies|shell|shellex)\\?$') {
        throw "Broad registry container forbidden: $Path"
    }
    if ($tail -match '^(WOW6432Node\\)?Microsoft\\' -and
        $tail -notmatch '^(WOW6432Node\\)?Microsoft\\Windows\\CurrentVersion\\Explorer\\Browser Helper Objects\\\{[0-9a-fA-F-]{36}\}(\\.*)?$' -and
        $tail -notmatch '^(WOW6432Node\\)?Microsoft\\Windows NT\\CurrentVersion\\Svchost\\[^\\]+$') {
        throw "Microsoft registry keys require a narrow supported persistence leaf: $Path"
    }
    if ($tail -match '^(WOW6432Node\\)?Classes\\' -and
        $tail -notmatch '^(WOW6432Node\\)?Classes\\CLSID\\\{[0-9a-fA-F-]{36}\}(\\.*)?$' -and
        $tail -notmatch '^(WOW6432Node\\)?Classes\\(\*|Directory|Folder|Drive|AllFilesystemObjects)(\\Background)?\\(shell|shellex)\\[^\\]+(\\.+)?$') {
        throw "Classes removal requires an explicit CLSID or shell leaf: $Path"
    }
}

function Read-Plan([string]$Path) {
    # Parse and hash the same immutable in-memory bytes, then archive those bytes.
    $script:PlanBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path).Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $script:PlanHash = ([BitConverter]::ToString($hasher.ComputeHash($script:PlanBytes))).Replace('-','') }
    finally { $hasher.Dispose() }
    $plan = [Text.Encoding]::UTF8.GetString($script:PlanBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
    if ((Get-Field $plan 'schemaVersion') -ne 1) { throw 'Expected schemaVersion=1' }
    if ([string]::IsNullOrWhiteSpace((Get-Field $plan 'caseId' ''))) { throw 'caseId required' }
    if (-not (Get-Field $plan 'computer' '') -or (Get-Field $plan 'userSid' '') -notmatch '^S-1-5-21-[\d-]+$') { throw 'computer and target userSid required' }
    $known = @('schemaVersion','caseId','computer','userSid','pupPattern','allowedRoots','paths','registryKeys',
        'registryValues','services','drivers','tasks','processes','svchostGroups','manualChecks','reviewContext')
    foreach ($property in $plan.PSObject.Properties) {
        if ($property.Name -notin $known) { throw "Unknown plan field: $($property.Name)" }
    }
    $pattern = Get-Field $plan 'pupPattern' ''
    if ($pattern) { [void][regex]::new($pattern) }
    foreach ($entry in @(Get-Field $plan 'paths' @())) {
        [void](Assert-SafeTarget (Get-Field $entry 'path' '') @(Get-Field $plan 'allowedRoots' @()))
        if ((Get-Field $entry 'kind') -notin @('file','directory','shortcut')) { throw 'Invalid path kind' }
        if ((Get-Field $entry 'kind') -eq 'shortcut' -and -not (Get-Field $entry 'expectedTarget' '')) { throw 'Shortcut expectedTarget required' }
    }
    foreach ($key in @(Get-Field $plan 'registryKeys' @())) { Assert-RegKey $key }
    foreach ($entry in @(Get-Field $plan 'registryValues' @())) {
        $key = Get-Field $entry 'key' ''
        if ($key -notmatch '^(HKLM|HKCU):\\SOFTWARE\\.+|^Registry::HKEY_USERS\\S-1-5-21-[\d-]+\\SOFTWARE\\.+') { throw "Invalid value key: $key" }
        if ($key -match '[*?]' -or $key -match '(^|\\)\.\.?($|\\)') { throw "Invalid value key: $key" }
        if (-not $entry.PSObject.Properties['name']) { throw 'Registry value name required (empty means default)' }
        if (-not $entry.PSObject.Properties['expectedData']) { throw 'Registry expectedData required' }
    }
    foreach ($entry in @(Get-Field $plan 'services' @()) + @(Get-Field $plan 'drivers' @())) {
        if ((Get-Field $entry 'name' '') -notmatch '^[A-Za-z0-9_.-]+$' -or
            [string]::IsNullOrWhiteSpace((Get-Field $entry 'expectedPath' ''))) { throw 'Service name and expectedPath required' }
    }
    foreach ($entry in @(Get-Field $plan 'tasks' @())) {
        if ((Get-Field $entry 'path' '') -notmatch '^\\.*\\$|^\\$' -or
            [string]::IsNullOrWhiteSpace((Get-Field $entry 'name' '')) -or
            [string]::IsNullOrWhiteSpace((Get-Field $entry 'expectedActions' ''))) { throw 'Task identity and expectedActions required' }
    }
    foreach ($entry in @(Get-Field $plan 'processes' @())) {
        [void](Assert-SafeTarget (Get-Field $entry 'path' '') @(Get-Field $plan 'allowedRoots' @()))
    }
    foreach ($entry in @(Get-Field $plan 'svchostGroups' @())) {
        if ((Get-Field $entry 'name' '') -notmatch '^[A-Za-z0-9_.-]+$' -or
            @(Get-Field $entry 'expectedMembers' @()).Count -eq 0) { throw 'Svchost group name and expectedMembers required' }
        foreach ($member in @(Get-Field $entry 'expectedMembers' @())) {
            if ($member -notin @(@(Get-Field $plan 'services' @()) | ForEach-Object { $_.name })) {
                throw "Svchost group includes an unapproved service: $member"
            }
        }
    }
    $script:CaseId = $plan.caseId
    return $plan
}

function Assert-Admin {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run elevated; use the target user identity' }
    if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit PowerShell' }
}

function Assert-CaseLocation([string]$CaseDir, $Plan) {
    if ($Plan.computer -ne $env:COMPUTERNAME -or $Plan.userSid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
        throw 'Plan computer/userSid differs from this session; use the target account, not alternate admin credentials'
    }
    $casePath = Resolve-TargetPath $CaseDir
    foreach ($entry in @(Get-Field $Plan 'paths' @())) {
        $target = Resolve-TargetPath $entry.path
        if ($casePath -eq $target -or (Test-Within $casePath $target)) { throw 'Case directory must be outside deletion targets' }
    }
}

function Remove-PlannedValue($Entry, [bool]$Preview) {
    if (-not (Test-Path -LiteralPath $Entry.key)) { Write-Host 'Already absent'; return }
    $item = Get-Item -LiteralPath $Entry.key
    if ($Entry.name -notin $item.GetValueNames()) { Write-Host 'Already absent'; return }
    $actual = $item.GetValue($Entry.name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    if (($actual | ConvertTo-Json -Compress) -cne ($Entry.expectedData | ConvertTo-Json -Compress)) { throw 'Registry value changed' }
    if ($Preview) { Write-Host "[would] delete value $($Entry.key)::$($Entry.name)"; return }
    Backup-Reg $Entry.key
    Mark-Mutation
    # Registry API accepts the empty name used by a default value.
    $writable = Open-WritableRegistryKey $Entry.key
    if ($null -eq $writable) { throw 'Cannot open writable registry key' }
    try { $writable.DeleteValue([string]$Entry.name, $false) } finally { $writable.Dispose() }
    if ($Entry.name -in (Get-Item -LiteralPath $Entry.key).GetValueNames()) { throw 'Registry value remains' }
}

function Open-WritableRegistryKey([string]$Path) {
    if ($Path -match '^HKLM:\\(.+)$') { $base = [Microsoft.Win32.Registry]::LocalMachine; $sub = $matches[1] }
    elseif ($Path -match '^HKCU:\\(.+)$') { $base = [Microsoft.Win32.Registry]::CurrentUser; $sub = $matches[1] }
    elseif ($Path -match '^Registry::HKEY_USERS\\(.+)$') { $base = [Microsoft.Win32.Registry]::Users; $sub = $matches[1] }
    else { throw 'Unsupported registry value path' }
    return $base.OpenSubKey($sub, $true)
}

function Stop-PlannedProcess($Entry, [bool]$Preview) {
    $path = Resolve-TargetPath $Entry.path
    $candidates = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $path })
    foreach ($process in $candidates) {
        if ($process.ProcessId -eq $PID) { throw 'Refusing to terminate the current process' }
        if ($Preview) { Write-Host "[would] stop $($process.ProcessId) $path"; continue }
        $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
        if (-not $fresh) { continue }
        if ($fresh.ExecutablePath -ne $path -or $fresh.CreationDate -ne $process.CreationDate) { throw 'Process identity changed' }
        Mark-Mutation
        Stop-Process -Id $process.ProcessId -Force
    }
}

function Start-CaseRun([string]$CaseDir, [string]$Stage, [string]$PlanPath) {
    $dir = Join-Path $CaseDir ($Stage + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
    [void](New-Item -ItemType Directory -Path $dir -Force)
    [IO.File]::WriteAllBytes((Join-Path $dir 'plan.json'), $script:PlanBytes)
    $script:RunDir = $dir
    $script:CaseRoot = Resolve-TargetPath $CaseDir
    $script:RunStage = $Stage
    $script:VerificationPhase = ''
    $script:MutationAttempted = $false
    $script:Results = New-Object 'System.Collections.Generic.List[object]'
    $script:BackupIndex = 0
    Start-Transcript -Path (Join-Path $dir 'transcript.txt') | Out-Null
    return $dir
}

function Mark-Mutation {
    # Invalidate older reviews BEFORE any mutation attempt; write failure blocks it.
    $marker = [pscustomobject]@{caseId=$script:CaseId;computer=$env:COMPUTERNAME;
        userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
        stage=$script:RunStage;runId=(Split-Path $script:RunDir -Leaf);planHash=$script:PlanHash;
        updatedAt=[DateTimeOffset]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText((Join-Path $script:CaseRoot 'last-mutation.json'),
        ($marker | ConvertTo-Json), [Text.Encoding]::UTF8)
    $script:MutationAttempted = $true
}

function Parse-ReviewTime($Value) {
    if ($Value -is [datetime]) { return [DateTimeOffset]$Value }
    if ($Value -is [DateTimeOffset]) { return $Value }
    $text = [string]$Value
    $parsed = [DateTimeOffset]::MinValue
    if ($text -notmatch '^\d{4}-\d{2}-\d{2}T.+(Z|[+-]\d{2}:\d{2})$' -or
        -not [DateTimeOffset]::TryParse($text,[Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None,[ref]$parsed)) { throw 'Expected ISO 8601 time with offset' }
    return $parsed
}

function Get-ReviewThreshold($Plan, [string]$CaseDir, [string]$Phase) {
    $context = Get-Field $Plan 'reviewContext'
    if (-not (Get-Field $context 'id' '') -or (Get-Field $context 'phase' '') -ne $Phase) {
        throw 'Missing/mismatched reviewContext id or phase'
    }
    $threshold = Parse-ReviewTime (Get-Field $context 'notBefore' '')
    if ($threshold -gt [DateTimeOffset]::UtcNow) { throw 'Review context starts in the future' }
    $markerPath = Join-Path $CaseDir 'last-mutation.json'
    if ($Phase -eq 'final' -and (Test-Path -LiteralPath $markerPath)) {
        $marker = [IO.File]::ReadAllText($markerPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
        if ((Get-Field $marker 'caseId') -ne $Plan.caseId -or (Get-Field $marker 'computer') -ne $Plan.computer -or
            (Get-Field $marker 'userSid') -ne $Plan.userSid) { throw 'Mutation marker identity mismatch' }
        $last = Parse-ReviewTime (Get-Field $marker 'updatedAt' '')
        if ($last -gt $threshold) { $threshold = $last }
    }
    return $threshold
}

function Assert-CurrentReview($Review, $Context, [string]$Phase, [DateTimeOffset]$Threshold, [string]$CaseDir) {
    if ((Get-Field $Review 'status' '') -notin @('PASS','FAIL') -or
        (Get-Field $Review 'phase' '') -ne $Phase -or
        (Get-Field $Review 'contextId' '') -ne $Context.id) { throw 'Review status/phase/contextId missing or stale' }
    $time = Parse-ReviewTime (Get-Field $Review 'reviewedAt' '')
    if ($time -lt $Threshold -or $time -gt [DateTimeOffset]::UtcNow) { throw 'Review predates current phase/mutation or is future-dated' }
    $evidence = [string](Get-Field $Review 'evidence' '')
    $file = ($evidence -split '#',2)[0]
    if (-not $file -or [IO.Path]::IsPathRooted($file)) { throw 'Evidence must name a case-relative file' }
    $path = Assert-SafeTarget (Join-Path $CaseDir $file) @($CaseDir)
    if (-not (Test-Path -LiteralPath $path)) { throw 'Evidence file missing' }
    $item = Get-Item -LiteralPath $path
    if ($item.PSIsContainer -or $item.Length -eq 0) { throw 'Evidence must be a nonempty file' }
}

function Add-Result([string]$Target, [string]$Status, [string]$Detail = '') {
    $script:Results.Add([pscustomobject]@{target=$Target;status=$Status;detail=$Detail;time=(Get-Date).ToString('o')})
    Write-Host "[$Status] $Target $Detail"
}

function Invoke-Checked([string]$Target, [scriptblock]$Action) {
    try { & $Action; Add-Result $Target 'PASS' }
    catch { Add-Result $Target 'ERROR' $_.Exception.Message }
}

function Backup-Reg([string]$Path) {
    $script:BackupIndex++
    $file = Join-Path $script:RunDir ("backup-{0:D4}.reg" -f $script:BackupIndex)
    $native = $Path -replace '^Registry::', '' -replace '^(HKLM|HKCU):\\','$1\'
    $output = & reg.exe export $native $file /y 2>&1
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $file) -or (Get-Item -LiteralPath $file).Length -eq 0) {
        throw "Backup failed; mutation blocked: $Path $output"
    }
    [IO.File]::AppendAllText((Join-Path $script:RunDir 'backup-index.txt'), "$file`t$Path`r`n", [Text.Encoding]::UTF8)
}

function Invoke-Sc([string[]]$Arguments, [int[]]$AllowedCodes = @(0)) {
    $output = & sc.exe @Arguments 2>&1
    $code = $LASTEXITCODE
    Write-Host ($output -join "`n")
    if ($code -notin $AllowedCodes) { throw "sc.exe failed ($code): $($Arguments -join ' ')" }
}

function Finish-CaseRun([string]$Stage, [bool]$DryRun = $false, [bool]$RebootRequired = $false) {
    if ($script:MutationAttempted) { Mark-Mutation }
    $bad = @($script:Results | Where-Object { $_.status -in @('ERROR','FAIL') }).Count
    $skipped = @($script:Results | Where-Object { $_.status -in @('SKIP','PENDING') }).Count
    $summary = [pscustomobject]@{stage=$Stage;dryRun=$DryRun;rebootRequired=$RebootRequired;verificationPhase=$script:VerificationPhase;
        planHash=$script:PlanHash;computer=$env:COMPUTERNAME;userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
        errors=$bad;skipped=$skipped;results=@($script:Results.ToArray());time=(Get-Date).ToString('o')}
    [IO.File]::WriteAllText((Join-Path $script:RunDir 'results.json'), ($summary | ConvertTo-Json -Depth 12), [Text.Encoding]::UTF8)
    Stop-Transcript | Out-Null
    Write-Host "Results: $script:RunDir"
    if ($bad) { return 1 }
    if ($skipped -or $RebootRequired) { return 2 }
    return 0
}
