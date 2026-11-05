# Runs a Meitav operation for the user and program type in user_and_program_type.txt
# (next to this script). The per-operation scripts in this folder call this one, e.g.:
#   .\buy_leveraged.ps1
param(
    [Parameter(Mandatory = $true)]
    [string]$Operation
)

$ErrorActionPreference = "Stop"

$settingsFile = Join-Path $PSScriptRoot "user_and_program_type.txt"
$settings = @{}
foreach ($line in Get-Content $settingsFile) {
    $line = $line.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    $key, $value = $line -split "=", 2
    if ($null -eq $value) { throw "Expected 'key = value' in $settingsFile, got '$line'" }
    $settings[$key.Trim().ToLower()] = $value.Trim()
}
foreach ($key in "user", "program_type") {
    if (-not $settings.ContainsKey($key)) { throw "$settingsFile has no '$key' line" }
}
$user = $settings["user"]
$programType = $settings["program_type"]

# scripts -> meitav -> utilities -> repo root
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

Write-Host "Running $Operation for user $user, program type $programType" -ForegroundColor Cyan
Push-Location $repoRoot
try {
    & $python -m utilities.meitav.run_operation $Operation $user $programType
    $exitCode = $LASTEXITCODE
} catch {
    # Python itself couldn't be started (e.g. no .venv and no real python on PATH).
    Write-Host "Could not run $python : $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    Pop-Location
}
if ($exitCode -ne 0) {
    Write-Host "$Operation failed (exit code $exitCode)" -ForegroundColor Red
    exit $exitCode
}
Write-Host "$Operation finished" -ForegroundColor Green
