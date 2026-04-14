@echo off
set VM_NAME=medium-sc
set MAX_RETRIES=3
set RETRY_DELAY=5
set ATTEMPT=1

REM Try to call docker info
call docker info

REM Check if the previous command failed (non-zero ERRORLEVEL)
if ERRORLEVEL 1 (
    echo Docker is not running. Starting Docker Desktop...
    start "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"
) else (
    echo Docker is running.
)

docker build -t option-trader-image .
if %ERRORLEVEL% neq 0 (
    echo Error occurred when building the image! Exiting...
    exit /b %ERRORLEVEL%
)

docker save -o option-trader-image.tar option-trader-image:latest
del /f /q .tmp-option-trader-image.tar*

:send_files
echo Sending tar file to VM...
call gcloud compute scp option-trader-image.tar option-trader.env %VM_NAME%:/home/barakc82

set EXITCODE=%ERRORLEVEL%
if %EXITCODE%==0 (
    echo Files sent successfully.
    goto :send_files_successful
) else (
    echo Failed to send file
    if !ATTEMPT! LSS %MAX_RETRIES% (
        echo Network error detected. Retrying in %RETRY_DELAY% seconds...
        timeout /t %RETRY_DELAY% /nobreak >nul
        set /a ATTEMPT+=1
        goto send_files
    )
)


if %ERRORLEVEL% neq 0 (
    echo Error occurred when sending files to %VM_NAME%! Exiting...
    exit /b %ERRORLEVEL%
)

:send_files_successful
gcloud compute ssh %VM_NAME% --command "sudo docker cp option-trader:/home/option-trader/cache /tmp && sudo docker rm -f option-trader && sudo docker system prune -af && cd ../barakc82 && sudo docker load -i option-trader-image.tar && sudo docker run -d --env-file /home/barakc82/option-trader.env -p 8050:8050 -v /home/barakc82/logs:/home/option-trader/logs --name option-trader option-trader-image && sudo docker cp /tmp/cache/. option-trader:/home/option-trader/cache"
