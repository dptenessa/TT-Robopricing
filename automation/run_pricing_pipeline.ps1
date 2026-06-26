$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$LogDir = Join-Path $Root "workable_data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "pricing_pipeline_$Stamp.log"

Set-Location $Root

"[$(Get-Date -Format s)] Starting pricing pipeline" | Tee-Object -FilePath $LogPath
"Root: $Root" | Tee-Object -FilePath $LogPath -Append

python "$ScriptDir\pricing_pipeline.py" --scrape *>&1 | Tee-Object -FilePath $LogPath -Append
$ExitCode = $LASTEXITCODE

"[$(Get-Date -Format s)] Finished with exit code $ExitCode" | Tee-Object -FilePath $LogPath -Append
exit $ExitCode