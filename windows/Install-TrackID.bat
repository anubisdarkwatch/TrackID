@echo off
title TrackID v2 installer
setlocal
echo.
echo  ===  TrackID v2 installer  ===
echo  This computer and the Pi must be on the same network (Wi-Fi or phone hotspot).
echo.
set /p PIUSER=Pi username (the one you set in Raspberry Pi Imager): 
set PIHOST=trackid.local
set /p PIHOST=Pi address [press Enter for trackid.local]: 
if "%PIHOST%"=="" set PIHOST=trackid.local
set TOKEN=
set /p TOKEN=AudD API token (press Enter to add it later): 
echo.
echo Connecting. Type "yes" if asked about the host key, then the Pi password.
echo Installing takes 5-10 minutes.
ssh -t -o StrictHostKeyChecking=accept-new %PIUSER%@%PIHOST% "sudo apt-get install -y -q git >/dev/null && (git -C ~/TrackID pull -q 2>/dev/null || git clone -q --depth 1 https://github.com/anubisdarkwatch/TrackID.git ~/TrackID) && cd ~/TrackID/trackid && sudo bash install.sh && if [ -n '%TOKEN%' ]; then sed -i 's/^token *=.*/token = %TOKEN%/' config.ini && echo 'AudD token saved.'; fi && echo && echo 'All done - TrackID starts on its own after the reboot.' && sudo reboot"
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
