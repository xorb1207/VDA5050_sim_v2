@echo off
REM vda5050_sim_v2 — Windows 단축 스크립트
REM 사용:
REM   run quickrun       Quickrun 라이브 시뮬 서버 (http://127.0.0.1:8765)
REM   run list           최근 실험 10개 목록
REM   run open           실험 인덱스 페이지 열기
REM   run open last      가장 최근 run report.html 열기
REM   run rebuild-index  index.html 재생성
REM   run <yaml-stem>    experiments\<yaml-stem>.yaml 실험 실행
REM
REM (Linux/Mac 의 './run' 와 동일 인터페이스)

setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "EXP_ROOT=%ROOT%\outputs\experiments"

if "%~1"=="" goto :help
if /I "%~1"=="-h" goto :help
if /I "%~1"=="--help" goto :help
if /I "%~1"=="help" goto :help

if /I "%~1"=="quickrun" (
    echo Quickrun 서버 시작: http://127.0.0.1:8765/
    start "" "http://127.0.0.1:8765/"
    python -m src.interfaces.quickrun.server
    goto :eof
)

if /I "%~1"=="list" (
    if not exist "%EXP_ROOT%" (
        echo 실험 디렉토리 없음: %EXP_ROOT%
        goto :eof
    )
    dir /B /O-D "%EXP_ROOT%" 2>nul
    goto :eof
)

if /I "%~1"=="open" (
    if "%~2"=="" (
        start "" "%EXP_ROOT%\index.html"
    ) else if /I "%~2"=="last" (
        for /F %%i in ('dir /B /O-D "%EXP_ROOT%"') do (
            start "" "%EXP_ROOT%\%%i\report.html"
            goto :eof
        )
    ) else (
        echo 알 수 없는 open 대상: %~2
    )
    goto :eof
)

if /I "%~1"=="rebuild-index" (
    python -m src.tools.build_index "%EXP_ROOT%"
    goto :eof
)

REM 기본: yaml-stem 으로 가정
set "YAML_PATH=%ROOT%\experiments\%~1.yaml"
if not exist "%YAML_PATH%" (
    echo experiments\%~1.yaml 없음.
    echo.
    echo 사용 가능한 yaml:
    if exist "%ROOT%\experiments" (
        for %%f in ("%ROOT%\experiments\*.yaml") do echo   %%~nf
    )
    exit /b 1
)
python -m src.application.usecases.experiment_runner --experiment "%YAML_PATH%" %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:help
echo vda5050_sim_v2 - Windows 단축 스크립트
echo.
echo 사용법:
echo   run quickrun       Quickrun 라이브 시뮬 서버
echo   run list           최근 실험 목록
echo   run open           실험 인덱스 페이지
echo   run open last      최근 run report.html
echo   run rebuild-index  index.html 재생성
echo   run ^<yaml-stem^>    experiments\^<yaml-stem^>.yaml 실험
endlocal
