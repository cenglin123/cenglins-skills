# ============================================================================
# collect-forensics.ps1  --  READ-ONLY forensic collection for Windows PUP/bundleware
# Usage:
#   powershell -NoProfile -File collect-forensics.ps1 -OutDir '<case>\evidence-01'
#   powershell ... -File collect-forensics.ps1 -PupPattern 'MyVendorName|FooBar'
# Output: evidence files + collection-status.json + evidence-hashes.json
# NOTE: read-only. Do NOT delete anything before this finishes.
#       Run elevated to see all services/drivers/user hives.
#       No case defaults; discovery collects all services/drivers and directory times.
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
  [switch]$SkipHashes,
  [switch]$CollectTempFootprint,
  [ValidateCount(0,20)][string[]]$TempRoots=@($env:TEMP,(Join-Path $env:SystemRoot 'Temp')),
  [switch]$CollectToolLogs,
  [ValidateCount(0,20)][string[]]$ToolLogPaths=@((Join-Path $env:SystemRoot 'Logs\DISM\dism.log'),(Join-Path $env:SystemRoot 'Logs\CBS\CBS.log')),
  [ValidateCount(0,20)][string[]]$CandidateConfigFiles=@(),
  [switch]$CollectTimelineBuckets
)
. (Join-Path $PSScriptRoot 'bounded-probes.ps1')
$ErrorActionPreference = 'SilentlyContinue'
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit PowerShell' }
[void][regex]::new($PupPattern); [void][regex]::new($DnsPattern)
if (Test-Path -LiteralPath $OutDir) { throw 'OutDir must be new; never overwrite evidence' }
New-Item -ItemType Directory -Path $OutDir -ErrorAction Stop | Out-Null
$started = (Get-Date).ToString('o')
$sections = New-Object 'System.Collections.Generic.List[object]'
$script:AcquisitionErrors = New-Object 'System.Collections.Generic.List[string]'
$Error.Clear()
# PowerShell names are case-insensitive: never reuse 'EvidenceBuf' as a loop variable.
$script:EvidenceBuf = New-Object System.Collections.Generic.List[string]
function W($s=""){ $script:EvidenceBuf.Add([string]$s) }
function Save($name,[bool]$Enabled=$true,[string]$DataState='AUTO',$Details=$null){
  # Missing optional keys/streams are absence; all other collection errors are explicit.
  $failures = @($Error | Where-Object { $_.FullyQualifiedErrorId -notmatch '^(PathNotFound|ItemNotFound|GetContentReaderFileNotFoundError)' } | ForEach-Object { $_.ToString() }) + @($script:AcquisitionErrors.ToArray())
  $rows=@($script:EvidenceBuf|Where-Object { $_ -and -not $_.StartsWith('#') }).Count
  if(-not $Enabled){$DataState='NOT_ENABLED'}elseif($DataState -eq 'AUTO'){$DataState=if($rows){'COMPLETE'}else{'NO_MATCH'}}
  $status = if($failures.Count -or $DataState -eq 'ERROR'){'ERROR'}elseif(-not $Enabled){'SKIP'}elseif($DataState -eq 'PARTIAL'){'PARTIAL'}else{'PASS'}
  W "# COLLECTION STATUS: $status (means acquisition succeeded, not clean)"
  foreach($failure in $failures){ W "# ERROR: $failure" }
  [IO.File]::WriteAllText((Join-Path $OutDir $name), ($script:EvidenceBuf -join "`r`n"), [Text.Encoding]::UTF8)
  $sections.Add([pscustomobject]@{file=$name;status=$status;enabled=$Enabled;dataState=$DataState;dataRows=$rows;
    details=$Details;errors=$failures;time=(Get-Date).ToString('o')})
  $script:EvidenceBuf.Clear(); $script:AcquisitionErrors.Clear(); $Error.Clear()
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

# ---- 1. Installed programs + signatures ------------------------------------
W "# Installed programs (PUP flagged)  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$paths=@('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')
Get-ItemProperty $paths | Where-Object { $_.DisplayName } | Sort-Object DisplayName | ForEach-Object {
  $hit = if("$($_.DisplayName)$($_.InstallLocation)$($_.UninstallString)" -match $PupPattern){' <<< CANDIDATE'}else{''}
  $exe=$null; if($_.DisplayIcon){ $exe=($_.DisplayIcon -split ',')[0].Trim('"') }
  W ("{0} | pub={1} | date={2} | loc={3}{4}" -f $_.DisplayName,$_.Publisher,$_.InstallDate,$_.InstallLocation,$hit)
  if($hit -and $exe){ W ("      sig: " + (Sig $exe)) }
}
Save '01-programs.txt'

# ---- 2. Folder creation timeline -------------------------------------------
W "# Suspicious directory creation times"
$roots=@($env:ProgramFiles,${env:ProgramFiles(x86)},$env:ProgramData,$env:APPDATA,$env:LOCALAPPDATA) | Where-Object { $_ }
$timeline=@()
foreach($r in ($roots|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode directories -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords
  $timeline+=@($probe); Write-ProbeEvidence $probe
}
Save '02-timeline.txt' $true (Get-ProbeDataState $timeline) @{scope='First-level directories; bounded per root'}

# ---- 3. Scheduled tasks -----------------------------------------------------
W "# Scheduled tasks (non-Microsoft + PUP-flagged)"
Get-ScheduledTask | ForEach-Object {
  $t=$_; $act=($t.Actions|ForEach-Object{"$($_.Execute) $($_.Arguments)"}) -join ' ; '
  $trg=($t.Triggers|ForEach-Object{$_.CimClass.CimClassName}) -join ','
  $flag= if($act -match $PupPattern){'  <<< CANDIDATE'}else{''}
  if($t.TaskPath -notlike '\Microsoft\*' -or $flag){ W ("[{0}] {1}{2} :: {3} (trg:{4}){5}" -f $t.State,$t.TaskPath,$t.TaskName,$act,$trg,$flag) }
}
Save '03-tasks.txt'

# ---- 4. Services ------------------------------------------------------------
W "# All services; system-hosted services are included"
Get-CimInstance Win32_Service | Sort-Object Name | ForEach-Object {
  W ("{0} [{1}/{2}] {3}`n    {4}`n    SIG: {5}" -f $_.Name,$_.State,$_.StartMode,$_.DisplayName,$_.PathName,(Sig (BinaryPath $_.PathName)))
  $serviceKey="HKLM:\SYSTEM\CurrentControlSet\Services\$($_.Name)"
  $image=(Get-Item -LiteralPath $serviceKey).GetValue('ImagePath',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  W ("    REG ImagePath (exact plan value): " + $image)
  $parameters="$serviceKey\Parameters"
  if(Test-Path -LiteralPath $parameters){
    $dll=(Get-ItemProperty -LiteralPath $parameters).ServiceDll
    if($dll){ W ("    ServiceDll: " + $dll + " SIG: " + (Sig (BinaryPath $dll))) }
  }
}
$svchost='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Svchost'
if(Test-Path -LiteralPath $svchost){
  $item=Get-Item -LiteralPath $svchost
  foreach($name in $item.GetValueNames()){ W ("Svchost group " + $name + " = " + (@($item.GetValue($name)) -join ',')) }
}
Save '04-services.txt'

# ---- 5. Kernel drivers ------------------------------------------------------
W "# All kernel drivers; location is not a signature verdict"
Get-CimInstance Win32_SystemDriver | Sort-Object Name | ForEach-Object {
  $p=BinaryPath $_.PathName
  W ("{0} [{1}/{2}] {3}`n    SIG: {4}" -f $_.Name,$_.State,$_.StartMode,$p,(Sig $p))
  $driverKey="HKLM:\SYSTEM\CurrentControlSet\Services\$($_.Name)"
  W ("    REG ImagePath (exact plan value): " + (Get-Item -LiteralPath $driverKey).GetValue('ImagePath',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames))
}
Save '05-drivers.txt'

# ---- 6. Autostart (all hives) ----------------------------------------------
W "# Autostart entries"
$runRel=@('Microsoft\Windows\CurrentVersion\Run','Microsoft\Windows\CurrentVersion\RunOnce','Microsoft\Windows\CurrentVersion\Policies\Explorer\Run','Microsoft\Windows\CurrentVersion\RunServices','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32','Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder')
$bases=@('HKEY_LOCAL_MACHINE\SOFTWARE','HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node')
foreach($sid in (Get-ChildItem 'Registry::HKEY_USERS' | ForEach-Object { $_.PSChildName })){ $bases += "HKEY_USERS\$sid\SOFTWARE" }
foreach($b in $bases){ foreach($rel in $runRel){ $k="Registry::$b\$rel"; if(Test-Path -LiteralPath $k){ $it=Get-Item -LiteralPath $k; if($it.GetValueNames().Count){ W "-- [$b] $rel"; $it.GetValueNames()|ForEach-Object{ $v=$it.GetValue($_); if($v -is [byte[]]){ $v= if($v.Length){'(StartupApproved raw: '+[BitConverter]::ToString($v)+')'}else{'(StartupApproved empty)'} }; W ("   {0} = {1}" -f $_,$v) } } } } }
Save '06-autostart.txt'

# ---- 7. Shell extensions / BHO / COM (careful: use -LiteralPath for '*' key) -
W "# Shell extensions / BHO / COM hijack"
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
Collect-ShellPersistence
Save '07-shell-bho-com.txt'

# ---- 8. Injection / policies ------------------------------------------------
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

# ---- 9. Network -------------------------------------------------------------
W "# Network: DNS cache + connections of suspicious processes"
$dnsEnabled=$DnsPattern -ne '(?!)';$tcpEnabled=$PupPattern -ne '(?!)'
if($dnsEnabled){Invoke-EvidenceNative 'ipconfig.exe' @('/displaydns') | ForEach-Object { [string]$_ } | Where-Object { $_ -match $DnsPattern } | Select-Object -Unique | Select-Object -First 60 | ForEach-Object { W $_.Trim() }}
if($tcpEnabled){
  $procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -match $PupPattern -or $_.ExecutablePath -match $PupPattern }
  $pids=$procs.ProcessId
  Get-NetTCPConnection | Where-Object { $pids -contains $_.OwningProcess } | ForEach-Object { W ("TCP {0}:{1} -> {2}:{3} pid={4}" -f $_.LocalAddress,$_.LocalPort,$_.RemoteAddress,$_.RemotePort,$_.OwningProcess) }
}
Save '09-network.txt' ($dnsEnabled -or $tcpEnabled) 'AUTO' @{dnsEnabled=$dnsEnabled;tcpEnabled=$tcpEnabled;scope='Filtered DNS/TCP, not complete network history'}

# ---- 10. Hashes + Mark-of-the-Web ------------------------------------------
# Zone.Identifier absence is a weak signal, never proof of replacement or maliciousness.
W "# Hashes (droppers / installers in Downloads & Temp)"
$hashProbes=@()
if(-not $SkipHashes){foreach($r in (@((Join-Path $env:USERPROFILE 'Downloads'),$env:TEMP)|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode hashes -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords -MaxHashFiles $MaxHashFiles -MaxHashBytes $MaxHashBytes
  $hashProbes+=@($probe);Write-ProbeEvidence $probe
}}
Save '10-hashes.txt' (-not $SkipHashes) (Get-ProbeDataState $hashProbes) @{scope='Top-level installers; bounded hashes per root, independent of DnsPattern'}

W '# Optional temporary directory footprint (observed counts, not recursive total size)'
$footprints=@()
if($CollectTempFootprint){foreach($r in ($TempRoots|Sort-Object -Unique)){
  $probe=Invoke-BoundedProbe -Mode footprint -Source $r -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxRecords $MaxRecords
  $footprints+=@($probe);Write-ProbeEvidence $probe
}}
Save '11-tempfootprint.txt' ($CollectTempFootprint -and $TempRoots.Count -gt 0) (Get-ProbeDataState $footprints) @{scope='Explicit roots, first level only; partial counts are lower bounds'}

W '# Optional built-in tool caller evidence; does not enable audit policies'
$logProbes=@()
if($CollectToolLogs){foreach($path in $ToolLogPaths){
  $probe=Invoke-BoundedProbe -Mode tool-log -Source $path -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxTextBytes $MaxTextBytes -MaxRecords $MaxRecords
  $logProbes+=@($probe);Write-ProbeEvidence $probe
}}
Save '12-toolcallers.txt' ($CollectToolLogs -and $ToolLogPaths.Count -gt 0) (Get-ProbeDataState $logProbes) @{scope='Bounded text tails; not WindowsUpdate ETL parsing or upstream-parent proof'}

W '# Optional explicit candidate configuration files; numeric schedule fields and domain hosts only'
$configProbes=@()
foreach($path in $CandidateConfigFiles){
  $probe=Invoke-BoundedProbe -Mode config -Source $path -OutputDir $OutDir -Seconds $ProbeSeconds -MaxEntries $MaxEntries -MaxTextBytes $MaxTextBytes -MaxRecords $MaxRecords
  $configProbes+=@($probe);Write-ProbeEvidence $probe
}
Save '13-configfields.txt' ($CandidateConfigFiles.Count -gt 0) (Get-ProbeDataState $configProbes) @{scope='Explicit text files; no directory-wide search or executable evaluation'}

W '# Optional directory creation date buckets from the same timeline snapshot'
if($CollectTimelineBuckets){foreach($probe in $timeline){W ([pscustomobject]@{source=$probe.source;dataState=$probe.dataState;
    createdDays=$probe.stats.createdDays;createdMonths=$probe.stats.createdMonths;observedAt=$probe.observedAt}|ConvertTo-Json -Depth 8)}}
Save '14-timeline-buckets.txt' ([bool]$CollectTimelineBuckets) (Get-ProbeDataState $timeline) @{scope='First-level directory creation counts, not installation counts'}

Write-Host ("Forensics written to: " + $OutDir)

$status=[pscustomobject]@{
  startedAt=$started;finishedAt=(Get-Date).ToString('o');computer=$env:COMPUTERNAME
  userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  powershell=$PSVersionTable.PSVersion.ToString();is64Bit=[Environment]::Is64BitProcess
  elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  pupPattern=$PupPattern;dnsPattern=$DnsPattern;probeSeconds=$ProbeSeconds;maxEntries=$MaxEntries;maxRecords=$MaxRecords;sections=@($sections.ToArray())
  scope='Current identity and loaded hives only; browser/policies/unloaded profiles require manual review'
}
[IO.File]::WriteAllText((Join-Path $OutDir 'collection-status.json'),($status|ConvertTo-Json -Depth 8),[Text.Encoding]::UTF8)
$hashes=@(Get-ChildItem -LiteralPath $OutDir -File -Recurse | Where-Object { $_.Extension -ne '.tmp' } | ForEach-Object {
  [pscustomobject]@{file=$_.FullName.Substring(([IO.Path]::GetFullPath($OutDir).TrimEnd('\')).Length+1);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash;length=$_.Length}
})
[IO.File]::WriteAllText((Join-Path $OutDir 'evidence-hashes.json'),($hashes|ConvertTo-Json -Depth 5),[Text.Encoding]::UTF8)
if(@($sections | Where-Object status -eq 'ERROR').Count){ exit 1 }
if(@($sections | Where-Object status -eq 'PARTIAL').Count){ exit 2 }
if(-not $status.elevated){ Write-Warning 'Non-elevated collection is incomplete'; exit 2 }
