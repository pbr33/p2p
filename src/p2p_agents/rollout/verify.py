"""Automated configuration tests run in every environment of a roll-out.

- preflight:  before anything is written, every reference (approver, parent object) must
              exist in the target or be created by the same roll-out.
- configured: after the change, each template object exists and holds the expected values.
- reference:  after the change, everything it points to exists in the target.
- rule:       the template's business-rule checks (thresholds, rates, chain lengths...).

File-based platforms (Ariba) cannot be read back until the import files have been loaded
and the realm re-exported, so missing objects there are `pending`, not failures.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from p2p_agents.models import Snapshot
from p2p_agents.paths import get_values
from p2p_agents.replication.diff import normalize, volatile_fields
from p2p_agents.rollout.template import CheckSpec, Rendered
from p2p_agents.settings import PlatformConfig

Status = Literal["pass", "fail", "pending", "skip"]


class TestResult(BaseModel):
    id: str
    category: Literal["preflight", "configured", "reference", "rule"]
    entities: str
    object_type: str
    key: str
    description: str
    status: Status
    detail: str = ""


def _scalar_equal(expected: Any, actual: Any) -> bool:
    if expected == actual:
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return str(expected).lower() == str(actual).lower()
    try:
        return float(expected) == float(actual)  # "18.0" from an API vs 18 in the template
    except (TypeError, ValueError):
        return False


def mismatches(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Fields of `expected` that differ in `actual`. Extra fields in `actual` are fine."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path or 'value'}: expected an object, found {actual!r}"]
        out: list[str] = []
        for k, v in expected.items():
            out += mismatches(v, actual.get(k), f"{path}.{k}" if path else str(k))
        return out
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path}: expected {len(expected)} entries, found {actual!r}"]
        out = []
        for i, (e, a) in enumerate(zip(expected, actual)):
            out += mismatches(e, a, f"{path}[{i}]")
        return out
    if actual is None:
        return [f"{path}: missing (expected {expected!r})"]
    return [] if _scalar_equal(expected, actual) else [f"{path}: expected {expected!r}, found {actual!r}"]


def reference_tests(rendered: Rendered, target: Snapshot, *, preflight: bool, file_based: bool = False) -> list[TestResult]:
    """Every reference resolves in the target (preflight: or is created by this roll-out)."""
    present = set(target.index())
    created = {(r.item.object_type, r.item.key) for r in rendered.items}
    extracted = set(target.object_types)
    seen: set[tuple[str, str, str, str]] = set()
    out = []
    for r in rendered.items:
        for dep in r.item.depends_on:
            sig = (r.item.object_type, r.item.key, dep.object_type, dep.key)
            if sig in seen:
                continue
            seen.add(sig)
            ref = (dep.object_type, dep.key)
            if ref in present:
                status, detail = "pass", f"exists in {target.instance}"
            elif preflight and ref in created:
                status, detail = "pass", "created by this roll-out"
            elif file_based and ref in created:
                status, detail = "pending", "created by this roll-out; awaiting import"
            elif dep.object_type not in extracted:
                status, detail = "skip", f"{dep.object_type} is not read from {target.instance}; check manually"
            else:
                status = "fail"
                detail = (f"{dep.object_type} {dep.key!r} does not exist in {target.instance}"
                          + (" — create it there first or change the answer" if preflight else ""))
            out.append(TestResult(
                id="", category="preflight" if preflight else "reference", entities=", ".join(r.entities),
                object_type=r.item.object_type, key=r.item.key,
                description=f"{r.item.object_type} {r.item.key!r} → {dep.object_type} {dep.key!r}",
                status=status, detail=detail))
    return out


def configured_tests(rendered: Rendered, target: Snapshot, platform: PlatformConfig, *, file_based: bool) -> list[TestResult]:
    idx = target.index()
    out = []
    for r in rendered.items:
        item = r.item
        cfg = platform.objects[item.object_type]
        found = idx.get((item.object_type, item.key))
        desc = f"{cfg.description or item.object_type}: {item.key}"
        if found is None:
            status: Status = "pending" if file_based else "fail"
            detail = ("import file written; load it and re-export, then run `p2p rollout verify`"
                      if file_based else f"not found in {target.instance}")
        else:
            vol = volatile_fields(platform, cfg)
            diffs = mismatches(normalize(item.data, vol), normalize(found.data, vol))
            status, detail = ("fail", "; ".join(diffs[:5])) if diffs else ("pass", "values match the template")
        out.append(TestResult(id="", category="configured", entities=", ".join(r.entities),
                              object_type=item.object_type, key=item.key, description=desc,
                              status=status, detail=detail))
    return out


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "exists":
        return actual is not None
    if op == "len_eq":
        return isinstance(actual, list) and len(actual) == int(expected)
    if op == "in":
        return any(_scalar_equal(e, actual) for e in (expected or []))
    if op in ("eq", "ne"):
        return _scalar_equal(expected, actual) == (op == "eq")
    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"gt": a > e, "ge": a >= e, "lt": a < e, "le": a <= e}[op]


def rule_tests(rendered: Rendered, target: Snapshot, *, file_based: bool) -> list[TestResult]:
    idx = target.index()
    out = []
    for entity, check in rendered.checks:
        found = idx.get((check.type, check.key))
        if found is None:
            status: Status = "pending" if file_based else "fail"
            detail = f"{check.type} {check.key!r} not found in {target.instance}"
        else:
            values = get_values(found.data, check.field)
            actual = values[0] if len(values) == 1 else (values or None)
            ok = _compare(check.op, actual, check.value)
            status = "pass" if ok else "fail"
            shown = f"{len(actual)} entries" if check.op == "len_eq" and isinstance(actual, list) else repr(actual)
            detail = f"{check.field} = {shown}" + ("" if ok else f" (expected {_expectation(check)})")
        out.append(TestResult(id=check.id, category="rule", entities=entity, object_type=check.type,
                              key=check.key, description=check.description, status=status, detail=detail))
    return out


def _expectation(check: CheckSpec) -> str:
    words = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "in": "one of", "len_eq": "entries ="}
    return "to exist" if check.op == "exists" else f"{words[check.op]} {check.value!r}"


def number(tests: list[TestResult]) -> list[TestResult]:
    prefix = {"preflight": "PRE", "configured": "CFG", "reference": "REF", "rule": "RULE"}
    counters: dict[str, int] = {}
    for t in tests:
        counters[t.category] = counters.get(t.category, 0) + 1
        base = f"{prefix[t.category]}-{counters[t.category]:03d}"
        t.id = f"{base} {t.entities}/{t.id}" if t.category == "rule" and t.id else base
    return tests


def summarize(tests: list[TestResult]) -> dict[str, int]:
    out = {"pass": 0, "fail": 0, "pending": 0, "skip": 0}
    for t in tests:
        out[t.status] += 1
    return out
