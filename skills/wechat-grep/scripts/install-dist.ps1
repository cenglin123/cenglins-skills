# install-dist.ps1 - wechat-grep 大件分发安装（bin/weflow.exe + models/ + wheels/）
# 触发: SKILL 部署时检测 bin\weflow.exe 缺失时运行; 也可手动执行。
# 幂等: 已存在且可用时直接退出 0。
# 来源: 优先 -ZipPath 本地包(内网离线携带)，否则从 GitHub Release 下载。
# 校验: SHA256 固定值，防篡改。
param(
    [string]$SkillDir,
    [string]$ZipPath
)

$ErrorActionPreference = 'Stop'
if (-not $SkillDir) { $SkillDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$wfExe = Join-Path $SkillDir 'bin\weflow.exe'
if (Test-Path $wfExe) { Write-Host "already installed: $wfExe"; exit 0 }

$sha256  = '02702a46f1140d43c6cce2150a95c9b224ea79bafe3324b91d2ee0d99f345e8f'
$zipOut  = Join-Path $SkillDir 'wechat-grep-dist.zip'
$urls = @(
    "https://github.com/cenglin123/cenglins-skills/releases/download/wechat-grep-v1.0.0/wechat-grep-dist.zip"
)

# 1) 定位 zip：显式 -ZipPath → skill 目录下已有的 → 下载
if (-not $ZipPath) {
    $local = Join-Path $SkillDir 'wechat-grep-dist.zip'
    if (Test-Path $local) { $ZipPath = $local }
}
if (-not $ZipPath) {
    $tmp = Join-Path $env:TEMP 'wechat-grep-dist.zip'
    $ok = $false
    foreach ($u in $urls) {
        try {
            Write-Host "downloading: $u"
            Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 600
            $ok = $true
            break
        } catch {
            Write-Warning "download failed: $u — $($_.Exception.Message)"
        }
    }
    if (-not $ok) {
        Write-Error "下载失败。内网环境请离线携带 wechat-grep-dist.zip 后用 -ZipPath 指定。"
        exit 1
    }
    $ZipPath = $tmp
} elseif (-not (Test-Path $ZipPath)) {
    Write-Error "ZipPath 不存在: $ZipPath"
    exit 1
}

# 2) SHA256 校验
$hash = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLower()
if ($hash -ne $sha256) {
    Write-Error "SHA256 不匹配：期望 $sha256，实际 $hash（文件损坏或版本不符）"
    exit 1
}
Write-Host "sha256 verified"

# 3) 解压（覆盖到 skill 目录：bin/ models/ wheels/）
Expand-Archive -Path $ZipPath -DestinationPath $SkillDir -Force

# 4) 终检
if (-not (Test-Path $wfExe)) {
    Write-Error "安装后仍缺少 $wfExe——zip 内容异常？"
    exit 1
}
Write-Host "installed: $wfExe"
Write-Host "models:    $(Join-Path $SkillDir 'models\zipformer-small-ctc-int8\ctc.int8.onnx')"
Write-Host "wheels:    $(Join-Path $SkillDir 'wheels\')"
