import json

import pytest
from conftest import item, snap

from p2p_agents.connectors.base import Connector
from p2p_agents.replication import PlanBlocked, apply_plan, build_plan, compare
from p2p_agents.settings import InstanceConfig


class RecordingConnector(Connector):
    def __init__(self, platform, production=False, fail_on=None):
        super().__init__(platform, InstanceConfig(name="prod", platform="coupa", is_production=production))
        self.calls, self.fail_on, self.closed = [], fail_on, False

    def fetch(self, cfg):
        return iter(())

    def create(self, cfg, payload):
        if payload.get("name") == self.fail_on:
            raise RuntimeError("HTTP 422 validation failed")
        self.calls.append(("create", payload))
        return "new-1"

    def update(self, cfg, target_id, payload):
        self.calls.append(("update", target_id, payload))

    def close(self):
        self.closed = True


def make_plan(platform):
    source = snap("sbx", [
        item("account_types", "APAC COA", {"currency": "SGD", "id": 3}),
        item("approval_rules", "Over 10k", {"limit": 20000}, deps=[("account_types", "APAC COA")]),
    ])
    target = snap("prod", [item("approval_rules", "Over 10k", {"limit": 10000}, deps=[("account_types", "APAC COA")], sid="77")])
    return build_plan(compare(source, target, platform), source, target)


def test_production_needs_an_approver(platform, tmp_path):
    with pytest.raises(PlanBlocked, match="approver"):
        apply_plan(make_plan(platform), RecordingConnector(platform, production=True), tmp_path)


def test_applies_in_order_strips_volatile_fields_and_audits(platform, tmp_path):
    conn = RecordingConnector(platform, production=True)
    results = apply_plan(make_plan(platform), conn, tmp_path, approved_by="cab-2026-114")
    assert all(r.ok for r in results)
    assert conn.calls[0] == ("create", {"name": "APAC COA", "currency": "SGD"})
    assert conn.calls[1][:2] == ("update", "77")
    events = [json.loads(line) for f in tmp_path.glob("*.jsonl") for line in f.read_text().splitlines()]
    assert [e["event"] for e in events] == ["start", "before_image", "action", "action", "end"]
    assert events[1]["data"]["limit"] == 10000 and events[0]["approved_by"] == "cab-2026-114"
    assert conn.closed


def test_stops_at_first_failure(platform, tmp_path):
    conn = RecordingConnector(platform, fail_on="APAC COA")
    results = apply_plan(make_plan(platform), conn, tmp_path)
    assert [(r.key, r.ok) for r in results] == [("APAC COA", False)]
    assert "422" in results[0].error and conn.calls == []


def test_blocked_plan_is_refused(platform, tmp_path):
    source = snap("sbx", [item("approval_rules", "R", {}, deps=[("account_types", "Missing")])])
    plan = build_plan(compare(source, snap("prod", []), platform), source, snap("prod", []))
    with pytest.raises(PlanBlocked):
        apply_plan(plan, RecordingConnector(platform), tmp_path)
