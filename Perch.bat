@echo off
if exist "%~dp0dist\Perch\Perch.exe" (
  start "Perch" "%~dp0dist\Perch\Perch.exe"
) else (
  "%~dp0.venv\Scripts\pythonw.exe" "%~dp0perch.py"
)
