import csv
import shutil
from pathlib import Path

import pytest
from conftest import FakeAnthropic, text_response, tool_response

from p2p_agents.agents.template_rollout import TemplateRolloutAgent
from p2p_agents.llm import LLM
from p2p_agents.models import Snapshot
from p2p_agents.rollout.template import (TemplateError, load_answers, load_template, render, resolve_all,
                                         resolve_entity)
from p2p_agents.rollout.verify import configured_tests, mismatches, reference_tests, rule_tests
from p2p_agents.settings import load_platform

ROOT = Path(__file__).resolve().parents[1]

COUPA_ROW = {
    "company_code": "IN10", "legal_entity": "Acme India", "country": "IN", "currency": "INR",
    "tax_codes": "GST18:18:GST 18%;GST5:5:GST 5%", "invoice_approval_limit": "100,000",
    "ap_approver": "ap.india", "finance_approver": "cfo.india",
}


@pytest.fixture
def coupa():
    return load_template("coupa-company-code"), load_platform("coupa", ROOT / "config")


def test_answers_are_typed_and_defaults_filled(coupa):
    template, _ = coupa
    e = resolve_entity(template, COUPA_ROW)
    assert e["invoice_approval_limit"] == 100000
    assert e["tax_codes"] == [{"code": "GST18", "rate": 18, "description": "GST 18%"},
                              {"code": "GST5", "rate": 5, "description": "GST 5%"}]
    assert e["buyer_group"] == "Acme India Buyers"


def test_every_bad_answer_is_reported_at_once(coupa):
    template, _ = coupa
    bad = {**COUPA_ROW, "currency": "RUPEE", "ap_approver": "", "invoice_approval_limit": "lots", "colour": "red"}
    with pytest.raises(TemplateError) as e:
        resolve_entity(template, bad)
    msg = str(e.value)
    assert "currency" in msg and "ap_approver: required" in msg and "invoice_approval_limit" in msg and "colour" in msg


def test_shared_objects_are_created_once_and_numbers_keep_their_type(coupa):
    template, platform = coupa
    in20 = {**COUPA_ROW, "company_code": "IN20", "legal_entity": "Acme Services"}
    rendered = render(template, platform, resolve_all(template, [COUPA_ROW, in20]))
    gst18 = [r for r in rendered.items if r.item.object_type == "tax_codes" and r.item.key == "GST18"]
    assert len(gst18) == 1 and gst18[0].entities == ["IN10", "IN20"]
    assert gst18[0].item.data["percentage"] == 18
    rule = next(r.item for r in rendered.items if r.item.key == "IN10 invoices over limit")
    assert rule.data["amount-threshold"] == 100000
    chain = next(r.item for r in rendered.items if r.item.key == "IN10 Finance Approval")
    assert {d.key for d in chain.depends_on} == {"ap.india", "cfo.india"}


def test_conflicting_shared_object_is_rejected(coupa):
    template, platform = coupa
    other = {**COUPA_ROW, "company_code": "IN20", "tax_codes": "GST18:12:GST 18%"}
    with pytest.raises(TemplateError, match="shared by IN10 and IN20"):
        render(template, platform, resolve_all(template, [COUPA_ROW, other]))


def test_verify_finds_wrong_values_missing_approvers_and_rule_breaks(coupa):
    template, platform = coupa
    rendered = render(template, platform, resolve_all(template, [COUPA_ROW]))
    items = [r.item.model_copy(deep=True) for r in rendered.items]
    for i in items:
        if i.key == "GST18":
            i.data["percentage"] = 12.0
        if i.key == "IN10 Finance Approval":
            i.data["approval-chain-steps"] = i.data["approval-chain-steps"][:1]
    target = Snapshot(platform="coupa", instance="coupa-qa", items=items,
                      object_types=[*template.object_types, "users"])
    cfg = {t.key: t for t in configured_tests(rendered, target, platform, file_based=False)}
    assert cfg["GST18"].status == "fail" and "expected 18, found 12.0" in cfg["GST18"].detail
    assert cfg["GST5"].status == "pass"
    refs = [t for t in reference_tests(rendered, target, preflight=True) if t.status == "fail"]
    assert {t.description.split("→ ")[1] for t in refs} == {"users 'ap.india'", "users 'cfo.india'"}
    rules = {t.id: t.status for t in rule_tests(rendered, target, file_based=False)}
    assert rules["APPR-2STEP"] == "fail" and rules["COA-CURRENCY"] == "pass"


def test_mismatches_ignores_extra_target_fields_and_number_formats():
    assert mismatches({"a": 18, "b": {"c": "X"}}, {"a": "18.0", "b": {"c": "X", "d": 1}, "e": 2}) == []
    assert mismatches({"a": True}, {"a": "true"}) == []


