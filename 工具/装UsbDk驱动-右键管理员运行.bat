@echo off
setlocal
title Install UsbDk driver
color 0f

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo [X] Must run as Administrator.
  echo     Right-click this .bat, choose Run as administrator.
  echo.
  pause
  exit /b 1
)

set "MSI=%~dp0UsbDk_1.0.22_x64.msi"
if not exist "%MSI%" (
  echo [X] UsbDk_1.0.22_x64.msi not found next to this .bat.
  echo     Expected at: %MSI%
  pause
  exit /b 1
)

echo.
echo Installing UsbDk from:
echo   %MSI%
msiexec /i "%MSI%" /qn /norestart
set RC=%ERRORLEVEL%
if not "%RC%"=="0" if not "%RC%"=="3010" (
  echo.
  echo [X] Installer returned %RC%.
  echo     Try double-clicking UsbDk_1.0.22_x64.msi manually.
  pause
  exit /b 1
)

reg query "HKLM\SYSTEM\CurrentControlSet\Services\UsbDk" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [X] UsbDk service not found after install.
  pause
  exit /b 1
)

echo.
echo [OK] UsbDk installed. No reboot needed.
pause
exit /b 0
