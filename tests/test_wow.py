"""Client document → answers, self-healing, and the time-saved counter."""

from pathlib import Path

import pytest
from conftest import FakeAnthropic, text_response, tool_response
from test_rollout import _ariba_env, _load_imports

from p2p_agents.agents.copilot import CopilotAgent, FixArgs, RunArg, VerifyArgs
from p2p_agents.agents.template_rollout import TemplateRolloutAgent
from p2p_agents.llm import LLM
from p2p_agents.rollout import documents
from p2p_agents.rollout.reports import _write_csv, mark_uat_retest, read_csv
from p2p_agents.rollout.template import load_answers, load_template
from p2p_agents.workspace import log_activity, roi, uat_progress

ROOT = Path(__file__).resolve().parents[1]
GOOD = {"company_code": "IN40", "legal_entity": "Acme Chennai Pvt Ltd", "country": "IN", "currency": "INR",
        "tax_codes": "GST18:18:GST 18%;GST5:5:GST 5%", "invoice_approval_limit": "150000",
        "ap_approver": "ap.india", "finance_approver": "cfo.india", "buyer_group": "", "source": "Wave 2 r4"}


def test_workbook_is_read_as_text_per_sheet(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Codes"
    wb.active.append(["Co. Code", "Limit"])
    wb.active.append(["IN40", "1,50,000"])
    wb.create_sheet("Tax").append(["GST5", 5])
    path = tmp_path / "client.xlsx"
    wb.save(path)
    (block,) = documents.read_document(path)
    assert "## sheet: Codes" in block["text"] and 'r2,IN40,"1,50,000"' in block["text"] and "r1,GST5,5" in block["text"]


def test_document_extraction_validates_and_keeps_gaps(tmp_path):
    doc = tmp_path / "codes.csv"
    doc.write_text("Co. Code,Entity\nIN40,Acme Chennai\nIN50,Acme Hyderabad\n", encoding="utf-8")
    in50 = {**GOOD, "company_code": "IN50", "legal_entity": "Acme Hyderabad Pvt Ltd", "finance_approver": "",
            "source": "Wave 2 r5"}
    gap = {"entity": "IN50", "question": "finance_approver", "issue": "TBC in the workbook", "owner": "Group Finance"}
    fake = FakeAnthropic([
        # first attempt: currency written as a name, which the template rejects
        tool_response(("t1", "submit_answers", {"entities": [{**GOOD, "currency": "Indian Rupee"}, in50], "gaps": [gap]})),
        tool_response(("t2", "submit_answers", {"entities": [GOOD, in50], "gaps": [gap],
                                                 "assumptions": ["'1,50,000' read as 150000"]})),
    ])
    template = load_template("coupa-company-code", ROOT / "config")
    ex = documents.extract(template, [doc], LLM(client=fake))
    first_error = fake.requests[1]["messages"][-1]["content"][0]
    assert first_error["is_error"] and "currency" in first_error["content"]
    assert [s["ok"] for s in ex.status] == [True, False]
    assert ex.status[1]["problems"] == ["finance_approver: (no value)"]

    ex = documents.save(ex, template, tmp_path / "answers.csv")
    rows = load_answers(tmp_path / "answers.csv")
    assert [r["company_code"] for r in rows] == ["IN40", "IN50"] and rows[1]["finance_approver"] == ""
    gaps = (tmp_path / "answers.gaps.md").read_text(encoding="utf-8")
    assert "TBC in the workbook" in gaps and "Group Finance" in gaps and "1,50,000" in gaps


def test_only_the_complete_company_codes_are_rolled_out(tmp_path):
    answers = _ariba_env(tmp_path)
    with answers.open("a", encoding="utf-8") as f:
        f.write("SG10,Acme Singapore,,\n")  # incomplete: no tax codes yet
    copilot = CopilotAgent(approve=lambda s: True, config_dir=tmp_path / "config", runs_root=tmp_path / "rollout",
                           workdir=tmp_path)
    args = {"template": "ariba-company-code", "answers_file": "answers.csv", "stages": ["ariba-dev"]}
    from p2p_agents.agents.copilot import RolloutArgs
    from p2p_agents.rollout.template import TemplateError

    with pytest.raises(TemplateError, match="tax_codes"):
        copilot.t_plan(RolloutArgs(**args))
    with pytest.raises(ValueError, match="XX99 not in answers.csv"):
        copilot.t_plan(RolloutArgs(**args, only=["XX99"]))
    out = copilot.t_apply(RolloutArgs(**args, only=["IN10"]))
    assert out["entities"] == ["IN10"] and out["stages"][0]["status"] == "pending"
    # later operations on this roll-out keep to IN10 although SG10 is still incomplete
    assert copilot.t_verify(VerifyArgs(run="ariba-company-code-answers", instance="ariba-dev"))["status"] == "pending"


def test_mark_uat_retest_clears_result_and_explains(tmp_path):
    path = tmp_path / "uat-scripts.csv"
    _write_csv(path, ["Test ID", "Result (Pass/Fail)", "Tester", "Date", "Comments"],
               [["A-1", "Fail", "Ravi", "1-Nov", "18% instead of 5%"], ["A-2", "Pass", "Asha", "1-Nov", ""]])
    assert mark_uat_retest(tmp_path, ["A-1"], "GST5 restored") == ["A-1"]
    rows = read_csv(path)
    assert rows[0]["Result (Pass/Fail)"] == "" and rows[0]["Comments"].startswith("RE-TEST: GST5 restored")
    assert "18% instead of 5%" in rows[0]["Comments"] and rows[1]["Result (Pass/Fail)"] == "Pass"
    assert uat_progress(tmp_path)["retest"] == ["A-1"]


def _drifted_ariba(tmp_path):
    """Ariba DEV rolled out and verified, then someone edits the TAX_MAP target by hand; a tester fails a UAT case."""
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1", config_dir=tmp_path / "config")
    agent.run_stage("ariba-dev", apply=True)
    _load_imports(tmp_path, "dev")
    assert agent.verify_stage("ariba-dev").status == "passed"
    agent.uat()
    xref = tmp_path / "bundles" / "dev" / "cig_cross_references.csv"
    xref.write_text(xref.read_text(encoding="utf-8").replace("GST18,G8", "GST18,G9"), encoding="utf-8")
    uat = tmp_path / "rollout" / "r1" / "uat-scripts.csv"
    rows = read_csv(uat)
    tax_case = next(r for r in rows if "GST18" in r["Scenario"])
    tax_case["Result (Pass/Fail)"], tax_case["Comments"] = "Fail", "posted with tax code G9"
    _write_csv(uat, list(rows[0]), [list(r.values()) for r in rows])
    return tax_case["Test ID"]


def test_diagnose_links_failed_uat_to_the_drifted_setting(tmp_path):
    case_id = _drifted_ariba(tmp_path)
    copilot = CopilotAgent(config_dir=tmp_path / "config", runs_root=tmp_path / "rollout", workdir=tmp_path)
    d = copilot.t_diagnose(RunArg(run="r1"))
    assert d["environments"][0]["status"] == "failed" and d["fixable_by_reapplying_template"] == ["ariba-dev"]
    (uat,) = d["failed_uat_cases"]
    assert uat["id"] == case_id and any("GST18" in t and "G8" in t for t in uat["related_failed_tests"])


def test_fix_needs_approval_then_reapplies_the_template(tmp_path):
    case_id = _drifted_ariba(tmp_path)
    fix = {"run": "r1", "instance": "ariba-dev", "uat_cases": [case_id]}
    fake = FakeAnthropic([tool_response(("t1", "fix_environment", fix)), text_response("approve it")])
    copilot = CopilotAgent(llm=LLM(client=fake), approve=lambda s: None, config_dir=tmp_path / "config",
                           runs_root=tmp_path / "rollout", workdir=tmp_path)
    copilot.ask("fix it")
    (pending,) = copilot.pending.values()
    assert "update cig_cross_references:TAX_MAP|GST18" in pending.summary
    assert not list((tmp_path / "outbox" / "dev").glob("*.import.csv"))  # nothing written before approval

    result = copilot.decide(pending.id, approved=True)["result"]
    assert result["status"] == "pending" and result["updates"] == 1  # Ariba: import file written, awaiting load
    imp = next((tmp_path / "outbox" / "dev").glob("cig_cross_references.import.csv")).read_text(encoding="utf-8")
    assert "update,GST18,TAX_MAP,G8" in imp

    _load_imports(tmp_path, "dev")
    assert copilot.t_verify(VerifyArgs(run="r1", instance="ariba-dev"))["status"] == "passed"


def test_fix_on_rest_platform_sends_uat_back_for_retest(tmp_path, monkeypatch):
    """When the re-applied environment passes straight away (REST platforms), the UAT case goes back to testers."""
    case_id = _drifted_ariba(tmp_path)
    copilot = CopilotAgent(approve=lambda s: True, config_dir=tmp_path / "config",
                           runs_root=tmp_path / "rollout", workdir=tmp_path)
    original = TemplateRolloutAgent.run_stage

    def apply_and_load(self, name, **kw):  # the "system" applies the change immediately
        stage = original(self, name, **kw)
        if kw.get("apply"):
            _load_imports(tmp_path, "dev")
            stage = self.verify_stage(name)
        return stage

    monkeypatch.setattr(TemplateRolloutAgent, "run_stage", apply_and_load)
    result = copilot.t_fix(FixArgs(run="r1", instance="ariba-dev", uat_cases=[case_id]))
    assert result["approval"] == "approved" and result["status"] == "passed"
    assert result["uat_sent_for_retest"] == [case_id]
    row = next(r for r in read_csv(tmp_path / "rollout" / "r1" / "uat-scripts.csv") if r["Test ID"] == case_id)
    assert row["Result (Pass/Fail)"] == "" and row["Comments"].startswith("RE-TEST: fixed by Copilot")
    assert roi(tmp_path / "rollout", tmp_path / "config")["fixes"] == 1


def test_time_saved_counts_runs_documents_and_fixes(tmp_path):
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1", config_dir=tmp_path / "config")
    agent.run_stage("ariba-dev", apply=True)
    agent.uat()
    log_activity(tmp_path / "rollout", "document", answers=40, entities=4)
    log_activity(tmp_path / "rollout", "fix", run="r1")
    (tmp_path / "config" / "roi.toml").write_text("hourly_rate = 6000\nminutes_per_fix = 60\n", encoding="utf-8")
    r = roi(tmp_path / "rollout", tmp_path / "config")
    assert r["objects"] >= 3 and r["uat_cases"] == 3 and r["answers_read"] == 40 and r["fixes"] == 1
    expected = (r["objects"] * 20 + r["tests"] * 2 + 3 * 30 + 40 * 3 + 60) / 60
    assert r["hours"] == round(expected, 1) and r["money"] == round(expected * 6000)
    assert r["company_codes"] == 1
    with (tmp_path / "rollout" / "activity.jsonl").open("a") as f:
        f.write("not json\n")
    assert roi(tmp_path / "rollout", tmp_path / "config")["fixes"] == 1
