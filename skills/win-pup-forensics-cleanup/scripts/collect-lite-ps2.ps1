# ============================================================================
# collect-lite-ps2.ps1 -- READ-ONLY, PowerShell 2.0 compatible lite collector
#
# For targets reachable only through WinRM/remote shell where the full
# collect-forensics.ps1 (5.1+, 64-bit, JSON/CIM) cannot run. It writes ONE
# UTF-8 text file and changes nothing else. Copy the file back with the same
# remote tool, then hash and archive it on the analysis host (certutil output
# parsing is unreliable; hash locally).
#
# Coverage is deliberately narrow: identity, uninstall entries, services,
# drivers, Run/RunOnce, scheduled tasks (XML), IE start page/search, BHO,
# security-center state, proxy, recent temp installers, Prefetch names, startup
# folders, hosts file, fltmc filters. Browser profiles, other user hives and
# injection surfaces still require manual review.
#
# Keep this file ASCII-only.
# ============================================================================
param(
  [string]$OutFile = (Join-Path $env:TEMP 'collect-lite.txt'),
  [int]$MaxItems = 20000,
  [switch]$Force
)
$ErrorActionPreference = 'SilentlyContinue'
if ((Test-Path -LiteralPath $OutFile) -and -not $Force) {
  [Console]::Error.WriteLine('OutFile exists; use a new path or -Force (evidence is never overwritten by default)')
  exit 2
}
$script:Lines = New-Object System.Collections.Generic.List[string]
$script:Emitted = 0
$script:Truncated = $false
function Emit([string]$Text) {
  if ($script:Emitted -ge $MaxItems) { $script:Truncated = $true; return }
  $script:Lines.Add($Text)
  $script:Emitted++
}
function Section([string]$Name, [scriptblock]$Body) {
  Emit ''
  Emit ('## ' + $Name)
  $errorBaseline = $Error.Count
  try { & $Body } catch { Emit ('# SECTION ERROR ' + $Name + ': ' + $_.Exception.Message) }
  if ($Error.Count -gt $errorBaseline) {
    Emit ('# SECTION ERRORS ' + $Name + ': ' + [string]$Error[0])
  }
}
function Note([string]$Text) { Emit ('# ' + $Text) }
function RegValue([string]$Path, [string]$Name) {
  try { $item = Get-Item -LiteralPath $Path -ErrorAction Stop }
  catch { return '(key missing)' }
  try { return [string]$item.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
  catch { return '(unreadable)' }
}

Emit ('# collect-lite-ps2 ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Emit ('# coverage: current identity and loaded hives only; manual review still required')

Section 'identity' {
  Emit ('HOST: ' + $env:COMPUTERNAME)
  Emit ('USER: ' + $env:USERNAME)
  try { Emit ('SID: ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) } catch { Note ('SID unavailable: ' + $_.Exception.Message) }
  Emit ('POWERSHELL: ' + $PSVersionTable.PSVersion.ToString())
  Emit ('IS64BIT: ' + ([IntPtr]::Size -eq 8))
  $elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  Emit ('ELEVATED: ' + $elevated)
  foreach ($os in @(Get-WmiObject -Class Win32_OperatingSystem)) {
    Emit ('OS: ' + $os.Caption + ' | ' + $os.Version + ' | installed=' + $os.InstallDate + ' | boot=' + $os.LastBootUpTime)
  }
}

Section 'uninstall-entries' {
  $rows = @()
  foreach ($root in @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
      $p = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
      if ($p -and $p.DisplayName) {
        $rows += ('' + $p.DisplayName + ' | ' + $p.DisplayVersion + ' | install=' + $p.InstallDate +
          ' | ' + $p.InstallLocation + ' | ' + $p.UninstallString + ' | ' + $key.PSPath)
      }
    }
  }
  foreach ($row in @($rows | Sort-Object)) { Emit $row }
  Emit ('# count: ' + @($rows).Count)
}

Section 'services' {
  $services = @(Get-WmiObject -Class Win32_Service | Sort-Object Name)
  if (-not $services.Count) { Note 'no services returned (WMI or access)' }
  foreach ($service in $services) {
    $raw = RegValue ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $service.Name) 'ImagePath'
    Emit ($service.Name + ' [' + $service.State + '/' + $service.StartMode + '] ' + $service.DisplayName +
      ' | WMI path=' + $service.PathName + ' | REG ImagePath=' + $raw)
  }
}

Section 'drivers' {
  $drivers = @(Get-WmiObject -Class Win32_SystemDriver | Sort-Object Name)
  if (-not $drivers.Count) { Note 'no drivers returned (WMI or access)' }
  foreach ($driver in $drivers) {
    $raw = RegValue ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $driver.Name) 'ImagePath'
    Emit ($driver.Name + ' [' + $driver.State + '/' + $driver.StartMode + '] ' + $driver.DisplayName +
      ' | WMI path=' + $driver.PathName + ' | REG ImagePath=' + $raw)
  }
}

Section 'autostart-run' {
  $roots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run')
  foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $key = Get-Item -LiteralPath $root -ErrorAction SilentlyContinue
    if (-not $key) { continue }
    foreach ($name in @($key.GetValueNames())) {
      Emit ($root + '::' + $name + ' = ' + [string]$key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames))
    }
  }
}

