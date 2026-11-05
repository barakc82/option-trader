# Runs audit_buy_transaction for the user and program type in user_and_program_type.txt.
& (Join-Path $PSScriptRoot "run_operation.ps1") -Operation audit_buy_transaction
exit $LASTEXITCODE
