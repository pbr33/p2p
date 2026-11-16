# P2P showcase on any Windows laptop - double-click START-SHOWCASE.cmd, or:
#   .\start-showcase.ps1            set up on first run, then start mock systems + cockpit
#   .\start-showcase.ps1 -Reset     also restore the demo story (drift in QA, failed UAT case)
#   .\start-showcase.ps1 -Stop      stop the mock systems
# Needs Python 3.11+ (offers to install it with winget) and internet access to Claude.
param(
    [switch]$Reset,
    [switch]$Stop,
    [string]$FoundryKey = "",     # optional: skip the key prompt
    [switch]$NoBrowser
)
$ErrorActionPreference = "Continue"   # native tools (pip) write warnings to stderr; exit codes are checked instead
$root = $PSScriptRoot
Set-Location $root
$mockUrl = "http://127.0.0.1:8765"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Up($url) { try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null; $true } catch { $false } }

if ($Stop) {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like "*mock_server.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped mock systems (pid $($_.ProcessId))" }
    exit 0
}

# 1. Python 3.11+
Step "Checking Python"
$python = $null
foreach ($candidate in "py -3.13", "py -3.12", "py -3.11", "python") {
    $exe, $pyArgs = $candidate -split " "
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $v = & $exe @($pyArgs) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -and [version]"$v" -ge [version]"3.11") { $python = @($exe) + @($pyArgs | Where-Object { $_ }); break }
    } catch {}
}
if (-not $python) {
    Write-Host "Python 3.11 or newer is needed." -ForegroundColor Yellow
    if ((Read-Host "Install Python 3.12 now with winget? (y/n)") -eq "y") {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        Write-Host "Python installed. Close this window and start the showcase again." -ForegroundColor Green
    } else {
        Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
    }
    exit 1
}
Write-Host "using $($python -join ' ') ($v)"

# 2. Virtual environment and packages (first run, or after an update)
$venvPy = "$root\.venv\Scripts\python.exe"
$marker = "$root\.venv\.installed"
if (-not (Test-Path $venvPy)) {
    Step "Creating the Python environment (first run only)"
    & $python[0] @($python | Select-Object -Skip 1) -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { Write-Host "could not create the Python environment" -ForegroundColor Red; exit 1 }
}
if (-not (Test-Path $marker) -or (Get-Item "$root\pyproject.toml").LastWriteTime -gt (Get-Item $marker).LastWriteTime) {
    Step "Installing packages (first run only, about a minute)"
    & $venvPy -m pip install --disable-pip-version-check -q -e $root
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Retrying with the Windows certificate store (corporate proxy)..." -ForegroundColor Yellow
        & $venvPy -m pip install --disable-pip-version-check -q --use-feature=truststore -e $root
        if ($LASTEXITCODE -ne 0) { Write-Host "package installation failed - check the internet/proxy connection" -ForegroundColor Red; exit 1 }
    }
    Set-Content $marker (Get-Date)
}

# 3. Claude access (demo\.env, never committed)
$envFile = "$root\demo\.env"
$hasKey = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^(ANTHROPIC_FOUNDRY_API_KEY|ANTHROPIC_API_KEY)=\S+' -Quiet)
if (-not $hasKey) {
    Step "Claude access"
    if (-not $FoundryKey) {
        Write-Host "The Copilot uses Claude on the company's Azure AI Foundry (ella-claude-sonnet)."
        Write-Host "Paste the Foundry API key (or press Enter to skip - everything except the Copilot/AI features works without it):"
        $secure = Read-Host -AsSecureString
        $FoundryKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    if ($FoundryKey) {
        @(
            "ANTHROPIC_FOUNDRY_BASE_URL=https://ella-claude-sonnet.services.ai.azure.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY=$FoundryKey",
            "P2P_MODEL=claude-opus-4-8"
        ) | Set-Content -Path $envFile -Encoding ascii
        Write-Host "saved to demo\.env"
    }
}

# 4. Mock Coupa / ServiceNow systems
if (-not (Up $mockUrl)) {
    Step "Starting the mock Coupa and ServiceNow systems"
    Start-Process -FilePath $venvPy -ArgumentList "`"$root\demo\mock_server.py`"" -WorkingDirectory $root -WindowStyle Minimized
    $t = 0
    while (-not (Up $mockUrl)) {
        Start-Sleep -Milliseconds 500
        if (($t += 1) -gt 30) { Write-Host "the mock systems did not start (is port 8765 in use?)" -ForegroundColor Red; exit 1 }
    }
    $Reset = $true   # fresh mock systems: the demo state must match them
}

# 5. Demo story
if ($Reset -or -not (Test-Path "$root\rollout")) {
    Step "Preparing the demo story (about 30 seconds)"
    & "$root\demo\stage-wow-demo.ps1" | Out-Host
}

# 6. Cockpit
Step "Starting the Go-Live Cockpit on http://127.0.0.1:8770  (close this window or Ctrl+C to stop)"
$cockpitArgs = @("cockpit")
if (-not $NoBrowser) { $cockpitArgs += "--open" }
& "$root\demo\p2p.ps1" @cockpitArgs
