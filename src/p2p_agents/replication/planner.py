"""Turn a diff into an ordered, validated replication plan."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from p2p_agents.models import DiffReport, Issue, PlannedAction, ReplicationPlan, Snapshot


def _selected(object_type: str, key: str, select: set[str] | None) -> bool:
    return select is None or object_type in select or f"{object_type}:{key}" in select


def build_plan(
    report: DiffReport,
    source: Snapshot,
    target: Snapshot,
    select: set[str] | None = None,
) -> ReplicationPlan:
    """Plan creates/updates for added and changed objects, parents before children.

    `select` limits the plan to object types ("tax_codes") or single objects
    ("tax_codes:VAT20"). Removals are never planned: deleting production config is
    reported for a human to do deliberately.
    """
    src_idx, tgt_idx = source.index(), target.index()
    chosen = {
        (d.object_type, d.key): d
        for d in report.diffs
        if d.status in ("added", "changed") and _selected(d.object_type, d.key, select)
    }
    moving_all = {(d.object_type, d.key) for d in report.diffs if d.status in ("added", "changed")}

    issues = [i for i in report.issues if (i.object_type, i.key) in chosen or i.code == "DUPLICATE_KEY"]
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {ref: set() for ref in chosen}
    for ref in chosen:
        for dep in src_idx[ref].depends_on:
            dep_ref = (dep.object_type, dep.key)
            if dep_ref in chosen:
                graph[ref].add(dep_ref)
            elif dep_ref not in tgt_idx and dep_ref in moving_all:
                issues.append(Issue(
                    severity="error", code="MISSING_DEPENDENCY", object_type=ref[0], key=ref[1],
                    message=f"needs {dep.object_type} {dep.key!r}, which is not in {target.instance} yet; "
                            f"include it in the selection ({dep.object_type}:{dep.key})",
                ))

    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as e:
        cycle = " -> ".join(f"{t}:{k}" for t, k in e.args[1])
        issues.append(Issue(severity="error", code="DEPENDENCY_CYCLE", object_type=e.args[1][0][0],
                            key=e.args[1][0][1], message=f"circular references: {cycle}"))
        order = sorted(graph)

    actions = []
    for ref in order:
        item = src_idx[ref]
        existing = tgt_idx.get(ref)
        if existing is None:
            actions.append(PlannedAction(action="create", item=item))
        else:
            actions.append(PlannedAction(action="update", item=item, target_id=existing.source_id, before=existing.data))
            if existing.source_id is None:
                issues.append(Issue(severity="error", code="MISSING_TARGET_ID", object_type=ref[0], key=ref[1],
                                    message=f"object exists in {target.instance} but its ID could not be read; "
                                            f"check id_field in the platform config"))

    removals = [f"{d.object_type}:{d.key}" for d in report.diffs
                if d.status == "removed" and _selected(d.object_type, d.key, select)]
    return ReplicationPlan(platform=report.platform, source_instance=report.source_instance,
                           target_instance=report.target_instance, actions=actions,
                           issues=issues, skipped_removals=removals)
