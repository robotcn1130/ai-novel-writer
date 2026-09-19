@echo off
setlocal enabledelayedexpansion
title AI 小说创作系统 v6.0

set "ROOT=%~dp0"
cd /d "%ROOT%"

REM ---- 定位 Python ----
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  for %%V in (3.13.12 3.13.14 3.12.10 3.11.9) do (
    if not defined PY (
      if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%V\python.exe" (
        set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%V\python.exe"
      )
    )
  )
)
if not defined PY (
  echo [错误] 没找到 Python。请先安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)

REM ---- 数据库不存在则自动建库（幂等） ----
if not exist "%ROOT%sql\novel.db" (
  echo   首次运行，正在初始化数据库...
  "%PY%" "%ROOT%src_v6\core\migrate.py"
  if errorlevel 1 (
    echo [错误] 数据库初始化失败。
    pause
    exit /b 1
  )
)

echo ============================================================
echo   AI 小说创作系统 v6.0 . 世界模拟范式
echo ============================================================
echo   创作台：http://127.0.0.1:8787
echo   浏览器会自动打开；关闭本窗口即可停止服务。
echo.
echo   首次使用请进「设置」配置 AI 服务商与模型档位。
echo ============================================================
echo.

"%PY%" "%ROOT%src_v6\webui\server.py" --port 8787 --open

echo.
echo 服务已停止。
pause
