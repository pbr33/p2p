from conftest import item, snap

from p2p_agents.replication import build_plan, compare


def codes(report_or_plan):
    return sorted((i.code, i.severity, i.key) for i in report_or_plan.issues)


def test_statuses_and_volatile_fields_ignored(platform):
    source = snap("sbx", [
        item("account_types", "US COA", {"currency": "USD", "id": 1, "updated-at": "2026-01-01"}),
        item("account_types", "EU COA", {"currency": "EUR"}),
    ])
    target = snap("prod", [
        item("account_types", "US COA", {"currency": "USD", "id": 99, "updated-at": "2025-05-05"}),
        item("account_types", "Legacy", {"currency": "GBP"}),
    ])
    report = compare(source, target, platform)
    status = {d.key: d.status for d in report.diffs}
    assert status == {"US COA": "unchanged", "EU COA": "added", "Legacy": "removed"}
    assert not report.has_blockers


def test_missing_parent_chart_of_accounts_blocks(platform):
    source = snap("sbx", [item("approval_rules", "Over 10k", {"limit": 10000}, deps=[("account_types", "APAC COA")])])
    target = snap("prod", [])
    report = compare(source, target, platform)
    assert codes(report) == [("MISSING_DEPENDENCY", "error", "Over 10k")]
    assert "exists in neither instance" in report.issues[0].message


def test_parent_replicated_in_same_release_is_fine_and_ordered_first(platform):
    source = snap("sbx", [
        item("approval_rules", "Over 10k", {"limit": 10000}, deps=[("account_types", "APAC COA")]),
        item("account_types", "APAC COA", {"currency": "SGD"}),
    ])
    target = snap("prod", [])
    report = compare(source, target, platform)
    assert not report.issues
    plan = build_plan(report, source, target)
    assert [(a.action, a.item.key) for a in plan.actions] == [("create", "APAC COA"), ("create", "Over 10k")]
    assert not plan.blocked


def test_selection_that_leaves_out_a_needed_parent_is_blocked(platform):
    source = snap("sbx", [
        item("approval_rules", "Over 10k", {"limit": 10000}, deps=[("account_types", "APAC COA")]),
        item("account_types", "APAC COA", {"currency": "SGD"}),
    ])
    target = snap("prod", [])
    plan = build_plan(compare(source, target, platform), source, target, select={"approval_rules"})
    assert plan.blocked
    assert "account_types:APAC COA" in plan.issues[0].message


def test_schema_conflict_and_immutable_change(platform):
    source = snap("sbx", [item("account_types", "US COA", {"currency": "CAD", "segments": "4"})])
    target = snap("prod", [item("account_types", "US COA", {"currency": "USD", "segments": 4}, sid="7")])
    report = compare(source, target, platform)
    assert codes(report) == [("IMMUTABLE_FIELD_CHANGE", "error", "US COA"), ("SCHEMA_CONFLICT", "warning", "US COA")]


def test_update_carries_target_id_and_before_image_and_removals_are_not_planned(platform):
    source = snap("sbx", [item("account_types", "US COA", {"currency": "USD", "segments": 5})])
    target = snap("prod", [item("account_types", "US COA", {"currency": "USD", "segments": 4}, sid="7"),
                           item("account_types", "Old", {"currency": "USD"}, sid="8")])
    plan = build_plan(compare(source, target, platform), source, target)
    [action] = plan.actions
    assert (action.action, action.target_id, action.before["segments"]) == ("update", "7", 4)
    assert plan.skipped_removals == ["account_types:Old"]


def test_dependency_cycle_is_reported(platform):
    source = snap("sbx", [
        item("approval_rules", "A", {}, deps=[("approval_rules", "B")]),
        item("approval_rules", "B", {}, deps=[("approval_rules", "A")]),
    ])
    plan = build_plan(compare(source, snap("prod", []), platform), source, snap("prod", []))
    assert any(i.code == "DEPENDENCY_CYCLE" for i in plan.issues)
    assert plan.blocked


def test_duplicate_natural_keys_block(platform):
    source = snap("sbx", [item("account_types", "X", {"currency": "USD"}), item("account_types", "X", {"currency": "EUR"})])
    report = compare(source, snap("prod", []), platform)
    assert ("DUPLICATE_KEY", "error", "X") in codes(report)
