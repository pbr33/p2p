"""Configuration templates: one standard setup, filled per company code from a few answers.

A template (config/templates/<name>.toml) declares:

- ``[params.<name>]``  the questions to ask (type, pattern, default, example)
- ``[[objects]]``      the configuration objects to create, with ``${param}`` placeholders;
                       ``for_each = "<list param>"`` repeats an object per list entry (``${item.<field>}``)
- ``[[checks]]``       business rules verified in every environment after configuration
- ``[[uat]]``          user acceptance test cases
- ``[[manual]]``       steps that cannot be automated, listed on the checklist

Answers come from a CSV (one row per company code, for bulk roll-outs) or a TOML file
(``[[entity]]`` tables, or top-level keys for a single entity).
"""

from __future__ import annotations

import csv
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from p2p_agents.connectors.base import build_item
from p2p_agents.models import ConfigItem
from p2p_agents.settings import CONFIG_DIR, PlatformConfig

BUILTIN_TEMPLATES = Path(__file__).resolve().parents[3] / "config" / "templates"
_VAR = re.compile(r"\$\{([A-Za-z_][\w.]*)\}")


class TemplateError(ValueError):
    pass


class Param(BaseModel):
    name: str
    question: str
    type: Literal["text", "number", "bool", "list"] = "text"
    required: bool = True
    default: Any = None  # may use other answers, e.g. "${legal_entity} Buyers"
    pattern: str | None = None
    choices: list[str] = Field(default_factory=list)
    example: str = ""
    # list params: entries separated by ';', fields inside an entry by ':'; "rate:number" types a field
    fields: list[str] = Field(default_factory=list)


class ObjectSpec(BaseModel):
    type: str
    data: dict[str, Any]
    for_each: str | None = None


class CheckSpec(BaseModel):
    id: str
    description: str
    type: str
    key: str  # natural key; multi-part keys are joined with '|', e.g. "COMPANY_CODE_MAP|${company_code}"
    field: str
    op: Literal["eq", "ne", "gt", "ge", "lt", "le", "in", "exists", "len_eq"] = "eq"
    value: Any = None
    for_each: str | None = None


class UatSpec(BaseModel):
    id: str
    area: str
    scenario: str
    steps: list[str]
    expected: str
    priority: Literal["High", "Medium", "Low"] = "High"
    for_each: str | None = None


class ManualSpec(BaseModel):
    item: str
    owner: str = ""
    for_each: str | None = None


class Template(BaseModel):
    name: str
    platform: str
    description: str = ""
    label: str = ""  # how an entity is named in reports; defaults to the first answer
    params: dict[str, Param]
    objects: list[ObjectSpec]
    checks: list[CheckSpec] = Field(default_factory=list)
    uat: list[UatSpec] = Field(default_factory=list)
    manual: list[ManualSpec] = Field(default_factory=list)

    @property
    def object_types(self) -> list[str]:
        return list(dict.fromkeys(o.type for o in self.objects))


# ───────────────────────── loading ─────────────────────────

def template_dirs(config_dir: Path = CONFIG_DIR) -> list[Path]:
    return [config_dir / "templates", BUILTIN_TEMPLATES]


def list_templates(config_dir: Path = CONFIG_DIR) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in reversed(template_dirs(config_dir)):
        if d.is_dir():
            found.update({p.stem: p for p in sorted(d.glob("*.toml"))})
    return found


def load_template(name_or_path: str, config_dir: Path = CONFIG_DIR) -> Template:
    path = Path(name_or_path)
    if not path.suffix:
        path = list_templates(config_dir).get(name_or_path, path)
    if not path.exists():
        known = ", ".join(list_templates(config_dir)) or "none"
        raise FileNotFoundError(f"no template {name_or_path!r} (known: {known})")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    params = {k: Param(name=k, **v) for k, v in raw.pop("params", {}).items()}
    raw.setdefault("name", path.stem)
    template = Template(params=params, **raw)
    for spec in [*template.objects, *template.checks, *template.uat, *template.manual]:
        if spec.for_each and (spec.for_each not in params or params[spec.for_each].type != "list"):
            raise TemplateError(f"{path.name}: for_each={spec.for_each!r} is not a list param")
    return template


# ───────────────────────── answers ─────────────────────────

Entity = dict[str, Any]


