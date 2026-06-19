@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Instalar agente Fleetboard
set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py"
%PYTHON% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo No se ha encontrado Python en este ordenador.
    echo Instala Python 3 y vuelve a ejecutar este archivo.
    pause
    exit /b 1
)
if not exist "vendor\paramiko" (
    echo Preparando el instalador por primera vez...
    %PYTHON% -m pip install --disable-pip-version-check --no-input --target vendor -r requirements.txt
    if errorlevel 1 (
        echo No se pudieron preparar las dependencias.
        pause
        exit /b 1
    )
)
%PYTHON% instalar_agente.py
echo.
pause
