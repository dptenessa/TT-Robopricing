$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $Root "run_pricing_pipeline.ps1"
$TaskName = "T Global Robopricing Weekly Scrape"

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Runner not found: $Runner"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3:00am
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Runs python pricing_pipeline.py --scrape every Monday at 03:00." `
    -Force | Out-Null

Write-Host "Scheduled task created or updated: $TaskName"
Write-Host "It can wake the PC from sleep, but it cannot run if the PC is fully shut down."
Write-Host "Logs will be written under: $(Join-Path $Root 'workable_data\logs')"
