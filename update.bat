@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Update Tool Tu GitHub - Auto Login ^& Register Tool
cd /d "%~dp0"

set "REPO_URL=https://github.com/thincole/RegSP.git"
set "ZIP_URL=https://github.com/thincole/RegSP/archive/refs/heads/main.zip"

echo ============================================================
echo        CAP NHAT PHAN MEM TU GITHUB
echo        Nguon: https://github.com/thincole/RegSP
echo ============================================================
echo.

set "OLD_VER=Chua xac dinh"
if exist "VERSION" (
    set /p OLD_VER=<VERSION
)
echo [INFO] Phien ban hien tai tren may: !OLD_VER!
echo.

:: 1. Sao luu cau hinh ca nhan (settings.json, .env, service_account.json)
set "BACKUP_DIR=%TEMP%\regsp_backup_%RANDOM%"
mkdir "%BACKUP_DIR%" >nul 2>&1
if exist "settings.json" copy /y "settings.json" "%BACKUP_DIR%\settings.json" >nul
if exist ".env" copy /y ".env" "%BACKUP_DIR%\.env" >nul
if exist "service_account.json" copy /y "service_account.json" "%BACKUP_DIR%\service_account.json" >nul

:: 2. Kiem tra Git de cap nhat (neu khong co Git thi dung PowerShell tai ZIP)
git --version >nul 2>&1
if not errorlevel 1 (
    echo [1/3] Dang ket noi GitHub qua Git...
    if not exist ".git" (
        git init >nul
        git branch -M main >nul 2>&1
        git remote add origin "%REPO_URL%" >nul 2>&1
    ) else (
        git remote get-url origin >nul 2>&1
        if errorlevel 1 (
            git remote add origin "%REPO_URL%" >nul 2>&1
        ) else (
            git remote set-url origin "%REPO_URL%" >nul 2>&1
        )
    )

    echo [2/3] Dang tai ban cap nhat moi nhat tu GitHub...
    git fetch origin main
    if errorlevel 1 (
        echo [LOI] Khong the ket noi toi GitHub! Vui long kiem tra mang Internet.
        goto :restore_and_fail
    )
    git reset --hard origin/main
    if errorlevel 1 (
        echo [LOI] Cap nhat code that bai!
        goto :restore_and_fail
    )
) else (
    echo [1/3] May chua cai Git, chuyen sang tai ban cap nhat truc tiep qua PowerShell...
    echo [2/3] Dang tai va giai nen ban moi nhat tu GitHub...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference = 'Stop'; " ^
        "$zip = Join-Path $env:TEMP 'regsp_update.zip'; " ^
        "$ext = Join-Path $env:TEMP 'regsp_extract'; " ^
        "if (Test-Path $zip) { Remove-Item $zip -Force }; " ^
        "if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }; " ^
        "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile $zip; " ^
        "Expand-Archive -Path $zip -DestinationPath $ext -Force; " ^
        "$inner = Get-ChildItem -Path $ext -Directory | Select-Object -First 1; " ^
        "Copy-Item -Path (Join-Path $inner.FullName '*') -Destination (Get-Location) -Recurse -Force; " ^
        "Remove-Item $zip -Force -ErrorAction SilentlyContinue; " ^
        "Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue"
    if errorlevel 1 (
        echo [LOI] Tai ban cap nhat tu GitHub that bai!
        goto :restore_and_fail
    )
)

echo.
echo [3/3] Kiem tra va bo sung thu vien Python (neu co)...
python --version >nul 2>&1
if not errorlevel 1 (
    if exist "requirements.txt" python -m pip install -r requirements.txt --quiet
)

:: 3. Khoi phuc cau hinh ca nhan
if exist "%BACKUP_DIR%\settings.json" copy /y "%BACKUP_DIR%\settings.json" "settings.json" >nul
if exist "%BACKUP_DIR%\.env" copy /y "%BACKUP_DIR%\.env" ".env" >nul
if exist "%BACKUP_DIR%\service_account.json" copy /y "%BACKUP_DIR%\service_account.json" "service_account.json" >nul
rmdir /s /q "%BACKUP_DIR%" >nul 2>&1

set "NEW_VER=Chua xac dinh"
if exist "VERSION" (
    set /p NEW_VER=<VERSION
)

echo.
echo ============================================================
echo   CAP NHAT HOAN TAT!
echo   Phien ban truoc : !OLD_VER!
echo   Phien ban moi   : !NEW_VER!
echo ============================================================
if /i not "%~1"=="--no-pause" pause
exit /b 0

:restore_and_fail
if exist "%BACKUP_DIR%\settings.json" copy /y "%BACKUP_DIR%\settings.json" "settings.json" >nul
if exist "%BACKUP_DIR%\.env" copy /y "%BACKUP_DIR%\.env" ".env" >nul
if exist "%BACKUP_DIR%\service_account.json" copy /y "%BACKUP_DIR%\service_account.json" "service_account.json" >nul
rmdir /s /q "%BACKUP_DIR%" >nul 2>&1
if /i not "%~1"=="--no-pause" pause
exit /b 1
