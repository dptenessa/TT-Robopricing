@echo off
setlocal

cd /d "%~dp0.."

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\automation\import_weekly_pack.ps1"

echo.
pause

endlocal
