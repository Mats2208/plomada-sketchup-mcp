@echo off
rem Starts a second, isolated SketchUp 2025 for Plomada development: the
rem extension loads from this worktree, listens on 7884 and keeps its token,
rem endpoint and audit log in %LOCALAPPDATA%\Plomada-dev. The SketchUp that is
rem already open (port 7883, %LOCALAPPDATA%\Plomada) is not touched.
rem With no argument it opens a fresh copy of an empty model, so the new
rem instance starts on it instead of the welcome screen: dev-empty-2025.skp
rem in PLOMADA_HOME when it exists (the stock template was saved by a newer
rem SketchUp and stops on a version warning), else the stock template.
setlocal
set "PLOMADA_DEV_DIR=C:\mcp\plomada-wt-06\extension\plomada"
set "PLOMADA_PORT=7884"
set "PLOMADA_HOME=%LOCALAPPDATA%\Plomada-dev"
set "SU_DIR=C:\Program Files\SketchUp\SketchUp 2025\SketchUp"
set "MODEL=%~1"
if "%MODEL%"=="" (
  if not exist "%PLOMADA_HOME%" mkdir "%PLOMADA_HOME%"
  if exist "%PLOMADA_HOME%\dev-empty-2025.skp" (
    copy /Y "%PLOMADA_HOME%\dev-empty-2025.skp" "%PLOMADA_HOME%\dev-empty.skp" >nul
  ) else (
    copy /Y "%SU_DIR%\resources\en-US\Templates\Temp01a - Simple.skp" "%PLOMADA_HOME%\dev-empty.skp" >nul
  )
  set "MODEL=%PLOMADA_HOME%\dev-empty.skp"
)
start "" "%SU_DIR%\SketchUp.exe" "%MODEL%"
endlocal
