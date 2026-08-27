@echo off
cd /d "%~dp0"
echo Installing dependencies ...
python -m pip install pillow pystray -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo Done. Double-click run.vbs to start the widget.
pause
