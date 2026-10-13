. (Join-Path $source 'scripts/bounded-probes.ps1')
$probeOutput=Join-Path $run 'probe-output';[void](New-Item -ItemType Directory $probeOutput)
Test-Case 'footprint-counts-first-level-and-creation-buckets' {
    $root=Join-Path $run 'footprint-source';[void](New-Item -ItemType Directory $root)
    [void](New-Item -ItemType Directory (Join-Path $root '{00000000-0000-0000-0000-000000000001}'))
    [void](New-Item -ItemType Directory (Join-Path $root 'scoped_dir123'))
    [IO.File]::WriteAllText((Join-Path $root 'plain.txt'),'12345')
    $result=Invoke-BoundedProbe -Mode footprint -Source $root -OutputDir $probeOutput
    Assert ($result.dataState -eq 'COMPLETE' -and $result.stats.entries -eq 3 -and $result.stats.directories -eq 2 -and $result.stats.files -eq 1) ($result|ConvertTo-Json -Depth 8)
    Assert ($result.stats.names.guid -eq 1 -and $result.stats.names.scoped_dir -eq 1 -and $result.stats.fileBytes -eq 5) 'Footprint histogram/bytes incorrect'
    Assert (@($result.stats.createdMonths.PSObject.Properties).Count -gt 0) 'Creation buckets missing'
}
Test-Case 'entry-limit-records-partial-lower-bound' {
    $root=Join-Path $run 'entry-cap';[void](New-Item -ItemType Directory $root)
    foreach($number in 1..10){[IO.File]::WriteAllText((Join-Path $root "$number.txt"),'x')}
    $result=Invoke-BoundedProbe -Mode footprint -Source $root -OutputDir $probeOutput -MaxEntries 3
    Assert ($result.dataState -eq 'PARTIAL' -and -not $result.complete -and $result.stats.entries -eq 3 -and 'entry-limit' -in $result.limits) 'Entry budget reported a total count'
}
Test-Case 'footprint-does-not-follow-junctions' {
    $root=Join-Path $run 'probe-link-source';$outside=Join-Path $run 'probe-link-outside'
    [void](New-Item -ItemType Directory $root);[void](New-Item -ItemType Directory $outside)
    [IO.File]::WriteAllText((Join-Path $outside 'outside.txt'),'must not count')
    [void](New-Item -ItemType Junction -Path (Join-Path $root 'junction') -Target $outside)
    $result=Invoke-BoundedProbe -Mode footprint -Source $root -OutputDir $probeOutput -Depth 1
    Assert ($result.stats.reparsePoints -eq 1 -and $result.stats.files -eq 0) 'Junction target traversed'
}
Test-Case 'probe-source-under-junction-is-rejected' {
    $root=Join-Path $run 'probe-link-source'
    $result=Invoke-BoundedProbe -Mode footprint -Source (Join-Path $root 'junction') -OutputDir $probeOutput
    Assert ($result.dataState -eq 'ERROR' -and ($result.errors -join '; ').Contains('Reparse ancestor')) 'Linked source accepted'
}
Test-Case 'hash-byte-budget-is-partial-not-no-match' {
    $root=Join-Path $run 'hash-budget';[void](New-Item -ItemType Directory $root)
    [IO.File]::WriteAllText((Join-Path $root 'setup.exe'),'too large for tiny budget')
    $result=Invoke-BoundedProbe -Mode hashes -Source $root -OutputDir $probeOutput -MaxHashBytes 1
    Assert ($result.dataState -eq 'PARTIAL' -and $result.stats.hashedFiles -eq 0 -and 'hash-budget' -in $result.limits) 'Skipped hash presented as no match'
}
Test-Case 'bounded-installer-hash-retains-source-hash' {
    $root=Join-Path $run 'hash-source';[void](New-Item -ItemType Directory $root)
    $file=Join-Path $root 'setup.exe';[IO.File]::WriteAllText($file,'fixture installer, never execute')
    $result=Invoke-BoundedProbe -Mode hashes -Source $root -OutputDir $probeOutput
    Assert ($result.dataState -eq 'COMPLETE' -and $result.records[0].sha256 -eq (Get-FileHash -LiteralPath $file).Hash) ($result|ConvertTo-Json -Depth 8)
}
Test-Case 'tool-log-reads-bounded-tail-and-labels-partial' {
    $file=Join-Path $run 'fixture-dism.log'
    [IO.File]::WriteAllText($file,(('x'*4096)+"`nParent process command line: fixture-cleanmgr.exe /autoclean`n"))
    $result=Invoke-BoundedProbe -Mode tool-log -Source $file -OutputDir $probeOutput -MaxTextBytes 256
    Assert ($result.dataState -eq 'PARTIAL' -and $result.stats.bytesRead -le 256 -and $result.records[0].line.Contains('fixture-cleanmgr.exe')) 'Tail budget/caller extraction failed'
}
Test-Case 'config-fields-do-not-copy-url-credentials-or-query' {
    $file=Join-Path $run 'fixture-config.ini'
    [IO.File]::WriteAllText($file,"interval=300000`nrp_interval_h=2`nurl=https://user:secret@example.com/path?token=secret`n")
    $result=Invoke-BoundedProbe -Mode config -Source $file -OutputDir $probeOutput
    $text=$result.records|ConvertTo-Json -Depth 8
    Assert ($text.Contains('300000') -and $text.Contains('example.com') -and -not $text.Contains('secret')) 'Configuration extraction included credential/query data'
}
Test-Case 'text-line-budget-and-binary-rejection' {
    $file=Join-Path $run 'line-budget.ini';[IO.File]::WriteAllText($file,"interval=1`ninterval=2`ninterval=3`n")
    $result=Invoke-BoundedProbe -Mode config -Source $file -OutputDir $probeOutput -MaxEntries 1
    Assert ($result.dataState -eq 'PARTIAL' -and 'text-line-limit' -in $result.limits -and $result.records.Count -eq 1) 'Line budget ignored'
    $file=Join-Path $run 'binary-config.dat';[IO.File]::WriteAllBytes($file,[byte[]]@(1,0,3,4))
    $result=Invoke-BoundedProbe -Mode config -Source $file -OutputDir $probeOutput
    Assert ($result.dataState -eq 'ERROR' -and @($result.records).Count -eq 0) 'Binary content treated as configuration evidence'
}
Test-Case 'disabled-no-match-and-partial-acquisition-are-distinct' {
    $tokens=$null;$errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'scripts/collect-forensics.ps1'),[ref]$tokens,[ref]$errors)
    foreach($name in @('W','Save')){
        $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
        . ([scriptblock]::Create($node.Extent.Text))
    }
    $OutDir=Join-Path $run 'section-states';[void](New-Item -ItemType Directory $OutDir)
    $sections=New-Object 'System.Collections.Generic.List[object]'
    $script:EvidenceBuf=New-Object 'System.Collections.Generic.List[string]'
    $script:AcquisitionErrors=New-Object 'System.Collections.Generic.List[string]';$Error.Clear()
    W '# disabled';Save 'disabled.txt' $false
    W '# enabled, no hit';Save 'no-match.txt'
    W '# partial';Save 'partial.txt' $true 'PARTIAL'
    Assert ($sections[0].status -eq 'SKIP' -and $sections[0].dataState -eq 'NOT_ENABLED' -and
        $sections[1].status -eq 'PASS' -and $sections[1].dataState -eq 'NO_MATCH' -and $sections[2].status -eq 'PARTIAL') 'Coverage states collapsed'
}
Test-Case 'missing-probe-source-is-error' {
    $result=Invoke-BoundedProbe -Mode footprint -Source (Join-Path $run 'missing-probe-source') -OutputDir $probeOutput
    Assert ($result.dataState -eq 'ERROR') 'Unavailable source treated as empty directory'
}
Test-Case 'parent-timeout-terminates-only-owned-worker' {
    $tools=Join-Path $run 'timeout-tools';[void](New-Item -ItemType Directory $tools)
    Copy-Item -LiteralPath (Join-Path $source 'scripts/bounded-probes.ps1') -Destination $tools
    [IO.File]::WriteAllText((Join-Path $tools 'bounded-worker.ps1'),'param($Config) Start-Sleep -Seconds 10')
    . (Join-Path $tools 'bounded-probes.ps1')
    $clock=[Diagnostics.Stopwatch]::StartNew()
    $result=Invoke-BoundedProbe -Mode footprint -Source $run -OutputDir $probeOutput -Seconds 1
    Assert ($result.dataState -eq 'PARTIAL' -and 'worker-timeout' -in $result.limits -and $clock.Elapsed.TotalSeconds -lt 8) 'Blocked worker escaped deadline'
}
