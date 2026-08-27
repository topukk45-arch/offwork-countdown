@echo off
cd /d "%~dp0"

echo ================ ENV CHECK ================
python -c "import sys,struct;print(sys.executable);print(struct.calcsize(chr(80))*8,'bit')"
if errorlevel 1 goto :nopython
echo.

echo ================ 1/4 DEPS ================
python -m pip install pillow pystray pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.

echo ================ 2/4 SMOKE TEST ================
echo Close the widget (tray icon - Exit) to continue.
python widget.py
if errorlevel 1 goto :smokefail
echo.

echo ================ 3/4 BUILD ================
python -m PyInstaller --noconfirm --clean --onedir --noconsole --name OffworkWidget --icon app.ico --add-data "app.ico;." --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder widget.py
if not exist "dist\OffworkWidget\OffworkWidget.exe" goto :buildfail
echo.

echo ================ 4/4 ZIP ================
powershell -NoProfile -Command "Compress-Archive -Path 'dist\OffworkWidget' -DestinationPath 'dist\OffworkWidget.zip' -Force"
echo.
echo DONE
echo   folder :  dist\OffworkWidget\
echo   zip    :  dist\OffworkWidget.zip   ^(send this to friends^)
for %%A in ("dist\OffworkWidget.zip") do echo   size   :  %%~zA bytes
goto :end

:buildfail
echo FAILED - scroll up for the error.
goto :end

:smokefail
echo Smoke test failed - fix the script before packaging.
goto :end

:nopython
echo Python not found in PATH.

:end
pause
