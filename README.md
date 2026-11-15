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

## Tests

```bash
pytest
```

The tests run offline: platform APIs and ServiceNow are mocked with `httpx.MockTransport`, and Claude with a scripted client.
