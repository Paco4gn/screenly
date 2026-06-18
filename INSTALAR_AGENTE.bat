@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Instalar agente Fleetboard
where py >nul 2>nul
if %errorlevel%==0 (
    py instalar_agente.py
) else (
    python instalar_agente.py
)
echo.
pause