def load_answers(path: Path) -> list[dict[str, str]]:
    """Raw answers, one dict per entity (company code)."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
        return [r for r in rows if any(r.values()) and not next(iter(r.values()), "").startswith("#")]
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    entities = raw.get("entity") or raw.get("entities") or [raw]
    return [{k: v for k, v in e.items()} for e in entities]


def _number(value: Any, what: str) -> int | float:
    if isinstance(value, bool):
        raise TemplateError(f"{what}: expected a number, got {value!r}")
    try:
        n = float(str(value).replace(",", ""))
    except ValueError:
        raise TemplateError(f"{what}: expected a number, got {value!r}") from None
    return int(n) if n.is_integer() else n


def _bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    raise TemplateError(f"{what}: expected yes/no, got {value!r}")


def _list(param: Param, value: Any, what: str) -> list[dict[str, Any]]:
    entries = value if isinstance(value, list) else [e for e in str(value).split(";") if e.strip()]
    fields = param.fields or ["value"]
    out = []
    for entry in entries:
        if isinstance(entry, dict):
            parts = [entry.get(f.split(":")[0], "") for f in fields]
        else:
            parts = [p.strip() for p in str(entry).split(":", len(fields) - 1)]
        if len(parts) < len(fields):
            raise TemplateError(f"{what}: {entry!r} needs {len(fields)} parts ({':'.join(f.split(':')[0] for f in fields)})")
        row: dict[str, Any] = {}
        for spec, part in zip(fields, parts):
            name, _, kind = spec.partition(":")
            row[name] = _number(part, f"{what} {name}") if kind == "number" else str(part).strip()
        out.append(row)
    return out


def resolve_entity(template: Template, answers: dict[str, Any], label: str = "") -> Entity:
    """Validate and type one entity's answers, filling defaults. Raises TemplateError listing every problem."""
    entity: Entity = {}
    problems: list[str] = []
    for name, p in template.params.items():
        value = answers.get(name)
        if value in (None, "") and p.default is not None:
            value = substitute(p.default, entity) if isinstance(p.default, str) else p.default
        if value in (None, "", []):
            if p.required:
                problems.append(f"{name}: required ({p.question})")
            else:
                entity[name] = [] if p.type == "list" else ""
            continue
        try:
            if p.type == "number":
                value = _number(value, name)
            elif p.type == "bool":
                value = _bool(value, name)
            elif p.type == "list":
                value = _list(p, value, name)
            else:
                value = str(value).strip()
                if p.pattern and not re.fullmatch(p.pattern, value):
                    raise TemplateError(f"{name}: {value!r} does not match {p.pattern}" + (f" (e.g. {p.example})" if p.example else ""))
                if p.choices and value not in p.choices:
                    raise TemplateError(f"{name}: {value!r} is not one of {', '.join(p.choices)}")
            entity[name] = value
        except TemplateError as e:
            problems.append(str(e))
    unknown = sorted(set(answers) - set(template.params))
    if unknown:
        problems.append(f"unknown answer(s): {', '.join(unknown)}")
    if problems:
        raise TemplateError(f"{label or 'answers'}: " + "; ".join(problems))
    return entity


def entity_label(template: Template, entity: Entity) -> str:
    if template.label:
        return str(substitute(template.label, entity))
    first = next(iter(template.params))
    return str(entity.get(first, "?"))


def resolve_all(template: Template, raw: list[dict[str, Any]]) -> list[Entity]:
    if not raw:
        raise TemplateError("the answers file has no entities")
    entities, problems = [], []
    for n, answers in enumerate(raw, 1):
        try:
            entities.append(resolve_entity(template, answers, label=f"entity {n}"))
        except TemplateError as e:
            problems.append(str(e))
    if problems:
        raise TemplateError("invalid answers:\n  " + "\n  ".join(problems))
    labels = [entity_label(template, e) for e in entities]
    dupes = sorted({x for x in labels if labels.count(x) > 1})
    if dupes:
        raise TemplateError(f"duplicate entities in answers: {', '.join(dupes)}")
    return entities


# ───────────────────────── rendering ─────────────────────────

def _lookup(name: str, ctx: dict[str, Any]) -> Any:
    node: Any = ctx
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            raise TemplateError(f"unknown placeholder ${{{name}}}")
        node = node[part]
    return node


