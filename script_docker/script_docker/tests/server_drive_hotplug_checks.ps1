# V1.31.3 offline checks: the launcher verifies that the forensic containers reach the workstation helper,
# which is how drives connected to the server after startup are found (Docker mocked).
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = '1'
. (Join-Path $ProjectRoot 'script_docker/start_docker.ps1') -Profile prod
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = ''
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$script:said = New-Object System.Collections.Generic.List[string]
$script:execArgs = @()
$script:reply = ''
$script:api = [pscustomobject]@{ Id = 'abc123'; Status = 'running'; Health = 'healthy'; ExitCode = 0 }
function Write-Ok([string]$Message) { $script:said.Add("OK $Message") }
function Write-Warn([string]$Message) { $script:said.Add("WARN $Message") }
function Write-Step([string]$Message) { $script:said.Add("STEP $Message") }
function Get-ComposeServiceContainerState { param([string]$ProjectName, [string]$Service) if ($ProjectName -eq 'aetheris-forensic' -and $Service -eq 'api') { return $script:api }; return $null }
function Invoke-DockerCapture { param([string[]]$DockerArgs) $script:execArgs = $DockerArgs; return $script:reply }

$script:reply = "some log line`nPOLARON_HELPER {`"ok`": true, `"url`": `"http://host.docker.internal:9876`", `"drives`": [`"C`", `"D`", `"G`"], `"error`": `"`", `"errors`": []}"
Assert-True ((Test-ContainersReachWorkstationHelper) -eq $true) 'Helper reachable from the API container: check passes'
Assert-True ($script:execArgs[0] -eq 'exec' -and $script:execArgs -contains 'abc123' -and $script:execArgs -contains '/app' -and ($script:execArgs[-1] -match 'probe_helper') -and ($script:execArgs[-1] -notmatch '"')) 'Asks probe_helper inside the forensic API container (no double quotes for Windows PowerShell 5.1)'
Assert-True (@($script:said | Where-Object { $_ -match '^OK .*appear in the evidence browser by themselves\. Drives now: C: D: G:' }).Count -eq 1) 'Reports the live drive list'

$script:said.Clear()
$script:reply = "POLARON_HELPER {`"ok`": false, `"url`": `"`", `"drives`": [], `"error`": `"The workstation helper is running but only accepts requests addressed to 127.0.0.1 (its http://+:9876/ reservation is missing).`", `"errors`": [`"http://host.docker.internal:9876: HTTP Error 400: Bad Request`"]}"
Assert-True ((Test-ContainersReachWorkstationHelper) -eq $false) 'Helper unreachable: check fails (never throws)'
Assert-True (@($script:said | Where-Object { $_ -match '^WARN The forensic containers cannot reach the workstation helper: .*reservation is missing' }).Count -eq 1) 'Says why'
Assert-True (@($script:said | Where-Object { $_ -match 'HTTP Error 400' }).Count -eq 1) 'Shows the raw error'
Assert-True (@($script:said | Where-Object { $_ -match 'configure-host-drive-helper-network\.ps1' -and $_ -match 'ForceRestart' }).Count -eq 1) 'Says how to fix it'
Assert-True (@($script:said | Where-Object { $_ -match 'lists only the drives Docker mounted at startup' }).Count -eq 1) 'Says what the examiner will see'

$script:said.Clear(); $script:reply = $null
Assert-True ((Test-ContainersReachWorkstationHelper) -eq $false -and @($script:said | Where-Object { $_ -match 'Could not ask' }).Count -eq 1) 'docker exec failure: warning, no exception'
$script:said.Clear(); $script:api = $null
Assert-True ((Test-ContainersReachWorkstationHelper) -eq $false -and @($script:said | Where-Object { $_ -match 'API container is not running' }).Count -eq 1) 'No API container: warning, no exception'

$launcher = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'script_docker/start_docker.ps1')) -replace "`r", ''
Assert-True ($launcher -match "Ensure-WorkstationHelper\n\s*\`$null = Test-ContainersReachWorkstationHelper") 'Launcher runs the check right after starting the helper'
Write-Host "PASS: $script:checks server drive hot-plug launcher checks" -ForegroundColor Green
