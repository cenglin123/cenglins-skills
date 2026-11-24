# Pure registry fixtures; no real registry provider is consulted.
function Initialize-ShellFixture {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/collect-forensics.ps1'),[ref]$tokens,[ref]$errors)
    foreach($name in @('W','Get-ClsidServers','Write-ComReference','Collect-ShellPersistence')){
        $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
        . ([scriptblock]::Create($node.Extent.Text))
    }
    $script:ShellKeys=@{}; $script:ShellChildren=@{}
    $script:EvidenceBuf=New-Object 'System.Collections.Generic.List[string]'
    function Add-ShellKey([string]$Path,[string]$Default='') {
        $item=[pscustomobject]@{PSPath=$Path;PSChildName=($Path -split '\\')[-1];Default=$Default}
        $item | Add-Member ScriptMethod GetValue { param($name,$default=$null,$options=$null) if($name -eq ''){return $this.Default};return $default }
        $script:ShellKeys[$Path]=$item
    }
    function Test-Path { param([string]$LiteralPath) return $script:ShellKeys.ContainsKey($LiteralPath) }
    function Get-Item { param([string]$LiteralPath) if(-not $script:ShellKeys.ContainsKey($LiteralPath)){throw 'Unexpected registry fixture read'};return $script:ShellKeys[$LiteralPath] }
    function Get-ChildItem {
        param([string]$LiteralPath)
        foreach($path in @($script:ShellChildren[$LiteralPath])){if($path){$script:ShellKeys[$path]}}
    }
}
Test-Case 'named-shell-handler-default-clsid-and-user-override-retained' {
    . Initialize-ShellFixture
    $PupPattern='SuspiciousVendor'
    $root='HKLM:\SOFTWARE\Classes\*\shellex'; $category="$root\ContextMenuHandlers"; $handler="$category\ProductName"
    $clsid='{00000000-0000-0000-0000-000000000001}'
    Add-ShellKey $root; Add-ShellKey $category; Add-ShellKey $handler $clsid
    $script:ShellChildren[$root]=@($category); $script:ShellChildren[$category]=@($handler)
    Add-ShellKey "HKLM:\SOFTWARE\Classes\CLSID\$clsid\InprocServer32" 'C:\Benign\shell.dll'
    Add-ShellKey "HKCU:\SOFTWARE\Classes\CLSID\$clsid\InprocServer32" 'C:\SuspiciousVendor\override.dll'
    Collect-ShellPersistence
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('ProductName') -and $text.Contains('override.dll') -and $text.Contains('shell.dll')) 'Named/default CLSID or user override lost'
    Assert ($text.Contains('[user/64]') -and $text.Contains('[machine/64]')) 'Registry scope was collapsed'
}
Test-Case '32-bit-bho-resolves-both-user-and-machine-32-bit-servers' {
    . Initialize-ShellFixture
    $PupPattern='(?!)'; $clsid='{00000000-0000-0000-0000-000000000002}'
    $root='HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects'
    $entry="$root\$clsid"
    Add-ShellKey $root; Add-ShellKey $entry; $script:ShellChildren[$root]=@($entry)
    Add-ShellKey "HKLM:\SOFTWARE\Classes\CLSID\$clsid\InprocServer32" 'C:\Wrong64\server.dll'
    Add-ShellKey "HKLM:\SOFTWARE\Classes\WOW6432Node\CLSID\$clsid\InprocServer32" 'C:\Machine32\server.dll'
    Add-ShellKey "HKCU:\SOFTWARE\Classes\WOW6432Node\CLSID\$clsid\InprocServer32" 'C:\User32\server.dll'
    Collect-ShellPersistence
    $text=$script:EvidenceBuf -join "`n"
    Assert ($text.Contains('Machine32') -and $text.Contains('User32') -and -not $text.Contains('Wrong64')) '32-bit BHO resolved the wrong registry view'
}
Test-Case 'direct-icon-handler-collected-without-family-pattern' {
    . Initialize-ShellFixture
    $PupPattern='(?!)'; $clsid='{00000000-0000-0000-0000-000000000003}'
    $root='HKCU:\SOFTWARE\Classes\Directory\shellex';$entry="$root\IconHandler"
    Add-ShellKey $root;Add-ShellKey $entry $clsid;$script:ShellChildren[$root]=@($entry)
    Add-ShellKey "HKCU:\SOFTWARE\Classes\CLSID\$clsid\InprocServer32" 'C:\UnknownProduct\icon.dll'
    Collect-ShellPersistence
    Assert (($script:EvidenceBuf -join "`n").Contains('icon.dll')) 'Initial discovery suppressed an unknown family'
}
