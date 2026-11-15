import csv
from pathlib import Path

import pytest
from conftest import FakeAnthropic, text_response, tool_response
from test_rollout import COUPA_ROW, _ariba_env, _load_imports

from p2p_agents.agents.copilot import CopilotAgent
from p2p_agents.agents.template_rollout import TemplateRolloutAgent
from p2p_agents.cockpit import Cockpit
from p2p_agents.llm import LLM
from p2p_agents.rollout.reports import read_csv
from p2p_agents.rollout.template import TemplateError, load_answers
from p2p_agents.workspace import all_runs, load_run, run_summary, upsert_entity

ROOT = Path(__file__).resolve().parents[1]


def _coupa_answers(tmp_path: Path) -> Path:
    path = tmp_path / "codes.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(COUPA_ROW))
        w.writeheader()
        w.writerow(COUPA_ROW)
    return path


def test_add_company_code_like_another_rederives_dependent_answers(tmp_path):
    answers = _coupa_answers(tmp_path)
    out = upsert_entity("coupa-company-code", answers, {"company_code": "IN30", "legal_entity": "Acme Pune"},
                        copy_from="IN10", config_dir=ROOT / "config")
    assert out["action"] == "added" and out["entities"] == ["IN10", "IN30"]
    assert out["values"]["buyer_group"] == "Acme Pune Buyers"  # not copied from IN10
    rows = load_answers(answers)
    assert rows[1]["tax_codes"] == "GST18:18:GST 18%;GST5:5:GST 5%" and rows[1]["invoice_approval_limit"] == "100000"


def test_invalid_company_code_leaves_the_answers_file_alone(tmp_path):
    answers = _coupa_answers(tmp_path)
    before = answers.read_bytes()
    with pytest.raises(TemplateError, match="currency"):
        upsert_entity("coupa-company-code", answers, {"company_code": "IN40", "currency": "RUPEE"},
                      copy_from="IN10", config_dir=ROOT / "config")
    assert answers.read_bytes() == before


def test_tester_results_survive_regeneration_and_feed_readiness(tmp_path):
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1", config_dir=tmp_path / "config")
    agent.run_stage("ariba-dev", apply=True)
    _load_imports(tmp_path, "dev")
    agent.verify_stage("ariba-dev")
    csv_path, _, n = agent.uat()

    rows = read_csv(csv_path)  # a tester fills in results in Excel (saved back as ANSI)
    rows[0]["Result (Pass/Fail)"], rows[0]["Tester"] = "Pass", "Asha"
    rows[1]["Result (Pass/Fail)"], rows[1]["Comments"] = "Fail", "posting blocked"
    with csv_path.open("w", newline="", encoding="cp1252", errors="replace") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    agent.uat()  # regenerated after a re-run
    again = read_csv(csv_path)
    assert again[0]["Tester"] == "Asha" and again[1]["Comments"] == "posting blocked"

    run_dir, state = load_run("r1", tmp_path / "rollout")
    s = run_summary(run_dir, state)
    assert s["uat"]["executed"] == 2 and s["uat"]["pass"] == 1 and s["uat"]["fail"] == 1
    assert any("UAT" in b and "failed" in b for b in s["readiness"]["blockers"])
    assert 0 < s["readiness"]["score"] < 100 and s["readiness"]["config"] == 100
    assert state.answers == str(answers.resolve())


def _copilot(tmp_path, responses, approve):
    answers = _ariba_env(tmp_path)
    fake = FakeAnthropic(responses)
    copilot = CopilotAgent(llm=LLM(client=fake), approve=approve, config_dir=tmp_path / "config",
                           runs_root=tmp_path / "rollout", workdir=tmp_path)
    return copilot, fake, answers


ROLL = {"template": "ariba-company-code", "answers_file": "answers.csv", "stages": ["ariba-dev", "ariba-qa"]}


def test_copilot_plans_then_waits_for_approval_before_changing_anything(tmp_path):
    copilot, fake, _ = _copilot(tmp_path, [
        tool_response(("t1", "plan_rollout", ROLL)),
        tool_response(("t2", "apply_rollout", ROLL)),
        text_response("Approve the change to run it."),
    ], approve=lambda summary: None)
    assert copilot.ask("roll out IN10") == "Approve the change to run it."
    plan = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert '"status": "planned"' in plan and "COMPANY_CODE_MAP" in plan
    assert not (tmp_path / "outbox" / "dev").exists()  # nothing written yet

    (pending,) = copilot.pending.values()
    assert "IN10" in pending.summary and "ariba-dev → ariba-qa" in pending.summary
    result = copilot.decide(pending.id, approved=True)
    assert result["result"]["stages"][0]["status"] == "pending"  # Ariba import files written, awaiting load
    assert "ariba-qa" in result["result"]["stopped"]
    assert list((tmp_path / "outbox" / "dev").glob("*.import.csv"))


def test_copilot_declined_change_is_not_applied(tmp_path):
    copilot, fake, _ = _copilot(tmp_path, [
        tool_response(("t1", "apply_rollout", ROLL)),
        text_response("ok"),
    ], approve=lambda summary: False)
    copilot.ask("go")
    assert '"declined"' in fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert not (tmp_path / "outbox" / "dev").exists()


def test_copilot_rejects_answers_files_outside_the_project(tmp_path):
    copilot, fake, _ = _copilot(tmp_path, [
        tool_response(("t1", "add_company_code", {"template": "ariba-company-code", "answers_file": "../evil.csv",
                                                  "values": {"company_code": "X1"}})),
        text_response("no"),
    ], approve=lambda s: True)
    copilot.ask("x")
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "inside the project folder" in result["content"]


def test_conversation_continues_with_history(tmp_path):
    copilot, fake, _ = _copilot(tmp_path, [text_response("first"), text_response("second")], approve=None)
    copilot.ask("hello")
    copilot.ask("and then?")
    sent = fake.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"] and sent[-1]["content"] == "and then?"


def test_cockpit_api(tmp_path):
    answers = _ariba_env(tmp_path)
    TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1",
                         config_dir=tmp_path / "config").run_stage("ariba-dev", apply=True)
    cockpit = Cockpit(config_dir=tmp_path / "config", runs_root=tmp_path / "rollout")
    status, runs = cockpit.handle("GET", "/api/runs", {})
    assert status == 200 and runs[0]["name"] == "r1" and runs[0]["stages"][0]["status"] == "pending"

    _load_imports(tmp_path, "dev")
    status, stage = cockpit.handle("POST", "/api/runs/r1/verify", {"instance": "ariba-dev"})
    assert stage["status"] == "passed"

    status, marks = cockpit.handle("POST", "/api/runs/r1/manual", {"step": "M1", "done": True, "by": "Ravi"})
    assert "M1" in marks and "Ravi" in marks["M1"]
    detail = cockpit.handle("GET", "/api/runs/r1", {})[1]
    assert detail["manual"][0]["done"] and detail["checklist"]
    assert cockpit.handle("POST", "/api/runs/r1/manual", {"step": "../x"})[0] == 400
    assert all_runs(tmp_path / "rollout")[0]["readiness"]["manual"] > 0