def substitute(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill ${...} placeholders. A value that is exactly one placeholder keeps the answer's type."""
    if isinstance(value, str):
        m = _VAR.fullmatch(value)
        if m:
            return _lookup(m.group(1), ctx)
        return _VAR.sub(lambda m: str(_lookup(m.group(1), ctx)), value)
    if isinstance(value, dict):
        return {substitute(k, ctx): substitute(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, ctx) for v in value]
    return value


def contexts(spec: ObjectSpec | CheckSpec | UatSpec | ManualSpec, entity: Entity) -> list[dict[str, Any]]:
    if not spec.for_each:
        return [entity]
    return [{**entity, "item": entry} for entry in entity[spec.for_each]]


class RenderedItem(BaseModel):
    item: ConfigItem
    entities: list[str]


class Rendered(BaseModel):
    """Everything a template produces for a set of entities."""

    template: str
    platform: str
    entities: list[str]
    answers: list[dict[str, Any]]
    items: list[RenderedItem]
    checks: list[tuple[str, CheckSpec]]  # (entity, rendered check)
    uat: list[tuple[str, UatSpec]]
    manual: list[tuple[str, ManualSpec]]

    def config_items(self) -> list[ConfigItem]:
        return [r.item for r in self.items]


def render(template: Template, platform: PlatformConfig, entities: list[Entity]) -> Rendered:
    if platform.name != template.platform:
        raise TemplateError(f"template {template.name} is for {template.platform}, not {platform.name}")
    by_ref: dict[tuple[str, str], RenderedItem] = {}
    checks, uat, manual = [], [], []
    for entity in entities:
        label = entity_label(template, entity)
        for spec in template.objects:
            cfg = platform.objects.get(spec.type)
            if cfg is None:
                raise TemplateError(f"{platform.display_name} has no object type {spec.type!r}")
            if cfg.read_only:
                raise TemplateError(f"{spec.type} is read only and cannot be configured by a template")
            for ctx in contexts(spec, entity):
                item = build_item(platform.name, cfg, substitute(spec.data, ctx))
                if item is None:
                    raise TemplateError(f"{label}: {spec.type} object has no value for its key {cfg.key!r}")
                ref = (item.object_type, item.key)
                if ref not in by_ref:
                    by_ref[ref] = RenderedItem(item=item, entities=[label])
                elif by_ref[ref].item.data != item.data:
                    first = by_ref[ref]
                    raise TemplateError(
                        f"{spec.type} {item.key!r} is shared by {', '.join(first.entities)} and {label} "
                        f"but with different values; make the answers agree or the key unique per entity")
                elif label not in by_ref[ref].entities:
                    by_ref[ref].entities.append(label)
        for c in template.checks:
            checks += [(label, CheckSpec(**substitute(c.model_dump(exclude={"for_each"}), ctx))) for ctx in contexts(c, entity)]
        for u in template.uat:
            uat += [(label, UatSpec(**substitute(u.model_dump(exclude={"for_each"}), ctx))) for ctx in contexts(u, entity)]
        for m in template.manual:
            manual += [(label, ManualSpec(**substitute(m.model_dump(exclude={"for_each"}), ctx))) for ctx in contexts(m, entity)]
    return Rendered(
        template=template.name, platform=platform.name,
        entities=[entity_label(template, e) for e in entities], answers=entities,
        items=list(by_ref.values()), checks=checks, uat=uat, manual=_dedupe(manual),
    )


def _dedupe(rows: list[tuple[str, ManualSpec]]) -> list[tuple[str, ManualSpec]]:
    seen, out = set(), []
    for label, m in rows:
        if m.item not in seen:
            seen.add(m.item)
            out.append((label, m))
    return out


# ───────────────────────── questions ─────────────────────────

def answers_template(template: Template, path: Path) -> None:
    """Write an answers file with one example row, ready to fill for every company code."""
    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(list(template.params))
            writer.writerow([_example(p) for p in template.params.values()])
        return
    lines = [f"# Answers for template {template.name}. Copy the [[entity]] block once per company code.", "", "[[entity]]"]
    for p in template.params.values():
        lines.append(f"# {p.question}" + ("" if p.required else " (optional)"))
        lines.append(f"{p.name} = {_toml_value(_example(p), p)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _example(p: Param) -> Any:
    """Value for the example row. Defaults built from other answers stay empty so they still apply."""
    if p.example:
        return p.example
    if p.default is None or (isinstance(p.default, str) and "${" in p.default):
        return ""
    return p.default


def _toml_value(value: Any, p: Param) -> str:
    if p.type == "number" and value not in ("", None):
        return str(value)
    if p.type == "bool" and isinstance(value, bool):
        return "true" if value else "false"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(template: Template, existing: list[dict[str, Any]] | None = None, input_fn=input) -> list[dict[str, Any]]:
    """Interactive questions, one company code at a time; answers are validated as they are typed."""
    entities = list(existing or [])
    while True:
        answers: dict[str, Any] = {}
        print(f"\n— New entity for template {template.name} —")
        for name, p in template.params.items():
            hint = []
            if p.example:
                hint.append(f"e.g. {p.example}")
            if p.default is not None:
                hint.append(f"default {substitute(p.default, answers) if isinstance(p.default, str) else p.default}")
            if p.type == "list":
                hint.append("separate entries with ;" + (f", fields {':'.join(f.split(':')[0] for f in p.fields)}" if p.fields else ""))
            while True:
                value = input_fn(f"{p.question}{' [' + '; '.join(hint) + ']' if hint else ''}: ").strip()
                trial = {**answers, name: value}
                try:
                    resolve_entity(template.model_copy(update={"params": {k: template.params[k] for k in trial}}), trial)
                    answers[name] = value
                    break
                except TemplateError as e:
                    print(f"  ✗ {str(e).split(': ', 1)[-1]}")
        entities.append(answers)
        if input_fn("Add another company code? [y/N]: ").strip().lower() not in ("y", "yes"):
            return entities


def write_answers(entities: list[dict[str, Any]], template: Template, path: Path) -> None:
    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(template.params))
            writer.writeheader()
            writer.writerows(entities)
        return
    blocks = []
    for e in entities:
        lines = ["[[entity]]"] + [f"{k} = {_toml_value(v, template.params[k])}" for k, v in e.items() if v != ""]
        blocks.append("\n".join(lines))
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
