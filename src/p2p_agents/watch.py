"""Drift watch: re-test every environment that passed, and raise an alert when one no longer does.

Someone "quickly fixes" a tax rate by hand in QA, a transport overwrites an approval rule...
the configuration no longer matches what was tested and signed off. The watch reads each
passed environment again and runs its tests (nothing is changed). New failures are written
to rollout/watch.json (shown in the cockpit) and, when P2P_TEAMS_WEBHOOK is set, posted to
a Microsoft Teams channel through an incoming webhook.

    p2p watch               # once
    p2p watch --every 30    # every 30 minutes
    p2p cockpit --watch-every 30
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from p2p_agents.settings import CONFIG_DIR

log = logging.getLogger(__name__)
MAX_ALERTS = 100


def read_watch(runs_root: Path) -> dict[str, Any]:
    path = runs_root / "watch.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def check_drift(runs_root: Path, config_dir: Path = CONFIG_DIR, notify: bool = True) -> dict[str, Any]:
    from p2p_agents.agents.template_rollout import TemplateRolloutAgent
    from p2p_agents.workspace import load_run, run_dirs

    now = datetime.now().isoformat(timespec="seconds")
    checked, new_alerts, errors = [], [], []
    for d in run_dirs(runs_root):
        try:
            _, state = load_run(d.name, runs_root)
            passed = [e for e in state.order if e in state.stages and state.stages[e].status == "passed"]
            if not passed or not state.answers:
                continue
            agent = TemplateRolloutAgent(state.template, Path(state.answers), d, config_dir=config_dir,
                                         only=state.entities)
        except Exception as e:  # noqa: BLE001 - report and carry on with the other roll-outs
            errors.append({"run": d.name, "error": str(e)})
            continue
        for env in passed:
            try:
                stage = agent.verify_stage(env)
            except Exception as e:  # noqa: BLE001 - e.g. the system is unreachable
                errors.append({"run": d.name, "environment": env, "error": str(e)})
                continue
            checked.append(f"{d.name}/{env}")
            if stage.status != "passed":
                fails = [f"{t.id} [{t.entities}] {t.description}: {t.detail}" for t in stage.tests if t.status == "fail"]
                new_alerts.append({"at": now, "run": d.name, "environment": env, "status": stage.status,
                                   "failed": fails[:20]})

    state = read_watch(runs_root)
    alerts = (new_alerts + state.get("alerts", []))[:MAX_ALERTS]
    result = {"last_check": now, "checked": checked, "new_alerts": new_alerts, "alerts": alerts, "errors": errors}
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "watch.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if notify and new_alerts:
        result["teams"] = post_to_teams(new_alerts)
    return result


def post_to_teams(alerts: list[dict[str, Any]], webhook: str | None = None) -> str:
    """Post drift alerts to a Teams incoming webhook (P2P_TEAMS_WEBHOOK). Returns what happened."""
    webhook = webhook or os.environ.get("P2P_TEAMS_WEBHOOK")
    if not webhook:
        return "not sent (P2P_TEAMS_WEBHOOK not set)"
    lines = ["**⚠️ Configuration drift detected**", ""]
    for a in alerts:
        lines.append(f"**{a['environment']}** ({a['run']}) is now **{a['status']}**:")
        lines += [f"- {f}" for f in a["failed"][:5]]
    lines += ["", "Open the Go-Live Cockpit and use 🩹 Diagnose & fix."]
    try:
        r = httpx.post(webhook, json={"text": "\n\n".join(lines)}, timeout=15)
        r.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        log.warning("Teams webhook failed: %s", e)
        return f"failed: {e}"
