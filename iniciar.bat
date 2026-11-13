@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Primero ejecuta instalar.bat
  pause
  exit /b 1
)
echo Buscando actualizaciones...
".venv\Scripts\python.exe" actualizar.py
start "" ".venv\Scripts\pythonw.exe" app.py
