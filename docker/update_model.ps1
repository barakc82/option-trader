# --- Configuration ---
$vmName = "medium-sc"
$containerName = "option-trader"
$containerPath = "/home/option-trader/cache/close_events.csv"              # Path inside the Docker container
$vmTempPath = "/tmp/transfer_temp.csv"            # Temporary staging path on the VM
$localDest = "machine_learning\data\options_data.csv" # Where to save on your Windows PC

# --- Execution ---
Write-Host "1. Extracting CSV from Docker to VM host..."
gcloud compute ssh $vmName --command="sudo docker cp ${containerName}:${containerPath} $vmTempPath"

Write-Host "2. Downloading CSV to Windows..."
gcloud compute scp "${vmName}:${vmTempPath}" $localDest

Write-Host "3. Cleaning up temporary file on VM..."
gcloud compute ssh $vmName --command="sudo rm -rf $vmTempPath"

Write-Host "Transfer complete!"