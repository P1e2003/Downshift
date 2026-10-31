: <<"::CMDLITERAL"
@ECHO OFF
GOTO :CMDSCRIPT
::CMDLITERAL
exec sh "$(dirname "$0")/run_hook.sh" "$@"
:CMDSCRIPT
SETLOCAL EnableDelayedExpansion
SET "SCRIPT_DIR=%~dp0"
SET "TARGET_ARG=%~1"
IF "%TARGET_ARG%"=="" GOTO :FAIL_OPEN

IF EXIST "%TARGET_ARG%" (
    SET "RESOLVED_SCRIPT=%TARGET_ARG%"
) ELSE (
    FOR %%F IN ("%TARGET_ARG%") DO SET "BASE_NAME=%%~nxF"
    SET "RESOLVED_SCRIPT=%SCRIPT_DIR%!BASE_NAME!"
)

IF NOT EXIST "!RESOLVED_SCRIPT!" GOTO :FAIL_OPEN

WHERE uv >NUL 2>NUL
IF !ERRORLEVEL! EQU 0 (
    uv run --no-project python "!RESOLVED_SCRIPT!"
    EXIT /B 0
)

IF EXIST "%USERPROFILE%\.local\bin\uv.exe" (
    "%USERPROFILE%\.local\bin\uv.exe" run --no-project python "!RESOLVED_SCRIPT!"
    EXIT /B 0
)

IF EXIST "%USERPROFILE%\.cargo\bin\uv.exe" (
    "%USERPROFILE%\.cargo\bin\uv.exe" run --no-project python "!RESOLVED_SCRIPT!"
    EXIT /B 0
)

FOR /F "delims=" %%P IN ('WHERE python3 2^>NUL') DO (
    ECHO "%%P" | FINDSTR /I "WindowsApps" >NUL
    IF !ERRORLEVEL! NEQ 0 (
        "%%P" "!RESOLVED_SCRIPT!"
        EXIT /B 0
    )
)

FOR /F "delims=" %%P IN ('WHERE python 2^>NUL') DO (
    ECHO "%%P" | FINDSTR /I "WindowsApps" >NUL
    IF !ERRORLEVEL! NEQ 0 (
        "%%P" "!RESOLVED_SCRIPT!"
        EXIT /B 0
    )
)

WHERE py >NUL 2>NUL
IF !ERRORLEVEL! EQU 0 (
    py -3 "!RESOLVED_SCRIPT!"
    EXIT /B 0
)

:FAIL_OPEN
ECHO "%TARGET_ARG%" | FINDSTR /I "pre_tool_use" >NUL
IF !ERRORLEVEL! EQU 0 (
    ECHO {"decision":"allow"}
) ELSE (
    ECHO {}
)
EXIT /B 0

