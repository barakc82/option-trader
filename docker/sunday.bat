@echo off
setlocal enabledelayedexpansion

set LOGFILE=%TEMP%\option-trader.log

@echo off
set VM_NAME=medium-sc

gcloud compute ssh %VM_NAME% --command "sudo docker exec option-trader /home/ibgateway/ibc/stop.sh"  | echo ibgateway is not running
gcloud compute ssh %VM_NAME% --command "nohup sudo docker exec option-trader /home/ibgateway/scripts/run.sh > /dev/null 2>&1 < /dev/null & disown"