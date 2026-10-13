# Read-only worker. Invoked only by bounded-probes.ps1 with a generated job file.
param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference='Stop'
$job=[IO.File]::ReadAllText($Config,[Text.Encoding]::UTF8)|ConvertFrom-Json
$clock=[Diagnostics.Stopwatch]::StartNew()
$records=New-Object 'System.Collections.Generic.List[object]'
$errors=New-Object 'System.Collections.Generic.List[string]'
$limits=New-Object 'System.Collections.Generic.List[string]'
$stats=@{entries=0;files=0;directories=0;reparsePoints=0;fileBytes=0L;hashedFiles=0;hashedBytes=0L;
    names=@{guid=0;scoped_dir=0;other=0};createdDays=@{};createdMonths=@{};bytesRead=0L}
function Limit([string]$Reason){if($Reason -notin $limits){$limits.Add($Reason)}}
function Add-Record($Record){if($records.Count -lt $job.maxRecords){$records.Add($Record)}else{Limit 'record-limit'}}
function Save-Probe([bool]$Complete){
    $state=if($errors.Count){'ERROR'}elseif(-not $Complete -or $limits.Count){'PARTIAL'}elseif($job.mode -ne 'footprint' -and $records.Count -eq 0){'NO_MATCH'}else{'COMPLETE'}
    $result=[pscustomobject]@{mode=$job.mode;source=$job.source;dataState=$state;complete=($state -in @('COMPLETE','NO_MATCH'));
        elapsedSeconds=$clock.Elapsed.TotalSeconds;depth=$job.depth;limits=@($limits.ToArray());errors=@($errors.ToArray());
        stats=$stats;records=@($records.ToArray());observedAt=[DateTimeOffset]::UtcNow.ToString('o')}
    $temporary=$job.resultPath+'.tmp'
    [IO.File]::WriteAllText($temporary,($result|ConvertTo-Json -Depth 12),[Text.Encoding]::UTF8)
    if([IO.File]::Exists($job.resultPath)){[IO.File]::Replace($temporary,$job.resultPath,[NullString]::Value)}else{[IO.File]::Move($temporary,$job.resultPath)}
}
function Check-Time {
    if($clock.Elapsed.TotalSeconds -ge $job.seconds){Limit 'time-limit';return $false}
    return $true
}
function Assert-ProbeSource([string]$Source){
    $path=[IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Source))
    if($path -notmatch '^[A-Za-z]:\\' -or $path.Substring(2).Contains(':')){throw 'Probe source must be a local absolute path without streams'}
    $cursor=$path
    while($cursor){
        if([IO.File]::Exists($cursor) -or [IO.Directory]::Exists($cursor)){
            if([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint){throw "Reparse ancestor refused: $cursor"}
        }
        $cursor=[IO.Path]::GetDirectoryName($cursor)
    }
    return $path
}
function Read-TextSlice([string]$Path,[bool]$Tail){
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    try{
        $header=New-Object byte[] 3;[void]$stream.Read($header,0,3)
        $unicode=$header[0] -eq 255 -and $header[1] -eq 254
        $bigEndian=$header[0] -eq 254 -and $header[1] -eq 255
        $length=[int][Math]::Min($stream.Length,$job.maxTextBytes)
        $offset=if($Tail){[Math]::Max(0,$stream.Length-$length)}else{0}
        if(($unicode -or $bigEndian) -and $offset%2){$offset--; $length++}
        [void]$stream.Seek($offset,[IO.SeekOrigin]::Begin)
        $buffer=New-Object byte[] $length;$read=0
        while($read -lt $length){$n=$stream.Read($buffer,$read,$length-$read);if(-not $n){break};$read+=$n}
        $stats.bytesRead+=$read
        if($stream.Length -gt $read){Limit 'text-byte-limit'}
        $encoding=if($unicode){[Text.Encoding]::Unicode}elseif($bigEndian){[Text.Encoding]::BigEndianUnicode}else{[Text.Encoding]::UTF8}
        $text=$encoding.GetString($buffer,0,$read).TrimStart([char]0xFEFF)
        if(-not $unicode -and -not $bigEndian -and $text.Contains([string][char]0)){throw 'Unsupported binary or unmarked UTF-16 text'}
        if($offset -gt 0){$break=$text.IndexOf("`n");$text=if($break -ge 0){$text.Substring($break+1)}else{''}}
        return $text
    }finally{$stream.Dispose()}
}
Save-Probe $false
try{
    if($job.mode -in @('footprint','directories','hashes')){
        $root=Assert-ProbeSource $job.source
        if(-not [IO.Directory]::Exists($root)){throw "Directory unavailable: $root"}
        if([IO.File]::GetAttributes($root) -band [IO.FileAttributes]::ReparsePoint){throw "Reparse root refused: $root"}
        $queue=New-Object 'System.Collections.Generic.Queue[object]';$queue.Enqueue(@{path=$root;level=0})
        while($queue.Count -and (Check-Time)){
            $current=$queue.Dequeue()
            $enumerator=[IO.Directory]::EnumerateFileSystemEntries($current.path).GetEnumerator()
            try{
                while(Check-Time){
                    if($stats.entries -ge $job.maxEntries){Limit 'entry-limit';break}
                    if(-not $enumerator.MoveNext()){break}
                    $path=$enumerator.Current;$stats.entries++
                    try{
                        $attributes=[IO.File]::GetAttributes($path)
                        if($attributes -band [IO.FileAttributes]::ReparsePoint){$stats.reparsePoints++;continue}
                        $directory=[bool]($attributes -band [IO.FileAttributes]::Directory)
                        $name=[IO.Path]::GetFileName($path)
                        if($name -match '^\{[0-9a-fA-F-]{36}\}$'){$stats.names.guid++}
                        elseif($name -like 'scoped_dir*'){$stats.names.scoped_dir++}else{$stats.names.other++}
                        if($directory){
                            $stats.directories++;$info=New-Object IO.DirectoryInfo $path
                            $day=$info.CreationTime.ToString('yyyy-MM-dd');$month=$info.CreationTime.ToString('yyyy-MM')
                            if(-not $stats.createdDays.ContainsKey($day)){$stats.createdDays[$day]=0};$stats.createdDays[$day]++
                            if(-not $stats.createdMonths.ContainsKey($month)){$stats.createdMonths[$month]=0};$stats.createdMonths[$month]++
                            if($job.mode -eq 'directories'){Add-Record ([pscustomobject]@{path=$path;createdAt=$info.CreationTime.ToString('o');level=$current.level})}
                            if($current.level -lt $job.depth){$queue.Enqueue(@{path=$path;level=$current.level+1})}
                        }else{
                            $stats.files++;$info=New-Object IO.FileInfo $path;$stats.fileBytes+=$info.Length
                            if($job.mode -eq 'hashes'){
                                $install=[string]([char]0x5B89)+[char]0x88C5
                                $hit=($info.Extension -in @('.exe','.msi') -and ($name -match "_bl|shred_|setup|$install" -or $info.LastWriteTime -gt (Get-Date).AddDays(-14))) -or $name -match '^ins[0-9A-F]+\.tmp(?:\.exe)?$'
                                if($hit){
                                    if($stats.hashedFiles -ge $job.maxHashFiles -or $stats.hashedBytes+$info.Length -gt $job.maxHashBytes){Limit 'hash-budget';continue}
                                    $hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
                                    $stats.hashedFiles++;$stats.hashedBytes+=$info.Length
                                    $zone='NO Zone.Identifier';$ads=$path+':Zone.Identifier'
                                    if([IO.File]::Exists($ads)){$zone=Read-TextSlice $ads $false}
                                    Add-Record ([pscustomobject]@{path=$path;sha256=$hash;length=$info.Length;modifiedAt=$info.LastWriteTime.ToString('o');zone=$zone})
                                }
                            }
                        }
                    }catch{$errors.Add("$path : $($_.Exception.Message)")}
                    if($stats.entries%500 -eq 0){Save-Probe $false}
                }
            }finally{$enumerator.Dispose()}
            if('entry-limit' -in $limits){break}
        }
    }elseif($job.mode -in @('tool-log','config')){
        $path=Assert-ProbeSource $job.source
        if([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint){throw "Reparse file refused: $path"}
        $text=Read-TextSlice $path ($job.mode -eq 'tool-log')
        $lines=0
        foreach($line in ($text -split "`r?`n")){
            if(-not (Check-Time)){break}
            if($lines -ge $job.maxEntries){Limit 'text-line-limit';break};$lines++
            if($job.mode -eq 'tool-log'){
                if($line -match '(?i)Parent process command line:|Client Process:'){Add-Record ([pscustomobject]@{path=$path;line=$line})}
            }else{
                foreach($match in [regex]::Matches($line,'(?i)["'']?([a-z0-9_.-]*(?:interval|delay|rp_interval_h)[a-z0-9_.-]*)["'']?\s*[:=]\s*["'']?(\d+)')){
                    Add-Record ([pscustomobject]@{path=$path;field=$match.Groups[1].Value;value=$match.Groups[2].Value})
                }
                foreach($match in [regex]::Matches($line,'(?i)https?://[^\s"''<>]+')){
                    $uri=$null
                    if([Uri]::TryCreate($match.Value,[UriKind]::Absolute,[ref]$uri)){Add-Record ([pscustomobject]@{path=$path;domain=$uri.DnsSafeHost})}
                }
            }
        }
        $stats.entries=$lines
    }else{throw 'Unknown probe mode'}
}catch{$errors.Add($_.Exception.Message)}
Save-Probe $true
