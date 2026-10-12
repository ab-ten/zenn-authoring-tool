@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run from the ARTICLE repository root, not the tool repository root.
rem The Python CLI still receives an explicit --article-root.
set "TOOL_ROOT=%~dp0"
set "ARTICLE_ROOT=%CD%"

if /I "%~1"=="preview" goto preview
if /I "%~1"=="converter" goto converter
if /I "%~1"=="checker" goto checker
goto usage

:converter
if /I "%~2"=="lint" goto publisher_readonly
if /I "%~2"=="convert" goto publisher_writable
if /I "%~2"=="--help" goto publisher_readonly
goto usage

:checker
if /I "%~2"=="init" goto publisher_writable
if /I "%~2"=="check" goto publisher_writable
if /I "%~2"=="confirm-pending-update" goto publisher_writable
if /I "%~2"=="discard-all-pendings" goto publisher_writable
if /I "%~2"=="--help" goto publisher_readonly
goto usage

:check_article_repo
if not exist "%ARTICLE_ROOT%\.git" goto invalid_repo
if not exist "%ARTICLE_ROOT%\_deploy\.git" goto invalid_repo
if not exist "%TOOL_ROOT%publisher\converter\__main__.py" goto invalid_tool
exit /b 0

:publisher_readonly
call :check_article_repo
if errorlevel 1 exit /b 2
goto run_readonly

:publisher_writable
call :check_article_repo
if errorlevel 1 exit /b 2
goto run_writable

:run_readonly
rem Keep the original quoting of the remaining CLI arguments.
set "FORWARDED=%*"
set "FORWARDED=%FORWARDED:* =%"
docker run --rm ^
  --mount "type=bind,source=%TOOL_ROOT%publisher,target=/src,readonly" ^
  --mount "type=bind,source=%ARTICLE_ROOT%,target=/article-repo,readonly" ^
  --workdir /src ^
  zenn-authoring-publisher python -m %~1 %FORWARDED% --article-root /article-repo
exit /b %ERRORLEVEL%

:run_writable
set "FORWARDED=%*"
set "FORWARDED=%FORWARDED:* =%"
docker run --rm ^
  --mount "type=bind,source=%TOOL_ROOT%publisher,target=/src,readonly" ^
  --mount "type=bind,source=%ARTICLE_ROOT%,target=/article-repo,readonly" ^
  --mount "type=bind,source=%ARTICLE_ROOT%\_deploy,target=/article-repo/_deploy" ^
  --workdir /src ^
  zenn-authoring-publisher python -m %~1 %FORWARDED% --article-root /article-repo
exit /b %ERRORLEVEL%

:preview
set "PREVIEW_LISTEN=127.0.0.1:8000"
if "%~2"=="" goto run_preview
if /I not "%~2"=="--listen" goto usage
if "%~3"=="" goto invalid_listen
if not "%~4"=="" goto usage

rem Require an explicit IPv4 host address and a port, never an implicit all-interface bind.
set "PREVIEW_LISTEN=%~3"
set "PREVIEW_HOST="
set "PREVIEW_PORT="
set "PREVIEW_EXTRA="
for /f "tokens=1-3 delims=:" %%A in ("%PREVIEW_LISTEN%") do (
  set "PREVIEW_HOST=%%A"
  set "PREVIEW_PORT=%%B"
  set "PREVIEW_EXTRA=%%C"
)
if not defined PREVIEW_HOST goto invalid_listen
if not defined PREVIEW_PORT goto invalid_listen
if defined PREVIEW_EXTRA goto invalid_listen

rem Reject shell syntax and non-numeric ports; Docker validates the IP and port range.
set "PREVIEW_BAD="
for /f "delims=0123456789." %%A in ("%PREVIEW_HOST%") do set "PREVIEW_BAD=%%A"
if defined PREVIEW_BAD goto invalid_listen
for /f "delims=0123456789" %%A in ("%PREVIEW_PORT%") do set "PREVIEW_BAD=%%A"
if defined PREVIEW_BAD goto invalid_listen

:run_preview
if not exist "%ARTICLE_ROOT%\.git" goto invalid_repo
if not exist "%ARTICLE_ROOT%\articles\" goto invalid_repo
if not exist "%ARTICLE_ROOT%\images\" goto invalid_repo
docker run --init --rm -it ^
  --publish "%PREVIEW_LISTEN%:8000" ^
  --mount "type=bind,source=%ARTICLE_ROOT%,target=/authoring,readonly" ^
  zenn-authoring-previewer
exit /b %ERRORLEVEL%

:invalid_listen
>&2 echo ERROR: --listen requires an explicit IPv4-address:port, such as 127.0.0.1:8001.
exit /b 2

:invalid_repo
>&2 echo ERROR: Run this command from an article repository root with the required .git / _deploy worktree.
exit /b 2

:invalid_tool
>&2 echo ERROR: invoke.cmd must be located in the zenn-authoring-tool repository root.
exit /b 2

:usage
>&2 echo Usage: invoke.cmd converter lint --all
>&2 echo        invoke.cmd converter lint --article articles/example.md
>&2 echo        invoke.cmd converter convert
>&2 echo        invoke.cmd checker init ZENN_USERNAME
>&2 echo        invoke.cmd checker check
>&2 echo        invoke.cmd checker confirm-pending-update ARTICLE_ID [required flags]
>&2 echo        invoke.cmd checker discard-all-pendings [required flags]
>&2 echo        invoke.cmd preview [--listen IPv4_ADDRESS:HOST_PORT]
>&2 echo Run from the ARTICLE repository root. Do not pass --article-root.
exit /b 2
