import json

import httpx
from conftest import FakeAnthropic, item, snap, text_response, tool_response

from p2p_agents import store
from p2p_agents.agents.docugen import DocuGenAgent, is_stale
from p2p_agents.agents.triage import Routing, TriageAgent, TriageResult
from p2p_agents.llm import LLM
from p2p_agents.servicenow import ServiceNowClient

ROUTING = Routing(triage_queue="P2P-Triage", platforms={"ariba": "P2P-Ariba-L2"},
                  overrides={"integration_failure": {"ariba": "P2P-Ariba-CIG-Integration"}})

RESULT = {
    "platform": "ariba", "category": "integration_failure", "summary": "Invoices stuck in CIG",
    "probable_root_cause": "Expired client certificate on the CIG ERP connection",
    "evidence": ["cig.log:3 certificate expired"], "confidence": 0.85,
    "recommended_actions": ["Renew the certificate", "Resend failed invoices"],
    "related_incidents": ["INC0009001"], "needs_human": False, "caller_update": "We found the cause.",
}


class FakeLogs:
    def search(self, platform, query, max_lines):
        return [f"cig.log:3: {platform} certificate expired"]


def servicenow(patches):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/now/table/incident" and "number=" in request.url.params["sysparm_query"]:
            return httpx.Response(200, json={"result": [{"sys_id": "abc", "number": "INC0012345",
                                                         "short_description": "Ariba invoices not reaching SAP"}]})
        if path == "/api/now/table/incident":
            assert "stateIN6,7" in request.url.params["sysparm_query"]
            return httpx.Response(200, json={"result": [{"number": "INC0009001", "close_notes": "renewed cert"}]})
        if path == "/api/now/table/sys_user_group":
            return httpx.Response(200, json={"result": [{"sys_id": "grp1", "name": "P2P-Ariba-CIG-Integration"}]})
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(404)

    return ServiceNowClient("https://acme.service-now.com", "u", "p", transport=httpx.MockTransport(handler))


def test_triage_investigates_submits_and_dispatches(tmp_path):
    fake = FakeAnthropic([
        tool_response(("t1", "search_similar_incidents", {"text": "invoices not reaching SAP"}),
                      ("t2", "query_platform_logs", {"platform": "ariba", "query": "certificate"})),
        tool_response(("t3", "submit_triage", {**RESULT, "confidence": 2})),  # invalid: retried
        tool_response(("t4", "submit_triage", RESULT)),
    ])
    patches = []
    agent = TriageAgent(servicenow(patches), ROUTING, logs=FakeLogs(), llm=LLM(client=fake), snapshot_dir=tmp_path)
    incident, result = agent.triage("INC0012345")

    assert result.probable_root_cause.startswith("Expired client certificate")
    assert len(fake.requests) == 3
    req = fake.requests[0]
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["extra_body"] == {"fallbacks": "default"}
    assert "Ariba invoices not reaching SAP" in req["messages"][0]["content"]
    second_turn_results = fake.requests[1]["messages"][2]["content"]
    assert "INC0009001" in second_turn_results[0]["content"] and "certificate expired" in second_turn_results[1]["content"]
    retry = fake.requests[2]["messages"][-1]["content"][0]
    assert retry["is_error"] and "INVALID_INPUT" in retry["content"]

    assert agent.dispatch(incident, result) == "P2P-Ariba-CIG-Integration"
    assert patches[0]["assignment_group"] == "grp1"
    assert "AI-generated" in patches[0]["work_notes"] and "Renew the certificate" in patches[0]["work_notes"]


def test_low_confidence_goes_to_triage_queue():
    result = TriageResult.model_validate({**RESULT, "confidence": 0.3})
    assert ROUTING.group_for(result) == "P2P-Triage"
    assert ROUTING.group_for(TriageResult.model_validate({**RESULT, "category": "master_data"})) == "P2P-Ariba-L2"


def test_docugen_keeps_manual_blocks_and_tracks_staleness(tmp_path):
    snapshot = snap("coupa-prod", [item("account_types", "US COA", {"currency": "USD"})])
    existing = ("# Old\n<!-- manual:sign-off -->\nApproved by J. Rao\n<!-- /manual:sign-off -->\n"
                "<!-- manual:glossary -->\nCOA = chart of accounts\n<!-- /manual:glossary -->\n")
    fake = FakeAnthropic([text_response("# FRD\n\n## Sign-off\n<!-- manual:sign-off -->\n\n## Scope\nUS COA")])
    doc = DocuGenAgent(LLM(client=fake)).generate("frd", snapshot, "Coupa", existing=existing)

    assert "Approved by J. Rao" in doc.split("## Scope")[0]  # placed where the model put the marker
    assert doc.rstrip().endswith("<!-- /manual:glossary -->")  # unplaced block kept at the end
    assert "<!-- manual:sign-off -->, <!-- manual:glossary -->" in fake.requests[0]["messages"][0]["content"]

    path = tmp_path / "frd.md"
    path.write_text(doc, encoding="utf-8")
    assert not is_stale(path, snapshot)
    changed = snap("coupa-prod", [item("account_types", "US COA", {"currency": "CAD"})])
    assert is_stale(path, changed)


def test_snapshot_store_round_trip(tmp_path):
    s = snap("coupa-sbx", [item("account_types", "US COA", {"currency": "USD"})])
    path = store.save(s, tmp_path)
    assert store.load(path).fingerprint() == s.fingerprint()
    assert store.latest("coupa", "coupa-sbx", tmp_path).items[0].key == "US COA"
