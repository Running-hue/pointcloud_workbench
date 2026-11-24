@echo off
chcp 65001 >nul
call "%~dp0scripts\启动点云工作台.bat" %*
exit /b %errorlevel%
