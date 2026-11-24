@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] Installing packages...
python -m pip install --upgrade pip
python -m pip install pyautogui opencv-python mss numpy rapidocr_onnxruntime websocket-client cryptography certifi pyinstaller
if errorlevel 1 goto fail

echo [2/3] Building...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name RoBot ^
  --collect-all rapidocr_onnxruntime --collect-binaries onnxruntime --hidden-import browser_cdp --hidden-import websocket --hidden-import updater --hidden-import licensing --hidden-import minimap --hidden-import boss --hidden-import report --hidden-import certifi --collect-data certifi gui.py
if errorlevel 1 goto fail

echo [3/3] Copying files...
rem 移除打包進來的舊版 C++ 執行階段，改用系統內較新的版本（避免 onnxruntime 當掉）
del /q dist\RoBot\_internal\msvcp140*.dll 2>nul
copy /y bot_config.json dist\RoBot\ >nul
copy /y *.txt dist\RoBot\ >nul
xcopy /y /i /q templates dist\RoBot\templates >nul
echo.
echo Done! Run dist\RoBot\RoBot.exe
explorer dist\RoBot
pause
exit /b 0

:fail
echo.
echo Build failed. Check the messages above.
pause
exit /b 1
