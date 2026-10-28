@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=D:/miniconda3/envs/llm-proxy-lab/python.exe"
if not exist "%PY%" (
    echo [!] conda env llm-proxy-lab not found, trying py launcher
    set "PY=python"
)
echo Using: %PY%
"%PY%" start.py
echo.
pause
