# P2P Agent Framework

Agentic orchestration and run-support for a multi-platform Procure-to-Pay landscape: Coupa, SAP Ariba, Process Director Accounts Payable (PDAP), ReadSoft Online / Tungsten AP Essentials and Tungsten Network.

| Agent | What it does |
|---|---|
| **ConfigReplication** | Extracts platform configuration, diffs staging against production, flags schema conflicts and missing dependencies, and replicates in dependency order with an approval gate and audit trail |
| **DocuGen** | Generates Functional Requirements (FRD) and Technical Design (TDD) documents from live configuration, and detects when they are out of date |
| **Triage & Run-Support** | Reads ServiceNow incidents, investigates with similar incidents, platform logs and configuration, posts a root-cause analysis and routes to the right group |
| **Template Rollout** | Sets up a standard configuration for many company codes from one template and a few answers each, promotes it DEV → QA only when the previous environment's tests pass, and produces a configuration checklist, test results, an optional AI QA review and UAT scripts |
| **Copilot + Go-Live Cockpit** | One assistant for the whole team (English or Hindi): explains why an environment is blocked, compares environments, adds a company code "like IN10", plans and — after a human clicks Approve — rolls it out. The cockpit page shows every roll-out's readiness score, DEV → QA pipeline, go-live blockers, UAT progress from the testers' results and manual steps to tick off |

The design and requirements traceability are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Showcase on any Windows laptop

1. Copy the folder (or unzip `P2P-Showcase.zip`, or `git clone -b feature/template-rollout https://github.com/pbr33/p2p.git`)
   to a short path such as `C:\P2P` or your Downloads folder.
2. Double-click **`START-SHOWCASE.cmd`**. The first run installs everything (about 2 minutes) and asks
   once for the Claude key (Azure AI Foundry); later runs start in seconds.
3. The Go-Live Cockpit opens at http://127.0.0.1:8770 with the demo story ready.

`START-SHOWCASE.cmd reset` restores the demo story; `START-SHOWCASE.cmd stop` stops the mock
systems. Needs Python 3.11+ (the launcher offers to install it with winget) and internet access
to Claude. The key is kept in `demo\.env` on that laptop only.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp config/instances.example.toml config/instances.toml   # then edit
cp .env.example .env                                       # fill in, then export the variables
```

Credentials are read only from environment variables named in `config/instances.toml`.

## Usage

```bash
# Extract and compare
p2p snapshot  --platform coupa --instance coupa-sbx
p2p diff      --platform coupa --source coupa-sbx --target coupa-prod --explain

# Replicate: dry run first, then apply (production needs an approver)
p2p replicate --platform coupa --source coupa-sbx --target coupa-prod --select tax_codes approval_rules
p2p replicate --platform coupa --source coupa-sbx --target coupa-prod --select tax_codes approval_rules \
              --apply --approved-by CAB-2026-114

# Ariba / PDAP: diff export bundles and produce import files for upload or transport
p2p replicate --platform pdap --source pdap-dev --target pdap-qa --apply

# Documents
p2p docgen --platform coupa --instance coupa-prod --kind frd
p2p docgen --platform coupa --instance coupa-prod --kind tdd --compare-to coupa-sbx
p2p docgen --platform coupa --instance coupa-prod --kind frd --check      # exit 1 if stale

