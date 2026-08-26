<#
.SYNOPSIS
    Compile src\wtlaunch.cs into a launcher named powershell.exe.

.DESCRIPTION
    Uses the in-box .NET Framework C# compiler (csc.exe), which exists on every
    Windows 10/11 install - no SDK, no NuGet, no internet required.

.PARAMETER OutDir
    Directory that receives powershell.exe. Default: $env:LOCALAPPDATA\WinXWT

.EXAMPLE
    .\build-launcher.ps1
    .\build-launcher.ps1 -OutDir C:\Tools\WinXWT
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA 'WinXWT'),
    [string]$Source = (Join-Path $PSScriptRoot 'src\wtlaunch.cs')
)

$ErrorActionPreference = 'Stop'

function Find-Csc {
    $roots = @(
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64'),
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework')
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $csc = Get-ChildItem -Path $root -Filter 'csc.exe' -Recurse -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($csc) { return $csc.FullName }
    }
    return $null
}

if (-not (Test-Path $Source)) { throw "source not found: $Source" }

$csc = Find-Csc
if (-not $csc) { throw 'csc.exe not found (.NET Framework compiler missing)' }
Write-Host "[build] compiler : $csc"

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$out = Join-Path $OutDir 'powershell.exe'

if (Test-Path $out) {
    # a running launcher cannot be overwritten; move it aside instead of failing
    try { Remove-Item $out -Force } catch { Move-Item $out "$out.old-$(Get-Date -Format yyyyMMddHHmmss)" -Force }
}

# /target:winexe -> no console window flashes when the wrapper starts wt.exe
$cscArgs = @('/nologo', '/target:winexe', '/optimize+', "/out:$out", $Source)
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "csc failed with exit code $LASTEXITCODE" }

$info = Get-Item $out
Write-Host ("[build] output   : {0} ({1} bytes)" -f $info.FullName, $info.Length)
Write-Host ("[build] sha256   : {0}" -f (Get-FileHash $out -Algorithm SHA256).Hash)
Write-Output $info.FullName
