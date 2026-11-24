# install-runtime.ps1 - foxgrep 嵌入式 Python 运行时下载安装
# 触发: fg.cmd 检测 runtime\python.exe 缺失时自动调用; 也可手动执行。
# 幂等: 已存在且可用时直接退出 0。
param([string]$SkillDir)

$ErrorActionPreference = 'Stop'
if (-not $SkillDir) { $SkillDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$pyDir  = Join-Path $SkillDir 'runtime'
$pyExe  = Join-Path $pyDir 'python.exe'
if (Test-Path $pyExe) { exit 0 }

$ver   = '3.12.8'
$file  = "python-$ver-embed-amd64.zip"
$sha256 = '8d3f33be9eb810f23c102f08475af2854e50484b8e4e06275e937be61ce3d2fb'
$urls = @(
    "https://mirrors.huaweicloud.com/python/$ver/$file",            # 华为云
    "https://mirrors.tuna.tsinghua.edu.cn/python/$ver/$file",       # 清华 TUNA
    "https://registry.npmmirror.com/-/binary/python/$ver/$file",    # 淘宝 npmmirror
    "https://www.python.org/ftp/python/$ver/$file"                  # 官方源回退
)

$tmp = Join-Path $env:TEMP $file
$ok = $false
foreach ($u in $urls) {
    try {
        Write-Host "downloading: $u"
        Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 120
        # 用 .NET 算哈希 (Get-FileHash 在部分环境缺失)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $fs = [System.IO.File]::OpenRead($tmp)
        try { $h = ([BitConverter]::ToString($sha.ComputeHash($fs))).Replace('-','').ToLower() }
        finally { $fs.Close(); $sha.Dispose() }
        if ($h -ne $sha256) { Write-Host "hash mismatch, try next mirror"; continue }
        $ok = $true; break
    } catch { Write-Host "failed: $($_.Exception.Message)" }
}
if (-not $ok) { Write-Error "runtime download failed from all mirrors"; exit 1 }

New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
Expand-Archive -Path $tmp -DestinationPath $pyDir -Force
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
if (-not (Test-Path $pyExe)) { Write-Error "runtime extract failed"; exit 1 }
Write-Host "runtime installed: $pyExe"
