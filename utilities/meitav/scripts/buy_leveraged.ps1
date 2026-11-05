# Runs buy_leveraged for the user and program type in user_and_program_type.txt.
& (Join-Path $PSScriptRoot "run_operation.ps1") -Operation buy_leveraged
exit $LASTEXITCODE
