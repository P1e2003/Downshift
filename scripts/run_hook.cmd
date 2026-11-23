: <<"::CMDLITERAL"
@ECHO OFF
GOTO :CMDSCRIPT
::CMDLITERAL
exec sh "$(dirname "$0")/run_hook.sh" "$@"
:CMDSCRIPT
SETLOCAL DisableDelayedExpansion
SET "SCRIPT_DIR=%~dp0"
SET "TARGET_ARG=%~1"
IF "%TARGET_ARG%"=="" GOTO :FAIL_OPEN

SET "EXTRA_ARGS="
:COLLECT_ARGS
SHIFT
IF ""%1""=="""" GOTO :ARGS_DONE
SET EXTRA_ARGS=%EXTRA_ARGS% %1
GOTO :COLLECT_ARGS
:ARGS_DONE

IF EXIST "%TARGET_ARG%" (
    SET "RESOLVED_SCRIPT=%TARGET_ARG%"
) ELSE (
    FOR %%F IN ("%TARGET_ARG%") DO SET "RESOLVED_SCRIPT=%SCRIPT_DIR%%%~nxF"
)

IF NOT EXIST "%RESOLVED_SCRIPT%" GOTO :FAIL_OPEN

WHERE uv >NUL 2>NUL
IF NOT ERRORLEVEL 1 (
    uv run --no-project python "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
    EXIT /B 0
)

IF EXIST "%USERPROFILE%\.local\bin\uv.exe" (
    "%USERPROFILE%\.local\bin\uv.exe" run --no-project python "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
    EXIT /B 0
)

IF EXIST "%USERPROFILE%\.cargo\bin\uv.exe" (
    "%USERPROFILE%\.cargo\bin\uv.exe" run --no-project python "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
    EXIT /B 0
)

FOR /F "delims=" %%P IN ('WHERE python3 2^>NUL') DO (
    ECHO "%%P" | FINDSTR /I "WindowsApps" >NUL
    IF ERRORLEVEL 1 (
        "%%P" "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
        EXIT /B 0
    )
)

FOR /F "delims=" %%P IN ('WHERE python 2^>NUL') DO (
    ECHO "%%P" | FINDSTR /I "WindowsApps" >NUL
    IF ERRORLEVEL 1 (
        "%%P" "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
        EXIT /B 0
    )
)

WHERE py >NUL 2>NUL
IF NOT ERRORLEVEL 1 (
    py -3 "%RESOLVED_SCRIPT%"%EXTRA_ARGS%
    EXIT /B 0
)

:FAIL_OPEN
ECHO "%TARGET_ARG%" | FINDSTR /I "pre_tool_use" >NUL
IF NOT ERRORLEVEL 1 (
    ECHO {"decision":"allow"}
) ELSE (
    ECHO {}
)
EXIT /B 0

