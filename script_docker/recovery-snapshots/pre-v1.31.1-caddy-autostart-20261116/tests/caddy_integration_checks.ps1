# V1.30 offline checks: Polaron joins an existing Caddy for HTTPS (Caddy and Docker mocked).
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
