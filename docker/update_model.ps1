# Stop execution if any command fails
$ErrorActionPreference = "Stop"

Write-Host "1/3 Running fetch_options_data.ps1..." -ForegroundColor Cyan
.\docker\fetch_options_data.ps1

Write-Host "2/3 Running Python probability model selection..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -u -m app.machine_learning.select_probability_model

Write-Host "3/3 Running install_probability_classifier.ps1..." -ForegroundColor Cyan
.\docker\install_probability_classifier.ps1

Write-Host "All scripts executed successfully!" -ForegroundColor Green