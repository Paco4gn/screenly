@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Centro de mando Screenly
set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py"
%PYTHON% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo No se ha encontrado Python en este ordenador.
    echo Instala Python 3 y vuelve a ejecutar este archivo.
    pause
    exit /b 1
)
set "NEED_DEPS=0"
if not exist "vendor\flask" set "NEED_DEPS=1"
if not exist "vendor\requests" set "NEED_DEPS=1"
if not exist "vendor\paramiko" set "NEED_DEPS=1"
if "%NEED_DEPS%"=="1" (
    echo Preparando Centro de mando Screenly por primera vez...
    %PYTHON% -m pip install --disable-pip-version-check --no-input --target vendor -r requirements.txt
    if errorlevel 1 (
        echo.
        echo No se pudieron preparar las dependencias.
        pause
        exit /b 1
    )
)
%PYTHON% app.py
if errorlevel 1 (
    echo.
    echo No se pudo iniciar el panel.
    pause
)
