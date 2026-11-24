param(
    [string]$SkillsRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) "skills")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $SkillsRoot -PathType Container)) {
    throw "Skills directory not found: $SkillsRoot"
}

$errors = [System.Collections.Generic.List[string]]::new()
$seenNames = @{}
$skillDirs = @(Get-ChildItem -LiteralPath $SkillsRoot -Directory | Sort-Object Name)

if ($skillDirs.Count -eq 0) {
    $errors.Add("No skill directories found under $SkillsRoot")
}

foreach ($skillDir in $skillDirs) {
    $skillFile = Join-Path $skillDir.FullName "SKILL.md"
    if (-not (Test-Path -LiteralPath $skillFile -PathType Leaf)) {
        $errors.Add("Missing SKILL.md: $($skillDir.Name)")
        continue
    }

    $content = [System.IO.File]::ReadAllText($skillFile, [System.Text.Encoding]::UTF8)
    if (-not $content.StartsWith("---`n")) {
        $errors.Add("SKILL.md must start with YAML frontmatter and LF endings: $($skillDir.Name)")
        continue
    }

    $frontmatterMatch = [regex]::Match($content, "(?s)\A---\n(.*?)\n---(?:\n|\z)")
    if (-not $frontmatterMatch.Success) {
        $errors.Add("Invalid YAML frontmatter boundary: $($skillDir.Name)")
        continue
    }

    $frontmatter = $frontmatterMatch.Groups[1].Value
    $nameMatch = [regex]::Match($frontmatter, "(?m)^name:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*$")
    $descriptionMatch = [regex]::Match($frontmatter, "(?m)^description:\s*(?:\S.*|[>|]-?)\s*$")

    if (-not $nameMatch.Success) {
        $errors.Add("Missing or invalid lowercase-hyphen name: $($skillDir.Name)")
    } else {
        $name = $nameMatch.Groups[1].Value
        if ($name -ne $skillDir.Name) {
            $errors.Add("Directory/name mismatch: $($skillDir.Name) != $name")
        }
        if ($seenNames.ContainsKey($name)) {
            $errors.Add("Duplicate skill name: $name")
        } else {
            $seenNames[$name] = $true
        }
    }

    if (-not $descriptionMatch.Success) {
        $errors.Add("Missing description: $($skillDir.Name)")
    }

    $bytes = [System.IO.File]::ReadAllBytes($skillFile)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $errors.Add("UTF-8 BOM is not allowed: $($skillDir.Name)")
    }
    if ($content.Contains("`r`n")) {
        $errors.Add("CRLF line endings found: $($skillDir.Name)")
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output "Validated $($skillDirs.Count) skills under $SkillsRoot"