# ───────── end to end on a file-based platform (Ariba) ─────────

def _ariba_env(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    lines = []
    for env in ("dev", "qa"):
        bundle = tmp_path / "bundles" / env
        shutil.copytree(ROOT / "demo" / "data" / "ariba" / env, bundle)
        lines += [f"[ariba-{env}]", 'platform = "ariba"',
                  f"bundle_dir = '{bundle}'", f"outbox_dir = '{tmp_path / 'outbox' / env}'", ""]
    (config / "instances.toml").write_text("\n".join(lines), encoding="utf-8")
    answers = tmp_path / "answers.csv"
    answers.write_text("company_code,legal_entity,purchasing_unit,tax_codes\n"
                       "IN10,Acme India,,GST18:G8:GST 18%\n", encoding="utf-8")
    return answers


def _load_imports(tmp_path: Path, env: str) -> None:
    """Play the Ariba admin: append every import row to the export file."""
    for imp in (tmp_path / "outbox" / env).glob("*.import.csv"):
        export = tmp_path / "bundles" / env / f"{imp.name.split('.')[0]}.csv"
        with export.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns, rows = list(reader.fieldnames), list(reader)
        with imp.open(newline="", encoding="utf-8") as f:
            rows += [{k: v for k, v in r.items() if k != "_action"} for r in csv.DictReader(f)]
        with export.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        imp.unlink()


def test_dev_to_qa_waits_for_import_and_verification(tmp_path):
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "run", config_dir=tmp_path / "config")

    dev = agent.run_stage("ariba-dev", apply=True)
    assert dev.status == "pending" and dev.import_files
    assert {a["key"] for a in dev.actions} >= {"COMPANY_CODE_MAP|IN10", "TAX_MAP|GST18", "COMPANY_CODE_MAP"}
    assert "TAX_MAP" not in {a["key"] for a in dev.actions}  # already in DEV, unchanged

    qa = agent.run_stage("ariba-qa", apply=True, previous="ariba-dev")
    assert qa.status == "blocked" and "ariba-dev is pending" in qa.note

    _load_imports(tmp_path, "dev")
    assert agent.verify_stage("ariba-dev").status == "passed"

    again = agent.run_stage("ariba-dev", apply=True)
    assert again.status == "passed" and again.actions == []
    assert agent.run_stage("ariba-qa", apply=True, previous="ariba-dev").status == "pending"

    checklist = (tmp_path / "run" / "checklist.md").read_text(encoding="utf-8")
    assert "✅ created, verified" in checklist and "⏳ created (import file), awaiting load" in checklist
    assert "Upload the CIG import files" in checklist


def test_dry_run_changes_nothing(tmp_path):
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "run", config_dir=tmp_path / "config")
    stage = agent.run_stage("ariba-dev", apply=False)
    assert stage.status == "planned" and not stage.applied
    assert not (tmp_path / "outbox" / "dev").exists()


def test_uat_combines_template_cases_with_validated_ai_cases(tmp_path):
    answers = _ariba_env(tmp_path)
    case = {"entity": "IN10", "area": "Boundary", "scenario": "Unknown company code is rejected",
            "steps": ["Submit invoice for company code ZZ99", "Check CIG monitor"], "expected": "Mapping error"}
    fake = FakeAnthropic([
        tool_response(("t1", "submit_uat_cases", {"cases": [{**case, "entity": "XX99"}]})),  # unknown entity
        tool_response(("t2", "submit_uat_cases", {"cases": [case]})),
        text_response("done"),
    ])
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "run",
                                 llm=LLM(client=fake), config_dir=tmp_path / "config")
    csv_path, md_path, n = agent.uat(ai=True)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert n == len(rows) == 4  # UAT-01, one UAT-02 per tax code (1), UAT-03, plus one AI case
    assert rows[-1]["Test ID"] == "IN10-AI01" and rows[-1]["Source"] == "AI"
    assert "1. Submit invoice for company code ZZ99" in rows[-1]["Steps"]
    assert fake.requests[1]["messages"][-1]["content"][0]["is_error"]  # unknown entity was sent back


def test_answers_csv_and_toml_are_equivalent(tmp_path, coupa):
    template, _ = coupa
    toml = tmp_path / "a.toml"
    toml.write_text("[[entity]]\n" + "\n".join(f'{k} = "{v}"' for k, v in COUPA_ROW.items()), encoding="utf-8")
    c = tmp_path / "a.csv"
    with c.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(COUPA_ROW))
        w.writeheader()
        w.writerow(COUPA_ROW)
    assert resolve_all(template, load_answers(toml)) == resolve_all(template, load_answers(c))
