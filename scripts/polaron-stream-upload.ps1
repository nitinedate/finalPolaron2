<#
.SYNOPSIS
  V1.31.14: sends a phone collection made on this computer to the Polaron forensic server while it runs.

.DESCRIPTION
  Started hidden by the Polaron helper (host-drive-helper.ps1 5.66+) with -StreamFile <jobs>\<job>.stream.json
  when the Polaron page starts a collection on this computer. The stream file holds the upload session the
  server issued for this case; the server address is the one this computer was set up for (setup file).

  While the collection runs, every finished file of the run (and of the iPhone backup's staging folder) is
  sent in chunks; when the collection ends, the rest is sent and the server registers the run. The server
  checks every file's SHA-256 on its own disk before it keeps it and never overwrites a file.

  Only after the server confirmed the whole run is the copy on this computer removed (unless the examiner
  chose to keep one). A receipt MOVED_TO_SERVER_<run>.json stays in the case folder. When the collection
  fails or is stopped, or anything could not be sent, the files stay here.

  If this computer restarts, the helper starts this script again; it continues from what the server holds.
  Log: <jobs>\<job>.stream.log   Status (read by the page through the helper): <jobs>\<job>.stream.status.json
#>
param(
    [Parameter(Mandatory = $true)][string]$StreamFile,
    [int]$PollSec = 5,
    [int]$StableSec = 45,
    [int]$ChunkMB = 8,
    [int]$SmallFileKB = 1024,
    [int]$GraceSec = 45
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Net.ServicePointManager]::Expect100Continue = $false } catch {}
try { [Net.ServicePointManager]::DefaultConnectionLimit = 8 } catch {}

$StreamFile = [IO.Path]::GetFullPath($StreamFile)
$jobsDir = Split-Path -Parent $StreamFile
$cfg = Get-Content -LiteralPath $StreamFile -Raw | ConvertFrom-Json
$job = [string]$cfg.job_id
if ($job -notmatch '^[0-9a-f]{32}$') { throw "stream file has no job id" }
$base = Join-Path $jobsDir $job
$logFile = $base + ".stream.log"
$statusFile = $base + ".stream.status.json"
$doneFile = $base + ".stream.done"
$ledgerFile = $base + ".stream.ledger"
$stateFile = $base + ".stream.state.json"

$script:Server = ([string]$cfg.server).TrimEnd('/')
$script:SessionId = [string]$cfg.session_id
$script:Token = [string]$cfg.token
$script:Service = [string]$cfg.service
$keepLocal = [bool]$cfg.keep_local
$platform = [string]$cfg.platform
$chunkBytes = [Math]::Max(1, [Math]::Min(32, $ChunkMB)) * 1MB
$smallBytes = [int64]$SmallFileKB * 1KB
$scope = $(if ([string]$cfg.scope -eq "packages") { "packages" } else { "full" })
$scopeFolders = @("02_Original_Extraction", "03_Working_Copy", "05_Exports", "06_Reports", "07_Logs", "08_Hashes")
$liveFolders = @("02_Original_Extraction", "03_Working_Copy")
if ($scope -eq "packages") {
    # Packages, logs and hashes only (slow link): built at the end; the full run stays on this computer.
    $scopeFolders = @("05_Exports", "07_Logs", "08_Hashes")
    $liveFolders = @()
    $keepLocal = $true
}
# Files a running collector still rewrites: sent only once the collection has ended.
$mutableNameRe = '^(?i)(Manifest\.db|Manifest\.plist|Manifest\.mbdb|Status\.plist|Info\.plist|backup\.out|backup\.err|progress\.json|EXTERNAL_IOS_PAYLOAD\.json)$'
$mutableSuffixRe = '(?i)(-journal|-wal|-shm|\.tmp|\.temp|\.part|\.partial|\.lock|\.crdownload|\.polaron-part|\.log)$'

function Write-Log([string]$Text) {
    try {
        if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 4MB) {
            Move-Item -LiteralPath $logFile -Destination ($logFile + ".old") -Force
        }
        Add-Content -LiteralPath $logFile -Value ((Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "  " + $Text) -Encoding ASCII
    } catch {}
}

