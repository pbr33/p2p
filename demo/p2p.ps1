# Runs the p2p CLI against the demo systems. Usage (from the repo root):
#   .\demo\p2p.ps1 diff --platform coupa --source coupa-sbx --target coupa-prod
# The mock server must be running:  .\.venv\Scripts\python demo\mock_server.py
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$env:P2P_CONFIG_DIR = "$root\demo\config"
$env:P2P_SNAPSHOT_DIR = "$root\demo\snapshots"
$env:P2P_LOG_DIR = "$root\demo\logs"
$env:DEMO_COUPA_CLIENT_ID = "demo-client"
$env:DEMO_COUPA_CLIENT_SECRET = "demo-secret"
$env:SN_INSTANCE_URL = "http://127.0.0.1:8765/sn"
$env:SN_USER = "demo"
$env:SN_PASSWORD = "demo"
$env:PYTHONIOENCODING = "utf-8"

# demo\.env (not committed) holds ANTHROPIC_API_KEY for the AI commands
$envFile = "$root\demo\.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2].Trim() }
    }
}

$routingArgs = @()
if ($args.Count -gt 0 -and $args[0] -eq "triage") { $routingArgs = @("--routing", "$root\demo\config\routing.toml") }
& "$root\.venv\Scripts\p2p.exe" @args @routingArgs
exit $LASTEXITCODE
