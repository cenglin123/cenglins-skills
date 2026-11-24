<#
.SYNOPSIS
    Open the Win+X menu with synthesized keystrokes and screenshot it.

.DESCRIPTION
    GUI verification fallback for when no screen-control MCP/automation is
    available: keybd_event() opens the menu, System.Drawing.CopyFromScreen
    captures it, Esc closes it. Requires an interactive desktop session
    (it will do nothing useful over a headless/service context).

.PARAMETER OutFile
    PNG path. Default: %TEMP%\winx-menu-<stamp>.png
#>
[CmdletBinding()]
param(
    [string]$OutFile = (Join-Path $env:TEMP ("winx-menu-{0}.png" -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WinXKbd {
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public const byte VK_LWIN = 0x5B;
    public const byte VK_X = 0x58;
    public const byte VK_ESC = 0x1B;
    public const uint KEYUP = 0x0002;
}
"@ -ErrorAction SilentlyContinue

[WinXKbd]::keybd_event([WinXKbd]::VK_LWIN, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[WinXKbd]::keybd_event([WinXKbd]::VK_X, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[WinXKbd]::keybd_event([WinXKbd]::VK_X, 0, [WinXKbd]::KEYUP, [UIntPtr]::Zero)
[WinXKbd]::keybd_event([WinXKbd]::VK_LWIN, 0, [WinXKbd]::KEYUP, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 1200

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
$bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

[WinXKbd]::keybd_event([WinXKbd]::VK_ESC, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 50
[WinXKbd]::keybd_event([WinXKbd]::VK_ESC, 0, [WinXKbd]::KEYUP, [UIntPtr]::Zero)

Write-Output "saved $OutFile"
