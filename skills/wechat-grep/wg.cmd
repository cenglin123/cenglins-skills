@echo off
rem wechat-grep wrapper: pass all args to weflow.exe (UTF-8 output)
chcp 65001 >nul
"%~dp0bin\weflow.exe" %*
