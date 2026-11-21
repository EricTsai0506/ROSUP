@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import rapidocr_onnxruntime, pyautogui, mss, websocket" 2>nul
if errorlevel 1 (
  echo Installing packages, first time only...
  python -m pip install pyautogui opencv-python mss numpy rapidocr_onnxruntime websocket-client
)
start "" pythonw gui.py
