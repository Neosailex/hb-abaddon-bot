@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Instalando HB Abaddon Bot
echo.
echo  === HB Abaddon Bot: instalacion ===
echo.
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY py -3.13 --version >nul 2>&1 && set "PY=py -3.13"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo No encontre Python. Lo instalo con winget ^(Python 3.12^)...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo Listo. Cerra esta ventana y abri instalar.bat de nuevo.
  pause
  exit /b 1
)
echo Usando: %PY%
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo No pude crear el entorno. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Fallo la instalacion de paquetes. & pause & exit /b 1)
echo.
echo  Instalado. Abri iniciar.bat ^(o iniciar_admin.bat si el juego no responde^).
echo.
pause
