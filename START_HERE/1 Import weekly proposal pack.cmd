@echo off
setlocal
cd /d "%~dp0.."
python "%~dp01_import_weekly_proposal_pack.py"
if errorlevel 1 (
  echo.
  echo Import finished with an error. Please copy the message above.
)
echo.
pause
endlocal
