@echo off
rem Smara Sukma - double-click to recover deleted photos & videos on Windows.
rem Re-launches itself with administrator rights, which reading a drive directly needs.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel% equ 0 (py -3 smara_sukma.py %*) else (python smara_sukma.py %*)
echo.
pause
