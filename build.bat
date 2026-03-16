@echo off
echo ============================================
echo  Macro Recorder - EXE Builder
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Download from https://python.org
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
pip install pyinstaller pynput pillow opencv-python numpy pystray --quiet

echo.
echo [2/3] Building exe (this takes 30-60 seconds)...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "MacroRecorder" ^
    --icon NONE ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    --hidden-import PIL._tkinter_finder ^
    --collect-all pynput ^
    --collect-all PIL ^
    --exclude-module matplotlib ^
    --exclude-module scipy ^
    --exclude-module pandas ^
    --exclude-module IPython ^
    --exclude-module jupyter ^
    macro_recorder.py

echo.
echo [3/3] Done!
if exist "dist\MacroRecorder.exe" (
    echo SUCCESS: dist\MacroRecorder.exe is ready.
    echo File size:
    dir /b /s dist\MacroRecorder.exe | findstr MacroRecorder
    echo.
    echo Opening dist folder...
    explorer dist
) else (
    echo ERROR: Build failed. Check output above for details.
)
pause
