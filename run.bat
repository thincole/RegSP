@echo off
title Auto Login ^& Register Tool
cd /d "%~dp0"

:: Kiem tra Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python. Vui long cai dat Python truoc.
    pause
    exit /b 1
)

:: Cai thu vien lan dau
if not exist ".installed" (
    echo [INFO] Dang cai dat thu vien lan dau...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [LOI] Cai dat thu vien that bai.
        pause
        exit /b 1
    )
    echo done > .installed
    echo [INFO] Cai dat thu vien thanh cong!
)

:: Chay phan mem
echo [INFO] Dang khoi dong phan mem...
python main.py
exit
