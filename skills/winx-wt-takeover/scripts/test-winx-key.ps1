<#
.SYNOPSIS
    Press Win+X then an accelerator (I or A) and screenshot what opened.

.DESCRIPTION
    Unattended end-to-end check of the takeover: it exercises the real menu,
    not the shortcut file, so it validates what TWINUI actually launches -
    including the elevated "(Administrator)" entry (A), which shows a UAC
    prompt unless the machine is configured to elevate silently.

.PARAMETER Key
    Accelerator to press: I (normal) or A (administrator). Default: I.

.PARAMETER OutFile
    PNG path. Default: %TEMP%\winx-launch-<key>-<stamp>.png

.EXAMPLE
    .\test-winx-key.ps1 -Key I
    .\test-winx-key.ps1 -Key A
#>
[CmdletBinding()]
param(
    [ValidateSet('I', 'A')]
    [string]$Key = 'I',
    [string]$OutFile,
    [int]$WaitSeconds = 4
)

if (-not $OutFile) {
    $OutFile = Join-Path $env:TEMP ("winx-launch-{0}-{1}.png" -f $Key, (Get-Date -Format 'yyyyMMdd-HHmmss'))
}

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WinXKbd2 {
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
"@ -ErrorAction SilentlyContinue

$VK = @{ LWIN = 0x5B; X = 0x58; A = 0x41; I = 0x49 }
$KEYUP = 2

# open Win+X
[WinXKbd2]::keybd_event([byte]$VK.LWIN, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[WinXKbd2]::keybd_event([byte]$VK.X, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[WinXKbd2]::keybd_event([byte]$VK.X, 0, $KEYUP, [UIntPtr]::Zero)
[WinXKbd2]::keybd_event([byte]$VK.LWIN, 0, $KEYUP, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 800

# press the accelerator
$code = [byte]$VK[$Key]
[WinXKbd2]::keybd_event($code, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[WinXKbd2]::keybd_event($code, 0, $KEYUP, [UIntPtr]::Zero)
Start-Sleep -Seconds $WaitSeconds

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
$bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

# also report, textually, which processes appeared
Get-Process -Name 'WindowsTerminal', 'powershell', 'OpenConsole' -ErrorAction SilentlyContinue |
    Select-Object Name, Id, StartTime | Sort-Object StartTime -Descending | Select-Object -First 5 | Format-Table | Out-String | Write-Output

Write-Output "saved $OutFile"
