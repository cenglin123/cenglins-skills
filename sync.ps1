# Sync all skills from this source repo to deployment copy directories.
#
# Why copy (not symlink): agent Write/Edit tools replace a file by delete-then-
# create, which breaks symbolic links (the link itself gets deleted or a plain
# file replaces it at the link path, leaving the real target stale). So we copy.
#
# Run this after editing any source SKILL.md here (or after `git pull`).
# Param -Targets overrides the default deployment dirs.

param(
    [string[]]$Targets = @("$env:USERPROFILE\.agents\skills", "$env:USERPROFILE\.claude\skills")
)

$src = $PSScriptRoot
$synced = 0

$skillFiles = Get-ChildItem $src -Recurse -Filter SKILL.md -File
foreach ($sf in $skillFiles) {
    $skillDir = $sf.Directory.FullName
    $skillName = $sf.Directory.Name
    foreach ($t in $Targets) {
        if (-not (Test-Path $t)) { continue }
        Copy-Item -Recurse -Force $skillDir $t
        Write-Output "synced: $skillName -> $t"
        $synced++
    }
}

# Verify: report any DRIFT remaining after sync
$drift = 0
foreach ($sf in $skillFiles) {
    $name = ((Get-Content $sf.FullName -TotalCount 3 | Select-String '^name:') -replace '^name:\s*', '').Trim()
    foreach ($t in $Targets) {
        $dst = Join-Path $t "$name\SKILL.md"
        if ((Test-Path $dst) -and ((Get-FileHash $sf.FullName).Hash -ne (Get-FileHash $dst).Hash)) {
            Write-Output "DRIFT remains: $name @ $t"
            $drift++
        }
    }
}

Write-Output "done: $synced copies synced" + $(if ($drift -gt 0) { " ($drift DRIFT remaining)" } else { ", 0 DRIFT" })
