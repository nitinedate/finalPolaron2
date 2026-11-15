# Repair only the known invalid pgAdmin placeholder from older Polaron releases.
# Never reset pgAdmin volumes, users, passwords, or unrelated environment values.
[CmdletBinding()]
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$replacement = 'admin@forensic.example.com'
$legacy = 'admin@example.test'

function Test-PolaronPgAdminEmail {
    param([Parameter(Mandatory = $true)][string]$Email)
    $address = $Email.Trim().Trim('"').Trim("'")
    if ($address -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { return $false }
    $domain = ($address -split '@', 2)[1].ToLowerInvariant()
    # pgAdmin/Flask uses email-validator. These reserved suffixes are not
    # accepted without specific allow-list settings. The shared Compose file
    # explicitly permits .local, so do not reject a custom .local address.
    if ($domain -match '(^|\.)(test|invalid|localhost|example)$') { return $false }
    return $true
}

function Update-PolaronPgAdminEnv {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $original = [System.IO.File]::ReadAllText($Path)
    $regexOptions = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor [System.Text.RegularExpressions.RegexOptions]::Multiline
    $rx = [regex]::new('(?m)^([ \t]*PGADMIN_DEFAULT_EMAIL[ \t]*=[ \t]*)([^\r\n#]*)([ \t]*(?:#[^\r\n]*)?)$', $regexOptions)
    $updated = $rx.Replace($original, [System.Text.RegularExpressions.MatchEvaluator]{
        param($m)
        $configured = $m.Groups[2].Value.Trim().Trim('"').Trim("'")
        if ($configured -ieq $legacy) {
            $script:PgadminRepaired = $true
            return ($m.Groups[1].Value + $replacement + $m.Groups[3].Value)
        }
        if (-not (Test-PolaronPgAdminEmail -Email $configured)) {
            throw "Invalid PGADMIN_DEFAULT_EMAIL='$configured' in $Path. Set an email with a normal domain, for example $replacement. The existing file is not changed."
        }
        return $m.Value
    })
    if ($updated -cne $original) {
        # Backup is kept beside the original so private .env values stay on
        # the same protected disk. It must never be included in release ZIPs.
        $backup = "$Path.pgadmin-backup-$(Get-Date -Format yyyyMMddHHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,6))"
        Copy-Item -LiteralPath $Path -Destination $backup -ErrorAction Stop
        [System.IO.File]::WriteAllText($Path, $updated, [System.Text.UTF8Encoding]::new($false))
        Write-Host "[pgAdmin] Repaired legacy email placeholder in $Path (backup saved next to file)." -ForegroundColor Yellow
    }
}

$script:PgadminRepaired = $false
Update-PolaronPgAdminEnv -Path (Join-Path $ProjectRoot '.env')
Update-PolaronPgAdminEnv -Path (Join-Path $ProjectRoot 'common-docker/.env')

# The calling shell can override both files during Docker Compose variable
# substitution. Fix only the same known invalid default in this process.
if ($env:PGADMIN_DEFAULT_EMAIL) {
    $effective = $env:PGADMIN_DEFAULT_EMAIL.Trim().Trim('"').Trim("'")
    if ($effective -ieq $legacy) {
        $env:PGADMIN_DEFAULT_EMAIL = $replacement
        $script:PgadminRepaired = $true
        Write-Host '[pgAdmin] Corrected invalid PGADMIN_DEFAULT_EMAIL inherited from the calling shell.' -ForegroundColor Yellow
    } elseif (-not (Test-PolaronPgAdminEmail -Email $effective)) {
        throw "PGADMIN_DEFAULT_EMAIL='$effective' was inherited from your shell and is invalid. Set it to a valid login email or remove the environment override."
    }
}
if ($script:PgadminRepaired) {
    Write-Host '[pgAdmin] Updated login default for NEW installations. Existing pgAdmin accounts and Docker volumes are unchanged.' -ForegroundColor Yellow
}
