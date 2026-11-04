@echo off
cd /d "%~dp0"
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set MKL_NUM_THREADS=1
if exist "..\..\work\mano-env\Scripts\python.exe" (
  "..\..\work\mano-env\Scripts\python.exe" server.py --open
  goto done
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py --open
) else (
  python server.py --open
)
:done
pause
