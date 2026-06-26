@echo off
setlocal

cd /d "%~dp0.."

python "automation\fast pricing editor.py"

if errorlevel 1 (
    echo.
    echo The editor closed with an error. Please copy the message above and send it to Codex.
    pause
)

endlocal
