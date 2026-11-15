"""Consistency across company codes, drift watch, steering pack."""

import csv
import json
from pathlib import Path

import httpx
from conftest import FakeAnthropic, text_response
from test_rollout import _ariba_env, _load_imports

from p2p_agents import watch
from p2p_agents.agents.template_rollout import TemplateRolloutAgent
from p2p_agents.cockpit import Cockpit
from p2p_agents.llm import LLM
from p2p_agents.report import ai_summary, gather, render
from p2p_agents.rollout.consistency import check, collect, consistency

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["company_code", "legal_entity", "country", "currency", "tax_codes", "invoice_approval_limit",
          "ap_approver", "finance_approver", "buyer_group"]


def _row(code, limit, taxes="GST18:18:GST 18%;GST5:5:GST 5%", country="IN", currency="INR", ap="ap.india"):
    return [code, f"Acme {code}", country, currency, taxes, limit, ap, "cfo.india", ""]


def _draft(tmp_path, name, rows):
    """A draft answers file as written by the document reader (with its .meta.json)."""
    answers = tmp_path / "rollout" / "answers" / f"{name}.csv"
    answers.parent.mkdir(parents=True, exist_ok=True)
    with answers.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    answers.with_suffix(".meta.json").write_text(
        json.dumps({"template": "coupa-company-code", "answers": str(answers)}), encoding="utf-8")
    return answers


def test_consistency_finds_typos_conflicts_missing_codes_and_unusual_approvers(tmp_path):
    _draft(tmp_path, "wave1", [_row("IN10", 100000), _row("IN20", 250000), _row("IN30", 200000)])
    _draft(tmp_path, "wave2", [
        _row("IN40", 1500000),                                  # 7.5× the median: lakh/crore slip
        _row("IN50", 150000, taxes="GST18:18:GST 18%"),         # no GST5, which all other IN codes have
        _row("IN60", 120000, taxes="GST18:12:GST 18%;GST5:5:GST 5%"),  # GST18 at another rate
        _row("SG20", 50000, taxes="GST9:9:GST 9%", country="SG", currency="SGD", ap="ap.sg"),
        _row("SG30", 60000, taxes="GST9:9:GST 9%", country="SG", currency="SGD", ap="ap.sg"),
        _row("SG40", 55000, taxes="GST9:9:GST 9%", country="SG", currency="SGD", ap="ap.india"),  # Indian AP for SG
        _row("IN10", 999),                                      # same code, other answers than wave1
    ])
    r = consistency(tmp_path / "rollout", ROOT / "config")
    kinds = {(f["kind"], f["entity"]) for f in r["findings"]}
    assert ("outlier", "IN40") in kinds and ("missing", "IN50") in kinds and ("duplicate", "IN10") in kinds
    assert ("conflict", "IN60") in kinds and ("conflict", "IN20") not in kinds  # only the odd one out
    assert ("unusual", "SG40") in kinds
    assert not any(f["entity"] in ("SG20", "SG30", "IN20") for f in r["findings"])
    outlier = next(f for f in r["findings"] if f["kind"] == "outlier")
    assert "1,500,000 INR" in outlier["message"] and "typo" in outlier["message"]
    assert r["findings"][0]["severity"] == "error"  # errors first

    m = r["matrix"]
    col = [e["code"] for e in m["entities"]].index("IN40")
    row = m["questions"].index("invoice_approval_limit")
    assert m["cells"][row][col] == "1,500,000" and m["flags"][row][col] == "warning"


def test_rolled_out_companies_are_included_once(tmp_path):
    answers = _ariba_env(tmp_path)
    TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1",
                         config_dir=tmp_path / "config").run_stage("ariba-dev", apply=True)
    rows = collect(tmp_path / "rollout", ROOT / "config")
    assert [(r["code"], r["origin"]) for r in rows] == [("IN10", "rolled out (r1)")]
    assert check(rows)["findings"] == []


