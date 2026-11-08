@echo off
chcp 65001 >nul
title Setup Environment - Auto Login ^& Register Tool
cd /d "%~dp0"

echo ============================================================
echo        CAI DAT MOI TRUONG CHAY TOOL AUTO LOGIN ^& REG SP
echo ============================================================
echo.

:: 1. Kiem tra Python da duoc cai dat chua
echo [1/5] Kiem tra Python...
python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
if errorlevel 1 (
    echo [CANH BAO] Chua tim thay Python ^(>= 3.9^) tren may!
    echo [INFO] Dang tu dong tai va cai dat Python 3.12.7 ^(64-bit^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$url = 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'; " ^
        "$out = Join-Path $env:TEMP 'python-3.12.7-amd64.exe'; " ^
        "Invoke-WebRequest -Uri $url -OutFile $out; " ^
        "Start-Process -FilePath $out -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1' -Wait; " ^
        "Remove-Item $out -Force -ErrorAction SilentlyContinue"

    :: Cap nhat lai PATH trong phien hien tai
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
    if errorlevel 1 (
        echo [LOI] Khong the tu dong cai dat Python. Vui long tai Python 3.12 tai https://www.python.org/downloads/ ^( nho tich 'Add Python to PATH' ^) roi chay lai setup.bat!
        pause
        exit /b 1
    )
)

for /f "delims=" %%v in ('python --version 2^>^&1') do echo [OK] Da tim thay: %%v
echo.

:: 2. Nang cap pip, setuptools, wheel
echo [2/5] Nang cap pip, setuptools, wheel...
python -m pip install --upgrade pip setuptools wheel
echo.

:: 3. Cai dat cac thu vien trong requirements.txt
echo [3/5] Cai dat cac thu vien Python tu requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [LOI] Cai dat thu vien tu requirements.txt that bai!
    pause
    exit /b 1
)
echo [OK] Da cai dat day du thu vien Python!
echo.

:: 4. Kiem tra va tu dong tai gemlogindriver.exe cho cac browser Gemlogin (neu thieu)
echo [4/5] Kiem tra ChromeDriver ^(gemlogindriver.exe^) trong Gemlogin...
python -c "import os, glob; from modules.gemlogin_manager import _ensure_driver_exists; base = os.path.expanduser(r'~\.gemlogin\browser'); [(_ensure_driver_exists(os.path.join(d, 'Chrome-bin', 'gemlogindriver.exe'))) for d in (glob.glob(os.path.join(base, '*')) if os.path.isdir(base) else []) if os.path.isdir(os.path.join(d, 'Chrome-bin'))]; print('[OK] Hoan tat kiem tra Gemlogin Driver.')"
echo.

:: 5. Xac minh toan bo thu vien va module cua tool
echo [5/5] Kiem tra import toan bo module cua phan mem...
if not exist "logs" mkdir "logs"
python -c "import tkinter, selenium, gspread, google.oauth2.service_account, pyotp, requests, dotenv, loguru; import config, main; print('[OK] Tat ca thu vien va module cua tool da san sang!')"
if errorlevel 1 (
    echo [LOI] Kiem tra module that bai! Vui long xem thong bao loi phia tren.
    pause
    exit /b 1
)

echo done > .installed
echo.
echo ============================================================
echo   CAI DAT HOAN TAT! Ban co the chay tool bang file run.bat
echo ============================================================
pause
