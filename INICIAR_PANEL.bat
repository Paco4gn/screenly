@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Panel Screenly
if not exist "vendor\flask" (
    echo Preparando Fleetboard por primera vez...
    where py >nul 2>nul
    if %errorlevel%==0 (
        py -m pip install --disable-pip-version-check --no-input --target vendor -r requirements.txt
    ) else (
        python -m pip install --disable-pip-version-check --no-input --target vendor -r requirements.txt
    )
    if errorlevel 1 (
        echo.
        echo No se pudieron preparar las dependencias.
        pause
        exit /b 1
    )
)
where py >nul 2>nul
if %errorlevel%==0 (
    py app.py
) else (
    python app.py
)
if errorlevel 1 (
    echo.
    echo No se pudo iniciar el panel.
    pause
)
