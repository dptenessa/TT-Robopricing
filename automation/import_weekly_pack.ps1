param(
    [string]$Pack
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path

if ([string]::IsNullOrWhiteSpace($Pack)) {
    Add-Type -AssemblyName System.Windows.Forms

    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Select weekly-proposal-pack.zip"
    $dialog.Filter = "Weekly proposal pack (*.zip)|*.zip|All files (*.*)|*.*"
    $dialog.CheckFileExists = $true
    $dialog.Multiselect = $false

    $downloads = Join-Path $env:USERPROFILE "Downloads"
    if (Test-Path -LiteralPath $downloads) {
        $dialog.InitialDirectory = $downloads
    }

    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Write-Host "Import cancelled."
        exit 0
    }

    $Pack = $dialog.FileName
}

python "$ScriptDir\import_weekly_pack.py" "$Pack" --project-root "$ProjectRoot"

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Weekly pack imported successfully."
Write-Host "Running local data preparation and pricing model..."
Write-Host ""

python "$ScriptDir\pricing_pipeline.py"

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Weekly pack was imported, but local preparation/model generation failed."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Weekly pack imported and local model proposals regenerated."
Write-Host "You can now open the fast pricing editor."