# Sets up the "wow" demo state in about 30 seconds (mock server must be running):
#   - Coupa wave 1 (IN10, IN20, SG10) rolled out DEV -> QA and passed
#   - Ariba DEV waiting for its import files
#   - drift watch ran once (no drift); then someone changed GST5 by hand in QA
#   - testers have run some UAT; one case failed because of that change
# Demo: "Check drift" → Diagnose & fix → drop demo\documents\acme-wave2-company-codes.xlsx → Steering pack
# Then open the cockpit:  .\demo\p2p.ps1 cockpit --open
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$p2p = "$root\demo\p2p.ps1"
$mock = "http://127.0.0.1:8765"

& $p2p reset-demo
& $p2p rollout run --template ariba-company-code --answers demo\rollout\ariba-company-codes.csv --stages ariba-dev ariba-qa --apply | Select-String "==="
& $p2p rollout run --template coupa-company-code --answers demo\rollout\coupa-company-codes.csv --stages coupa-dev coupa-qa --apply | Out-Null
Invoke-RestMethod -Method Post "$mock/coupa-qa/demo/users" -ContentType "application/json" -Body '{"login":"apac.controller"}' | Out-Null
& $p2p rollout run --template coupa-company-code --answers demo\rollout\coupa-company-codes.csv --stages coupa-dev coupa-qa --apply | Select-String "==="

# Drift watch baseline: everything still matches
& $p2p watch | Select-Object -First 1

# Someone "quickly fixes" a tax rate by hand in QA...
Invoke-RestMethod -Method Post "$mock/coupa-qa/demo/edit" -ContentType "application/json" `
    -Body '{"type":"tax_codes","match":{"code":"GST5"},"set":{"percentage":18.0}}' | Out-Null
"manual change made in coupa-qa: GST5 set to 18%"

# ...and the testers record their UAT results in Excel
& "$root\.venv\Scripts\python.exe" -c @"
from pathlib import Path
from p2p_agents.rollout.reports import read_csv, _write_csv
p = Path('rollout/coupa-company-code-coupa-company-codes/uat-scripts.csv')
rows = read_csv(p)
results = {
    'IN10-UAT-01': ('Pass', 'Asha', ''), 'IN10-UAT-02': ('Pass', 'Asha', ''),
    'IN10-UAT-03': ('Pass', 'Ravi', ''),
    'IN10-UAT-03.2': ('Fail', 'Ravi', 'Invoice with GST5 showed 18% tax instead of 5%'),
    'IN20-UAT-01': ('Pass', 'Meena', ''), 'SG10-UAT-01': ('Pass', 'Wei', ''),
}
for r in rows:
    if r['Test ID'] in results:
        r['Result (Pass/Fail)'], r['Tester'], r['Comments'] = results[r['Test ID']]
        r['Date'] = '15-Nov-2026'
_write_csv(p, list(rows[0]), [list(r.values()) for r in rows])
print('UAT results recorded:', ', '.join(f'{k}={v[0]}' for k, v in results.items()))
"@
"ready: .\demo\p2p.ps1 cockpit --open"
