@echo off
title TrackID Ink installer
setlocal
echo.
echo  ===  TrackID Ink installer  ===
echo  This computer and the Pi must be on the same network (Wi-Fi or phone hotspot).
echo.
set /p PIUSER=Pi username (the one you set in Raspberry Pi Imager): 
set PIHOST=trackid-ink.local
set /p PIHOST=Pi address [press Enter for trackid-ink.local]: 
if "%PIHOST%"=="" set PIHOST=trackid-ink.local
set PANEL=waveshare
set /p PANEL=E-paper: type waveshare or adafruit [Enter = waveshare]: 
if "%PANEL%"=="" set PANEL=waveshare
set TIDURL=
set /p TIDURL=TrackID address, e.g. http://192.168.1.42:8080 [Enter = http://trackid.local:8080]: 
echo.
echo Connecting. Type "yes" if asked about the host key, then the Pi password.
echo Installing takes 5-10 minutes.
ssh -t -o StrictHostKeyChecking=accept-new %PIUSER%@%PIHOST% "sudo apt-get install -y -q git >/dev/null && (git -C ~/TrackID pull -q 2>/dev/null || git clone -q --depth 1 https://github.com/anubisdarkwatch/TrackID.git ~/TrackID) && cd ~/TrackID/trackid-ink && sudo bash install.sh %PANEL% && if [ -n '%TIDURL%' ]; then sed -i 's#^url *=.*#url = %TIDURL%#' config.ini; fi && echo && echo 'All done - TrackID Ink starts on its own after the reboot.' && sudo reboot"
if errorlevel 1 goto fail
echo.
echo Finished. The Pi is rebooting.
pause
exit /b 0
:fail
echo.
echo Something went wrong (or the Pi couldn't be reached at %PIHOST%).
echo  - Check both devices are on the same network.
echo  - If the address doesn't resolve, run "arp -a" in PowerShell and use the Pi's IP instead.
pause
exit /b 1
