# Runs update_spreadsheet for the user and program type in user_and_program_type.txt.
& (Join-Path $PSScriptRoot "run_operation.ps1") -Operation update_spreadsheet
exit $LASTEXITCODE
