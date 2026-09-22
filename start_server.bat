@echo off
echo Installing unified server dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Failed to install dependencies. Make sure python is in your PATH.
    pause
    exit /b %errorlevel%
)

echo Starting Unified Mapping Agent Server...
python main.py
pause
