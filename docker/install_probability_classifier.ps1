# --- Configuration ---
$vmName = "medium-sc"
$containerName = "option-trader"
$localSource = "machine_learning\model\probability_classifier.pkl" # File on your Windows PC
$vmTempPath = "/tmp/probability_classifier.pkl"                     # Temporary staging path on the VM
$containerDir = "/home/option-trader/machine_learning"              # Directory inside the Docker container
$containerPath = "$containerDir/probability_classifier.pkl"         # Path inside the Docker container

# --- Execution ---
Write-Host "1. Uploading pkl file to VM host..."
gcloud compute scp $localSource "${vmName}:${vmTempPath}"

Write-Host "2. Ensuring the target directory exists inside the container..."
gcloud compute ssh $vmName --command="sudo docker exec $containerName mkdir -p $containerDir"

Write-Host "3. Copying pkl file from VM into Docker container..."
gcloud compute ssh $vmName --command="sudo docker cp $vmTempPath ${containerName}:${containerPath}"

Write-Host "4. Cleaning up temporary file on VM..."
gcloud compute ssh $vmName --command="sudo rm -rf $vmTempPath"

Write-Host "Install complete!"
