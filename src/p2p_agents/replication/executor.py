"""Apply a replication plan to the target instance, with an audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from p2p_agents.connectors.base import Connector
from p2p_agents.models import ActionResult, ReplicationPlan
from p2p_agents.replication.diff import normalize, volatile_fields


class PlanBlocked(RuntimeError):
    pass


def apply_plan(
    plan: ReplicationPlan,
    connector: Connector,
    audit_dir: Path,
    approved_by: str | None = None,
) -> list[ActionResult]:
    """Execute the plan in order and stop at the first failure (later actions may depend on it).

    Production targets require `approved_by`. Before-images of every updated object are
    written to the audit log first, so a failed release can be rolled back.
    """
    if plan.blocked:
        raise PlanBlocked("plan has blocking issues; resolve them before applying")
    if connector.instance.is_production and not approved_by:
        raise PlanBlocked(f"{connector.instance.name} is production: an approver is required")

    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = audit_dir / f"{plan.platform}-{plan.target_instance}-{stamp}.jsonl"

    def audit(event: dict) -> None:
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}, default=str) + "\n")

    audit({"event": "start", "source": plan.source_instance, "target": plan.target_instance,
           "approved_by": approved_by, "actions": len(plan.actions)})
    for a in plan.actions:
        if a.action == "update":
            audit({"event": "before_image", "object_type": a.item.object_type, "key": a.item.key,
                   "target_id": a.target_id, "data": a.before})

    results: list[ActionResult] = []
    try:
        for a in plan.actions:
            cfg = connector.object_config(a.item.object_type)
            payload = normalize(a.item.data, volatile_fields(connector.platform, cfg))
            try:
                if a.action == "create":
                    new_id = connector.create(cfg, payload)
                    result = ActionResult(action="create", object_type=cfg.name, key=a.item.key, ok=True, target_id=new_id)
                else:
                    assert a.target_id is not None
                    connector.update(cfg, a.target_id, payload)
                    result = ActionResult(action="update", object_type=cfg.name, key=a.item.key, ok=True, target_id=a.target_id)
            except Exception as e:  # noqa: BLE001 - recorded and surfaced to the operator
                result = ActionResult(action=a.action, object_type=cfg.name, key=a.item.key, ok=False, error=str(e))
            results.append(result)
            audit({"event": "action", **result.model_dump()})
            if not result.ok:
                break
    finally:
        connector.close()
        audit({"event": "end", "ok": all(r.ok for r in results) and len(results) == len(plan.actions),
               "audit_file": str(audit_path)})
    return results