# Incidents: dry run prints the analysis; --apply posts the work note and routes it
p2p triage INC0012345
p2p triage --queue P2P-Triage --apply
```

`diff` exits with 1 when there are blocking issues, so it can gate a CI/CD pipeline.

## Company-code roll-out from a template

A template (`config/templates/*.toml`) holds the standard setup once: the questions to ask
(company code, legal entity, currency, tax codes, approvers...), the objects to create with
`${placeholders}`, business-rule checks, UAT cases and the steps that stay manual. Shipped:
`coupa-company-code` and `ariba-company-code`.

```bash
p2p rollout templates
p2p rollout questions --template coupa-company-code --out company-codes.csv   # one row per company code
p2p rollout questions --template coupa-company-code --out company-codes.toml --interactive

# Dry run, then configure DEV, test it, and only if everything passed configure and test QA
p2p rollout run --template coupa-company-code --answers company-codes.csv --stages coupa-dev coupa-qa
p2p rollout run --template coupa-company-code --answers company-codes.csv --stages coupa-dev coupa-qa \
                --apply --review --ai-uat

# Ariba: the agent writes CIG import files; after they are loaded and the realm re-exported:
p2p rollout verify --template ariba-company-code --answers company-codes.csv --instance ariba-dev
```

In each environment the agent first checks that every approver and parent object exists
(nothing is written otherwise), then applies, then reads the configuration back and tests
it. Re-running is safe: objects that are already correct are left alone. The run folder
(`rollout/<template>-<answers>/`) holds `checklist.md/.csv`, `tests-<env>.csv`,
`uat-scripts.csv/.md` (opens in Excel), `review-<env>.md` and the audit log.

## Copilot and Go-Live Cockpit

```bash
p2p cockpit --open                  # http://127.0.0.1:8770 — dashboard with the Copilot built in
p2p chat                            # the Copilot in the terminal
p2p ask "why is coupa-qa blocked and who has to fix it?"
```

The cockpit reads the run folders: readiness = configuration 50% + UAT passed 35% + manual
steps done 15%. Testers keep typing results into `uat-scripts.csv` in Excel; regenerating
the scripts keeps them. **Re-test** reads an environment again and re-runs its tests without
changing it (drift, or Ariba imports loaded). The Copilot only changes a system through
`apply_rollout`, which waits for an Approve click (or a typed `yes` in the terminal);
production also needs a change reference. The cockpit has no login: keep it on 127.0.0.1.

### From a client document to configured company codes

Drop the client's Excel, CSV or PDF on the cockpit (or `p2p rollout from-document --template
coupa-company-code --files wave2.xlsx --out wave2.csv`). Claude maps it onto the template's
questions; every answer is validated by the template, values it cannot support stay empty,
and `<answers>.gaps.md` lists the open points per owner plus the assumptions to confirm
("2 lakh" read as 200000). The Copilot can then roll out only the complete company codes
(`only`) and draft the email with the open questions.

### Self-healing

**Diagnose & fix** (or "why did UAT fail?") re-tests every environment live and connects a
failed UAT case to the configuration test behind it — e.g. GST5 changed to 18% by hand in QA.
When the fix is to put the template's values back, the Copilot asks for approval, re-applies
only what differs, re-tests, and sends the UAT case back to the tester with a `RE-TEST` note.
Causes outside the template (missing users, wrong answers) are explained and routed instead.

### Time saved

The cockpit's counter converts the agents' work into consultant-hours and money: objects
configured, checks run, UAT scripts written, answers read from documents, defects fixed.
Override the assumed minutes and hourly rate in `config/roi.toml` (keys: `hourly_rate`,
`currency`, `minutes_per_object`, `minutes_per_test`, `minutes_per_uat_case`,
`minutes_per_answer`, `minutes_per_fix`).

### Consistency across company codes

`p2p consistency` (and the cockpit's heatmap) compares every company code — rolled out and
drafts read from client documents — and flags the same tax code with different rates, numbers
far from their peers in the same currency (a limit 7.5× the median: a lakh/crore slip?), tax
codes most company codes of a country have but one lacks, approvers who otherwise only approve
for another country, and the same company code with different answers in two files. No AI;
exits 1 on conflicts so it can gate a pipeline.

### Drift watch

`p2p watch [--every 30]` or `p2p cockpit --watch-every 30` re-tests every environment that
passed and raises an alert when one no longer does — someone changed configuration by hand.
Alerts appear in the cockpit header and `rollout/watch.json`; set `P2P_TEAMS_WEBHOOK` to an
incoming-webhook URL to post them to a Teams channel. Then use 🩹 Diagnose & fix.

### Steering committee pack

The cockpit's **📊 Steering pack** (`/report`, or `/report?ai=0` without AI) is one printable
page — Print → Save as PDF: readiness, roll-outs, blockers, consistency findings, drift and time
saved, plus a Claude-written executive summary, decisions needed (with owners) and top risks,
drawn only from the data printed below it.

### Voice

The 🎤 button in the Copilot panel takes questions in English or Hindi (Edge or Chrome speech
recognition) and reads the answers aloud.

### Demo

```powershell
.\.venv\Scripts\python demo\mock_server.py      # separate window
.\demo\stage-wow-demo.ps1                       # wave 1 live, a manual change in QA, a failed UAT case
.\demo\p2p.ps1 cockpit --open
```

Then: **Check drift** (QA drifted) → **🩹 Diagnose & fix** → Approve → drop
`demo\documents\acme-wave2-company-codes.xlsx` (the consistency check flags IN40's limit) →
**Plan the roll-out** → **📊 Steering pack** → ask by voice: "How ready are we for go-live?"

Claude on Microsoft Foundry: set `ANTHROPIC_FOUNDRY_BASE_URL` (…/anthropic), `ANTHROPIC_FOUNDRY_API_KEY`
and `P2P_MODEL` to the deployment name (e.g. `claude-opus-4-8`) in `demo\.env` or the environment.

## Tests

```bash
pytest
```

The tests run offline: platform APIs and ServiceNow are mocked with `httpx.MockTransport`, and Claude with a scripted client.
