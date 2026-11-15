"""Checklist, test-result and UAT-script files written to the roll-out run folder.

CSV files are written with a UTF-8 BOM so Excel opens them with the right characters.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from p2p_agents.models import ActionResult, Issue
from p2p_agents.rollout.template import Rendered
from p2p_agents.rollout.verify import TestResult, summarize

StageStatus = Literal["planned", "blocked", "failed", "pending", "passed"]


class UatCase(BaseModel):
    id: str
    entity: str
    area: str
    scenario: str
    preconditions: str = ""
    steps: list[str]
    test_data: str = ""
    expected: str
    priority: Literal["High", "Medium", "Low"] = "High"
    source: Literal["template", "ai"] = "template"


class StageResult(BaseModel):
    instance: str
    status: StageStatus
    at: datetime
    applied: bool
    actions: list[dict[str, Any]] = Field(default_factory=list)  # planned: action, object_type, key
    results: list[ActionResult] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    import_files: list[str] = Field(default_factory=list)
    # "object_type:key" -> "created" / "updated", accumulated over every run of this environment
    changed: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class RunState(BaseModel):
    template: str
    platform: str
    entities: list[str]
    stages: dict[str, StageResult] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


STATUS_ICON = {"pass": "✅", "fail": "❌", "pending": "⏳", "skip": "➖"}
STAGE_ICON = {"passed": "✅", "failed": "❌", "blocked": "⛔", "pending": "⏳", "planned": "📝"}


def write_tests(path: Path, stage: StageResult) -> None:
    _write_csv(path, ["Test ID", "Category", "Entity", "Object type", "Key", "Test", "Result", "Detail"],
               [[t.id, t.category, t.entities, t.object_type, t.key, t.description, t.status.upper(), t.detail]
                for t in stage.tests])


def _cell(stage: StageResult | None, object_type: str, key: str) -> str:
    if stage is None:
        return "— not run"
    test = next((t for t in stage.tests if t.category == "configured" and t.object_type == object_type and t.key == key), None)
    action = next((a for a in stage.actions if a["object_type"] == object_type and a["key"] == key), None)
    result = next((r for r in stage.results if r.object_type == object_type and r.key == key), None)
    if result is not None and not result.ok:
        return f"❌ {result.action} failed: {result.error}"
    if not stage.applied:
        if stage.status == "blocked":
            return "⛔ blocked"
        return {"create": "📝 will create", "update": "📝 will update"}.get(action["action"] if action else "", "✔ already correct")
    verb = stage.changed.get(f"{object_type}:{key}", "already correct")
    if test is None:
        return f"✔ {verb}"
    if test.status == "pass":
        return f"✅ {verb}, verified"
    if test.status == "pending":
        return f"⏳ {verb} (import file), awaiting load"
    return f"❌ {verb}, check failed: {test.detail}"


def write_checklist(run_dir: Path, rendered: Rendered, state: RunState, descriptions: dict[str, str]) -> Path:
    stages = [state.stages.get(s) for s in state.order]
    header = ["#", "Entity", "Configuration", "Key", *state.order]
    rows: list[list[Any]] = []
    for n, r in enumerate(rendered.items, 1):
        rows.append([n, ", ".join(r.entities), descriptions.get(r.item.object_type, r.item.object_type), r.item.key,
                     *[_cell(s, r.item.object_type, r.item.key) for s in stages]])
    manual_rows = [[f"M{n}", label, "Manual step", m.item, *["☐ to do" for _ in stages]]
                   for n, (label, m) in enumerate(rendered.manual, 1)]
    _write_csv(run_dir / "checklist.csv", header, rows + manual_rows)

    md = [f"# Configuration checklist — {rendered.template}", "",
          f"Entities: **{', '.join(rendered.entities)}** · generated {datetime.now():%d %b %Y %H:%M}", ""]
    md.append("| Environment | Status | Tests passed | Failed | Pending |")
    md.append("|---|---|---|---|---|")
    for name in state.order:
        s = state.stages.get(name)
        if s is None:
            md.append(f"| {name} | — not run | | | |")
            continue
        c = summarize(s.tests)
        md.append(f"| {name} | {STAGE_ICON[s.status]} {s.status}{' — ' + s.note if s.note else ''} | {c['pass']} | {c['fail']} | {c['pending']} |")
    md += ["", "## Configuration items", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    md += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    rules = [(name, t) for name in state.order if (s := state.stages.get(name)) for t in s.tests if t.category == "rule"]
    if rules:
        md += ["", "## Business-rule checks", "", "| Environment | Test | Check | Result |", "|---|---|---|---|"]
        md += [f"| {name} | {t.id} | {t.description} | {STATUS_ICON[t.status]} {t.detail} |" for name, t in rules]
    if rendered.manual:
        md += ["", "## Manual steps (not automated)", ""]
        md += [f"- [ ] **{label}** — {m.item}" + (f" _(owner: {m.owner})_" if m.owner else "") for label, m in rendered.manual]
    path = run_dir / "checklist.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return path


def write_uat(run_dir: Path, template: str, cases: list[UatCase]) -> tuple[Path, Path]:
    csv_path = run_dir / "uat-scripts.csv"
    _write_csv(csv_path, ["Test ID", "Entity", "Area", "Scenario", "Preconditions", "Steps", "Test data",
                          "Expected result", "Priority", "Source", "Result (Pass/Fail)", "Tester", "Date", "Comments"],
               [[c.id, c.entity, c.area, c.scenario, c.preconditions,
                 "\n".join(f"{i}. {s}" for i, s in enumerate(c.steps, 1)), c.test_data, c.expected,
                 c.priority, "AI" if c.source == "ai" else "Template", "", "", "", ""] for c in cases])
    md = [f"# UAT test scripts — {template}", "", f"{len(cases)} test cases · generated {datetime.now():%d %b %Y %H:%M}", ""]
    for c in cases:
        md += [f"## {c.id} · {c.entity} · {c.scenario}", "",
               f"**Area:** {c.area} · **Priority:** {c.priority}" + (" · _AI-suggested_" if c.source == "ai" else ""), ""]
        if c.preconditions:
            md += [f"**Preconditions:** {c.preconditions}", ""]
        md += ["**Steps:**", *[f"{i}. {s}" for i, s in enumerate(c.steps, 1)], ""]
        if c.test_data:
            md += [f"**Test data:** {c.test_data}", ""]
        md += [f"**Expected result:** {c.expected}", "", "**Result:** ☐ Pass ☐ Fail   **Tester:** ________   **Date:** ________", ""]
    md_path = run_dir / "uat-scripts.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return csv_path, md_path
