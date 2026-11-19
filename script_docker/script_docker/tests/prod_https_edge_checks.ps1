# V1.29 offline checks for the production HTTPS edge (start_docker.ps1 Ensure-PublicHttpsEdge).
# Docker, Certbot and Let's Encrypt are mocked; nothing is started and no project file is written.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = '1'
. (Join-Path $ProjectRoot 'script_docker/start_docker.ps1') -Profile prod
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = ''
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw "FAIL: $Message" }
    $script:checks++
}
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('aetheris-edge-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory (Join-Path $tempRoot 'deployment/windows-ip-https') -Force | Out-Null
$root = $tempRoot
$Recreate = $false
$nginxPath = Join-Path $tempRoot 'deployment/windows-ip-https/nginx.gateway.https.conf'

# --- mocks ---------------------------------------------------------------------------------
function Invoke-Docker { param([string]$Title, [string[]]$DockerArgs) $script:docker.Add(($DockerArgs -join ' ')) }
function Assert-PublicPortOwners { }
function Enable-PublicFirewall { }
function Assert-PublicGatewayBindings { param([string[]]$ComposeArgs) }
function Test-DomainResolvesTo { param([string]$Domain, [string]$IP) return $true }
function Wait-AcmeChallengeRoute { param([string[]]$ComposeArgs) if (-not $script:routeOk) { throw 'challenge route down' } }
function Invoke-CertificateTool {
    param([string[]]$ComposeArgs, [string[]]$ToolArgs)
    $name = $ToolArgs[[array]::IndexOf($ToolArgs, '--name') + 1]
    $row = $script:certs[$name]
    if (-not $row) { return [pscustomobject]@{ managed = $false; valid = $false; reason = 'missing_acme_lineage' } }
    return [pscustomobject]@{ managed = $true; valid = $true; reason = 'ok'
        expires_utc = [DateTime]::UtcNow.AddHours($row.Hours).ToString('o') }
}
function Ensure-PublicCertificate {
    param([string[]]$ComposeArgs, [string]$Identity, [string]$CertName, [string]$Email, [switch]$IsIP)
    $script:issued.Add($CertName)
    if ($script:failIssue -contains $CertName) { throw "Some challenges have failed for $Identity" }
    $script:certs[$CertName] = @{ Hours = 150 }
}
function Start-PublicHttpsGateway {
    param([string[]]$ComposeArgs, [string]$PublicIP, [string]$Domain, [switch]$AllowStoredCertificate)
    $script:started = [pscustomobject]@{ IP = $PublicIP; Domain = $Domain; Stored = [bool]$AllowStoredCertificate }
}
function Reset-Scenario {
    param([hashtable]$Certs, [bool]$ImagesChanged = $false, [string[]]$FailIssue = @(), [bool]$RouteOk = $true)
    $script:docker = New-Object System.Collections.Generic.List[string]
    $script:issued = New-Object System.Collections.Generic.List[string]
    $script:certs = $Certs
    $script:failIssue = $FailIssue
    $script:routeOk = $RouteOk
    $script:started = $null
    $script:AetherisAppImagesChanged = $ImagesChanged
    $env:AETHERIS_CERT_NAMES = ''
}
$ip = '8.8.8.8'; $dns = 'example.org'
$ipName = "aetheris-ip-$ip"; $dnsName = "aetheris-domain-$dns"
function Get-DockerText { return ($script:docker -join "`n") }

try {
    # 1. Unchanged deployment with current certificates: no ACME challenge, renewer restarted.
    [IO.File]::WriteAllText($nginxPath, (Get-PublicNginxConfig -PublicIP $ip -Domain $dns))
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 100 }; $dnsName = @{ Hours = 1500 } }
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS $dns -Email ops@example.com
    $text = Get-DockerText
    Assert-True ($text -notmatch 'acme-bootstrap' -and $script:issued.Count -eq 0 -and -not $script:started) 'Fast path: no challenge, no gateway recreation'
    Assert-True ($text -match 'up -d --no-build --pull missing --no-deps --wait --wait-timeout 180 gateway') 'Fast path: stopped gateway is started again'
    Assert-True ($text -match '--force-recreate certbot-renewer') 'Fast path: renewer is restarted (it stops with the gateway)'
    Assert-True ($env:AETHERIS_CERT_NAMES -eq "$ipName $dnsName") 'Renewer renews exactly the served certificates'
    Assert-True ($script:AetherisServedDomain -eq $dns) 'Domain is served'

    # 2. New application image, certificates current: recreate gateway, still no ACME challenge.
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 100 }; $dnsName = @{ Hours = 1500 } } -ImagesChanged $true
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS $dns -Email ops@example.com
    Assert-True ((Get-DockerText) -notmatch 'acme-bootstrap' -and $script:issued.Count -eq 0) 'Code change: no ACME challenge while certificates are current'
    Assert-True ($script:started -and $script:started.Domain -eq $dns -and -not $script:started.Stored) 'Code change: gateway recreated with IP and domain'

    # 3. IP certificate due (< 36 h): challenge runs and renews it.
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 20 }; $dnsName = @{ Hours = 1500 } }
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS $dns -Email ops@example.com
    $text = Get-DockerText
    Assert-True ($text -match 'acme-bootstrap up' -and $text -match 'acme-bootstrap rm -sf acme-bootstrap') 'Due certificate: bootstrap started and removed'
    Assert-True ($script:issued -contains $ipName -and $script:started.Domain -eq $dns) 'Due certificate: renewed, both identities served'

    # 4. Domain newly added but DNS not ready: IP HTTPS starts, domain left out (nginx would not start).
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 100 } } -FailIssue @($dnsName)
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS $dns -Email ops@example.com
    $conf = [IO.File]::ReadAllText($nginxPath)
    Assert-True ($script:started.Domain -eq '' -and $script:AetherisServedDomain -eq '') 'Missing domain certificate: HTTPS starts on the IP only'
    Assert-True ($conf -notmatch 'aetheris-domain' -and $conf -match "aetheris-ip-$ip") 'Missing domain certificate: nginx config references only existing certificates'
    Assert-True ($env:AETHERIS_CERT_NAMES -eq $ipName) 'Missing domain certificate: renewer renews only the IP'

    # 5. Renewal fails but stored certificates exist: keep serving them, trust probe tolerated.
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 20 }; $dnsName = @{ Hours = 20 } } -FailIssue @($ipName, $dnsName)
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS $dns -Email ops@example.com
    Assert-True ($script:started.Stored -and $script:started.Domain -eq $dns) 'Failed renewal: stored IP and domain certificates stay in use'

    # 6. Challenge route not reachable on TCP 80: no issuance attempt, stored certificates used.
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 20 } } -RouteOk $false
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS '' -Email ops@example.com
    Assert-True ($script:issued.Count -eq 0 -and $script:started.Stored -and $script:started.Domain -eq '') 'Unreachable TCP 80: stored IP certificate used, IP only'

    # 7. First launch with no certificate and no public TCP 80: clear failure.
    Reset-Scenario -Certs @{} -FailIssue @($ipName)
    $threw = $false
    try { $null = Ensure-PublicHttpsEdge -IP $ip -DNS '' -Email ops@example.com } catch { $threw = ($_.Exception.Message -match 'no certificate is stored') }
    Assert-True $threw 'No stored IP certificate and issuance failed: startup stops with the reason'
    Assert-True ((Get-DockerText) -match 'acme-bootstrap rm -sf acme-bootstrap') 'Bootstrap is removed even when issuance fails'

    # 8. Domain removed from .env: IP-only config, no challenge, gateway recreated.
    [IO.File]::WriteAllText($nginxPath, (Get-PublicNginxConfig -PublicIP $ip -Domain $dns))
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 100 }; $dnsName = @{ Hours = 1500 } }
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS '' -Email ops@example.com
    Assert-True ($script:issued.Count -eq 0 -and $script:started.Domain -eq '' -and $env:AETHERIS_CERT_NAMES -eq $ipName) 'Domain removed: IP-only gateway without a challenge'
    Assert-True ([IO.File]::ReadAllText($nginxPath) -notmatch 'aetheris-domain') 'Domain removed: generated config drops the domain server'

    # 9. -Recreate forces gateway recreation even when nothing changed.
    [IO.File]::WriteAllText($nginxPath, (Get-PublicNginxConfig -PublicIP $ip -Domain ''))
    Reset-Scenario -Certs @{ $ipName = @{ Hours = 100 } }
    $Recreate = $true
    $null = Ensure-PublicHttpsEdge -IP $ip -DNS '' -Email ops@example.com
    $Recreate = $false
    Assert-True ($script:started -and $script:issued.Count -eq 0) '-Recreate recreates the gateway without a challenge'

    Write-Host "PASS: $script:checks production HTTPS edge checks (offline Docker/ACME mocks)."
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
