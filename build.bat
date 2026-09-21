@echo off
REM ============================================================
REM AITerminal Windows 打包脚本
REM 需要先安装依赖: pip install -r requirements.txt pyinstaller
REM ============================================================
setlocal

echo [1/5] 检查 Python 环境...
python --version
if errorlevel 1 (
    echo 未找到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

echo [2/5] 安装/更新依赖...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] 安装 PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto :fail

echo [4/5] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist AITerminal.spec del /q AITerminal.spec

echo [5/5] 开始打包（单文件、无控制台）...
python -m PyInstaller ^
    --name=AITerminal ^
    --onefile ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --add-data "resources/styles/dark.qss;resources/styles" ^
    --hidden-import paramiko ^
    --hidden-import serial ^
    --hidden-import anthropic ^
    --hidden-import pypdf ^
    --collect-submodules pypdf ^
    main.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo 打包完成: dist\AITerminal.exe
echo ============================================================
pause
exit /b 0

:fail
echo.
echo 打包失败，请检查上面的错误信息。
pause
exit /b 1