Section 'scheduled-tasks' {
  $taskRoot = Join-Path $env:SystemRoot 'System32\Tasks'
  $files = @(Get-ChildItem -LiteralPath $taskRoot -Recurse -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer })
  foreach ($file in $files) {
    $relative = $file.FullName.Substring($taskRoot.Length + 1)
    $actions = ''
    try {
      $xml = [xml](Get-Content -LiteralPath $file.FullName -ErrorAction Stop)
      foreach ($exec in @($xml.GetElementsByTagName('Exec'))) {
        $command = ''
        $arguments = ''
        foreach ($child in @($exec.ChildNodes)) {
          if ($child.LocalName -eq 'Command') { $command = [string]$child.InnerText }
          elseif ($child.LocalName -eq 'Arguments') { $arguments = [string]$child.InnerText }
        }
        if ($command) { $actions = $actions + $command + ' ' + $arguments + ' ; ' }
      }
    } catch { $actions = '(unparsable)' }
    Emit ($relative + ' | ' + $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') + ' | ' + $actions)
  }
  Emit ('# count: ' + $files.Count)
}

Section 'internet-explorer' {
  foreach ($root in @('HKCU:\Software\Microsoft\Internet Explorer\Main', 'HKLM:\SOFTWARE\Microsoft\Internet Explorer\Main',
    'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Internet Explorer\Main')) {
    Emit ($root + ' Start Page = ' + (RegValue $root 'Start Page'))
  }
  foreach ($root in @('HKCU:\Software\Microsoft\Internet Explorer\SearchScopes', 'HKLM:\SOFTWARE\Microsoft\Internet Explorer\SearchScopes')) {
    Emit ($root + ' DefaultScope = ' + (RegValue $root 'DefaultScope'))
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($scope in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
      $p = Get-ItemProperty -LiteralPath $scope.PSPath -ErrorAction SilentlyContinue
      if ($p) { Emit ('SCOPE ' + $scope.PSChildName + ' | ' + $p.DisplayName + ' | ' + $p.URL) }
    }
  }
}

Section 'browser-helper-objects' {
  foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects',
    'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects')) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($bho in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
      Emit ($root + '\' + $bho.PSChildName + ' | ' + (RegValue $bho.PSPath ''))
    }
  }
}

Section 'security-status' {
  $products = @(Get-WmiObject -Namespace 'root\SecurityCenter2' -Class AntiVirusProduct)
  if (-not $products.Count) { Note 'SecurityCenter2 returned no AntiVirusProduct' }
  foreach ($product in $products) {
    Emit ('AV: ' + $product.displayName + ' | state=' + $product.productState + ' | ' + $product.pathToSignedProductExe)
  }
  $defender = Get-Service -Name WinDefend -ErrorAction SilentlyContinue
  if ($defender) {
    $start = RegValue 'HKLM:\SYSTEM\CurrentControlSet\Services\WinDefend' 'Start'
    Emit ('WinDefend: ' + $defender.Status + ' / Start=' + $start)
  } else { Note 'WinDefend service not found' }
  foreach ($policy in @('HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender',
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection')) {
    $key = Get-Item -LiteralPath $policy -ErrorAction SilentlyContinue
    if (-not $key) { continue }
    foreach ($name in @($key.GetValueNames())) { Emit ('POLICY ' + $policy + '::' + $name + ' = ' + [string]$key.GetValue($name)) }
  }
}

Section 'proxy' {
  $root = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
  foreach ($name in @('ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL')) {
    Emit ($name + ' = ' + (RegValue $root $name))
  }
}

Section 'recent-temp-installers' {
  $rows = @()
  foreach ($pattern in @('*.exe', '*.tmp', '*.msi')) {
    foreach ($file in @(Get-ChildItem -LiteralPath $env:TEMP -Filter $pattern -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-180) })) {
      $rows += ($file.LastWriteTime.ToString('yyyy-MM-dd HH:mm') + ' | ' + $file.Length + ' | ' + $file.FullName)
    }
  }
  foreach ($row in @($rows | Sort-Object -Descending | Select-Object -First 100)) { Emit $row }
}

Section 'prefetch' {
  $directory = Join-Path $env:SystemRoot 'Prefetch'
  foreach ($file in @(Get-ChildItem -LiteralPath $directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 100)) {
    Emit ($file.Name + ' | ' + $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') + ' | ' + $file.Length)
  }
}

Section 'startup-folders' {
  foreach ($folder in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('CommonStartup'))) {
    Emit ('# ' + $folder)
    if (Test-Path -LiteralPath $folder) { foreach ($item in @(Get-ChildItem -LiteralPath $folder -ErrorAction SilentlyContinue)) { Emit ('  ' + $item.Name) } }
  }
}

Section 'hosts-file' {
  $hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
  foreach ($line in @(Get-Content -LiteralPath $hosts -ErrorAction SilentlyContinue)) {
    $trimmed = ([string]$line).Trim()
    if ($trimmed -and $trimmed.Substring(0, 1) -ne '#') { Emit $trimmed }
  }
}

Section 'filter-drivers' {
  foreach ($command in @('filters', 'instances')) {
    $output = @(& fltmc.exe $command 2>&1)
    $code = $LASTEXITCODE
    foreach ($line in $output) { if (([string]$line).Trim()) { Emit ('fltmc ' + $command + ': ' + ([string]$line).Trim()) } }
    if ($code -ne 0) { Emit ('# fltmc ' + $command + ' exit ' + $code + ' (elevation or availability)') }
  }
}

if ($script:Truncated) { $script:Lines.Add('# TRUNCATED at ' + $MaxItems + ' lines') }
$script:Lines.Add('# finished ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
try {
  [IO.File]::WriteAllText($OutFile, ($script:Lines -join "`r`n"), (New-Object Text.UTF8Encoding $false))
} catch {
  [Console]::Error.WriteLine('Cannot write ' + $OutFile + ': ' + $_.Exception.Message)
  exit 1
}
Write-Output ('saved: ' + $OutFile + ' (' + (Get-Item -LiteralPath $OutFile).Length + ' bytes)')
