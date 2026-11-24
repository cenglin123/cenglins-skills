# ============================================================================
# collect-forensics.ps1  --  READ-ONLY forensic collection for Windows PUP/bundleware
# Usage:
#   powershell -NoProfile -File collect-forensics.ps1 -OutDir '<case>\evidence-01'
#   powershell ... -File collect-forensics.ps1 -PupPattern 'MyVendorName|FooBar'
#   powershell ... -File collect-forensics.ps1 -OutDir '<case>\evidence-01' -Resume
# Output: evidence files + collection-status.json (updated per section) +
#         evidence-hashes.json (updated per section) + collection-done.json (DONE marker)
# NOTE: read-only. Do NOT delete anything before this finishes.
#       Run elevated to see all services/drivers/user hives.
#       No case defaults; discovery collects all services/drivers and directory times.
#       Each section is persisted immediately; if the process is killed, re-run with
#       -Resume to continue (completed sections are skipped, their evidence kept).
#       Keep this file ASCII-only (PowerShell 5.1 reads BOM-less .ps1 as ANSI).
# ============================================================================
param(
  [string]$PupPattern = '(?!)',
  [string]$DnsPattern = '(?!)',
  [Parameter(Mandatory=$true)][string]$OutDir,
  [ValidateRange(1,45)][int]$ProbeSeconds=15,
  [ValidateRange(1,100000)][int]$MaxEntries=20000,
  [ValidateRange(1,10000)][int]$MaxRecords=2000,
  [ValidateRange(1,500)][int]$MaxHashFiles=50,
  [ValidateRange(1,2147483647)][long]$MaxHashBytes=536870912,
  [ValidateRange(256,8388608)][int]$MaxTextBytes=1048576,
  [ValidateRange(65536,268435456)][int]$MaxShimBytes=33554432,
  [ValidateRange(1048576,2147483647)][long]$MaxSruBytes=134217728,
  [switch]$SkipHashes,
  [switch]$SkipSruDatabase,
  [switch]$CollectTempFootprint,
  [ValidateCount(0,20)][string[]]$TempRoots=@($env:TEMP,(Join-Path $env:SystemRoot 'Temp')),
  [switch]$CollectToolLogs,
  [ValidateCount(0,20)][string[]]$ToolLogPaths=@((Join-Path $env:SystemRoot 'Logs\DISM\dism.log'),(Join-Path $env:SystemRoot 'Logs\CBS\CBS.log')),
  [ValidateCount(0,20)][string[]]$CandidateConfigFiles=@(),
  [switch]$CollectTimelineBuckets,
  [switch]$Resume
)
. (Join-Path $PSScriptRoot 'bounded-probes.ps1')
$ErrorActionPreference = 'SilentlyContinue'
if (-not [Environment]::Is64BitProcess) {
  # Under ErrorActionPreference=SilentlyContinue a bare throw continues; refuse explicitly.
  [Console]::Error.WriteLine('Use 64-bit PowerShell'); exit 2
}
try { [void][regex]::new($PupPattern) } catch { [Console]::Error.WriteLine('Invalid -PupPattern: ' + $_.Exception.Message); exit 2 }
try { [void][regex]::new($DnsPattern) } catch { [Console]::Error.WriteLine('Invalid -DnsPattern: ' + $_.Exception.Message); exit 2 }
if (Test-Path -LiteralPath $OutDir) {
  if (-not $Resume) {
    [Console]::Error.WriteLine('OutDir must be new; never overwrite evidence (use -Resume to continue an interrupted run)')
    exit 2
  }
} else {
  if ($Resume) {
    [Console]::Error.WriteLine('Resume requires an existing OutDir from an interrupted run')
    exit 2
  }
  try { [void](New-Item -ItemType Directory -Path $OutDir -ErrorAction Stop) }
  catch { [Console]::Error.WriteLine('Cannot create OutDir: ' + $_.Exception.Message); exit 2 }
}
$script:ResumeActive = [bool]$Resume
$script:StartedAt = (Get-Date).ToString('o')
$sections = New-Object 'System.Collections.Generic.List[object]'
$script:AcquisitionErrors = New-Object 'System.Collections.Generic.List[string]'
$script:ItemErrors = New-Object 'System.Collections.Generic.List[string]'
$script:HashCache = @{}
$script:ResumeSkip = @()
$Error.Clear()
# PowerShell names are case-insensitive: never reuse 'EvidenceBuf' as a loop variable.
$script:EvidenceBuf = New-Object System.Collections.Generic.List[string]
function W($s=""){ $script:EvidenceBuf.Add([string]$s) }
function Test-Skip([string]$File){ return ($script:ResumeSkip -contains $File) }
# Safe script-variable reader (tests extract single functions under strict mode).
# Get-Variable keeps this self-contained: no helper function is needed by extracted copies.
function Get-PropText($Object,[string]$Name){
  if($null -ne $Object -and $Object.PSObject.Properties[$Name]){ return [string]$Object.$Name }
  return '(absent)'
}
function Add-ItemError([string]$Item,[string]$Message){
  # Per-item failures are recorded in the section but do not fail the whole section.
  $script:ItemErrors.Add(('{0}: {1}' -f $Item,$Message))
  W ('# ITEM ERROR: {0}: {1}' -f $Item,$Message)
  if($Error.Count -gt 0){ $Error.RemoveAt(0) }
}
function Get-ResumeFlag {
  $variable=Get-Variable -Name ResumeActive -Scope Script -ErrorAction Ignore
  if($variable){ return [bool]$variable.Value }
  return $false
}
function Backup-EvidenceFile([string]$Path){
  # Preserve an existing artifact before a resume overwrite. Returns $false when the
  # existing file could not be moved; the caller must then NOT overwrite the original.
  if(-not (Test-Path -LiteralPath $Path -PathType Leaf)){ return $true }
  if(-not (Get-ResumeFlag)){ return $true }
  $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
  $candidate=('{0}.prev-{1}' -f $Path,$stamp)
  $counter=1
  while(Test-Path -LiteralPath $candidate){
    $candidate=('{0}.prev-{1}-{2}' -f $Path,$stamp,$counter)
    $counter++
  }
  try { [IO.File]::Move($Path,$candidate); return $true }
  catch { Add-ItemError ('backup:' + [IO.Path]::GetFileName($Path)) ('previous file not preserved: ' + $_.Exception.Message); return $false }
}
function Get-EvidenceHashList {
  $root=[IO.Path]::GetFullPath($OutDir).TrimEnd('\')
  $cacheVariable=Get-Variable -Name HashCache -Scope Script -ErrorAction Ignore
  $cache=@{}
  if($cacheVariable -and $cacheVariable.Value){ $cache=$cacheVariable.Value }
  @(Get-ChildItem -LiteralPath $OutDir -File -Recurse | Where-Object { $_.Extension -ne '.tmp' -and $_.Name -ne 'evidence-hashes.json' } | ForEach-Object {
    $key=$_.FullName
    $stamp=('{0}|{1}' -f $_.Length,$_.LastWriteTimeUtc.Ticks)
    $cached=$cache[$key]
    if($cached -and $cached.stamp -eq $stamp){ $hash=$cached.sha }
    else {
      # Probe stdout/stderr can still be held by the worker process; record the failure
      # in the hash list instead of letting it contaminate the next section's error list.
      $errorBaseline=$Error.Count
      try { $hash=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256 -ErrorAction Stop).Hash }
      catch { $hash='(hash-failed: ' + $_.Exception.Message + ')'; $cache[$key]=$null }
      finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
      if($hash -notlike '(hash-failed*'){ $cache[$key]=[pscustomobject]@{stamp=$stamp;sha=$hash} }
    }
    [pscustomobject]@{file=$_.FullName.Substring($root.Length+1);sha256=$hash;length=$_.Length}
  })
  Set-Variable -Name HashCache -Value $cache -Scope Script
}
function Write-CollectionState([bool]$Complete){
  if(-not $OutDir){ return }
  $startedVariable=Get-Variable -Name StartedAt -Scope Script -ErrorAction Ignore
  $startedValue=if($startedVariable){$startedVariable.Value}else{$null}
  $pupVariable=Get-Variable -Name PupPattern -Scope Script -ErrorAction Ignore
  $pupValue=if($pupVariable){$pupVariable.Value}else{''}
  $dnsVariable=Get-Variable -Name DnsPattern -Scope Script -ErrorAction Ignore
  $dnsValue=if($dnsVariable){$dnsVariable.Value}else{''}
  $probeVariable=Get-Variable -Name ProbeSeconds -Scope Script -ErrorAction Ignore
  $probeValue=if($probeVariable){$probeVariable.Value}else{0}
  $entriesVariable=Get-Variable -Name MaxEntries -Scope Script -ErrorAction Ignore
  $entriesValue=if($entriesVariable){$entriesVariable.Value}else{0}
  $recordsVariable=Get-Variable -Name MaxRecords -Scope Script -ErrorAction Ignore
  $recordsValue=if($recordsVariable){$recordsVariable.Value}else{0}
  $resumeVariable=Get-Variable -Name ResumeActive -Scope Script -ErrorAction Ignore
  $resumeValue=if($resumeVariable){$resumeVariable.Value}else{$false}
  $skipVariable=Get-Variable -Name ResumeSkip -Scope Script -ErrorAction Ignore
  $skipValue=if($skipVariable){$skipVariable.Value}else{@()}
  # Build the typed array outside the object literal: $( ... ) over an empty array is
  # unrolled to {} on 5.1 and $null on 7, breaking the JSON contract.
  $skipItems=@($skipValue | Where-Object { $_ })
  $resumeSkipped=[string[]]::new(0)
  if($skipItems.Count -gt 0){ $resumeSkipped=[string[]]@($skipItems) }
  $status=[pscustomobject]@{
    startedAt=$startedValue;finishedAt=(Get-Date).ToString('o');computer=$env:COMPUTERNAME
    userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    powershell=$PSVersionTable.PSVersion.ToString();is64Bit=[Environment]::Is64BitProcess
    elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    pupPattern=$pupValue;dnsPattern=$dnsValue
    probeSeconds=$probeValue;maxEntries=$entriesValue;maxRecords=$recordsValue
    complete=$Complete;resumed=[bool]$resumeValue
    resumeSkipped=$resumeSkipped
    sections=@($sections.ToArray())
    scope='Current identity and loaded hives only; browser/policies/unloaded profiles require manual review'
  }
  [IO.File]::WriteAllText((Join-Path $OutDir 'collection-status.json'),($status|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
  $hashes=@(Get-EvidenceHashList)
  [IO.File]::WriteAllText((Join-Path $OutDir 'evidence-hashes.json'),($hashes|ConvertTo-Json -Depth 5),[Text.Encoding]::UTF8)
}
function Complete-Collection {
  $script:FinishedAt=(Get-Date).ToString('o')
  Write-CollectionState $true
  $done=[pscustomobject]@{complete=$true;startedAt=$script:StartedAt;finishedAt=$script:FinishedAt;
    computer=$env:COMPUTERNAME;userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    sections=@($sections.ToArray()).Count
    errors=@($sections|Where-Object status -eq 'ERROR').Count
    partial=@($sections|Where-Object status -eq 'PARTIAL').Count
    skipped=@($sections|Where-Object status -eq 'SKIP').Count
    resumed=[bool]$script:ResumeActive}
  [IO.File]::WriteAllText((Join-Path $OutDir 'collection-done.json'),($done|ConvertTo-Json),[Text.Encoding]::UTF8)
  Write-CollectionState $true
}
function Get-ResumePlan([string]$Path){
  # Returns {refused, skip, sections}; throwing is unreliable under ErrorActionPreference=SilentlyContinue.
  if(Test-Path -LiteralPath (Join-Path $Path 'collection-done.json')){
    return [pscustomobject]@{refused='completed';skip=@();sections=@()}
  }
  $statusPath=Join-Path $Path 'collection-status.json'
  if(-not (Test-Path -LiteralPath $statusPath)){
    return [pscustomobject]@{refused='no-status';skip=@();sections=@()}
  }
  $previous=[IO.File]::ReadAllText($statusPath,[Text.Encoding]::UTF8)|ConvertFrom-Json
  $skip=@(); $kept=@()
  foreach($entry in @($previous.sections)){
    $file=[string]$entry.file
    if(-not $file){ continue }
    $evidence=Join-Path $Path $file
    if($entry.status -in @('PASS','PARTIAL','SKIP') -and (Test-Path -LiteralPath $evidence) -and (Get-Item -LiteralPath $evidence).Length -gt 0){
      $skip+=$file; $kept+=$entry
    }
  }
  return [pscustomobject]@{refused='';skip=[string[]]$skip;sections=@($kept)}
}
function Save($name,[bool]$Enabled=$true,[string]$DataState='AUTO',$Details=$null){
  $target=Join-Path $OutDir $name
  $writeTarget=$target
  $resumeActiveVariable=Get-Variable -Name ResumeActive -Scope Script -ErrorAction Ignore
  $resumeActive=if($resumeActiveVariable){[bool]$resumeActiveVariable.Value}else{$false}
  if($resumeActive -and (Test-Path -LiteralPath $target)){
    $backupCommand=Get-Command -Name Backup-EvidenceFile -CommandType Function -ErrorAction Ignore
    $backedUp=$true
    if($backupCommand){ $backedUp=Backup-EvidenceFile $target }
    else {
      $previousFile=('{0}.prev-{1}' -f $target,(Get-Date -Format 'yyyyMMdd-HHmmss'))
      try { [IO.File]::Move($target,$previousFile) } catch { $backedUp=$false }
    }
    if(-not $backedUp){
      $writeTarget=('{0}.unpreserved-{1}' -f $target,(Get-Date -Format 'yyyyMMdd-HHmmss'))
      $script:AcquisitionErrors.Add('Existing evidence could not be backed up; new section written to ' + [IO.Path]::GetFileName($writeTarget))
    }
  }
  # Missing optional keys/streams are absence; all other collection errors are explicit.
  $failures = @($Error | Where-Object { $_.FullyQualifiedErrorId -notmatch '^(PathNotFound|ItemNotFound|GetContentReaderFileNotFoundError)' } | ForEach-Object { $_.ToString() }) + @($script:AcquisitionErrors.ToArray())
  $rows=@($script:EvidenceBuf|Where-Object { $_ -and -not $_.StartsWith('#') }).Count
  if(-not $Enabled){$DataState='NOT_ENABLED'}elseif($DataState -eq 'AUTO'){$DataState=if($rows){'COMPLETE'}else{'NO_MATCH'}}
  $status = if($failures.Count -or $DataState -eq 'ERROR'){'ERROR'}elseif(-not $Enabled){'SKIP'}elseif($DataState -eq 'PARTIAL'){'PARTIAL'}else{'PASS'}
  W "# COLLECTION STATUS: $status (means acquisition succeeded, not clean)"
  foreach($failure in $failures){ W "# ERROR: $failure" }
  [IO.File]::WriteAllText($writeTarget, ($script:EvidenceBuf -join "`r`n"), [Text.Encoding]::UTF8)
  $itemErrorsVariable=Get-Variable -Name ItemErrors -Scope Script -ErrorAction Ignore
  # [string[]] over an empty array is $null in PowerShell; build an explicit empty array.
  $itemErrors=[string[]]::new(0)
  if($itemErrorsVariable -and $null -ne $itemErrorsVariable.Value -and @($itemErrorsVariable.Value).Count -gt 0){
    $itemErrors=[string[]]@($itemErrorsVariable.Value | ForEach-Object { [string]$_ })
  }
  $sections.Add([pscustomobject]@{file=$name;status=$status;enabled=$Enabled;dataState=$DataState;dataRows=$rows;
    details=$Details;itemErrors=$itemErrors;errors=$failures;time=(Get-Date).ToString('o')})
  $script:EvidenceBuf.Clear(); $script:AcquisitionErrors.Clear()
  if($itemErrorsVariable){ $itemErrorsVariable.Value.Clear() }
  $Error.Clear()
  # Persist status and hashes immediately so a killed run leaves resumable state.
  # (Legacy unit tests extract Save alone; skip when the writer is not loaded.)
  $stateWriter=Get-Command -Name Write-CollectionState -CommandType Function -ErrorAction Ignore
  if($stateWriter){ Write-CollectionState $false }
}
function Invoke-EvidenceNative([string]$Command,[string[]]$Arguments) {
  try {
    $application=Get-Command -Name $Command -CommandType Application -ErrorAction Stop
    $output=@(& $application.Source @Arguments 2>&1)
    $code=$LASTEXITCODE
    if($code -ne 0){$script:AcquisitionErrors.Add("Native command failed ($code): $Command $($Arguments -join ' ')")}
    return $output
  } catch { $script:AcquisitionErrors.Add("Native command unavailable/failed: $Command $($_.Exception.Message)") }
}
function Invoke-NativeText([string]$Command,[string[]]$Arguments) {
  # Native helper whose failure is item-level (e.g. fltmc needs elevation), not section-level.
  try {
    $application=Get-Command -Name $Command -CommandType Application -ErrorAction Stop
    $output=@(& $application.Source @Arguments 2>&1)
    $code=$LASTEXITCODE
    return [pscustomobject]@{ok=($code -eq 0);code=$code;lines=$output}
  } catch {
    return [pscustomobject]@{ok=$false;code=$null;lines=@($_.Exception.Message)}
  }
}
function BinaryPath($command){
  $p = [Environment]::ExpandEnvironmentVariables([string]$command)
  if($p -match '^"([^"]+)"'){ $p=$matches[1] }
  elseif($p -match '^(.+?\.(exe|sys|dll))(\s|$)'){ $p=$matches[1] }
  $p=$p -replace '^\\\?\?\\',''
  $p=$p -replace '^\\SystemRoot', $env:SystemRoot
  if($p -match '^System32\\'){ $p=Join-Path $env:SystemRoot $p }
  return $p
}
function Sig($p){ try{ if($p -and (Test-Path -LiteralPath $p)){ $s=Get-AuthenticodeSignature -LiteralPath $p; if($s.SignerCertificate){return "$($s.Status) | $($s.SignerCertificate.Subject)"}; return "$($s.Status)" } }catch{}; return '(n/a)' }

# ---- per-item writers: a single bad item must never fail the whole section ----
function Write-ServiceEntry($Service){
  $name=[string](Get-PropText $Service 'Name')
  $errorBaseline=$Error.Count
  try {
    $path=[string](Get-PropText $Service 'PathName')
    $signature=if($path -and $path -ne '(absent)'){ Sig (BinaryPath $path) } else { '(n/a)' }
    W ("{0} [{1}/{2}] {3}`n    {4}`n    SIG: {5}" -f $name,(Get-PropText $Service 'State'),(Get-PropText $Service 'StartMode'),
      (Get-PropText $Service 'DisplayName'),$path,$signature)
    $serviceKey="HKLM:\SYSTEM\CurrentControlSet\Services\$name"
    $key=Get-Item -LiteralPath $serviceKey -ErrorAction SilentlyContinue
    $image=if($key){ $key.GetValue('ImagePath',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } else { '(registry key missing - orphan or removed registration)' }
    W ("    REG ImagePath (exact plan value): " + $image)
    $parameters="$serviceKey\Parameters"
    if(Test-Path -LiteralPath $parameters){
      $dll=(Get-ItemProperty -LiteralPath $parameters -ErrorAction SilentlyContinue).ServiceDll
      if($dll){ W ("    ServiceDll: " + $dll + " SIG: " + (Sig (BinaryPath $dll))) }
    }
  } catch { Add-ItemError $name $_.Exception.Message }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
}
function Write-DriverEntry($Driver){
  $name=[string](Get-PropText $Driver 'Name')
  $errorBaseline=$Error.Count
  try {
    $path=BinaryPath (Get-PropText $Driver 'PathName')
    W ("{0} [{1}/{2}] {3}`n    SIG: {4}" -f $name,(Get-PropText $Driver 'State'),(Get-PropText $Driver 'StartMode'),$path,(Sig $path))
    $driverKey="HKLM:\SYSTEM\CurrentControlSet\Services\$name"
    $key=Get-Item -LiteralPath $driverKey -ErrorAction SilentlyContinue
    $image=if($key){ $key.GetValue('ImagePath',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } else { '(registry key missing - loaded without registration)' }
    W ("    REG ImagePath (exact plan value): " + $image)
  } catch { Add-ItemError $name $_.Exception.Message }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
}
function Add-FilterDriverState {
  W ''
  W '# File filter drivers (fltmc filters / instances; elevation required)'
  foreach($command in @('filters','instances')){
    $result=Invoke-NativeText 'fltmc.exe' @($command)
    if($result.ok){
      foreach($line in $result.lines){ if([string]$line -match '\S'){ W ('fltmc ' + $command + ': ' + ([string]$line).Trim()) } }
    } else {
      W ('fltmc ' + $command + ': unavailable (exit ' + $result.code + ')')
    }
  }
}

# ---- execution artifacts (A3): prefetch / shimcache / BAM / SRUDB ----
function Collect-Prefetch([string]$PrefetchDir,[int]$Limit){
  W "# Prefetch: $PrefetchDir"
  $prefetchKey='HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters'
  $parameters=Get-Item -LiteralPath $prefetchKey -ErrorAction SilentlyContinue
  if($parameters){
    W ('EnablePrefetcher=' + $parameters.GetValue('EnablePrefetcher','(absent)') + ' EnableSuperfetch=' + $parameters.GetValue('EnableSuperfetch','(absent)'))
  }
  if(-not (Test-Path -LiteralPath $PrefetchDir)){
    W '(Prefetch directory not found; elevated read may be required)'
    return
  }
  $errorBaseline=$Error.Count
  try {
    $files=@(Get-ChildItem -LiteralPath $PrefetchDir -File -ErrorAction Stop | Sort-Object LastWriteTime -Descending | Select-Object -First $Limit)
  } catch { $files=@(); Add-ItemError 'Prefetch' $_.Exception.Message; W ('(Prefetch read denied: ' + $_.Exception.Message + ')') }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  if(-not $files.Count){ W '(no .pf files visible)' }
  foreach($file in $files){ W ('{0} | {1} | {2} bytes' -f $file.Name,$file.LastWriteTime.ToString('o'),$file.Length) }
}
function Collect-ShimCache([string]$KeyPath,[string]$EvidenceDir,[int]$MaxBytes){
  W '# AppCompatCache (ShimCache)'
  $key=Get-Item -LiteralPath $KeyPath -ErrorAction SilentlyContinue
  if(-not $key){ W '(AppCompatCache key not present)'; return }
  $blob=$key.GetValue('AppCompatCache',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  if($null -eq $blob){ W '(AppCompatCache value not present)'; return }
  if($blob -isnot [byte[]]){ W ('(unexpected value type: ' + $blob.GetType().Name + ')'); return }
  $memory=New-Object IO.MemoryStream(,$blob)
  try { $hash=(Get-FileHash -InputStream $memory -Algorithm SHA256).Hash } finally { $memory.Dispose() }
  W ('size=' + $blob.Length + ' sha256=' + $hash)
  if($blob.Length -le $MaxBytes){
    $directory=Join-Path $EvidenceDir 'execution-artifacts'
    [void](New-Item -ItemType Directory -Force -Path $directory)
    $destination=Join-Path $directory 'appcompatcache.bin'
    $backupCommand=Get-Command -Name Backup-EvidenceFile -CommandType Function -ErrorAction Ignore
    $canWrite=if($backupCommand){ Backup-EvidenceFile $destination } else { -not (Test-Path -LiteralPath $destination) }
    if($canWrite){
      [IO.File]::WriteAllBytes($destination,$blob)
      W 'raw: execution-artifacts/appcompatcache.bin'
    } else { W 'raw not preserved: existing appcompatcache.bin could not be backed up; refusing to overwrite' }
  } else {
    W ('raw not preserved: exceeds -MaxShimBytes (' + $MaxBytes + ')')
  }
}
function Collect-Bam([string]$BaseKey,[int]$Limit){
  W '# BAM (Background Activity Moderator; per-user executed programs)'
  $errorBaseline=$Error.Count
  try { $subKeys=@(Get-ChildItem -LiteralPath $BaseKey -ErrorAction Stop) }
  catch { $subKeys=@(); Add-ItemError 'BAM' $_.Exception.Message; W ('(bam UserSettings unreadable: ' + $_.Exception.Message + ')') }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  if($null -eq $subKeys){ $subKeys=@() }
  if(-not $subKeys.Count){ W '(no per-user bam entries visible)' }
  $count=0
  foreach($sub in $subKeys){
    $errorBaseline=$Error.Count
    try { $key=Get-Item -LiteralPath $sub.PSPath -ErrorAction Stop } catch { $key=$null; Add-ItemError ('BAM:'+[string]$sub.PSChildName) $_.Exception.Message }
    finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
    if(-not $key){ continue }
    foreach($valueName in @($key.GetValueNames())){
      if($count -ge $Limit){ W ('(limit reached: ' + $Limit + ')'); return }
      $errorBaseline=$Error.Count
      try {
        $value=$key.GetValue($valueName,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $when='(unparsed)'
        if($value -is [byte[]] -and $value.Length -ge 8){
          $fileTime=[BitConverter]::ToInt64($value,0)
          if($fileTime -gt 0){ $when=[DateTime]::FromFileTime($fileTime).ToString('o') }
        }
        W ('{0} | {1} | {2}' -f $sub.PSChildName,$when,$valueName)
        $count++
      } catch { Add-ItemError ('BAM:'+[string]$valueName) $_.Exception.Message }
      finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
    }
  }
}
function Collect-SruDatabase([string]$DbPath,[long]$MaxBytes,[string]$EvidenceDir){
  W '# SRUDB.dat (user activity database)'
  # Non-elevated Test-Path on a protected path raises ItemExistsUnauthorizedAccessError;
  # treat that as a per-item note, never as a section error (11-17 regression).
  $denied=$false
  $exists=$false
  $errorBaseline=$Error.Count
  try { $exists=[bool](Test-Path -LiteralPath $DbPath -ErrorAction Stop) }
  catch { $denied=$true; Add-ItemError 'SRUDB' $_.Exception.Message; W ('(SRUDB.dat path not accessible: ' + $_.Exception.Message + ')') }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  if($denied){ return }
  if(-not $exists){ W '(SRUDB.dat not found)'; return }
  $item=$null
  $errorBaseline=$Error.Count
  try { $item=Get-Item -LiteralPath $DbPath -ErrorAction Stop }
  catch { Add-ItemError 'SRUDB' $_.Exception.Message; W ('(SRUDB.dat unreadable: ' + $_.Exception.Message + ')') }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  if(-not $item){ return }
  W ('size=' + $item.Length + ' lastWrite=' + $item.LastWriteTime.ToString('o'))
  if($item.Length -gt $MaxBytes){ W ('not copied: exceeds -MaxSruBytes (' + $MaxBytes + ')'); return }
  $directory=Join-Path $EvidenceDir 'execution-artifacts'
  [void](New-Item -ItemType Directory -Force -Path $directory)
  $destination=Join-Path $directory 'SRUDB.dat'
  $backupCommand=Get-Command -Name Backup-EvidenceFile -CommandType Function -ErrorAction Ignore
  $canWrite=if($backupCommand){ Backup-EvidenceFile $destination } else { -not (Test-Path -LiteralPath $destination) }
  if(-not $canWrite){ W 'not copied: existing SRUDB.dat could not be backed up; refusing to overwrite'; return }
  $errorBaseline=$Error.Count
  try {
    [IO.File]::Copy($DbPath,$destination,$true)
    W ('copied: execution-artifacts/SRUDB.dat sha256=' + (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash)
  } catch { Add-ItemError 'SRUDB' $_.Exception.Message; W ('copy failed (database in use?): ' + $_.Exception.Message) }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
}

# ---- security software status (A4) ----
function Collect-SecurityStatus {
  W '# SecurityCenter2 AntiVirusProduct instances'
  $errorBaseline=$Error.Count
  try {
    $products=@(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop)
    if(-not $products.Count){ W '(no AntiVirusProduct instances)' }
    foreach($product in $products){
      try { W ('{0} | productState={1} | {2}' -f (Get-PropText $product 'displayName'),(Get-PropText $product 'productState'),(Get-PropText $product 'pathToSignedProductExe')) }
      catch { Add-ItemError 'SecurityCenter2' $_.Exception.Message }
    }
  } catch { Add-ItemError 'SecurityCenter2' $_.Exception.Message; W ('SecurityCenter2 unavailable: ' + $_.Exception.Message) }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  W '# Defender service'
  $errorBaseline=$Error.Count
  try { $defender=Get-CimInstance Win32_Service -Filter "Name='WinDefend'" -ErrorAction Stop } catch { $defender=$null; Add-ItemError 'WinDefend' $_.Exception.Message }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  if($defender){ W ('WinDefend [{0}/{1}] {2}' -f (Get-PropText $defender 'State'),(Get-PropText $defender 'StartMode'),(Get-PropText $defender 'PathName')) }
  else { W 'WinDefend: no service instance visible' }
  W '# Defender policy values (32/64-bit views are the same key on Win10+)'
  foreach($keyPath in @('HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender','HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection')){
    $key=Get-Item -LiteralPath $keyPath -ErrorAction SilentlyContinue
    if(-not $key){ W ('POLICY ' + $keyPath + ': absent'); continue }
    $names=@($key.GetValueNames())
    if(-not $names.Count){ W ('POLICY ' + $keyPath + ': empty') }
    foreach($valueName in $names){ W ('POLICY {0}::{1} = {2}' -f $keyPath,$valueName,$key.GetValue($valueName)) }
  }
  W '# Defender exclusions (registry leaves)'
  foreach($relative in @('Paths','Extensions','Processes')){
    $keyPath='HKLM:\SOFTWARE\Microsoft\Windows Defender\Exclusions\' + $relative
    $key=Get-Item -LiteralPath $keyPath -ErrorAction SilentlyContinue
    if(-not $key){ W ('EXCLUSION ' + $relative + ': absent'); continue }
    $names=@($key.GetValueNames())
    if(-not $names.Count){ W ('EXCLUSION ' + $relative + ': empty') }
    foreach($valueName in $names){ W ('EXCLUSION {0}: {1}' -f $relative,$valueName) }
  }
  W '# MpPreference (Defender cmdlets; module may be unavailable)'
  $errorBaseline=$Error.Count
  try {
    $preference=Get-MpPreference -ErrorAction Stop
    W ('MpPreference DisableRealtimeMonitoring=' + (Get-PropText $preference 'DisableRealtimeMonitoring'))
    W ('MpPreference DisableBehaviorMonitoring=' + (Get-PropText $preference 'DisableBehaviorMonitoring'))
    W ('MpPreference DisableIOAVProtection=' + (Get-PropText $preference 'DisableIOAVProtection'))
    W ('MpPreference ExclusionPath=' + ((@($preference.ExclusionPath | Where-Object { $_ })) -join ';'))
    W ('MpPreference ExclusionExtension=' + ((@($preference.ExclusionExtension | Where-Object { $_ })) -join ';'))
    W ('MpPreference ExclusionProcess=' + ((@($preference.ExclusionProcess | Where-Object { $_ })) -join ';'))
  } catch { Add-ItemError 'MpPreference' $_.Exception.Message; W ('MpPreference unavailable: ' + $_.Exception.Message) }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
}

# ---- resume wiring: functions above are now defined --------------------------
if ($Resume) {
  $resumePlan = Get-ResumePlan $OutDir
  if ($null -eq $resumePlan -or $resumePlan.refused) {
    $reason = if($resumePlan){ $resumePlan.refused } else { 'unreadable' }
    [Console]::Error.WriteLine('Resume refused (' + $reason + '); use a new OutDir')
    exit 2
  }
  $script:ResumeSkip = @($resumePlan.skip)
  foreach ($previous in @($resumePlan.sections)) { $sections.Add($previous) }
}

# ---- 1. Installed programs + signatures ------------------------------------
if(-not (Test-Skip '01-programs.txt')){
W "# Installed programs (PUP flagged)  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$paths=@('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')
Get-ItemProperty $paths | Where-Object { $_.DisplayName } | Sort-Object DisplayName | ForEach-Object {
  $hit = if("$($_.DisplayName)$($_.InstallLocation)$($_.UninstallString)" -match $PupPattern){' <<< CANDIDATE'}else{''}
  $exe=$null; if($_.DisplayIcon){ $exe=($_.DisplayIcon -split ',')[0].Trim('"') }
  W ("{0} | pub={1} | date={2} | loc={3}{4}" -f $_.DisplayName,$_.Publisher,$_.InstallDate,$_.InstallLocation,$hit)
  if($hit -and $exe){ W ("      sig: " + (Sig $exe)) }
}
Save '01-programs.txt'
}

# ---- 2. Folder creation timeline -------------------------------------------
$roots=@($env:ProgramFiles,${env:ProgramFiles(x86)},$env:ProgramData,$env:APPDATA,$env:LOCALAPPDATA) | Where-Object { $_ }
$timeline=@()
if(-not (Test-Skip '02-timeline.txt')){
W "# Suspicious directory creation times"
foreach($r in ($roots|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode directories -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords
  $timeline+=@($probe); Write-ProbeEvidence $probe
}
Save '02-timeline.txt' $true (Get-ProbeDataState $timeline) @{scope='First-level directories; bounded per root'}
}

# ---- 3. Scheduled tasks -----------------------------------------------------
if(-not (Test-Skip '03-tasks.txt')){
W "# Scheduled tasks (non-Microsoft + PUP-flagged)"
Get-ScheduledTask | ForEach-Object {
  $task=$_
  $errorBaseline=$Error.Count
  try {
    # ComHandler actions have no Execute property; classify instead of failing the scan.
    $act=($task.Actions|ForEach-Object{
      $action=$_
      if($null -eq $action){ return '(null action)' }
      $execute=if($action.PSObject.Properties['Execute']){ [string]$action.Execute } else { '' }
      $arguments=if($action.PSObject.Properties['Arguments']){ [string]$action.Arguments } else { '' }
      if($execute){ "$execute $arguments" } else {
        $className=if($action.PSObject.Properties['CimClass']){ [string]$action.CimClass.CimClassName } else { 'Unknown' }
        "($className; no Execute)"
      }
    }) -join ' ; '
    $trg=($task.Triggers|ForEach-Object{
      $trigger=$_
      if($null -eq $trigger){ return '(null trigger)' }
      if($trigger.PSObject.Properties['CimClass']){ $trigger.CimClass.CimClassName } else { 'Unknown' }
    }) -join ','
    $flag= if($act -match $PupPattern){'  <<< CANDIDATE'}else{''}
    if($task.TaskPath -notlike '\Microsoft\*' -or $flag){ W ("[{0}] {1}{2} :: {3} (trg:{4}){5}" -f $task.State,$task.TaskPath,$task.TaskName,$act,$trg,$flag) }
  } catch { Add-ItemError ([string]$task.TaskName) $_.Exception.Message }
  finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
}
Save '03-tasks.txt'
}

# ---- 4. Services ------------------------------------------------------------
if(-not (Test-Skip '04-services.txt')){
W "# All services; system-hosted services are included"
Get-CimInstance Win32_Service | Sort-Object Name | ForEach-Object { Write-ServiceEntry $_ }
$svchost='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Svchost'
if(Test-Path -LiteralPath $svchost){
  $item=Get-Item -LiteralPath $svchost
  foreach($name in $item.GetValueNames()){ W ("Svchost group " + $name + " = " + (@($item.GetValue($name)) -join ',')) }
}
Save '04-services.txt'
}

# ---- 5. Kernel drivers + file filter drivers --------------------------------
if(-not (Test-Skip '05-drivers.txt')){
W "# All kernel drivers; location is not a signature verdict"
Get-CimInstance Win32_SystemDriver | Sort-Object Name | ForEach-Object { Write-DriverEntry $_ }
Add-FilterDriverState
Save '05-drivers.txt'
}

# ---- 6. Autostart (all hives) ----------------------------------------------
if(-not (Test-Skip '06-autostart.txt')){
W "# Autostart entries"
$runRel=@('Microsoft\Windows\CurrentVersion\Run','Microsoft\Windows\CurrentVersion\RunOnce','Microsoft\Windows\CurrentVersion\Policies\Explorer\Run','Microsoft\Windows\CurrentVersion\RunServices','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder')
$bases=@('HKEY_LOCAL_MACHINE\SOFTWARE','HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node')
$errorBaseline=$Error.Count
try { $userSids=@(Get-ChildItem 'Registry::HKEY_USERS' -ErrorAction Stop | ForEach-Object { $_.PSChildName }) }
catch { $userSids=@(); Add-ItemError 'HKEY_USERS' $_.Exception.Message; W '(HKEY_USERS enumeration denied; other users not covered in this section)' }
finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
foreach($sid in $userSids){ $bases += "HKEY_USERS\$sid\SOFTWARE" }
foreach($b in $bases){
  foreach($rel in $runRel){
    $k="Registry::$b\$rel"
    $errorBaseline=$Error.Count
    try {
      if(Test-Path -LiteralPath $k){
        $it=Get-Item -LiteralPath $k
        if($it.GetValueNames().Count){
          W "-- [$b] $rel"
          $it.GetValueNames()|ForEach-Object{
            $v=$it.GetValue($_)
            if($v -is [byte[]]){ $v= if($v.Length){'(StartupApproved raw: '+[BitConverter]::ToString($v)+')'}else{'(StartupApproved empty)'} }
            W ("   {0} = {1}" -f $_,$v)
          }
        }
      }
    } catch { Add-ItemError ("autostart " + $k) $_.Exception.Message }
    finally { while($Error.Count -gt $errorBaseline){ $Error.RemoveAt(0) } }
  }
}
Save '06-autostart.txt'
}

# ---- 7. Shell extensions / BHO / COM (careful: use -LiteralPath for '*' key) -
function Get-ClsidServers([string]$Clsid, [string]$View = '') {
  $roots=@(
    @{path='HKCU:\SOFTWARE\Classes\CLSID';scope='user';view='64'},
    @{path='HKLM:\SOFTWARE\Classes\CLSID';scope='machine';view='64'},
    @{path='HKCU:\SOFTWARE\Classes\WOW6432Node\CLSID';scope='user';view='32'},
    @{path='HKLM:\SOFTWARE\Classes\WOW6432Node\CLSID';scope='machine';view='32'})
  foreach($root in $roots){
    if($View -and $root.view -ne $View){continue}
    foreach($type in 'InprocServer32','LocalServer32'){
      $key="$($root.path)\$Clsid\$type"
      if(Test-Path -LiteralPath $key){
        $server=(Get-Item -LiteralPath $key).GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if($server){[pscustomobject]@{clsid=$Clsid;key=$key;server=[string]$server;scope=$root.scope;view=$root.view}}
      }
    }
  }
}
function Write-ComReference([string]$Kind,[string]$Source,[string]$Clsid,[string]$View=''){
  $servers=@(Get-ClsidServers $Clsid $View)
  if(-not $servers.Count){ W "$Kind $Source CLSID=$Clsid -> UNRESOLVED (requires review)" }
  foreach($server in $servers){
    $flag=if($server.server -match $PupPattern){' <<< CANDIDATE'}else{''}
    W ("{0} {1} CLSID={2} -> [{3}/{4}] {5} = {6}{7}" -f $Kind,$Source,$Clsid,$server.scope,$server.view,$server.key,$server.server,$flag)
  }
}
function Collect-ShellPersistence {
  $classRoots=@('HKCU:\SOFTWARE\Classes','HKLM:\SOFTWARE\Classes',
    'HKCU:\SOFTWARE\Classes\WOW6432Node','HKLM:\SOFTWARE\Classes\WOW6432Node')
  foreach($root in $classRoots){
    foreach($object in '*','AllFilesystemObjects','Directory','Directory\Background','Folder','Drive'){
      $shellex="$root\$object\shellex"
      if(Test-Path -LiteralPath $shellex){
        foreach($category in @(Get-ChildItem -LiteralPath $shellex)){
          # IconHandler can store its CLSID directly; named handlers use the default value.
          $entries=@($category)+@(Get-ChildItem -LiteralPath $category.PSPath)
          foreach($entry in $entries){
            $default=[string](Get-Item -LiteralPath $entry.PSPath).GetValue('')
            $clsid=if($entry.PSChildName -match '^\{[0-9a-fA-F-]{36}\}$'){$entry.PSChildName}else{$default.Trim()}
            if($clsid -match '^\{[0-9a-fA-F-]{36}\}$'){ Write-ComReference 'SHELEX' $entry.PSPath $clsid }
          }
        }
      }
      $shell="$root\$object\shell"
      if(Test-Path -LiteralPath $shell){
        foreach($verb in @(Get-ChildItem -LiteralPath $shell)){
          $command="$($verb.PSPath)\command"
          if(Test-Path -LiteralPath $command){
            $value=(Get-Item -LiteralPath $command).GetValue('')
            $flag=if([string]$value -match $PupPattern){' <<< CANDIDATE'}else{''}
            W ("SHELLCMD {0} = {1}{2}" -f $command,$value,$flag)
          }
        }
      }
    }
  }
  foreach($hive in 'HKCU','HKLM'){
    foreach($view in '64','32'){
      $base=if($view -eq '32' -and $hive -eq 'HKLM'){"${hive}:\SOFTWARE\WOW6432Node"}else{"${hive}:\SOFTWARE"}
      $bho="$base\Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects"
      if(Test-Path -LiteralPath $bho){
        foreach($entry in @(Get-ChildItem -LiteralPath $bho)){
          if($entry.PSChildName -match '^\{[0-9a-fA-F-]{36}\}$'){Write-ComReference 'BHO' $entry.PSPath $entry.PSChildName $view}
        }
      }
    }
  }
}
if(-not (Test-Skip '07-shell-bho-com.txt')){
W "# Shell extensions / BHO / COM hijack"
Collect-ShellPersistence
Save '07-shell-bho-com.txt'
}

# ---- 8. Injection / policies ------------------------------------------------
if(-not (Test-Skip '08-injection.txt')){
W "# Injection points / policies"
$ac=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\AppCertDlls'; if($ac){ $ac.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object { W ("AppCertDll {0} = {1}" -f $_.Name,$_.Value) } } else { W 'AppCertDlls: none' }
Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options' | ForEach-Object { $db=(Get-ItemProperty $_.PSPath).Debugger; if($db){ W ("IFEO {0} Debugger={1}" -f $_.PSChildName,$db) } }
$wl=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'; W ("Winlogon Shell={0} Userinit={1}" -f $wl.Shell,$wl.Userinit)
foreach($k in 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Windows'){ $w=Get-ItemProperty -LiteralPath $k; W ("AppInit {0}: LoadAppInit_DLLs={1} AppInit_DLLs={2}" -f $k,$w.LoadAppInit_DLLs,$w.AppInit_DLLs) }
W ''; W '-- WMI event subscriptions (root\subscription)'
Get-CimInstance -Namespace root\subscription -ClassName __EventFilter | ForEach-Object { W ("Filter {0} :: {1}" -f $_.Name,$_.Query) }
Get-CimInstance -Namespace root\subscription -ClassName CommandLineEventConsumer | ForEach-Object { W ("CmdConsumer {0} :: {1}" -f $_.Name,$_.CommandLineTemplate) }
Get-CimInstance -Namespace root\subscription -ClassName ActiveScriptEventConsumer | ForEach-Object { W ("ScriptConsumer {0} :: {1}" -f $_.Name,$_.ScriptFileName) }
Get-CimInstance -Namespace root\subscription -ClassName __FilterToConsumerBinding | ForEach-Object { W ("Binding {0} -> {1}" -f $_.Filter,$_.Consumer) }
W ''; W '-- Non-Microsoft WinSock providers'
foreach($k in 'HKLM:\SYSTEM\CurrentControlSet\Services\WinSock2\Parameters\Protocol_Catalog9\Catalog_Entries','HKLM:\SYSTEM\CurrentControlSet\Services\WinSock2\Parameters\Protocol_Catalog9\Catalog_Entries64','HKLM:\SYSTEM\CurrentControlSet\Services\WinSock2\Parameters\NameSpace_Catalog5\Catalog_Entries','HKLM:\SYSTEM\CurrentControlSet\Services\WinSock2\Parameters\NameSpace_Catalog5\Catalog_Entries64'){
  Get-ChildItem -LiteralPath $k | ForEach-Object {
    $e=Get-ItemProperty -LiteralPath $_.PSPath
    $lib= if($e.LibraryPath){ $e.LibraryPath } elseif($e.PackedCatalogItem){ ([Text.Encoding]::Default.GetString($e.PackedCatalogItem[0..259])).Split([char]0)[0] } else { '' }
    if($lib -and $lib -notmatch '(?i)\\system32\\|\\SysWOW64\\|%SystemRoot%'){ W ("WinSock {0} -> {1}" -f $_.PSChildName,$lib) }
  }
}
Save '08-injection.txt'
}

# ---- 9. Network -------------------------------------------------------------
if(-not (Test-Skip '09-network.txt')){
W "# Network: DNS cache + connections of suspicious processes"
$dnsEnabled=$DnsPattern -ne '(?!)';$tcpEnabled=$PupPattern -ne '(?!)'
if($dnsEnabled){Invoke-EvidenceNative 'ipconfig.exe' @('/displaydns') | ForEach-Object { [string]$_ } | Where-Object { $_ -match $DnsPattern } | Select-Object -Unique | Select-Object -First 60 | ForEach-Object { W $_.Trim() }}
if($tcpEnabled){
  $procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -match $PupPattern -or $_.ExecutablePath -match $PupPattern }
  $pids=$procs.ProcessId
  Get-NetTCPConnection | Where-Object { $pids -contains $_.OwningProcess } | ForEach-Object { W ("TCP {0}:{1} -> {2}:{3} pid={4}" -f $_.LocalAddress,$_.LocalPort,$_.RemoteAddress,$_.RemotePort,$_.OwningProcess) }
}
Save '09-network.txt' ($dnsEnabled -or $tcpEnabled) 'AUTO' @{dnsEnabled=$dnsEnabled;tcpEnabled=$tcpEnabled;scope='Filtered DNS/TCP, not complete network history'}
}

# ---- 10. Hashes + Mark-of-the-Web ------------------------------------------
# Zone.Identifier absence is a weak signal, never proof of replacement or maliciousness.
if(-not (Test-Skip '10-hashes.txt')){
W "# Hashes (droppers / installers in Downloads & Temp)"
$hashProbes=@()
if(-not $SkipHashes){foreach($r in (@((Join-Path $env:USERPROFILE 'Downloads'),$env:TEMP)|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode hashes -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords -MaxHashFiles $MaxHashFiles -MaxHashBytes $MaxHashBytes
  $hashProbes+=@($probe);Write-ProbeEvidence $probe
}}
Save '10-hashes.txt' (-not $SkipHashes) (Get-ProbeDataState $hashProbes) @{scope='Top-level installers; bounded hashes per root, independent of DnsPattern'}
}

if(-not (Test-Skip '11-tempfootprint.txt')){
W '# Optional temporary directory footprint (observed counts, not recursive total size)'
$footprints=@()
if($CollectTempFootprint){foreach($r in ($TempRoots|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode footprint -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords
  $footprints+=@($probe);Write-ProbeEvidence $probe
}}
Save '11-tempfootprint.txt' ($CollectTempFootprint -and $TempRoots.Count -gt 0) (Get-ProbeDataState $footprints) @{scope='Explicit roots, first level only; partial counts are lower bounds'}
}

if(-not (Test-Skip '12-toolcallers.txt')){
W '# Optional built-in tool caller evidence; does not enable audit policies'
$logProbes=@()
if($CollectToolLogs){foreach($path in $ToolLogPaths){
  $probe=Invoke-BoundedProbe -Mode tool-log -Source $path -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxTextBytes $MaxTextBytes -MaxRecords $MaxRecords
  $logProbes+=@($probe);Write-ProbeEvidence $probe
}}
Save '12-toolcallers.txt' ($CollectToolLogs -and $ToolLogPaths.Count -gt 0) (Get-ProbeDataState $logProbes) @{scope='Bounded text tails; not WindowsUpdate ETL parsing or upstream-parent proof'}
}

if(-not (Test-Skip '13-configfields.txt')){
W '# Optional explicit candidate configuration files; numeric schedule fields and domain hosts only'
$configProbes=@()
foreach($path in $CandidateConfigFiles){
  $probe=Invoke-BoundedProbe -Mode config -Source $path -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxTextBytes $MaxTextBytes -MaxRecords $MaxRecords
  $configProbes+=@($probe);Write-ProbeEvidence $probe
}
Save '13-configfields.txt' ($CandidateConfigFiles.Count -gt 0) (Get-ProbeDataState $configProbes) @{scope='Explicit text files; no directory-wide search or executable evaluation'}
}

if(-not (Test-Skip '14-timeline-buckets.txt')){
W '# Optional directory creation date buckets from the same timeline snapshot'
if($CollectTimelineBuckets -and (Test-Skip '02-timeline.txt')){
  # Resume case: 02 was skipped, so re-derive the probe data needed by the buckets.
  $timeline=@()
  foreach($r in ($roots|Sort-Object -Unique)){
    $probe=Invoke-BoundedProbe -Mode directories -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords
    $timeline+=@($probe)
  }
}
if($CollectTimelineBuckets){foreach($probe in $timeline){W ([pscustomobject]@{source=$probe.source;dataState=$probe.dataState;
    createdDays=$probe.stats.createdDays;createdMonths=$probe.stats.createdMonths;observedAt=$probe.observedAt}|ConvertTo-Json -Depth 8)}}
Save '14-timeline-buckets.txt' ([bool]$CollectTimelineBuckets) (Get-ProbeDataState $timeline) @{scope='First-level directory creation counts, not installation counts'}
}

# ---- 15. Execution artifacts (Prefetch / ShimCache / BAM / SRUDB) ----------
if(-not (Test-Skip '15-execution-artifacts.txt')){
W "# Execution artifacts: who ran what (Prefetch / ShimCache / BAM / SRUDB)"
Collect-Prefetch (Join-Path $env:SystemRoot 'Prefetch') $MaxRecords
Collect-ShimCache 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\AppCompatCache' $OutDir $MaxShimBytes
Collect-Bam 'HKLM:\SYSTEM\CurrentControlSet\Services\bam\State\UserSettings' $MaxRecords
if($SkipSruDatabase){ W 'SRUDB: skipped by -SkipSruDatabase' }
else { Collect-SruDatabase (Join-Path $env:SystemRoot 'System32\sru\SRUDB.dat') $MaxSruBytes $OutDir }
Save '15-execution-artifacts.txt'
}

# ---- 16. Security software status (SecurityCenter2 / Defender) -------------
if(-not (Test-Skip '16-security-status.txt')){
W "# Security software status (SecurityCenter2 / Defender / exclusions)"
Collect-SecurityStatus
Save '16-security-status.txt'
}

Write-Host ("Forensics written to: " + $OutDir)
Complete-Collection

if(@($sections | Where-Object status -eq 'ERROR').Count){ exit 1 }
if(@($sections | Where-Object status -eq 'PARTIAL').Count){ exit 2 }
$elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if(-not $elevated){ Write-Warning 'Non-elevated collection is incomplete'; exit 2 }
