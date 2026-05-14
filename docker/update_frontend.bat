@echo off
set VM_NAME=medium-sc

echo Building Angular app...
cd frontend
call npm run build
cd ..

echo Creating archive of frontend files...
tar -czf frontend.tar.gz -C frontend/dist/option-trader-dashboard .

echo Sending archive to VM...
call gcloud compute scp frontend.tar.gz %VM_NAME%:/home/barakc82/

if %ERRORLEVEL% neq 0 (
    echo Error occurred when sending files to %VM_NAME%! Exiting...
    if exist frontend.tar.gz del frontend.tar.gz
    exit /b %ERRORLEVEL%
)

echo Extracting and updating frontend in Docker container...
gcloud compute ssh %VM_NAME% --command "mkdir -p /home/barakc82/frontend && tar -xzf /home/barakc82/frontend.tar.gz -C /home/barakc82/frontend/ && sudo docker cp /home/barakc82/frontend/. option-trader:/home/option-trader/frontend/"

echo Cleaning up...
if exist frontend.tar.gz del frontend.tar.gz
gcloud compute ssh %VM_NAME% --command "rm -rf /home/barakc82/frontend /home/barakc82/frontend.tar.gz"

echo Frontend update complete.
