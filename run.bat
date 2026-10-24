@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import PySide6" 2>nul
if errorlevel 1 (
    echo [!] 缺少依赖，请先执行：pip install -r requirements.txt
    pause
    exit /b 1
)

where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "clock_creature.py"
) else (
    start "" pythonw "clock_creature.py"
)
