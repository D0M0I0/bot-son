@echo off
rem Construit dist\BotSon.exe (un seul fichier, sans console)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name BotSon ^
  --icon icon.ico ^
  --collect-all customtkinter ^
  --collect-all discord ^
  --collect-all davey ^
  --hidden-import pystray._win32 ^
  --hidden-import keyring.backends.Windows ^
  app.py
pause
