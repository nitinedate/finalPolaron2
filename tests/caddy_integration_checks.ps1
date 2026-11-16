# V1.30/V1.31.1 offline checks: Polaron joins an existing Caddy for HTTPS, and starts it when it is stopped (Caddy and Docker mocked).
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = '1'
. (Join-Path $ProjectRoot 'script_docker/start_docker.ps1') -Profile prod
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = ''
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }
function Assert-Throws([scriptblock]$Body, [string]$Message, [string]$Pattern = '') {
    $threw = $false; $text = ''
    try { & $Body | Out-Null } catch { $threw = $true; $text = $_.Exception.Message }
    Assert-True ($threw -and (-not $Pattern -or $text -match $Pattern)) $Message
}
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('aetheris-caddy-check-' + [guid]::NewGuid().ToString('N'))
$proj = Join-Path $tmp 'project'; $caddyDir = Join-Path $tmp 'caddy'
New-Item -ItemType Directory -Path $proj, $caddyDir -Force | Out-Null
$root = $proj
$envFile = Join-Path $proj '.env'
$caddyfile = Join-Path $caddyDir 'Caddyfile'
$fakeExe = Join-Path $caddyDir 'caddy.exe'
[IO.File]::WriteAllText($fakeExe, 'fake')
try {
    # --- mode -----------------------------------------------------------------------------------
    [IO.File]::WriteAllText($envFile, "APP_BASE_URL=http://localhost:3001`n")
    Assert-True ((Get-PublicHttpsProxySetting -Root $proj) -eq 'auto') 'PUBLIC_HTTPS_PROXY defaults to auto'
    [IO.File]::WriteAllText($envFile, "PUBLIC_HTTPS_PROXY=Caddy`n")
    Assert-True ((Get-PublicHttpsProxySetting -Root $proj) -eq 'caddy') 'PUBLIC_HTTPS_PROXY=caddy is read (case-insensitive)'
    [IO.File]::WriteAllText($envFile, "PUBLIC_HTTPS_PROXY=nginx`n")
    Assert-Throws { Get-PublicHttpsProxySetting -Root $proj } 'Unknown PUBLIC_HTTPS_PROXY values are rejected'
    $script:listener = [pscustomobject]@{ Pid = 61940; Exe = $fakeExe; CommandLine = "`"$fakeExe`" run --config `"$caddyfile`" --adapter caddyfile" }
    function Get-CaddyListenerProcess { return $script:listener }
    [IO.File]::WriteAllText($envFile, "PUBLIC_HTTPS_PROXY=auto`n")
    Assert-True ((Resolve-PublicHttpsProxyMode -Root $proj) -eq 'caddy') 'auto: Caddy on TCP 80/443 -> Polaron joins Caddy'
    [IO.File]::WriteAllText($envFile, "PUBLIC_HTTPS_PROXY=builtin`n")
    Assert-True ((Resolve-PublicHttpsProxyMode -Root $proj) -eq 'builtin') 'builtin is kept even when Caddy runs'
    $script:listener = $null
    [IO.File]::WriteAllText($envFile, "`n")
    Assert-True ((Resolve-PublicHttpsProxyMode -Root $proj) -eq 'builtin') 'auto without Caddy -> builtin gateway'

    # --- command line, version ---------------------------------------------------------------------
    $parsed = Get-CaddyConfigFromCommandLine -CommandLine '"C:\caddy\caddy.exe" run --config "C:\Caddy Server\Caddyfile" --adapter caddyfile'
    Assert-True ($parsed.Config -eq 'C:\Caddy Server\Caddyfile' -and $parsed.Adapter -eq 'caddyfile') 'Quoted --config path with spaces is read'
    Assert-True ((Get-CaddyConfigFromCommandLine -CommandLine 'caddy.exe run --config=C:\caddy\Caddyfile').Config -eq 'C:\caddy\Caddyfile') '--config=path form is read'
    Assert-True ((Get-CaddyConfigFromCommandLine -CommandLine 'caddy run -c D:\web\Caddyfile').Config -eq 'D:\web\Caddyfile') '-c path form is read'
    Assert-True ((Get-CaddyConfigFromCommandLine -CommandLine 'caddy.exe run').Config -eq '') 'No --config -> unknown'
    Assert-True ((ConvertTo-CaddyVersion 'v2.11.4 h1:abc=') -eq [version]'2.11.4' -and (ConvertTo-CaddyVersion '2.6.2') -eq [version]'2.6.2' -and $null -eq (ConvertTo-CaddyVersion 'unknown')) 'Caddy version text is parsed'

    # --- site file ---------------------------------------------------------------------------------
    $ipOnly = Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain '' -Email 'ops@example.com'
    Assert-True ($ipOnly -match '(?m)^https://8\.8\.4\.4 \{$' -and $ipOnly -notmatch '(?m)^https://[a-z]') 'IP-only site, no domain block'
    Assert-True ($ipOnly -match 'issuer acme https://acme-v02\.api\.letsencrypt\.org/directory \{' -and $ipOnly -match '(?m)^\t\t\tprofile shortlived$' -and $ipOnly -match '(?m)^\t\t\tdisable_tlsalpn_challenge$') 'IP certificate: Let''s Encrypt production, short-lived profile, HTTP-01'
    Assert-True ($ipOnly -match '(?m)^\t\t\temail ops@example\.com$') 'ACME email is set for the IP certificate'
    Assert-True ($ipOnly -match '(?m)^\treverse_proxy 127\.0\.0\.1:3001 \{$' -and $ipOnly -match '(?m)^\t\tflush_interval -1$') 'All requests go to the loopback Polaron gateway, streamed'
    Assert-True (([regex]::Matches($ipOnly, '\{')).Count -eq ([regex]::Matches($ipOnly, '\}')).Count) 'Braces are balanced'
    $both = Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain 'polaron.example.com' -Email 'ops@example.com'
    Assert-True ($both -match '(?m)^https://polaron\.example\.com \{$' -and $both -match '(?m)^\ttls ops@example\.com$') 'Domain site with Caddy''s default certificate and email'
    Assert-True ((([regex]::Matches($both, 'reverse_proxy 127\.0\.0\.1:3001')).Count) -eq 2) 'IP and domain reach the same gateway'
    $noMail = Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain 'polaron.example.com' -Email ''
    Assert-True ($noMail -notmatch '(?m)^\s+email ' -and $noMail -notmatch '(?m)^\ttls [^{]') 'No email -> no email lines'
    Assert-Throws { Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain 'a.com { respond x }' -Email '' } 'Domain cannot inject Caddyfile syntax'
    Assert-Throws { Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain '' -Email 'a@b.c }' } 'Email cannot inject Caddyfile syntax'
    Assert-Throws { Get-CaddySiteConfig -PublicIP '8.8.4.4' -Domain '' -Email '' -Upstream '127.0.0.1:3001 {' } 'Upstream cannot inject Caddyfile syntax'
    Assert-Throws { Get-CaddySiteConfig -PublicIP 'evil.example' -Domain '' -Email '' } 'Site address must be an IPv4'

    # --- import line -------------------------------------------------------------------------------
    $orig = "{`r`n`temail admin@example.com`r`n}`r`nexample.com {`r`n`trespond `"hi`"`r`n}"
    $added = Add-CaddyImportLine -Text $orig
    Assert-True ($added.StartsWith($orig) -and $added -match "`r`nimport polaron\*\.caddy`r`n$") 'Import appended at the end, CRLF kept, existing text untouched'
    Assert-True ((Add-CaddyImportLine -Text $added) -eq $added) 'Import is added only once'
    Assert-True (Test-CaddyImportPresent -Text "x {`n}`nimport `"polaron.caddy`"`n") 'An existing quoted import is recognized'
    Assert-True (-not (Test-CaddyImportPresent -Text "# import polaron*.caddy`n")) 'A commented import does not count'

    # --- Resolve-CaddySetup ------------------------------------------------------------------------
    [IO.File]::WriteAllText($caddyfile, $orig)
    $script:versionText = 'v2.11.4 h1:x'
    $script:calls = New-Object System.Collections.Generic.List[string]
    $script:validateCode = 0; $script:reloadCode = 0
    function Invoke-CaddyCapture {
        param([string]$Exe, [string[]]$Arguments)
        $script:calls.Add($Arguments -join ' ')
        switch ($Arguments[0]) {
            'version' { return [pscustomobject]@{ Code = 0; Output = $script:versionText } }
            'validate' { return [pscustomobject]@{ Code = $script:validateCode; Output = 'Error: ambiguous site definition: https://8.8.4.4' } }
            'reload' { return [pscustomobject]@{ Code = $script:reloadCode; Output = 'Error: sending configuration to instance: connection refused' } }
        }
        return [pscustomobject]@{ Code = 0; Output = '' }
    }
    $script:listener = [pscustomobject]@{ Pid = 61940; Exe = $fakeExe; CommandLine = "`"$fakeExe`" run --config `"$caddyfile`"" }
    [IO.File]::WriteAllText($envFile, "PUBLIC_HTTPS_PROXY=caddy`n")
    $setup = Resolve-CaddySetup -Root $proj
    Assert-True ($setup.Exe -eq $fakeExe -and $setup.Config -eq (Resolve-Path $caddyfile).ProviderPath -and $setup.Upstream -eq '127.0.0.1:3001' -and $setup.Manage) 'Caddy and its Caddyfile are found from the running process'
    $script:listener = [pscustomobject]@{ Pid = 61940; Exe = ''; CommandLine = '' }
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$caddyfile`n")
    Assert-True ((Resolve-CaddySetup -Root $proj).Config -eq (Resolve-Path $caddyfile).ProviderPath) 'CADDY_EXE / CADDY_CONFIG in .env are used when the process cannot be read'
    $script:listener = [pscustomobject]@{ Pid = 61940; Exe = $fakeExe; CommandLine = 'caddy.exe run --config Caddyfile' }
    [IO.File]::WriteAllText($envFile, "`n")
    Assert-Throws { Resolve-CaddySetup -Root $proj } 'A relative --config is never guessed' 'CADDY_CONFIG'
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$(Join-Path $caddyDir 'missing')`n")
    Assert-Throws { Resolve-CaddySetup -Root $proj } 'A missing Caddyfile stops before any change' 'not found'
    $json = Join-Path $caddyDir 'caddy.json'; [IO.File]::WriteAllText($json, '{}')
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$json`n")
    Assert-Throws { Resolve-CaddySetup -Root $proj } 'A JSON Caddy config is not edited' 'CADDY_MANAGE_CONFIG=false'
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$caddyfile`n")
    $script:versionText = 'v2.6.2'
    Assert-Throws { Resolve-CaddySetup -Root $proj } 'Caddy older than 2.10 is refused before any change' 'caddy upgrade'
    $script:versionText = 'v2.11.4 h1:x'
    [IO.File]::WriteAllText($envFile, "CADDY_MANAGE_CONFIG=false`n")
    Assert-True (-not (Resolve-CaddySetup -Root $proj).Manage) 'CADDY_MANAGE_CONFIG=false is honoured'

    # --- Publish-CaddySite -------------------------------------------------------------------------
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$caddyfile`n")
    $setup = Resolve-CaddySetup -Root $proj
    $bomBytes = [byte[]](@(0xEF, 0xBB, 0xBF) + [Text.Encoding]::UTF8.GetBytes($orig))
    [IO.File]::WriteAllBytes($caddyfile, $bomBytes)
    $sitePath = Join-Path $caddyDir 'polaron.caddy'
    $script:calls.Clear(); $script:validateCode = 0; $script:reloadCode = 0
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS 'polaron.example.com' -Email 'ops@example.com'
    $after = [IO.File]::ReadAllBytes($caddyfile)
    Assert-True ($after[0] -eq 0xEF -and $after[1] -eq 0xBB -and $after[2] -eq 0xBF) 'Caddyfile byte-order mark is preserved'
    Assert-True ((Test-Path $sitePath) -and ([IO.File]::ReadAllText($sitePath) -match 'https://polaron\.example\.com')) 'polaron.caddy is written next to the Caddyfile'
    Assert-True (@(Get-ChildItem $caddyDir -Filter 'Caddyfile.polaron-backup-*').Count -eq 1) 'The original Caddyfile is backed up'
    Assert-True (($script:calls -join ';') -match "validate --config .*Caddyfile --adapter caddyfile;reload --config .*Caddyfile --adapter caddyfile") 'Caddy validates, then reloads'
    $script:calls.Clear()
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS 'polaron.example.com' -Email 'ops@example.com'
    Assert-True (([regex]::Matches([IO.File]::ReadAllText($caddyfile), 'import polaron\*\.caddy')).Count -eq 1 -and @(Get-ChildItem $caddyDir -Filter 'Caddyfile.polaron-backup-*').Count -eq 1) 'Next launch: no second import, no new backup'

    $beforeCfg = [IO.File]::ReadAllBytes($caddyfile); $beforeSite = [IO.File]::ReadAllBytes($sitePath)
    $script:validateCode = 1
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '1.1.1.1' -DNS '' -Email '' } 'A rejected configuration stops startup' 'left unchanged'
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($caddyfile)) -eq [Convert]::ToBase64String($beforeCfg) -and [Convert]::ToBase64String([IO.File]::ReadAllBytes($sitePath)) -eq [Convert]::ToBase64String($beforeSite)) 'Rejected: both files restored byte for byte'
    $script:validateCode = 0; $script:reloadCode = 1
    [IO.File]::WriteAllBytes($caddyfile, $bomBytes); Remove-Item $sitePath
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'A failed reload stops startup' 'admin API'
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($caddyfile)) -eq [Convert]::ToBase64String($bomBytes) -and -not (Test-Path $sitePath)) 'Failed reload: Caddyfile restored and polaron.caddy removed'
    $script:reloadCode = 0

    [IO.File]::WriteAllText($envFile, "CADDY_MANAGE_CONFIG=false`n")
    $manual = Resolve-CaddySetup -Root $proj
    $script:calls.Clear()
    Publish-CaddySite -Caddy $manual -IP '8.8.4.4' -DNS '' -Email ''
    Assert-True ((Test-Path (Join-Path $proj 'deployment/caddy/polaron.caddy')) -and $script:calls.Count -eq 0) 'CADDY_MANAGE_CONFIG=false: site file only, Caddy untouched'

    # --- V1.31.1: Caddy is not running ------------------------------------------------------------
    Assert-True ((Get-CaddyAdminAddress -Text "example.com {`n`trespond hi`n}") -eq 'localhost:2019') 'No global options -> default admin address'
    Assert-True ((Get-CaddyAdminAddress -Text "# main config`r`n{`r`n`temail a@b.c`r`n`tadmin 127.0.0.1:2020`r`n}`r`nexample.com {`r`n}") -eq '127.0.0.1:2020') 'admin address read from the global options block'
    Assert-True ((Get-CaddyAdminAddress -Text "{`n`tadmin off # no API`n}`n") -eq 'off') 'admin off is recognized'
    Assert-True ((Get-CaddyAdminAddress -Text "{`n`tadmin {`n`t`torigins x`n`t}`n}`n") -eq 'localhost:2019') 'admin block without address -> default'
    Assert-True ((Get-CaddyAdminAddress -Text "{`n`temail a@b.c`n}`nexample.com {`n`tadmin off`n}") -eq 'localhost:2019') 'admin inside a site block is not the global option'
    Assert-True ((ConvertTo-CaddyAdminUrl 'localhost:2019') -eq 'http://127.0.0.1:2019/config/' -and (ConvertTo-CaddyAdminUrl ':2020') -eq 'http://127.0.0.1:2020/config/' -and (ConvertTo-CaddyAdminUrl 'tcp/10.0.0.5:2019') -eq 'http://10.0.0.5:2019/config/') 'Admin address -> URL'
    Assert-True ((ConvertTo-CaddyAdminUrl 'off') -eq '' -and (ConvertTo-CaddyAdminUrl 'unix//run/caddy-admin.sock') -eq '') 'admin off / unix socket -> no TCP check'
    [IO.File]::WriteAllText($envFile, "`n")
    Assert-True ((Get-CaddyAutostartSetting -Root $proj) -eq 'auto') 'CADDY_AUTOSTART defaults to auto'
    [IO.File]::WriteAllText($envFile, "CADDY_AUTOSTART=Service`n")
    Assert-True ((Get-CaddyAutostartSetting -Root $proj) -eq 'service') 'CADDY_AUTOSTART=service is read'
    [IO.File]::WriteAllText($envFile, "CADDY_AUTOSTART=false`n")
    Assert-True ((Get-CaddyAutostartSetting -Root $proj) -eq 'off') 'CADDY_AUTOSTART=false means off'
    [IO.File]::WriteAllText($envFile, "CADDY_AUTOSTART=maybe`n")
    Assert-Throws { Get-CaddyAutostartSetting -Root $proj } 'Unknown CADDY_AUTOSTART values are rejected'

    $script:events = New-Object System.Collections.Generic.List[string]
    $script:warnings = New-Object System.Collections.Generic.List[string]
    $script:adminUp = $false; $script:holders = @(); $script:service = $null; $script:task = $null; $script:startWorks = $true
    function Write-Warn([string]$Message) { $script:warnings.Add($Message) }
    function Invoke-CaddyCapture {
        param([string]$Exe, [string[]]$Arguments)
        $script:calls.Add($Arguments -join ' '); $script:events.Add($Arguments[0])
        switch ($Arguments[0]) {
            'version' { return [pscustomobject]@{ Code = 0; Output = $script:versionText } }
            'validate' { return [pscustomobject]@{ Code = $script:validateCode; Output = 'Error: ambiguous site definition' } }
            'reload' { return [pscustomobject]@{ Code = $script:reloadCode; Output = 'Error: sending configuration to instance: performing request: Post "http://localhost:2019/load": dial tcp [::1]:2019: connectex: No connection could be made because the target machine actively refused it.' } }
        }
        return [pscustomobject]@{ Code = 0; Output = '' }
    }
    function Test-CaddyAdminApi { param([string]$Address) $script:events.Add("admin-probe:$Address"); return $script:adminUp }
    function Get-HttpsPortHolders { return @($script:holders) }
    function Find-CaddyService { param([string]$Exe) return $script:service }
    function Find-CaddyScheduledTask { param([string]$Exe) return $script:task }
    function Register-CaddyService { param($Caddy) $script:events.Add('register-service'); return [pscustomobject]@{ Name = 'caddy'; StartMode = 'Auto'; State = 'Stopped'; PathName = '' } }
    function Start-CaddyService { param([string]$Name) $script:events.Add("service-start:$Name"); if ($script:startWorks) { $script:listener = [pscustomobject]@{ Pid = 7001; Exe = $fakeExe; CommandLine = '' } } }
    function Start-ScheduledTask { param([string]$TaskName, [string]$TaskPath) $script:events.Add("task-start:$TaskName"); if ($script:startWorks) { $script:listener = [pscustomobject]@{ Pid = 7002; Exe = $fakeExe; CommandLine = '' } } }
    function Start-CaddyProcessInBackground { param($Caddy) $script:events.Add('process-start'); if ($script:startWorks) { $script:listener = [pscustomobject]@{ Pid = 4242; Exe = $fakeExe; CommandLine = '' } }; return [pscustomobject]@{ Process = [pscustomobject]@{ Id = 4242; HasExited = $false; ExitCode = 0 }; Log = '' } }
    function Invoke-Docker { param([string]$Title, [string[]]$DockerArgs) $script:events.Add('docker ' + ($DockerArgs -join ' ')) }
    function Stop-Process { param([int]$Id, [switch]$Force) $script:events.Add("process-stop:$Id") }
    function Stop-Service { param([string]$Name, [switch]$Force) $script:events.Add("service-stop:$Name") }
    function Stop-ScheduledTask { param([string]$TaskName) $script:events.Add("task-stop:$TaskName") }
    function sc.exe { $script:events.Add('sc ' + ($args -join ' ')) }
    function Reset-CaddyCase([string]$EnvText) {
        [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`nCADDY_CONFIG=$caddyfile`n$EnvText")
        [IO.File]::WriteAllBytes($caddyfile, $bomBytes); Remove-Item $sitePath -ErrorAction SilentlyContinue
        $script:listener = $null; $script:adminUp = $false; $script:holders = @(); $script:service = $null; $script:task = $null
        $script:startWorks = $true; $script:validateCode = 0; $script:reloadCode = 0
        $script:events.Clear(); $script:warnings.Clear(); $script:calls.Clear()
        $s = Resolve-CaddySetup -Root $proj
        $s.StartSeconds = 1
        $script:events.Clear(); $script:calls.Clear()
        return $s
    }
    function Get-CaddyCaseLog { return @($script:events | Where-Object { $_ -notmatch '^admin-probe' }) }
    function Test-CaddyFilesRestored { return ([Convert]::ToBase64String([IO.File]::ReadAllBytes($caddyfile)) -eq [Convert]::ToBase64String($bomBytes) -and -not (Test-Path $sitePath)) }

    $setup = Reset-CaddyCase ''
    Assert-True (-not $setup.Running -and $setup.Admin -eq 'localhost:2019' -and $setup.Autostart -eq 'auto') 'Stopped Caddy is detected (no listener, admin API silent)'
    Assert-True (@($script:warnings | Where-Object { $_ -match 'Caddy is not running on this PC' }).Count -eq 1) 'The launcher says Caddy is not running'
    $script:listener = $null; $script:adminUp = $true
    Assert-True ((Resolve-CaddySetup -Root $proj).Running) 'A Caddy whose admin API answers counts as running (sites on other ports)'
    $script:adminUp = $false
    [IO.File]::WriteAllText($caddyfile, "{`r`n`tadmin off`r`n}`r`nexample.com {`r`n}`r`n")
    $offBytes = [IO.File]::ReadAllBytes($caddyfile)
    Assert-Throws { Resolve-CaddySetup -Root $proj } "'admin off' is reported before anything changes" "admin off"
    Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($caddyfile)) -eq [Convert]::ToBase64String($offBytes)) "'admin off': Caddyfile untouched"
    [IO.File]::WriteAllText($caddyfile, "{`n`tadmin 127.0.0.1:2020`n}`n")
    $script:events.Clear()
    Assert-True ((Resolve-CaddySetup -Root $proj).Admin -eq '127.0.0.1:2020' -and $script:events -contains 'admin-probe:127.0.0.1:2020') 'A custom admin address is probed'
    [IO.File]::WriteAllText($envFile, "CADDY_EXE=$fakeExe`n")
    Assert-Throws { Resolve-CaddySetup -Root $proj } 'Stopped Caddy without CADDY_CONFIG: asks for CADDY_CONFIG' 'Caddy is not running.*CADDY_CONFIG'

    $setup = Reset-CaddyCase ''
    $script:service = [pscustomobject]@{ Name = 'caddy'; StartMode = 'Auto'; State = 'Stopped'; PathName = "$fakeExe run" }
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS 'polaron.example.com' -Email ''
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,service-start:caddy,reload') 'Stopped Caddy service: validate, start the service, then reload'
    Assert-True ($setup.Running -and (Test-Path $sitePath) -and @($script:warnings | Where-Object { $_ -match 'Set-Service|stops when Windows restarts' }).Count -eq 0) 'Service start: Polaron sites published, no reboot warning for an automatic service'

    $setup = Reset-CaddyCase ''
    $script:service = [pscustomobject]@{ Name = 'Caddy Web'; StartMode = 'Manual'; State = 'Stopped'; PathName = '' }
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email ''
    Assert-True ($script:events -contains 'service-start:Caddy Web' -and @($script:warnings | Where-Object { $_ -match "Set-Service -Name 'Caddy Web' -StartupType Automatic" }).Count -eq 1) 'Manual service: started, and the fix for reboots is shown'

    $setup = Reset-CaddyCase ''
    $script:service = [pscustomobject]@{ Name = 'caddy'; StartMode = 'Disabled'; State = 'Stopped'; PathName = '' }
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'Disabled service: stops with the fix' 'disabled'
    Assert-True ((Test-CaddyFilesRestored) -and -not ($script:events -match 'start')) 'Disabled service: nothing started, files restored'

    $setup = Reset-CaddyCase ''
    $script:task = [pscustomobject]@{ TaskName = 'Caddy at boot'; TaskPath = '\' }
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email ''
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,task-start:Caddy at boot,reload') 'Caddy scheduled task: started, then reload'

    $setup = Reset-CaddyCase ''
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email ''
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,process-start,reload') 'No service or task: Caddy started in the background, then reload'
    Assert-True (@($script:warnings | Where-Object { $_ -match 'CADDY_AUTOSTART=service' -and $_ -match 'sc\.exe create caddy start= auto binPath= ' -and $_ -match '--adapter caddyfile' }).Count -eq 1) 'Background Caddy: how to make it start with Windows is shown'

    $setup = Reset-CaddyCase "CADDY_AUTOSTART=service`n"
    Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email ''
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,register-service,service-start:caddy,reload') 'CADDY_AUTOSTART=service: Caddy registered as a service and started'

    $setup = Reset-CaddyCase "CADDY_AUTOSTART=off`n"
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'CADDY_AUTOSTART=off: stops with instructions' 'CADDY_AUTOSTART=off'
    Assert-True ((Test-CaddyFilesRestored) -and ((Get-CaddyCaseLog) -join ',') -eq 'validate') 'CADDY_AUTOSTART=off: nothing started, files restored'

    $setup = Reset-CaddyCase ''
    $script:holders = @([pscustomobject]@{ Port = 80; Kind = 'process'; Name = 'Windows HTTP Server (http.sys: IIS) (PID 4)'; PolaronGateway = $false })
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'Another program on TCP 80: named, with the way out' 'TCP 80: Windows HTTP Server.*PUBLIC_HTTPS_PROXY=builtin'
    Assert-True ((Test-CaddyFilesRestored) -and ((Get-CaddyCaseLog) -join ',') -eq 'validate') 'Port held by another program: nothing stopped or started, files restored'

    $setup = Reset-CaddyCase ''
    $script:holders = @(
        [pscustomobject]@{ Port = 80; Kind = 'container'; Name = 'aetheris-gateway-gateway-1'; PolaronGateway = $true },
        [pscustomobject]@{ Port = 443; Kind = 'container'; Name = 'aetheris-gateway-gateway-1'; PolaronGateway = $true })
    $script:reloadCode = 1
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'Polaron gateway on 80/443, reload fails: startup stops' 'left unchanged'
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,docker stop aetheris-gateway-gateway-1,process-start,reload,process-stop:4242,docker start aetheris-gateway-gateway-1') 'Polaron gateway moved aside for Caddy; on failure Caddy is stopped and the gateway started again'
    Assert-True ((Test-CaddyFilesRestored) -and -not $setup.Running) 'Failure after starting Caddy: files restored'
    $setup = Reset-CaddyCase ''
    $script:listener = [pscustomobject]@{ Pid = 61940; Exe = $fakeExe; CommandLine = '' }; $setup.Running = $true; $script:reloadCode = 1
    $reloadError = ''
    try { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } catch { $reloadError = $_.Exception.Message }
    Assert-True ($reloadError -match "admin API at localhost:2019 did not answer, so Caddy is not running" -and $reloadError -notmatch "must be enabled") 'Refused reload (the reported error) names the real cause: Caddy is not running'

    $setup = Reset-CaddyCase "CADDY_AUTOSTART=service`n"
    $script:startWorks = $false
    Assert-Throws { Publish-CaddySite -Caddy $setup -IP '8.8.4.4' -DNS '' -Email '' } 'Caddy that never comes up: clear timeout' 'did not start within 1 s.*CADDY_START_SECONDS'
    Assert-True ((Get-CaddyCaseLog) -join ',' -eq 'validate,register-service,service-start:caddy,service-stop:caddy,sc delete caddy') 'Timeout: the service is stopped and the registration removed'
    Assert-True (Test-CaddyFilesRestored) 'Timeout: files restored'

    $caddyLog = Join-Path $tmp 'caddy-background.log'
    [IO.File]::WriteAllText($caddyLog, "{`"level`":`"info`",`"msg`":`"using config from file`"}`n{`"level`":`"error`",`"msg`":`"loading new config: http app module: start: listening on :443: listen tcp :443: bind: Only one usage of each socket address is normally permitted.`"}`n")
    $script:listener = $null; $script:adminUp = $false
    $exited = [pscustomobject]@{ Id = 4243; HasExited = $true; ExitCode = 1 }
    Assert-Throws { Wait-CaddyReady -Caddy $setup -Process $exited -LogPath $caddyLog } 'A Caddy that exits at once is reported with its own log' '(?s)exit code 1.*listen tcp :443'

    $launcherText = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'script_docker/start_docker.ps1')) -replace "`r", ''
    $publish = [regex]::Match($launcherText, '(?s)function Publish-CaddySite \{.*?\n\}\n').Value
    Assert-True ($publish.IndexOf("'validate'") -lt $publish.IndexOf('Start-StoppedCaddy') -and $publish.IndexOf('Start-StoppedCaddy') -lt $publish.IndexOf("@('reload'")) 'Launcher: Caddy is started only after the new Caddyfile validates, and before the reload'
    $script:listener = $null

    # --- gateway behind Caddy (Docker mocked) ------------------------------------------------------
    $script:docker = New-Object System.Collections.Generic.List[string]
    function Invoke-Docker { param([string]$Title, [string[]]$DockerArgs) $script:docker.Add($DockerArgs -join ' ') }
    function Invoke-StackUp { param([string]$Name, [string[]]$ComposeArgs) $script:docker.Add("up $Name " + ($ComposeArgs -join ' ')) }
    function Wait-ComposeServiceReady { param([string]$ProjectName, [string]$Service, [int]$TimeoutSeconds) return $true }
    Start-CaddyBackedGateway
    $text = $script:docker -join "`n"
    Assert-True ($text -match 'rm -sf acme-bootstrap certbot-renewer') 'Polaron''s own certificate containers are removed'
    Assert-True ($text -match 'up gateway .*docker-compose\.gateway-local\.yml' -and $text -notmatch 'up gateway .*gateway-public-https') 'Gateway starts on loopback (local gateway configuration)'
    $local = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'deployment/windows-ip-https/docker-compose.gateway-local.yml'))
    Assert-True ($local -match '127\.0\.0\.1:3001:80' -and $local -notmatch '0\.0\.0\.0') 'Loopback gateway binds 127.0.0.1:3001 only'

    # --- launcher wiring ---------------------------------------------------------------------------
    $starter = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'script_docker/start_docker.ps1')) -replace "`r", ''
    $main = $starter.Split(@("if (`$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }"), [StringSplitOptions]::None)[1]
    Assert-True ($main.IndexOf('Publish-CaddySite') -lt $main.IndexOf('Stop-PolaronStacks')) 'Caddy is changed before any Polaron container stops'
    Assert-True ($main -match "HttpsProxyMode -eq 'caddy'\) \{\s+Write-Step 'Start the Polaron gateway") 'Caddy mode skips Polaron''s own TCP 80/443 edge'
    Assert-True ($main -match "-not \`$settings\.AcmeEmail -and \`$script:HttpsProxyMode -eq 'builtin'") 'Caddy mode does not demand an ACME email'

    Write-Host "PASS: $script:checks Caddy integration checks (Caddy and Docker mocked)."
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
