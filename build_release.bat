@echo off
rem One step: build both DLLs and the launcher, assemble dist\, run the smoke tests, write the zip.
rem Tool paths: set SAN14_LLVM / SAN14_PY_EMBED / SAN14_PILLOW to override the defaults (see README).
cd /d "%~dp0"
python -P release\make_release.py
echo.
if errorlevel 1 (echo FAILED) else (echo OK: see the dist folder)
pause
