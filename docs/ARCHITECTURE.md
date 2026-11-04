# Architecture

## Components

```
                    ┌──────────────────────────── p2p CLI ───────────────────────────┐
                    │ snapshot · diff · replicate · docgen · triage                  │
                    └───────┬───────────────────────┬───────────────────────┬────────┘
                            │                       │                       │
              ConfigReplication Agent         DocuGen Agent         Triage & Run-Support Agent
              (deterministic core,            (Claude writes         (Claude tool-use loop,
               Claude explains risk)           FRD / TDD)             deterministic dispatch)
                            │                       │                       │
        ┌───────────────────┼───────────┐           │          ┌────────────┼──────────────┐
   Connectors          diff / planner / │      snapshot store  ServiceNow   log sources   snapshot store
   rest · file_bundle  executor + audit │      (fingerprints)  Table API    (pluggable)   (config lookup)
        │                               │
  Coupa · RSO/Tungsten (REST)           │
  Ariba CIG · PDAP (export/import files)│
```

Every agent sends Claude requests through `p2p_agents.llm.LLM`:
- Every request streams.
- Effort is set explicitly.
- Server-side refusal fallback is on (`server-side-fallback-2026-07-01`, `fallbacks: "default"`).
- The tool-use loop is append-only and validates every tool input with Pydantic.

The default model is `claude-opus-5-5`; `P2P_MODEL` overrides it.

## Design decisions

**Correctness lives in code, judgement in the model.**
- Extraction, diffing, dependency validation, ordering and applying are deterministic and unit-tested.
- Claude only explains a diff (release-risk summary), writes documents, and investigates incidents.

**Natural keys, not IDs.** Platform IDs differ between sandbox and production, so objects are matched by a configured natural key, such as a tax code's `code` or a chart of accounts' `name`. References between objects use natural keys too, and the planner orders creates so parents exist before children.

**Safe by default.**
- `replicate` is a dry run unless `--apply` is given.
- Production targets also need `--approved-by`.
- Removals are reported, never executed.
- Apply stops at the first failure and writes an audit log with before-images for rollback.
- Triage writes only a work note and the assignment group. It never changes state or closes an incident.

**Files where there is no write API.**
- SAP Ariba CIG mappings, lookup tables and supplier workspace templates move as export and import files.
- PDAP customizing must travel in SAP transport requests, so both use the file-bundle connector. It reads exports and writes import files: a CSV to upload to Ariba, or a transport content list for PDAP.

**Precision dispatch.** The agent classifies the incident by platform and category, then `config/routing.toml` maps that to an assignment group. Low confidence or `needs_human` routes to the triage queue, so the group never comes from model free text.

**Untrusted input.** Incident text, logs and configuration data are passed as delimited data. System prompts tell the model to treat them as data, not instructions, and writes are limited to the actions above.

## Requirements traceability

| Requirement | Implementation | Tests |
|---|---|---|
| FR-1 Coupa: approval chains, custom fields (requisition/invoice/supplier), dynamic approval rules, content groups, tax codes, exchange rates | `config/platforms/coupa.toml`, `connectors/rest.py` (OAuth2 client credentials, offset paging, retry) | `test_connectors.py` |
| FR-1 Staging-to-production diff; schema conflicts; missing dependencies (e.g. parent chart of accounts) | `replication/diff.py` | `test_diff_and_plan.py` |
| FR-1 Replication with ordering, approval gate, audit and rollback data | `replication/planner.py`, `replication/executor.py` | `test_diff_and_plan.py`, `test_executor.py` |
| FR-1 Ariba CIG mapping verification and parameter propagation; lookup tables; supplier workspace templates | `config/platforms/ariba.toml`, `connectors/file_bundle.py` | `test_connectors.py` |
| FR-1 ReadSoft Online / Tungsten AP Essentials: extraction profiles, document types, supplier master mappings, validation rule sets across tenants | `config/platforms/readsoft.toml` (per-tenant `path_vars`) | `test_connectors.py` |
| FR-1 PDAP: workflow steps, validation check profiles, determination tables between DEV / QA / PROD clients | `config/platforms/pdap.toml`, file-bundle import files as transport content | `test_connectors.py` |
| DocuGen: FRD and TDD generation and synchronisation | `agents/docugen.py` (fingerprint header, `--check`, manual blocks preserved) | `test_agents.py` |
| Triage: bi-directional ServiceNow, root-cause analysis, log interrogation, precision dispatch | `servicenow.py`, `agents/triage.py`, `config/routing.toml` | `test_agents.py` |

## Before the first production run

1. **Endpoints and payload keys.** The paths in `config/platforms/*.toml` follow each vendor's API conventions, but they vary by release and by the OAuth scopes granted. Verify each object type against your tenant's API documentation with `p2p snapshot` on a sandbox before any `--apply`.
2. **Natural keys.** Make sure each configured key is unique in your data; `DUPLICATE_KEY` issues will tell you if it isn't.
3. **Logs.** `FileLogSource` searches exported logs under `logs/<platform>/`. Replace it with a source that queries your log store (e.g. Splunk) through the `LogSource` protocol.
