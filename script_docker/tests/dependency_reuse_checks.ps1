# V1.29.1 offline checks: application builds reuse installed libraries instead of reinstalling them.
# Docker is mocked; the real Dockerfiles of the project are parsed. Nothing is built or started.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = '1'
. (Join-Path $ProjectRoot 'script_docker/start_docker.ps1') -Profile local
$env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY = ''
$root = $ProjectRoot
$RefreshDependencies = $false; $NoCache = $false
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }
$backend = Join-Path $root 'backend/Dockerfile'
$malware = Join-Path $root 'backend/Dockerfile.malware'
$scanner = Join-Path $root 'backend/Dockerfile.scanner'
$frontend = Join-Path $root 'frontend/Dockerfile'
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('aetheris-deps-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    # --- Dockerfile parsing ------------------------------------------------------------------------
    $lines = @(Get-DockerfileStageLines -Path $backend -Stage 'dependencies')
    Assert-True (($lines -join "`n") -notmatch 'AS application' -and ($lines -join "`n") -match 'requirements\.txt') 'Backend dependency stage ends before the application stage'
    $runs = @(Get-DockerfileRunCommands -Lines $lines)
    Assert-True ($runs.Count -eq 2) 'Backend dependency stage has its two RUN steps'
    Assert-True ($runs[0].StartsWith('set -eux;') -and $runs[0] -notmatch '\\\s*$' -and $runs[0] -match "printf '%s\\n'") 'Continuation lines are joined, quoted backslashes kept'
    Assert-True ($runs[1].StartsWith('python -m pip install --upgrade pip') -and $runs[1] -notmatch '--mount') 'RUN --mount flags are not part of the recorded command'
    $stageArgs = Get-DockerfileStageArgs -Lines $lines -BuildArgs @{ TORCH_INDEX_URL = 'https://example/cu130' }
    Assert-True ($stageArgs['TORCH_INDEX_URL'] -eq 'https://example/cu130' -and -not $stageArgs.Contains('AETHERIS_DEPS_IMAGE')) 'Stage ARG values use build arguments; global ARGs are not stage ARGs'
    $scanLines = @(Get-DockerfileStageLines -Path $scanner -Stage 'dependencies')
    $scanArgs = Get-DockerfileStageArgs -Lines $scanLines -BuildArgs @{}
    Assert-True ($scanArgs['NUCLEI_VERSION'] -eq '3.11.1' -and $scanArgs['TORCH_INDEX_URL'] -eq 'https://download.pytorch.org/whl/cpu') 'Scanner stage ARG defaults are read'
    Assert-True ((Get-DockerfileGlobalArg -Path $scanner -Name 'TRIVY_VERSION') -eq '0.58.1') 'Trivy version is read from the global ARG'
    Assert-True ((Get-DockerfileRootBase -Lines $lines -Stage 'dependencies') -eq 'nvidia/cuda:12.6.3-runtime-ubuntu24.04') 'Backend base image found'
    Assert-True ((Get-DockerfileRootBase -Lines @(Get-DockerfileStageLines -Path $malware -Stage 'malware-dependencies') -Stage 'malware-dependencies') -eq 'nvidia/cuda:12.6.3-runtime-ubuntu24.04') 'Malware base found through its parent stage'
    Assert-True ((Get-DockerfileRootBase -Lines $scanLines -Stage 'dependencies') -eq 'python:3.12-slim') 'Scanner base found'
    Assert-True ((Get-DockerfileRootBase -Lines @(Get-DockerfileStageLines -Path $frontend -Stage 'dependencies') -Stage 'dependencies') -eq 'node:20-alpine') 'Website base found'
    foreach ($file in @($backend, $malware, $scanner, $frontend)) {
        $text = [IO.File]::ReadAllText($file)
        Assert-True ($text -match '(?m)^ARG AETHERIS_DEPS_IMAGE=\S+' -and $text -match '(?m)^FROM \$\{AETHERIS_DEPS_IMAGE\} AS (application|build)$') "$([IO.Path]::GetFileName($file)) can start from a dependency image"
    }

    # --- fingerprint: what is installed, nothing else ---------------------------------------------
    $df = Join-Path $tmp 'Dockerfile'; $req = Join-Path $tmp 'requirements.txt'
    $base = [IO.File]::ReadAllText($backend)
    [IO.File]::WriteAllText($df, $base); [IO.File]::WriteAllText($req, "fastapi>=0.110`nuvicorn`n")
    $fp = { Get-DependencyFingerprint -Dockerfile $df -Stage 'dependencies' -InputFiles @($req) -BuildArgs @{ TORCH_INDEX_URL = 'cu130' } }
    $first = & $fp
    Assert-True ($first -match '^[0-9a-f]{32}$') 'Fingerprint is a 32-character hash'
    [IO.File]::WriteAllText($df, $base.Replace('WORKDIR /app', "# explained`n`nWORKDIR /app"))
    Assert-True ((& $fp) -eq $first) 'Comment or blank-line edits do not reinstall'
    [IO.File]::WriteAllText($df, $base.Replace('RUN test -n "$AETHERIS_APP_BUILD_ID"', 'RUN echo app && test -n "$AETHERIS_APP_BUILD_ID"'))
    Assert-True ((& $fp) -eq $first) 'Application-stage edits do not reinstall'
    [IO.File]::WriteAllText($df, $base); [IO.File]::WriteAllText($req, "fastapi>=0.110`r`nuvicorn`r`n")
    Assert-True ((& $fp) -eq $first) 'CRLF line endings in requirements do not reinstall'
    [IO.File]::WriteAllText($req, "fastapi>=0.110`nuvicorn`nhttpx`n")
    Assert-True ((& $fp) -ne $first) 'A changed requirements file reinstalls'
    [IO.File]::WriteAllText($req, "fastapi>=0.110`nuvicorn`n")
    [IO.File]::WriteAllText($df, $base.Replace('tesseract-ocr-eng', 'tesseract-ocr-eng tesseract-ocr-deu'))
    Assert-True ((& $fp) -ne $first) 'A changed apt package list reinstalls'
    [IO.File]::WriteAllText($df, $base)
    Assert-True ((Get-DependencyFingerprint -Dockerfile $df -Stage 'dependencies' -InputFiles @($req) -BuildArgs @{ TORCH_INDEX_URL = 'cpu' }) -ne $first) 'A different TORCH_INDEX_URL reinstalls'

    # --- build arguments ---------------------------------------------------------------------------
    $refreshArgs = @(Get-AppBuildArgs -BuildId x -RefreshDependencies -NoPull)
    Assert-True ($refreshArgs -contains '--no-cache' -and $refreshArgs -notcontains '--pull') 'Refresh builds on a local dependency image do not --pull it'
    $normalArgs = @(Get-AppBuildArgs -BuildId x -NoPull)
    Assert-True ($normalArgs -notcontains '--no-cache' -and $normalArgs -notcontains '--pull') 'Ordinary builds keep the cache'

    # --- Ensure-DependencyImage decisions (Docker mocked) -------------------------------------------
    $spec = @(Get-DependencyImageSpecs -TorchIndexUrl 'https://download.pytorch.org/whl/cu130') | Where-Object { $_.Key -eq 'backend' }
    $realFp = Get-DependencyFingerprint -Dockerfile $spec.Dockerfile -Stage $spec.Stage -InputFiles $spec.InputFiles -BuildArgs $spec.BuildArgs
    function Get-DockerImageLabelValue { param([string]$Image, [string]$Label) return $script:label }
    function Test-DependencyImageAdoptable { param($Spec) return $script:adoptable }
    function Set-DependencyImageLabel { param([string]$Image, [string]$Fingerprint) $script:calls.Add("label $Image $Fingerprint") }
    function Invoke-DockerImageBuild { param([string]$Title, [string[]]$BuildArgs, [switch]$NoPull) $script:calls.Add("build $($BuildArgs -join ' ') nopull=$([bool]$NoPull)") }
    function Run-Ensure([string]$Label, [bool]$Adoptable, [bool]$Reuse = $true) {
        $script:label = $Label; $script:adoptable = $Adoptable
        $script:calls = New-Object System.Collections.Generic.List[string]
        Ensure-DependencyImage -Spec $spec -Reuse:$Reuse
        return ($script:calls -join "`n")
    }
    Assert-True ((Run-Ensure -Label $realFp -Adoptable $false) -eq '') 'Matching label: libraries reused, no build'
    Assert-True ((Run-Ensure -Label '' -Adoptable $true) -eq "label aetheris-backend-deps $realFp") 'Older matching image: labeled, not rebuilt'
    $built = Run-Ensure -Label 'old-fingerprint' -Adoptable $false
    Assert-True ($built -match '--target dependencies' -and $built -match "--label org\.aetheris\.dependency-fingerprint=$realFp" -and $built -match '-t aetheris-backend-deps backend') 'Changed dependencies: library stage built and labeled'
    $RefreshDependencies = $true
    Assert-True ((Run-Ensure -Label $realFp -Adoptable $true) -match '--target dependencies') '-RefreshDependencies always rebuilds the library stage'
    $RefreshDependencies = $false
    Assert-True ((Run-Ensure -Label $realFp -Adoptable $true -Reuse $false) -match '--target dependencies') 'Without local-image builds the stage is built as before'

    # --- Invoke-BuildProdApplications wiring (Docker mocked) -----------------------------------------
    function Ensure-DependencyImage { param($Spec, [switch]$Reuse) $script:calls.Add("ensure $($Spec.Key) reuse=$([bool]$Reuse)") }
    function Invoke-OneServiceBuild { param([string]$Title, [string[]]$ComposeArgs, [string]$Service, [string]$BuildId, [string]$DepsImage = '') $script:calls.Add("compose $Service deps=$DepsImage") }
    function Invoke-Docker { param([string]$Title, [string[]]$DockerArgs) $script:calls.Add("docker $($DockerArgs -join ' ')") }
    function Get-StackComposeArgs { param([string]$Root, [string]$Name) return @('compose', $Name) }
    function Get-PublicGatewayArgs { return @('compose', 'gateway') }
    function Run-Build([bool]$Local) {
        $script:AetherisLocalImageBuilds = $Local
        $script:calls = New-Object System.Collections.Generic.List[string]
        Invoke-BuildProdApplications -BuildId 'b1'
        return ($script:calls -join "`n")
    }
    $out = Run-Build -Local $true
    Assert-True ($out -match 'ensure backend reuse=True' -and $out -match 'ensure frontend reuse=True' -and $out -match 'ensure malware reuse=True' -and $out -match 'ensure scanner reuse=True') 'All four library images are checked before application builds'
    Assert-True ($out -match 'build .*AETHERIS_DEPS_IMAGE=aetheris-backend-deps -t rag_new2-api backend nopull=True') 'Backend application starts from aetheris-backend-deps'
    Assert-True ($out -match 'compose api deps=aetheris-malware-deps' -and $out -match 'compose worker-nessus deps=aetheris-scanner-deps') 'Malware and scanner applications start from their library images'
    Assert-True (([regex]::Matches($out, 'compose (gateway|frontend) deps=aetheris-frontend-deps')).Count -eq 3) 'All three websites start from aetheris-frontend-deps'
    Assert-True ($out -match 'compose clamav-update deps=$' -or $out -match "compose clamav-update deps=`n") 'ClamAV build is unchanged'
    Assert-True ($out.IndexOf('ensure backend') -lt $out.IndexOf('rag_new2-api') -and $out.IndexOf('ensure scanner') -lt $out.IndexOf('compose worker-nessus')) 'Library images are ready before each application build'
    $out = Run-Build -Local $false
    Assert-True ($out -notmatch 'AETHERIS_DEPS_IMAGE' -and $out -notmatch 'deps=aetheris' -and $out -notmatch 'ensure malware' -and $out -notmatch 'ensure scanner') 'Other Docker builders keep the previous build path'
    $RefreshDependencies = $true
    $out = Run-Build -Local $true
    $RefreshDependencies = $false
    Assert-True ($out -match 'docker pull nginx:1\.27-alpine') 'Refresh still updates the website runtime base image'

    Write-Host "PASS: $script:checks dependency reuse checks (Dockerfiles parsed, Docker mocked)."
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
