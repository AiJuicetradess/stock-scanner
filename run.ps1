# Launch the full-universe 52-week low scanner TUI
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt -q
# Loads .env from this folder via scanner.config
python -m scanner @args
