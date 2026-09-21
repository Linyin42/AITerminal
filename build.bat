@echo off
chcp 65001 >nul
REM ============================================================
REM AITerminal 打包脚本（基于 build.spec）
REM 前提：当前 Python 环境已安装 pyinstaller 与 pillow
REM       pip install pyinstaller pillow
REM 产物：dist\AITerminal.exe（单文件，双击即用）
REM ============================================================
setlocal

echo [1/3] 检查 Python 环境...
python --version
if errorlevel 1 (
    echo 未找到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

echo [2/3] 安装打包依赖（如未安装）...
python -m pip install --quiet pyinstaller pillow
if errorlevel 1 goto :fail

echo [3/3] 开始打包（单文件 / 无控制台 / 含图标）...
python -m PyInstaller build.spec --clean --noconfirm
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo 打包完成: dist\AITerminal.exe
echo 把 AITerminal.exe 单独发给别人即可双击运行（无需安装）。
echo ============================================================
pause
exit /b 0

:fail
echo.
echo 打包失败，请检查上面的错误信息。
pause
exit /b 1
