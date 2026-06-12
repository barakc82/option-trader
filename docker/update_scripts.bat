@echo off
set VM_NAME=medium-sc

echo Creating archive of python scripts...
tar --exclude="__pycache__" -czf scripts.tar.gz app utilities

echo Sending archive to VM...
call gcloud compute scp scripts.tar.gz %VM_NAME%:/home/barakc82/

if %ERRORLEVEL% neq 0 (
    echo Error occurred when sending files to %VM_NAME%! Exiting...
    if exist scripts.tar.gz del scripts.tar.gz
    exit /b %ERRORLEVEL%
)

echo Extracting and updating scripts in Docker container...
call gcloud compute ssh %VM_NAME% --command "tar -xzf /home/barakc82/scripts.tar.gz -C /home/barakc82/ && sudo docker cp /home/barakc82/app option-trader:/home/option-trader/ && sudo docker cp /home/barakc82/utilities option-trader:/home/option-trader/ && sudo docker exec option-trader sh -c 'echo \"{\\\"should_restart_option_trader\\\": 1}\" > /home/option-trader/config/supervisor_config.json'"

echo Cleaning up...
if exist scripts.tar.gz del scripts.tar.gz

echo Update complete. The supervisor should restart the app shortly.
echo The current time is: %TIME%