def test_drift_watch_alerts_once_and_posts_to_teams(tmp_path, monkeypatch):
    answers = _ariba_env(tmp_path)
    agent = TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1", config_dir=tmp_path / "config")
    agent.run_stage("ariba-dev", apply=True)
    _load_imports(tmp_path, "dev")
    agent.verify_stage("ariba-dev")

    posted = []
    monkeypatch.setenv("P2P_TEAMS_WEBHOOK", "https://example.invalid/webhook")
    monkeypatch.setattr(watch.httpx, "post", lambda url, json, timeout: posted.append(json) or
                        httpx.Response(200, request=httpx.Request("POST", url)))

    quiet = watch.check_drift(tmp_path / "rollout", tmp_path / "config")
    assert quiet["checked"] == ["r1/ariba-dev"] and quiet["new_alerts"] == [] and not posted

    xref = tmp_path / "bundles" / "dev" / "cig_cross_references.csv"
    xref.write_text(xref.read_text(encoding="utf-8").replace("GST18,G8", "GST18,G9"), encoding="utf-8")
    drift = watch.check_drift(tmp_path / "rollout", tmp_path / "config")
    (alert,) = drift["new_alerts"]
    assert alert["environment"] == "ariba-dev" and alert["status"] == "failed"
    assert any("expected 'G8', found 'G9'" in f for f in alert["failed"])
    assert drift["teams"] == "sent" and "ariba-dev" in posted[0]["text"]

    # the environment is now failed, so the next check does not alert again
    again = watch.check_drift(tmp_path / "rollout", tmp_path / "config")
    assert again["new_alerts"] == [] and len(again["alerts"]) == 1
    assert watch.read_watch(tmp_path / "rollout")["alerts"][0]["environment"] == "ariba-dev"


def test_teams_not_configured_is_reported():
    assert watch.post_to_teams([{"environment": "x", "run": "r", "status": "failed", "failed": []}], webhook="") \
        .startswith("not sent")


def test_steering_pack_with_and_without_ai(tmp_path):
    answers = _ariba_env(tmp_path)
    TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1",
                         config_dir=tmp_path / "config").run_stage("ariba-dev", apply=True)
    _draft(tmp_path, "wave2", [_row("IN40", 1500000), _row("IN50", 150000), _row("IN60", 120000), _row("IN70", 130000)])
    data = gather(tmp_path / "rollout", tmp_path / "config")

    plain = render(data, None, "no API key")
    assert "steering committee pack" in plain and "r1" in plain and "IN40" in plain and "typo" in plain
    assert "not generated: no API key" in plain

    fake = FakeAnthropic([text_response("## Executive summary\n- **IN10** is <b>pending</b> in DEV\n## Decisions needed\n- x")])
    summary = ai_summary(data, LLM(client=fake))
    page = render(data, summary)
    assert "<h3>Executive summary</h3>" in page and "<li><b>IN10</b> is &lt;b&gt;pending&lt;/b&gt; in DEV</li>" in page
    sent = fake.requests[0]["messages"][0]["content"]
    assert '"readiness"' in sent and "IN40" in sent


def test_cockpit_endpoints_for_consistency_watch_and_report(tmp_path):
    answers = _ariba_env(tmp_path)
    TemplateRolloutAgent("ariba-company-code", answers, tmp_path / "rollout" / "r1",
                         config_dir=tmp_path / "config").run_stage("ariba-dev", apply=True)
    cockpit = Cockpit(config_dir=tmp_path / "config", runs_root=tmp_path / "rollout")
    assert cockpit.handle("GET", "/api/consistency", {})[1]["company_codes"] == 1
    assert cockpit.handle("GET", "/api/watch", {})[1] == {"every_minutes": 0}
    w = cockpit.handle("POST", "/api/watch", {})[1]
    assert w["checked"] == [] and w["last_check"]  # ariba-dev is pending, so nothing to watch yet
    assert "steering committee pack" in cockpit.report(with_ai=False)
