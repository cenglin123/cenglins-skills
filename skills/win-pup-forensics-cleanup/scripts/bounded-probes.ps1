# Isolated read-only probes. Parent can terminate only its own timed-out worker.
function Invoke-BoundedProbe {
    param([string]$Mode,[string]$Source,[string]$OutputDir,[int]$Seconds=15,[int]$MaxEntries=20000,
        [int]$Depth=0,[int]$MaxRecords=2000,[long]$MaxTextBytes=1048576,[int]$MaxHashFiles=50,[long]$MaxHashBytes=536870912)
    $directory=Join-Path $OutputDir 'probes';[void](New-Item -ItemType Directory -Path $directory -Force)
    $tag=[guid]::NewGuid().ToString('N');$config=Join-Path $directory ($tag+'.job.json');$resultPath=Join-Path $directory ($tag+'.result.json')
    $job=[pscustomobject]@{mode=$Mode;source=$Source;resultPath=$resultPath;seconds=$Seconds;maxEntries=$MaxEntries;
        depth=$Depth;maxRecords=$MaxRecords;maxTextBytes=$MaxTextBytes;maxHashFiles=$MaxHashFiles;maxHashBytes=$MaxHashBytes}
    [IO.File]::WriteAllText($config,($job|ConvertTo-Json),[Text.Encoding]::UTF8)
    $worker=Join-Path $PSScriptRoot 'bounded-worker.ps1'
    $payload=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($worker+"`n"+$config)))
    $command='$p=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("'+$payload+'"))) -split "`n"; & $p[0] -Config $p[1]'
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $shell=(Get-Process -Id $PID).Path
    $parentClock=[Diagnostics.Stopwatch]::StartNew()
    $process=Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand',$encoded) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $directory ($tag+'.stdout.txt')) -RedirectStandardError (Join-Path $directory ($tag+'.stderr.txt'))
    # Cache the handle while alive; PS 5.1 Start-Process can otherwise lose ExitCode.
    $workerHandle=$process.Handle
    $timedOut=-not $process.WaitForExit(($Seconds+2)*1000)
    if($timedOut){
        if(-not $process.HasExited){try{$process.Kill()}catch{if(-not $process.HasExited){throw}}}
        [void]$process.WaitForExit(1000)
    }
    if([IO.File]::Exists($resultPath)){$result=[IO.File]::ReadAllText($resultPath,[Text.Encoding]::UTF8)|ConvertFrom-Json}
    else{$result=[pscustomobject]@{mode=$Mode;source=$Source;dataState='ERROR';complete=$false;elapsedSeconds=0;depth=$Depth;
        limits=@();errors=@('Worker did not produce a result');stats=$null;records=@();observedAt=[DateTimeOffset]::UtcNow.ToString('o')}}
    if($timedOut){$result.dataState='PARTIAL';$result.complete=$false;$result.limits=@($result.limits)+@('worker-timeout')}
    elseif($process.ExitCode -ne 0){$result.dataState='ERROR';$result.complete=$false;$result.errors=@($result.errors)+@("Worker exit $($process.ExitCode)")}
    $process.Dispose()
    $result|Add-Member NoteProperty parentElapsedSeconds $parentClock.Elapsed.TotalSeconds
    return $result
}
function Write-ProbeEvidence($Result){
    W ($Result|ConvertTo-Json -Depth 12)
    if($Result.dataState -eq 'ERROR'){foreach($failure in $Result.errors){$script:AcquisitionErrors.Add([string]$failure)}}
}
function Get-ProbeDataState($Results){
    if(@($Results|Where-Object dataState -eq 'ERROR').Count){return 'ERROR'}
    if(@($Results|Where-Object dataState -eq 'PARTIAL').Count){return 'PARTIAL'}
    if(@($Results|Where-Object dataState -eq 'COMPLETE').Count){return 'COMPLETE'}
    return 'NO_MATCH'
}
