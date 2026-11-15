"""Consistency across company codes: catch what a person comparing 50 rows by eye would miss.

Looks at every company code we know — rolled out (run folders) and drafts (answers files
read from client documents) — and flags:

- conflict: the same tax code with different rates in different company codes;
- outlier:  a number far from its peers in the same currency (e.g. an approval limit 10×
            the median: a typo like "15,00,000" for "1,50,000");
- missing:  a tax code most company codes of the same country have, but this one does not;
- unusual:  an approver who otherwise only approves for another country;
- duplicate: the same company code with different answers in two files.

Deterministic — no AI — so it can gate a roll-out. The matrix (question × company code)
feeds the cockpit's heatmap.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from p2p_agents.rollout.template import Template, TemplateError, _bool, _list, _number, load_answers, load_template
from p2p_agents.settings import CONFIG_DIR

OUTLIER_FACTOR = 5.0
MIN_PEERS = 3


def _lenient(template: Template, raw: dict[str, Any]) -> dict[str, Any]:
    """Typed answers where possible; a value that does not parse is kept as text."""
    out: dict[str, Any] = {}
    for name, p in template.params.items():
        value = raw.get(name, "")
        if value in ("", None):
            out[name] = "" if p.type != "list" else []
            continue
        try:
            out[name] = (_number(value, name) if p.type == "number" else _bool(value, name) if p.type == "bool"
                         else _list(p, value, name) if p.type == "list" else str(value).strip())
        except TemplateError:
            out[name] = str(value)
    return out


def collect(runs_root: Path, config_dir: Path = CONFIG_DIR) -> list[dict[str, Any]]:
    """Every (template, answers file, company code) we know about, rolled out or draft."""
    from p2p_agents.workspace import load_run, run_dirs

    sources: dict[Path, tuple[str, str, list[str] | None]] = {}
    for d in run_dirs(runs_root):
        try:
            _, state = load_run(d.name, runs_root)
        except Exception:  # noqa: BLE001
            continue
        if state.answers and Path(state.answers).exists():
            sources[Path(state.answers).resolve()] = (state.template, f"rolled out ({d.name})", state.entities)
    for meta in (runs_root / "answers").glob("*.meta.json") if (runs_root / "answers").exists() else []:
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            continue
        answers = Path(info.get("answers", "")).resolve()
        if answers.exists() and answers not in sources:
            sources[answers] = (info["template"], "draft from client document", None)

    rows = []
    for answers, (template_name, origin, only) in sources.items():
        try:
            template = load_template(template_name, config_dir)
        except (FileNotFoundError, TemplateError):
            continue
        key = next(iter(template.params))
        for raw in load_answers(answers):
            code = str(raw.get(key, "")).strip()
            if not code or (only is not None and code not in only):
                continue
            rows.append({"code": code, "template": template.name, "platform": template.platform,
                         "origin": origin, "file": answers.name, "raw": raw, "values": _lenient(template, raw),
                         "params": template.params})
    return rows


def check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def add(severity: str, kind: str, code: str, question: str, message: str) -> None:
        findings.append({"severity": severity, "kind": kind, "entity": code, "question": question, "message": message})

    # duplicates: same company code, same template, different answers
    by_code: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_code[(r["template"], r["code"])].append(r)
    for (template, code), same in by_code.items():
        for other in same[1:]:
            diff = [q for q in same[0]["values"] if str(same[0]["values"][q]) != str(other["values"].get(q))]
            if diff:
                add("error", "duplicate", code, ", ".join(diff),
                    f"{code} is in {same[0]['file']} and {other['file']} with different answers ({', '.join(diff)})")
    unique = [same[0] for same in by_code.values()]

    # list answers (tax codes): same code with different rates; codes most peers of the country have
    for (template, question), group in _by_question(unique, "list").items():
        rate_field = _numeric_field(group[0]["params"][question])
        rates: dict[str, dict[Any, list[str]]] = defaultdict(lambda: defaultdict(list))
        for r in group:
            for entry in r["values"][question]:
                if isinstance(entry, dict) and rate_field and "code" in entry:
                    rates[entry["code"]][entry.get(rate_field)].append(r["code"])
        for tax, by_rate in rates.items():
            if len(by_rate) > 1:
                detail = "; ".join(f"{rate}% in {', '.join(codes)}" for rate, codes in by_rate.items())
                sizes = sorted((len(c) for c in by_rate.values()), reverse=True)
                majority = max(by_rate, key=lambda rate: len(by_rate[rate])) if sizes[0] > sizes[1] else None
                for rate, codes in by_rate.items():
                    if rate == majority:
                        continue  # flag the odd ones out, not everyone who agrees
                    for c in codes:
                        add("error", "conflict", c, question, f"{tax} has different rates: {detail}")
        by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in group:
            by_country[str(r["values"].get("country", ""))].append(r)
        for country, peers in by_country.items():
            if len(peers) < MIN_PEERS:
                continue
            counts = Counter(e.get("code") for r in peers for e in r["values"][question] if isinstance(e, dict))
            for r in peers:
                have = {e.get("code") for e in r["values"][question] if isinstance(e, dict)}
                for tax, n in counts.items():
                    if tax not in have and n / len(peers) >= 0.6:
                        add("warning", "missing", r["code"], question,
                            f"{r['code']} has no {tax}, which {n} of {len(peers)} {country} company codes have")

    # numbers: far from the median of peers with the same currency
    for (template, question), group in _by_question(unique, "number").items():
        by_currency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in group:
            if isinstance(r["values"][question], (int, float)) and not isinstance(r["values"][question], bool):
                by_currency[str(r["values"].get("currency", ""))].append(r)
        for currency, peers in by_currency.items():
            if len(peers) < MIN_PEERS:
                continue
            for r in peers:
                others = [p["values"][question] for p in peers if p is not r]
                median = statistics.median(others)
                value = r["values"][question]
                if median > 0 and (value >= OUTLIER_FACTOR * median or value * OUTLIER_FACTOR <= median):
                    factor = value / median
                    add("warning", "outlier", r["code"], question,
                        f"{r['code']}: {value:,} {currency} is {factor:.1f}× the median of the other {currency} "
                        f"company codes ({median:,.0f}) — typo?")

    # approvers who otherwise only approve for another country
    for (template, question), group in _by_question(unique, "text").items():
        if "approver" not in question:
            continue
        countries: dict[str, set[str]] = defaultdict(set)
        for r in group:
            if r["values"][question]:
                countries[r["values"][question]].add(str(r["values"].get("country", "")))
        for r in group:
            login, country = r["values"][question], str(r["values"].get("country", ""))
            elsewhere = countries.get(login, set()) - {country}
            peers = [p for p in group if p["values"][question] == login]
            if login and elsewhere and len(peers) >= 2 and sum(str(p["values"].get("country")) == country for p in peers) == 1:
                add("info", "unusual", r["code"], question,
                    f"{r['code']} ({country}) uses {login}, who otherwise approves for {', '.join(sorted(elsewhere))}")

    flagged = {(f["entity"], f["question"]) for f in findings}
    questions = list(dict.fromkeys(q for r in unique for q in r["params"]))
    matrix = {
        "questions": questions,
        "entities": [{"code": r["code"], "template": r["template"], "origin": r["origin"]} for r in unique],
        "cells": [[_cell(r, q) for r in unique] for q in questions],
        "flags": [[_flag(findings, r["code"], q) for r in unique] for q in questions],
    }
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["entity"]))
    return {"findings": findings, "matrix": matrix, "company_codes": len(unique),
            "flagged_cells": len(flagged)}


def _by_question(rows: list[dict[str, Any]], kind: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        for q, p in r["params"].items():
            if p.type == kind:
                out[(r["template"], q)].append(r)
    return out


def _numeric_field(param: Any) -> str | None:
    return next((f.split(":")[0] for f in param.fields if f.endswith(":number")), None)


def _cell(row: dict[str, Any], question: str) -> str:
    value = row["values"].get(question, "")
    if isinstance(value, list):
        return ", ".join(f"{e.get('code')} {e.get('rate', '')}".strip() if isinstance(e, dict) else str(e) for e in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _flag(findings: list[dict[str, Any]], code: str, question: str) -> str:
    sev = [f["severity"] for f in findings if f["entity"] == code and question in f["question"].split(", ")]
    return "error" if "error" in sev else "warning" if "warning" in sev else "info" if sev else ""


def consistency(runs_root: Path, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return check(collect(runs_root, config_dir))
