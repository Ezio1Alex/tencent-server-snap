@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title 腾讯云秒杀 - 一键启动
cd /d "%~dp0"

echo ============================================
echo   [第1步/共2步] 打开浏览器登录，请扫码后等待
echo ============================================
echo.
python get_cookies.py
if errorlevel 1 goto :error

if not exist cookies.json goto :error
if not exist csrf_token.txt goto :error
for %%F in (csrf_token.txt) do if %%~zF EQU 0 goto :error

echo.
echo ============================================
echo   [第2步/共2步] 启动抢购脚本...
echo ============================================
echo.
python snap_up_server.py

echo.
echo 抢购脚本已退出。
pause
exit /b 0

:error
echo.
echo [错误] Cookie 或 CSRF Token 生成失败，请重新登录后重试
pause
exit /b 1
