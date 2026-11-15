"""What is going on across all roll-outs, read from the run folders (no AI, no system calls).

Used by the Go-Live Cockpit and the Copilot. A run folder is `rollout/<name>/` with
state.json (environments and tests), checklist.csv, uat-scripts.csv (results filled in
by testers in Excel) and manual.json (manual steps ticked off in the cockpit).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from p2p_agents.rollout.reports import RunState, manual_done, read_csv
from p2p_agents.rollout.template import (Template, TemplateError, entity_label, load_answers, load_template,
                                         resolve_entity, write_answers)
from p2p_agents.rollout.verify import summarize
from p2p_agents.settings import CONFIG_DIR

RUNS_DIR = Path(os.environ.get("P2P_ROLLOUT_DIR", "rollout"))
STAGE_SCORE = {"passed": 1.0, "pending": 0.5, "planned": 0.0, "blocked": 0.0, "failed": 0.0}


def run_dirs(root: Path = RUNS_DIR) -> list[Path]:
    return sorted((p.parent for p in root.glob("*/state.json")), key=lambda p: p.stat().st_mtime, reverse=True)


def load_run(name: str, root: Path = RUNS_DIR) -> tuple[Path, RunState]:
    run_dir = root / name
    path = run_dir / "state.json"
    if not path.exists():
        known = ", ".join(p.name for p in run_dirs(root)) or "none yet"
        raise LookupError(f"no roll-out named {name!r} (known: {known})")
    return run_dir, RunState.model_validate_json(path.read_text(encoding="utf-8"))


def _result(row: dict[str, str]) -> str:
    value = row.get("Result (Pass/Fail)", "").strip().lower()
    return "pass" if value.startswith("pass") else "fail" if value.startswith("fail") else ""


def uat_progress(run_dir: Path) -> dict[str, Any]:
    rows = read_csv(run_dir / "uat-scripts.csv")
    by_entity: dict[str, dict[str, int]] = {}
    failed = []
    for r in rows:
        e = by_entity.setdefault(r.get("Entity", "?"), {"total": 0, "pass": 0, "fail": 0})
        e["total"] += 1
        res = _result(r)
        if res:
            e[res] += 1
        if res == "fail":
            failed.append({"id": r.get("Test ID"), "scenario": r.get("Scenario"), "tester": r.get("Tester"),
                           "comments": r.get("Comments")})
    total = len(rows)
    passed = sum(e["pass"] for e in by_entity.values())
    fails = sum(e["fail"] for e in by_entity.values())
    return {"total": total, "executed": passed + fails, "pass": passed, "fail": fails,
            "by_entity": by_entity, "failed": failed}


def manual_steps(run_dir: Path) -> list[dict[str, str]]:
    marks = manual_done(run_dir)
    return [{"id": r["#"], "entity": r.get("Entity", ""), "step": r.get("Key", ""), "done": marks.get(r["#"], "")}
            for r in read_csv(run_dir / "checklist.csv") if r.get("#", "").startswith("M")]


def readiness(state: RunState, uat: dict[str, Any], manual: list[dict[str, str]]) -> dict[str, Any]:
    """Go-live readiness 0-100: configuration 50%, UAT 35%, manual steps 15% — plus what blocks it."""
    stages = [state.stages[s] for s in state.order if s in state.stages]
    config = sum(STAGE_SCORE[s.status] for s in stages) / len(stages) if stages else 0.0
    uat_score = uat["pass"] / uat["total"] if uat["total"] else 0.0
    manual_score = sum(bool(m["done"]) for m in manual) / len(manual) if manual else 1.0
    blockers = []
    for s in stages:
        if s.status != "passed":
            blockers.append(f"{s.instance} is {s.status}" + (f": {s.note}" if s.note else ""))
        blockers += [f"{s.instance} test {t.id} [{t.entities}] {t.description}: {t.detail}"
                     for t in s.tests if t.status == "fail"][:5]
    blockers += [f"UAT {f['id']} failed: {f['scenario']}" + (f" ({f['comments']})" if f["comments"] else "")
                 for f in uat["failed"]]
    todo = [m for m in manual if not m["done"]]
    nxt = []
    for s in stages:
        if s.status == "pending":
            nxt.append(f"load the import files into {s.instance}, then Re-test it")
        elif s.status != "passed":
            nxt.append(f"fix what blocks {s.instance}, then re-run the roll-out")
    if uat["total"] and uat["executed"] < uat["total"]:
        nxt.append(f"execute the remaining {uat['total'] - uat['executed']} UAT case(s)")
    if todo:
        nxt.append(f"complete {len(todo)} manual step(s)")
    return {"score": round(100 * (0.5 * config + 0.35 * uat_score + 0.15 * manual_score)),
            "config": round(100 * config), "uat": round(100 * uat_score), "manual": round(100 * manual_score),
            "blockers": blockers, "next": nxt or ["ready for go-live sign-off"]}


def run_summary(run_dir: Path, state: RunState, detail: bool = False) -> dict[str, Any]:
    uat = uat_progress(run_dir)
    manual = manual_steps(run_dir)
    out: dict[str, Any] = {
        "name": run_dir.name, "template": state.template, "platform": state.platform,
        "entities": state.entities, "answers": state.answers,
        "stages": [], "uat": uat, "manual": manual, "readiness": readiness(state, uat, manual),
    }
    for name in state.order:
        s = state.stages.get(name)
        if s is None:
            continue
        stage: dict[str, Any] = {
            "instance": name, "status": s.status, "note": s.note, "at": s.at.isoformat(), "applied": s.applied,
            "tests": summarize(s.tests),
            # once applied: everything this roll-out has changed there so far, across re-runs
            "creates": sum(v == "created" for v in s.changed.values()) if s.applied
            else sum(a["action"] == "create" for a in s.actions),
            "updates": sum(v == "updated" for v in s.changed.values()) if s.applied
            else sum(a["action"] == "update" for a in s.actions),
            "import_files": s.import_files,
        }
        if detail:
            stage["problems"] = [t.model_dump() for t in s.tests if t.status in ("fail", "pending")]
            stage["issues"] = [i.model_dump() for i in s.issues]
            stage["failed_changes"] = [r.model_dump() for r in s.results if not r.ok]
        out["stages"].append(stage)
    if detail:
        out["checklist"] = [r for r in read_csv(run_dir / "checklist.csv") if not r.get("#", "").startswith("M")]
    return out


def all_runs(root: Path = RUNS_DIR) -> list[dict[str, Any]]:
    out = []
    for d in run_dirs(root):
        try:
            _, state = load_run(d.name, root)
            out.append(run_summary(d, state))
        except Exception as e:  # noqa: BLE001 - one broken folder must not hide the others
            out.append({"name": d.name, "error": str(e)})
    return out


# ───────── answers files ─────────

def upsert_entity(template_name: str, answers: Path, values: dict[str, Any], copy_from: str | None = None,
                  config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Add a company code to an answers file (or change it), optionally copying another one's answers.

    Validates against the template first; the file is only written when the answers are valid.
    """
    template: Template = load_template(template_name, config_dir)
    rows = load_answers(answers) if answers.exists() else []
    key = next(iter(template.params))  # first question identifies the entity, e.g. company_code

    def label(row: dict[str, Any]) -> str:
        return str(row.get(key, ""))

    base: dict[str, Any] = {}
    if copy_from:
        base = next((dict(r) for r in rows if label(r) == copy_from), {})
        if not base:
            raise TemplateError(f"{copy_from} is not in {answers}; known: {', '.join(map(label, rows)) or 'none'}")
        # Answers derived from another answer (e.g. buyer group from legal entity) must be re-derived
        for name, p in template.params.items():
            if isinstance(p.default, str) and "${" in p.default and name not in values:
                base[name] = ""
    new = {**base, **{k: ("" if v is None else v) for k, v in values.items()}}
    entity = resolve_entity(template, new, label=str(new.get(key) or "new entity"))
    replaced = any(label(r) == label(new) for r in rows)
    rows = [new if label(r) == label(new) else r for r in rows] if replaced else [*rows, new]
    entities = [resolve_entity(template, r, label=label(r)) for r in rows]  # the whole file must stay valid
    answers.parent.mkdir(parents=True, exist_ok=True)
    write_answers([_normal(r, e, template) for r, e in zip(rows, entities)], template, answers)
    return {"entity": entity_label(template, entity), "action": "updated" if replaced else "added",
            "answers_file": str(answers), "entities": [entity_label(template, e) for e in entities],
            "values": entity}


def _normal(raw: dict[str, Any], entity: dict[str, Any], template: Template) -> dict[str, Any]:
    """One answers row as written to the file: typed numbers/yes-no, lists as code:rate:description;..."""
    out = {}
    for name, p in template.params.items():
        value = raw.get(name, "")
        if p.type in ("number", "bool") and value not in ("", None):
            value = entity[name]
        elif p.type == "list" and isinstance(value, list):
            fields = [f.split(":")[0] for f in (p.fields or ["value"])]
            value = ";".join(":".join(str(e.get(f, "")) if isinstance(e, dict) else str(e) for f in fields) for e in value)
        out[name] = value
    return out
