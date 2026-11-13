@echo off
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0iniciar.bat' -WorkingDirectory '%~dp0' -Verb RunAs"
