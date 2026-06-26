param(
    [Parameter(Mandatory = $true)]
    [string]$Pack
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

python "$ProjectRoot\import_weekly_pack.py" "$Pack" --project-root "$ProjectRoot"

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Weekly pack imported. You can now open the fast pricing editor."