# ---------------------------------------------------------------- single instance
$script:Mutex = $null
try {
    $script:Mutex = New-Object System.Threading.Mutex($false, ("Local\PolaronStreamUpload_" + $job))
    $owned = $false
    try { $owned = $script:Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
} catch { $script:Mutex = $null }

# ---------------------------------------------------------------- status for the page
$script:Status = @{
    ok = $true; state = "waiting"; detail = "Waiting for the first finished files of the collection"
    session_id = $script:SessionId; run_name = ""; files_sent = 0; bytes_sent = [int64]0
    files_seen = 0; bytes_seen = [int64]0; keep_local = $keepLocal; deleted_local = $false
    server_case_dir = ""; error = ""; warnings = @(); updated_utc = ""
}
function Save-Status([hashtable]$Values) {
    foreach ($k in $Values.Keys) { $script:Status[$k] = $Values[$k] }
    $script:Status.updated_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    try { [IO.File]::WriteAllText($statusFile, ($script:Status | ConvertTo-Json -Depth 4 -Compress), (New-Object Text.UTF8Encoding($false))) } catch {}
}

function Complete-Stream([string]$State, [string]$Detail, [string]$ErrorText = "") {
    Save-Status @{ state = $State; detail = $Detail; error = $ErrorText; ok = ($State -eq "completed") }
    try { [IO.File]::WriteAllText($doneFile, $State, (New-Object Text.UTF8Encoding($false))) } catch {}
    # The upload token is of no further use: remove it from the stream file.
    try {
        $c = Get-Content -LiteralPath $StreamFile -Raw | ConvertFrom-Json
        $c.token = ""
        [IO.File]::WriteAllText($StreamFile, ($c | ConvertTo-Json -Depth 4 -Compress), (New-Object Text.UTF8Encoding($false)))
    } catch {}
    Write-Log ("Finished: " + $State + " - " + $Detail + $(if ($ErrorText) { " (" + $ErrorText + ")" } else { "" }))
    if ($script:Mutex) { try { $script:Mutex.ReleaseMutex() } catch {} }
    exit 0
}

# ---------------------------------------------------------------- HTTP
function ConvertTo-JsonText([string]$Text) {
    if ($null -eq $Text) { return 'null' }
    $t = $Text.Replace('\', '\\').Replace('"', '\"')
    if ($t -match '[\x00-\x1f]') {
        $t = [regex]::Replace($t, '[\x00-\x1f]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    }
    return '"' + $t + '"'
}

function Invoke-Stream {
    param(
        [string]$Method,
        [string]$Path,
        [byte[]]$Body = $null,
        [int]$Count = -1,
        [hashtable]$Headers = @{},
        [int]$TimeoutSec = 300,
        [string]$ContentType = "application/json"
    )
    $uri = $script:Server + "/api/acquisition/client-stream/" + $script:SessionId + $Path
    $resp = $null
    try {
        $req = [Net.HttpWebRequest]::Create($uri)
        $req.Method = $Method
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $req.AllowAutoRedirect = $false
        $req.UserAgent = "PolaronStreamUpload/1.0"
        $req.Headers.Add("X-Polaron-Stream-Token", $script:Token)
        if ($script:Service) { $req.Headers.Add("X-Aetheris-Service", $script:Service) }
        foreach ($k in $Headers.Keys) { $req.Headers.Add($k, [string]$Headers[$k]) }
        if ($null -ne $Body) {
            if ($Count -lt 0) { $Count = $Body.Length }
            $req.ContentType = $ContentType
            $req.ContentLength = $Count
            $req.AllowWriteStreamBuffering = $false
            $rs = $req.GetRequestStream()
            try { $rs.Write($Body, 0, $Count) } finally { $rs.Close() }
        } elseif ($Method -ne "GET") {
            $req.ContentLength = 0
        }
        $resp = $req.GetResponse()
    } catch {
        $ex = $_.Exception
        while ($ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
        if ($ex -and $ex.Response) { $resp = $ex.Response }
        else { return @{ status = 0; json = $null; text = ""; error = $_.Exception.Message } }
    }
    $text = ""
    $status = 0
    try {
        $status = [int]$resp.StatusCode
        $reader = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
    } catch {
    } finally {
        try { $resp.Close() } catch {}
    }
    $json = $null
    if ($text -and $text.TrimStart().StartsWith("{")) { try { $json = $text | ConvertFrom-Json } catch {} }
    return @{ status = $status; json = $json; text = $text; error = "" }
}

function Get-ErrorCode($r) {
    if ($r.json -and $r.json.error) { return [string]$r.json.error }
    if ($r.status -eq 0) { return "unreachable" }
    return ("http_" + $r.status)
}

# Stops for good on answers that will not change by retrying (session gone, examiner stopped it).
function Test-FatalAnswer($r) {
    $code = Get-ErrorCode $r
    if ($r.status -eq 401 -or $r.status -eq 404 -or $code -eq "cancelled" -or $code -eq "unknown_session" -or $code -eq "unauthorized" -or $code -eq "failed") {
        $why = $(if ($r.json -and $r.json.detail) { [string]$r.json.detail } else { $code })
        if ($code -eq "cancelled") {
            Complete-Stream "cancelled" "Sending to the server was stopped by the examiner. The collection stays on this computer." $why
        }
        Complete-Stream "kept_local" "The server no longer accepts this upload. The collection stays on this computer; copy it to the server from the Polaron page." $why
    }
}

$script:Backoff = 2
function Wait-Retry([string]$Why) {
    Save-Status @{ detail = ("Waiting for the forensic server (" + $Why + "). Retrying in " + $script:Backoff + " s.") }
    Write-Log ("Retry in " + $script:Backoff + " s: " + $Why)
    Start-Sleep -Seconds $script:Backoff
    $script:Backoff = [Math]::Min(60, $script:Backoff * 2)
}
function Reset-Backoff { $script:Backoff = 2 }

function Get-RelHeader([string]$Rel) { return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Rel)) }

# ---------------------------------------------------------------- where the collection writes
$repoRoot = $(if ($cfg.root) { [string]$cfg.root } else { Split-Path -Parent $PSScriptRoot })
function Resolve-CaseDir($Payload) {
    $caseRoot = [string]$Payload.case_root
    if ($caseRoot -match '^[\\/]evidence([\\/]|$)') {
        $rel = ($caseRoot -replace '^[\\/]evidence[\\/]?', '' -replace '/', '\')
        $caseRoot = $(if ($rel) { Join-Path (Join-Path $repoRoot "evidence") $rel } else { Join-Path $repoRoot "evidence\cases" })
    } elseif (-not $caseRoot -or $caseRoot -eq "/evidence/cases" -or $caseRoot -eq "\evidence\cases") {
        $caseRoot = Join-Path $repoRoot "evidence\cases"
    }
    $caseId = ([string]$Payload.case_id) -replace '[^\w\-.]', '_'
    if (-not $caseId) { $caseId = "CASE-UNKNOWN" }
    return (Join-Path $caseRoot $caseId)
}

function Read-JsonFile([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = [IO.File]::ReadAllText($Path)
        if ($raw.Length -gt 0 -and $raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
        if (-not $raw.Trim()) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch { return $null }
}

$saved = Read-JsonFile $stateFile
$caseDir = $(if ($saved -and $saved.case_dir) { [string]$saved.case_dir } else { "" })
$runName = $(if ($saved -and $saved.run_name) { [string]$saved.run_name } else { "" })
if (-not $caseDir) {
    $payload = Read-JsonFile ([string]$cfg.payload_file)
    if (-not $payload) { Complete-Stream "kept_local" "The collection's settings file is gone; nothing was sent." "payload file missing" }
    $caseDir = Resolve-CaseDir $payload
}
function Save-RunState {
    try {
        [IO.File]::WriteAllText($stateFile, (@{ case_dir = $caseDir; run_name = $runName } | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    } catch {}
}

# The collector can write a result before it is really done (a failed step it then retries), so the
# collection counts as ended only once its process has exited.
function Test-JobAlive {
    $jobPid = [int]$cfg.pid
    if ($jobPid -le 0) { return $false }
    $proc = Get-Process -Id $jobPid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if ($proc.ProcessName -notmatch '(?i)^(powershell|pwsh)$') { return $false }
    try {
        $created = [DateTime]::Parse([string]$cfg.created_utc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
        if ($proc.StartTime.ToUniversalTime() -gt $created.AddMinutes(2)) { return $false }   # the number was reused after a restart
    } catch {}
    return $true
}

function Get-JobState {
    if (Test-JobAlive) { return @{ done = $false; result = $null } }
    return @{ done = $true; result = (Read-JsonFile ([string]$cfg.result_file)) }
}

function Find-RunName {
    foreach ($f in @([string]$cfg.progress_file, [string]$cfg.result_file)) {
        $j = Read-JsonFile $f
        if ($j -and $j.run_name -and ([string]$j.run_name) -notmatch '[\\/]') { return [string]$j.run_name }
    }
    return ""
}

# ---------------------------------------------------------------- local files
$script:SeenFiles = 0
$script:SeenBytes = [int64]0
function Add-Tree([System.Collections.Generic.List[object]]$Out, [string]$Dir, [string]$RelBase, [int]$Depth) {
    if ($Depth -gt 48) { return }
    $di = New-Object IO.DirectoryInfo($Dir)
    $items = $null
    try { $items = $di.GetFileSystemInfos() } catch { return }
    foreach ($it in $items) {
        if ($it -is [IO.DirectoryInfo]) {
            Add-Tree $Out $it.FullName ($RelBase + "/" + $it.Name) ($Depth + 1)
        } else {
            $Out.Add([pscustomobject]@{
                rel = $RelBase + "/" + $it.Name; full = $it.FullName; name = $it.Name
                size = [int64]$it.Length; ticks = [int64]$it.LastWriteTimeUtc.Ticks
            })
        }
    }
}

function Get-StagingRoots {
    # iPhone backups are written to <drive>:\ib\<id>\<stamp> first and moved into the case when complete.
    $out = New-Object System.Collections.Generic.List[string]
    if ($platform -ne "ios" -or $runName -notmatch '_(\d{8}_\d{6})$') { return ,$out }
    $stamp = $Matches[1]
    $ibs = New-Object System.Collections.Generic.List[string]
    foreach ($d in [IO.DriveInfo]::GetDrives()) {
        try { if ($d.DriveType -eq [IO.DriveType]::Fixed -and $d.IsReady) { $ibs.Add((Join-Path $d.RootDirectory.FullName "ib")) } } catch {}
    }
    if ($env:TEMP) { $ibs.Add((Join-Path $env:TEMP "ib")) }
    foreach ($ib in $ibs) {
        try {
            if (-not [IO.Directory]::Exists($ib)) { continue }
            foreach ($tail in [IO.Directory]::GetDirectories($ib)) {
                $cand = Join-Path $tail $stamp
                if ([IO.Directory]::Exists($cand)) { $out.Add($cand) }
            }
        } catch {}
    }
    return ,$out
}

function Get-LocalFiles([bool]$Running) {
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($folder in $scopeFolders) {
        $dir = Join-Path (Join-Path $caseDir $folder) $runName
        if ([IO.Directory]::Exists($dir)) { Add-Tree $list $dir ($folder + "/" + $runName) 0 }
    }
    if ($Running) {
        foreach ($stage in (Get-StagingRoots)) {
            foreach ($udid in [IO.Directory]::GetDirectories($stage)) {
                $leaf = Split-Path -Leaf $udid
                Add-Tree $list $udid ("02_Original_Extraction/" + $runName + "/ios_image/" + $leaf) 0
            }
        }
    }
    # The same file can be seen twice for a moment (staging and case); keep one per path.
    $byRel = @{}
    foreach ($f in $list) { $byRel[$f.rel] = $f }
    $script:SeenFiles = $byRel.Count
    $script:SeenBytes = [int64]0
    foreach ($f in $byRel.Values) { $script:SeenBytes += $f.size }
    return $byRel
}

# ---------------------------------------------------------------- what the server holds
$uploaded = @{}      # rel -> @{ size; ticks; sha }
$serverOnly = @{}    # rel -> @{ size; sha } (on the server, not in this computer's ledger)
$conflicts = @{}     # rel -> reason (never sent; the local copy is kept)
$stableSince = @{}   # rel -> @{ size; ticks; at }

function Add-Ledger([string]$Rel, [int64]$Size, [int64]$Ticks, [string]$Sha) {
    $uploaded[$Rel] = @{ size = $Size; ticks = $Ticks; sha = $Sha }
    try { [IO.File]::AppendAllText($ledgerFile, ($Rel + "`t" + $Size + "`t" + $Ticks + "`t" + $Sha + "`n"), (New-Object Text.UTF8Encoding($false))) } catch {}
    $script:Status.files_sent = $uploaded.Count
    $script:Status.bytes_sent = [int64]$script:Status.bytes_sent + $Size
}

function Sync-ServerState {
    while ($true) {
        $r = Invoke-Stream -Method GET -Path "/status?format=tsv" -TimeoutSec 600
        if ($r.status -eq 200 -and $r.text.StartsWith("#status")) { break }
        Test-FatalAnswer $r
        Wait-Retry (Get-ErrorCode $r)
    }
    Reset-Backoff
    $server = @{}
    foreach ($line in $r.text.Split("`n")) {
        if (-not $line -or $line.StartsWith("#")) { continue }
        $p = $line.Split("`t", 3)
        if ($p.Count -eq 3) { $server[$p[2]] = @{ size = [int64]$p[0]; sha = $p[1] } }
    }
    $ledger = @{}
    if (Test-Path -LiteralPath $ledgerFile) {
        foreach ($line in [IO.File]::ReadAllLines($ledgerFile)) {
            $p = $line.Split("`t")
            if ($p.Count -eq 4) { $ledger[$p[0]] = @{ size = [int64]$p[1]; ticks = [int64]$p[2]; sha = $p[3] } }
        }
    }
    $uploaded.Clear()
    $serverOnly.Clear()
    $bytes = [int64]0
    foreach ($rel in $server.Keys) {
        $s = $server[$rel]
        $l = $ledger[$rel]
        if ($l -and $l.size -eq $s.size -and $l.sha -eq $s.sha) { $uploaded[$rel] = $l; $bytes += $l.size }
        else { $serverOnly[$rel] = $s }
    }
    $script:Status.files_sent = $uploaded.Count
    $script:Status.bytes_sent = $bytes
    if ($server.Count) { Write-Log ("Server already holds " + $server.Count + " file(s) of this collection.") }
}

function Get-FileSha([string]$Path) {
    $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    try {
        $h = [Security.Cryptography.SHA256]::Create()
        try { return (([BitConverter]::ToString($h.ComputeHash($fs))) -replace '-', '').ToLowerInvariant() } finally { $h.Dispose() }
    } finally { $fs.Dispose() }
}

function Test-SameStat($f) {
    try {
        $fi = New-Object IO.FileInfo($f.full)
        $fi.Refresh()
        return ($fi.Exists -and [int64]$fi.Length -eq $f.size -and [int64]$fi.LastWriteTimeUtc.Ticks -eq $f.ticks)
    } catch { return $false }
}

# A file sent earlier changed on this computer: the server copy from this session is replaced.
function Remove-ServerCopy([string]$Rel) {
    while ($true) {
        $body = [Text.Encoding]::UTF8.GetBytes('{"rel":' + (ConvertTo-JsonText $Rel) + '}')
        $r = Invoke-Stream -Method POST -Path "/discard" -Body $body
        if ($r.status -eq 200) { break }
        Test-FatalAnswer $r
        if ($r.status -eq 409) { return $false }
        Wait-Retry (Get-ErrorCode $r)
    }
    Reset-Backoff
    if ($uploaded.ContainsKey($Rel)) {
        $script:Status.bytes_sent = [int64]$script:Status.bytes_sent - [int64]$uploaded[$Rel].size
        $uploaded.Remove($Rel)
    }
    $serverOnly.Remove($Rel)
    Write-Log ("Replacing the server copy of a file that changed here: " + $Rel)
    return $true
}

function Send-Commit($f, [string]$Sha) {
    $body = [Text.Encoding]::UTF8.GetBytes(
        '{"rel":' + (ConvertTo-JsonText $f.rel) + ',"size":' + $f.size + ',"sha256":"' + $Sha +
        '","mtime_ms":' + [int64](($f.ticks - 621355968000000000) / 10000) +
        ',"total_files":' + $script:SeenFiles + ',"total_bytes":' + $script:SeenBytes + '}')
    $timeout = [int][Math]::Max(300, $f.size / 20MB)
    while ($true) {
        $r = Invoke-Stream -Method POST -Path "/commit" -Body $body -TimeoutSec $timeout
        if ($r.status -eq 200 -and $r.json -and $r.json.ok) { Reset-Backoff; return "ok" }
        Test-FatalAnswer $r
        $code = Get-ErrorCode $r
        if ($code -eq "conflict") { return "conflict" }
        if ($r.status -eq 422) { return "resend" }
        if ($r.status -ge 400 -and $r.status -lt 500 -and $r.status -ne 408 -and $r.status -ne 429) { return ("error:" + $code) }
        Wait-Retry $code
    }
}

function Send-LargeFile($f) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::Open($f.full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    try {
        $buf = New-Object byte[] $chunkBytes
        $offset = [int64]0
        $relHeader = Get-RelHeader $f.rel
        $existsOnServer = $false
        while ($offset -lt $f.size) {
            $fs.Position = $offset
            $want = [int][Math]::Min([int64]$chunkBytes, $f.size - $offset)
            $n = 0
            while ($n -lt $want) {
                $got = $fs.Read($buf, $n, $want - $n)
                if ($got -le 0) { break }
                $n += $got
            }
            if ($n -ne $want) { return "changed" }
            $r = Invoke-Stream -Method PUT -Path "/chunk" -Body $buf -Count $n -ContentType "application/octet-stream" `
                -Headers @{ "X-Stream-Rel" = $relHeader; "X-Stream-Offset" = [string]$offset }
            if ($r.status -eq 200) {
                [void]$sha.TransformBlock($buf, 0, $n, $null, 0)
                $offset += $n
                Reset-Backoff
                Save-Status @{ state = "sending"; detail = ("Sending " + $f.name + " (" + [Math]::Round(100.0 * $offset / [Math]::Max(1, $f.size)) + "%)") }
                continue
            }
            Test-FatalAnswer $r
            $code = Get-ErrorCode $r
            if ($r.status -eq 409 -and $code -eq "exists") { $existsOnServer = $true; break }
            if ($r.status -eq 409 -and $code -eq "offset_mismatch") {
                $have = [int64]$(if ($r.json -and $r.json.size) { $r.json.size } else { 0 })
                if ($have -gt $offset -and $have -le $f.size) {
                    # An earlier attempt already sent up to $have: hash that part here instead of sending it again.
                    $fs.Position = $offset
                    while ($offset -lt $have) {
                        $want = [int][Math]::Min([int64]$chunkBytes, $have - $offset)
                        $got = $fs.Read($buf, 0, $want)
                        if ($got -le 0) { return "changed" }
                        [void]$sha.TransformBlock($buf, 0, $got, $null, 0)
                        $offset += $got
                    }
                } else {
                    $sha.Dispose()
                    $sha = [Security.Cryptography.SHA256]::Create()
                    $offset = 0
                }
                continue
            }
            if ($r.status -ge 400 -and $r.status -lt 500 -and $r.status -ne 408 -and $r.status -ne 429) { return ("error:" + $code) }
            Wait-Retry $code
        }
        if ($existsOnServer) {
            $fs.Position = $offset
            while ($true) {
                $got = $fs.Read($buf, 0, $buf.Length)
                if ($got -le 0) { break }
                [void]$sha.TransformBlock($buf, 0, $got, $null, 0)
            }
        }
        [void]$sha.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        $hex = (([BitConverter]::ToString($sha.Hash)) -replace '-', '').ToLowerInvariant()
    } finally {
        $fs.Dispose()
        $sha.Dispose()
    }
    if (-not (Test-SameStat $f)) { return "changed" }
    $c = Send-Commit $f $hex
    if ($c -eq "ok") { Add-Ledger $f.rel $f.size $f.ticks $hex }
    return $c
}

$script:Bundle = New-Object System.Collections.Generic.List[object]
$script:BundleBytes = [int64]0
function Send-Bundle {
    if ($script:Bundle.Count -eq 0) { return }
    $items = @($script:Bundle.ToArray())
    $script:Bundle.Clear()
    $script:BundleBytes = [int64]0
    $head = New-Object Text.StringBuilder
    [void]$head.Append('{"total_files":' + $script:SeenFiles + ',"total_bytes":' + $script:SeenBytes + ',"files":[')
    $ms = New-Object IO.MemoryStream
    $first = $true
    foreach ($it in $items) {
        if (-not $first) { [void]$head.Append(',') }
        $first = $false
        [void]$head.Append('{"rel":' + (ConvertTo-JsonText $it.f.rel) + ',"size":' + $it.f.size + ',"sha256":"' + $it.sha +
            '","mtime_ms":' + [int64](($it.f.ticks - 621355968000000000) / 10000) + '}')
    }
    [void]$head.Append("]}`n")
    $hb = [Text.Encoding]::UTF8.GetBytes($head.ToString())
    $ms.Write($hb, 0, $hb.Length)
    foreach ($it in $items) { $ms.Write($it.data, 0, $it.data.Length) }
    $body = $ms.ToArray()
    $ms.Dispose()
    while ($true) {
        $r = Invoke-Stream -Method POST -Path "/files" -Body $body -ContentType "application/octet-stream" -TimeoutSec 600
        if ($r.status -eq 200 -and $r.json) { break }
        Test-FatalAnswer $r
        $code = Get-ErrorCode $r
        if ($r.status -ge 400 -and $r.status -lt 500 -and $r.status -ne 408 -and $r.status -ne 429 -and $code -ne "short_body") {
            Write-Log ("The server refused a group of small files: " + $code)
            return
        }
        Wait-Retry $code
    }
    Reset-Backoff
    $byRel = @{}
    foreach ($res in @($r.json.results)) { $byRel[[string]$res.rel] = $res }
    foreach ($it in $items) {
        $res = $byRel[$it.f.rel]
        if ($res -and $res.ok) { Add-Ledger $it.f.rel $it.f.size $it.f.ticks $it.sha }
        elseif ($res -and $res.error -eq "conflict") { $conflicts[$it.f.rel] = "A different file with this name is already on the server" }
        else { Write-Log ("Will send again: " + $it.f.rel + " (" + $(if ($res) { $res.error } else { "no answer" }) + ")") }
    }
    Save-Status @{ state = "sending"; detail = ("" + $uploaded.Count + " file(s) verified on the forensic server") }
}

function Add-ToBundle($f) {
    $fs = $null
    try {
        $fs = [IO.File]::Open($f.full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        $data = New-Object byte[] $f.size
        $n = 0
        while ($n -lt $f.size) {
            $got = $fs.Read($data, $n, [int]($f.size - $n))
            if ($got -le 0) { break }
            $n += $got
        }
        if ($n -ne $f.size) { return $false }
    } catch { return $false } finally { if ($fs) { $fs.Dispose() } }
    if (-not (Test-SameStat $f)) { return $false }
    $h = [Security.Cryptography.SHA256]::Create()
    $sha = (([BitConverter]::ToString($h.ComputeHash($data))) -replace '-', '').ToLowerInvariant()
    $h.Dispose()
    $script:Bundle.Add(@{ f = $f; data = $data; sha = $sha })
    $script:BundleBytes += $f.size
    if ($script:Bundle.Count -ge 500 -or $script:BundleBytes -ge 16MB) { Send-Bundle }
    return $true
}

# Sends what is ready. While the collection runs, only files that have not changed for $StableSec and that
# the collector does not rewrite; once it has ended, everything.
function Send-Ready([bool]$Running) {
    $files = Get-LocalFiles $Running
    $now = [DateTime]::UtcNow
    $sent = 0
    foreach ($rel in @($files.Keys)) {
        $f = $files[$rel]
        if ($conflicts.ContainsKey($rel)) { continue }
        $u = $uploaded[$rel]
        if ($u -and $u.size -eq $f.size -and $u.ticks -eq $f.ticks) { continue }
        if ($Running) {
            $top = $rel.Substring(0, $rel.IndexOf('/'))
            if ($liveFolders -notcontains $top -or $f.name -match $mutableNameRe -or $f.name -match $mutableSuffixRe) { continue }
            $st = $stableSince[$rel]
            if (-not $st -or $st.size -ne $f.size -or $st.ticks -ne $f.ticks) {
                $stableSince[$rel] = @{ size = $f.size; ticks = $f.ticks; at = $now }
                continue
            }
            if (($now - $st.at).TotalSeconds -lt $StableSec) { continue }
        }
        if ($u) {
            # Sent before and changed since.
            if (-not (Remove-ServerCopy $rel)) { $conflicts[$rel] = "Changed on this computer after it was sent; the server copy is sealed"; continue }
        } elseif ($serverOnly.ContainsKey($rel)) {
            $so = $serverOnly[$rel]
            if ($so.size -eq $f.size) {
                $local = $null
                try { $local = Get-FileSha $f.full } catch {}
                if ($local -and $local -eq $so.sha -and (Test-SameStat $f)) {
                    Add-Ledger $rel $f.size $f.ticks $local
                    $serverOnly.Remove($rel)
                    continue
                }
            }
            if (-not (Remove-ServerCopy $rel)) { $conflicts[$rel] = "A different file with this name is already on the server"; continue }
        }
        if ($f.size -le $smallBytes) {
            if (Add-ToBundle $f) { $sent++ }
            continue
        }
        Send-Bundle
        $res = Send-LargeFile $f
        if ($res -eq "ok") { $sent++ }
        elseif ($res -eq "conflict") { $conflicts[$rel] = "A different file with this name is already on the server" }
        elseif ($res -eq "changed" -or $res -eq "resend") { $stableSince.Remove($rel) }
        else { Write-Log ("Not sent this time: " + $rel + " (" + $res + ")") }
    }
    Send-Bundle
    Save-Status @{ files_seen = $script:SeenFiles; bytes_seen = $script:SeenBytes; files_sent = $uploaded.Count }
    return $sent
}

# ---------------------------------------------------------------- remove the local copy (after the server confirmed)
function Remove-Tree([string]$Path) {
    if (-not $Path) { return }
    $di = New-Object IO.DirectoryInfo($Path)
    if (-not $di.Exists) { return }
    if (($di.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        # A junction: remove the link only, never what it points to.
        [IO.Directory]::Delete($Path)
        return
    }
    foreach ($it in $di.GetFileSystemInfos()) {
        if ($it -is [IO.DirectoryInfo]) { Remove-Tree $it.FullName }
        else {
            try { $it.Attributes = [IO.FileAttributes]::Normal } catch {}
            $it.Delete()
        }
    }
    try { $di.Attributes = [IO.FileAttributes]::Directory } catch {}
    [IO.Directory]::Delete($Path)
}

function Get-OwnPayloadTargets {
    # iPhone backups on another drive live in <drive>:\Aetheris_Mobile_Payloads\<id>\<stamp>\<udid>, linked
    # into the case by a junction and described by EXTERNAL_IOS_PAYLOAD.json. Only this run's payload counts.
    $out = New-Object System.Collections.Generic.List[string]
    if ($runName -notmatch '_(\d{8}_\d{6})$') { return ,$out }
    $stamp = $Matches[1]
    $orig = Join-Path (Join-Path $caseDir "02_Original_Extraction") $runName
    if (-not [IO.Directory]::Exists($orig)) { return ,$out }
    $pointers = New-Object System.Collections.Generic.List[object]
    Add-Tree $pointers $orig "x" 0
    foreach ($p in $pointers) {
        if ($p.name -ne "EXTERNAL_IOS_PAYLOAD.json") { continue }
        $j = Read-JsonFile $p.full
        $target = $(if ($j) { [string]$j.payload_path } else { "" })
        if ($target -match '^[A-Za-z]:\\Aetheris_Mobile_Payloads\\' -and $target.Contains("\" + $stamp + "\") -and $target -notmatch '\\\.\.(\\|$)') {
            $out.Add($target)
        }
    }
    return ,$out
}

function Remove-LocalRun($Files, [string]$ServerCaseDir, [string]$HashList, [string]$HashListSha) {
    $targets = Get-OwnPayloadTargets
    $removedFiles = 0
    $failed = New-Object System.Collections.Generic.List[string]
    foreach ($folder in @($scopeFolders + @("09_Review"))) {
        $dir = Join-Path (Join-Path $caseDir $folder) $runName
        try { Remove-Tree $dir } catch { $failed.Add($dir + ": " + $_.Exception.Message) }
    }
    # What is left of this run's iPhone staging folder (normally moved into the case already): removed only
    # when every file in it was sent under its case path.
    foreach ($stage in (Get-StagingRoots)) {
        try {
            $left = New-Object System.Collections.Generic.List[object]
            foreach ($udid in [IO.Directory]::GetDirectories($stage)) {
                Add-Tree $left $udid ("02_Original_Extraction/" + $runName + "/ios_image/" + (Split-Path -Leaf $udid)) 0
            }
            $allSent = $true
            foreach ($f in $left) {
                $u = $uploaded[$f.rel]
                if (-not $u -or $u.size -ne $f.size) { $allSent = $false; break }
            }
            if ($allSent -and @([IO.Directory]::GetFiles($stage)).Count -eq 0) { Remove-Tree $stage }
        } catch { $failed.Add($stage + ": " + $_.Exception.Message) }
    }
    foreach ($t in $targets) {
        try {
            $di = New-Object IO.DirectoryInfo($t)
            if ($di.Exists -and (($di.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)) {
                Remove-Tree $t
                # Empty parents up to (not including) Aetheris_Mobile_Payloads.
                $parent = Split-Path -Parent $t
                while ($parent -and $parent -match '^[A-Za-z]:\\Aetheris_Mobile_Payloads\\.+' ) {
                    if (@([IO.Directory]::GetFileSystemEntries($parent)).Count -gt 0) { break }
                    [IO.Directory]::Delete($parent)
                    $parent = Split-Path -Parent $parent
                }
            }
        } catch { $failed.Add($t + ": " + $_.Exception.Message) }
    }
    foreach ($f in $Files.Values) { if (-not (Test-Path -LiteralPath $f.full)) { $removedFiles++ } }
    $receipt = [ordered]@{
        schema = "polaron.client_mobile_stream_receipt/1"
        run_name = $runName
        moved_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        server = $script:Server
        session_id = $script:SessionId
        server_case_dir = $ServerCaseDir
        files = $Files.Count
        bytes = $script:SeenBytes
        hash_list_on_server = $HashList
        hash_list_sha256 = $HashListSha
        note = "This collection was sent to the Polaron forensic server, verified there file by file (SHA-256), and removed from this computer."
        not_removed = @($failed)
    }
    try {
        [IO.File]::WriteAllText((Join-Path $caseDir ("MOVED_TO_SERVER_" + $runName + ".json")), ($receipt | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    } catch {}
    return @{ removed = $removedFiles; failed = @($failed) }
}

# ---------------------------------------------------------------- main
Write-Log ("Upload of collection job " + $job + " to " + $script:Server + " (session " + $script:SessionId + ", keep local copy: " + $keepLocal + ")")
if (-not $script:Server -or -not $script:SessionId -or -not $script:Token) {
    Complete-Stream "kept_local" "The upload session is incomplete; the collection stays on this computer." "stream file incomplete"
}
Save-Status @{ state = "waiting" }
Sync-ServerState

$deadSince = $null
$jobState = $null
while ($true) {
    $jobState = Get-JobState
    if (-not $runName) {
        $runName = Find-RunName
        if ($runName) { Save-RunState; Save-Status @{ run_name = $runName }; Write-Log ("Run " + $runName + " in " + $caseDir) }
    }
    if ($jobState.done) {
        if ($jobState.result) { break }
        if (-not $deadSince) { $deadSince = Get-Date }
        if (((Get-Date) - $deadSince).TotalSeconds -ge $GraceSec) { break }
    }
    if ($runName -and -not $jobState.done) {
        $t0 = Get-Date
        try { [void](Send-Ready $true) } catch { Write-Log ("Send pass failed: " + $_.Exception.Message) }
        $took = ((Get-Date) - $t0).TotalSeconds
        Start-Sleep -Seconds ([int][Math]::Max($PollSec, [Math]::Min(60, $took * 2)))
    } else {
        Start-Sleep -Seconds $PollSec
    }
}

$result = $jobState.result
if (-not $runName -and $result -and $result.run_name) { $runName = [string]$result.run_name; Save-RunState }
$resultOk = $false
$resultError = ""
if ($result) {
    $resultOk = [bool]$result.ok
    $resultError = [string]$result.error
}
if (-not $result -or -not $resultOk -or -not $runName) {
    $reason = $(if (-not $result) { "The collection stopped without a result." } elseif ($resultError) { "The collection did not finish: " + $resultError } else { "The collection did not finish." })
    $body = [Text.Encoding]::UTF8.GetBytes('{"reason":' + (ConvertTo-JsonText $reason) + '}')
    for ($i = 0; $i -lt 5; $i++) {
        $r = Invoke-Stream -Method POST -Path "/abort" -Body $body
        if ($r.status -eq 200 -or $r.status -eq 401 -or $r.status -eq 404 -or $r.status -eq 409) { break }
        Start-Sleep -Seconds 5
    }
    $state = $(if ($resultError -match '(?i)cancel') { "cancelled" } else { "kept_local" })
    Complete-Stream $state "The collection did not finish, so it was not registered on the server. Its files stay on this computer." $reason
}

Save-Status @{ state = "finishing"; detail = "Sending the last files of the collection"; run_name = $runName }
Write-Log "Collection finished; sending the rest."
$files = $null
for ($pass = 0; $pass -lt 4; $pass++) {
    $sent = 0
    try { $sent = Send-Ready $false } catch { Write-Log ("Final pass failed: " + $_.Exception.Message); Start-Sleep -Seconds 10; continue }
    $files = Get-LocalFiles $false
    $pending = 0
    foreach ($rel in $files.Keys) {
        if ($conflicts.ContainsKey($rel)) { continue }
        $u = $uploaded[$rel]
        if (-not $u -or $u.size -ne $files[$rel].size -or $u.ticks -ne $files[$rel].ticks) { $pending++ }
    }
    if ($pending -eq 0) { break }
    Write-Log ("" + $pending + " file(s) still to send.")
}
$files = Get-LocalFiles $false
$notSent = New-Object System.Collections.Generic.List[string]
foreach ($rel in $files.Keys) {
    $u = $uploaded[$rel]
    if (-not $u -or $u.size -ne $files[$rel].size -or $u.ticks -ne $files[$rel].ticks) { $notSent.Add($rel) }
}

# Register the run on the server.
$warnings = New-Object System.Collections.Generic.List[string]
foreach ($rel in $conflicts.Keys) { $warnings.Add($rel + ": " + $conflicts[$rel]) }
if ($notSent.Count -gt 0) {
    $warnings.Add(("" + $notSent.Count + " file(s) could not be sent to the server; they stay on the examiner computer in " + $caseDir))
}
$summaryRaw = "null"
try {
    $raw = [IO.File]::ReadAllText([string]$cfg.result_file)
    if ($raw.Length -gt 0 -and $raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    if ($raw.Trim().StartsWith("{")) { $summaryRaw = $raw.Trim() }
} catch {}
$sb = New-Object Text.StringBuilder
[void]$sb.Append('{"run_name":' + (ConvertTo-JsonText $runName) + ',"client_host":' + (ConvertTo-JsonText ([Environment]::MachineName)) +
    ',"client_case_dir":' + (ConvertTo-JsonText $caseDir) + ',"warnings":[')
$first = $true
foreach ($w in $warnings | Select-Object -First 200) {
    if (-not $first) { [void]$sb.Append(',') }
    $first = $false
    [void]$sb.Append((ConvertTo-JsonText $w))
}
[void]$sb.Append('],"files":[')
$first = $true
foreach ($rel in $files.Keys) {
    $u = $uploaded[$rel]
    if (-not $u -or $u.size -ne $files[$rel].size) { continue }
    if (-not $first) { [void]$sb.Append(',') }
    $first = $false
    [void]$sb.Append('{"rel":' + (ConvertTo-JsonText $rel) + ',"size":' + $u.size + ',"sha256":"' + $u.sha + '"}')
}
[void]$sb.Append('],"summary":' + $summaryRaw + '}')
$finishBody = [Text.Encoding]::UTF8.GetBytes($sb.ToString())
Save-Status @{ state = "finishing"; detail = "The server is registering the collection" }
$fin = $null
while ($true) {
    $fin = Invoke-Stream -Method POST -Path "/finish" -Body $finishBody -TimeoutSec 1800
    if ($fin.status -eq 200 -and $fin.json -and $fin.json.ok) { break }
    Test-FatalAnswer $fin
    $code = Get-ErrorCode $fin
    if ($fin.status -eq 409 -and $fin.json -and $fin.json.missing) {
        # The server misses files this computer thinks it sent: forget them and send them again.
        foreach ($m in @($fin.json.missing)) { $uploaded.Remove([string]$m) }
        Sync-ServerState
        [void](Send-Ready $false)
        continue
    }
    if ($fin.status -ge 400 -and $fin.status -lt 500 -and $fin.status -ne 408 -and $fin.status -ne 429) {
        Complete-Stream "kept_local" "The server could not register the collection. Its files stay on this computer." ($code + ": " + [string]$fin.json.detail)
    }
    Wait-Retry $code
}
$serverCaseDir = [string]$fin.json.server_case_dir
Save-Status @{ server_case_dir = $serverCaseDir }
Write-Log ("Registered on the server in " + $serverCaseDir + " (" + $uploaded.Count + " files).")

if ($keepLocal) {
    Complete-Stream "completed" ("Stored on the forensic server in " + $serverCaseDir + ". A copy stays on this computer as requested.")
}
if ($notSent.Count -gt 0 -or $conflicts.Count -gt 0) {
    Save-Status @{ warnings = @($warnings | Select-Object -First 20) }
    Complete-Stream "completed" ("Stored on the forensic server in " + $serverCaseDir + ". " + ($notSent.Count + $conflicts.Count) + " file(s) could not be sent, so the copy on this computer was kept.")
}
# Every file that is deleted here was hashed when it was sent and has not changed since (size and time).
foreach ($rel in $files.Keys) {
    if (-not (Test-SameStat $files[$rel])) {
        Complete-Stream "completed" ("Stored on the forensic server in " + $serverCaseDir + ". A file changed on this computer after it was sent, so the local copy was kept.")
    }
}
$gone = $null
try { $gone = Remove-LocalRun $files $serverCaseDir ([string]$fin.json.hash_list) ([string]$fin.json.hash_list_sha256) } catch { $gone = @{ removed = 0; failed = @($_.Exception.Message) } }
if (@($gone.failed).Count -gt 0) {
    Save-Status @{ deleted_local = $false; warnings = @($gone.failed | Select-Object -First 20) }
    Complete-Stream "completed" ("Stored on the forensic server in " + $serverCaseDir + ". Some local files could not be removed (see the receipt in " + $caseDir + ").")
}
Save-Status @{ deleted_local = $true }
Complete-Stream "completed" ("Stored on the forensic server in " + $serverCaseDir + " and removed from this computer.")
