# P2P Agent Framework

Agentic orchestration and run-support for a multi-platform Procure-to-Pay landscape: Coupa, SAP Ariba, Process Director Accounts Payable (PDAP), ReadSoft Online / Tungsten AP Essentials and Tungsten Network.

| Agent | What it does |
|---|---|
| **ConfigReplication** | Extracts platform configuration, diffs staging against production, flags schema conflicts and missing dependencies, and replicates in dependency order with an approval gate and audit trail |
| **DocuGen** | Generates Functional Requirements (FRD) and Technical Design (TDD) documents from live configuration, and detects when they are out of date |
| **Triage & Run-Support** | Reads ServiceNow incidents, investigates with similar incidents, platform logs and configuration, posts a root-cause analysis and routes to the right group |

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

## Tests

```bash
pytest
```

The tests run offline: platform APIs and ServiceNow are mocked with `httpx.MockTransport`, and Claude with a scripted client.
