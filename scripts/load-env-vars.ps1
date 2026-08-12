# Usage: .\scripts\load-env-vars.ps1 [path-to-env-file]
# Reads a .env file and sets each variable in the active AZD environment.
param(
    [string]$EnvFile = ".env"
)

if (-not (Test-Path $EnvFile)) {
    Write-Error "Error: $EnvFile not found. Copy .env.example to .env and fill in your values."
    exit 1
}

Write-Host "Loading env vars from $EnvFile into AZD environment..."

foreach ($line in Get-Content $EnvFile) {
    # Skip comments and blank lines
    if ($line -match '^\s*#' -or [string]::IsNullOrWhiteSpace($line)) { continue }

    $parts = $line -split '=', 2
    if ($parts.Count -ne 2) { continue }

    $key   = $parts[0].Trim()
    $value = $parts[1].Trim()

    # Skip placeholders and empty values
    if ($value -match '^<.*>$' -or [string]::IsNullOrEmpty($value)) { continue }

    azd env set $key $value
    Write-Host "  OK $key"
}

Write-Host "Done."
