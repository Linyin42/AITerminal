@echo off
REM 开发环境一键启动：创建虚拟环境（首次）、安装依赖（首次）、运行程序
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo 创建虚拟环境失败，请确认已安装 Python 3.9+
        pause
        exit /b 1
    )
    echo [2/2] 安装依赖 ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败
        pause
        exit /b 1
    )
)

echo 启动 AITerminal ...
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
