@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Abre el bot mostrando errores en esta ventana.
".venv\Scripts\python.exe" app.py
pause
