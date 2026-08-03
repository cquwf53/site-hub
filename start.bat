@echo off
chcp 65001 >nul
title SiteHub
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "SITEHUB_EXIT=%ERRORLEVEL%"
if not "%SITEHUB_EXIT%"=="0" if /i not "%SITEHUB_NO_PAUSE%"=="1" pause
exit /b %SITEHUB_EXIT%
