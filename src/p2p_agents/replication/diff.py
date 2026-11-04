"""Staging-to-production diff with dependency and schema validation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from p2p_agents.models import DiffReport, FieldChange, Issue, ItemDiff, Snapshot
from p2p_agents.settings import ObjectTypeConfig, PlatformConfig


def volatile_fields(platform: PlatformConfig, cfg: ObjectTypeConfig) -> set[str]:
    return set(platform.default_volatile) | set(cfg.volatile) | {cfg.id_field}


def normalize(data: Any, volatile: set[str], path: str = "") -> Any:
    """Drop system-managed fields. Plain names are dropped at any depth; dotted names only at that path."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            here = f"{path}.{k}" if path else k
            if k in volatile or here in volatile:
                continue
            out[k] = normalize(v, volatile, here)
        return out
    if isinstance(data, list):
        return [normalize(v, volatile, path) for v in data]
    return data


def field_changes(source: Any, target: Any, path: str = "") -> list[FieldChange]:
    if isinstance(source, dict) and isinstance(target, dict):
        changes: list[FieldChange] = []
        for k in sorted(set(source) | set(target), key=str):
            here = f"{path}.{k}" if path else str(k)
            changes.extend(field_changes(source.get(k), target.get(k), here))
        return changes
    if isinstance(source, list) and isinstance(target, list) and len(source) == len(target):
        changes = []
        for i, (s, t) in enumerate(zip(source, target)):
            changes.extend(field_changes(s, t, f"{path}[{i}]"))
        return changes
    if source != target:
        return [FieldChange(path=path, source=source, target=target)]
    return []


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _top_field(path: str) -> str:
    return path.split(".")[0].split("[")[0]


def compare(source: Snapshot, target: Snapshot, platform: PlatformConfig) -> DiffReport:
    if source.platform != target.platform:
        raise ValueError(f"cannot diff {source.platform} against {target.platform}")
    issues: list[Issue] = []

    for snap in (source, target):
        dupes = Counter((i.object_type, i.key) for i in snap.items)
        for (object_type, key), n in dupes.items():
            if n > 1:
                issues.append(Issue(
                    severity="error", code="DUPLICATE_KEY", object_type=object_type, key=key,
                    message=f"{n} objects share natural key {key!r} in {snap.instance}; fix the key config or the data",
                ))

    src_idx, tgt_idx = source.index(), target.index()
    types = [t for t in source.object_types if t in platform.objects]
    diffs: list[ItemDiff] = []

    for object_type in types:
        cfg = platform.objects[object_type]
        volatile = volatile_fields(platform, cfg)
        keys = sorted({k for (t, k) in src_idx if t == object_type} | {k for (t, k) in tgt_idx if t == object_type})
        for key in keys:
            s, t = src_idx.get((object_type, key)), tgt_idx.get((object_type, key))
            if s and not t:
                diffs.append(ItemDiff(object_type=object_type, key=key, status="added"))
                continue
            if t and not s:
                diffs.append(ItemDiff(object_type=object_type, key=key, status="removed"))
                continue
            assert s and t
            changes = field_changes(normalize(s.data, volatile), normalize(t.data, volatile))
            diffs.append(ItemDiff(object_type=object_type, key=key, status="changed" if changes else "unchanged", changes=changes))
            for c in changes:
                if c.source is not None and c.target is not None and _type_name(c.source) != _type_name(c.target):
                    issues.append(Issue(
                        severity="warning", code="SCHEMA_CONFLICT", object_type=object_type, key=key,
                        message=f"{c.path} is a {_type_name(c.source)} in {source.instance} "
                                f"but a {_type_name(c.target)} in {target.instance}",
                    ))
                if _top_field(c.path) in cfg.immutable:
                    issues.append(Issue(
                        severity="error", code="IMMUTABLE_FIELD_CHANGE", object_type=object_type, key=key,
                        message=f"{c.path} cannot be changed once the object exists "
                                f"({c.target!r} in {target.instance}, {c.source!r} in {source.instance})",
                    ))

    issues.extend(dependency_issues(diffs, source, target))
    return DiffReport(platform=source.platform, source_instance=source.instance,
                      target_instance=target.instance, diffs=diffs, issues=issues)


def dependency_issues(diffs: list[ItemDiff], source: Snapshot, target: Snapshot) -> list[Issue]:
    """Every object that will be pushed must reference objects that exist in the target or are pushed too."""
    src_idx, tgt_idx = source.index(), target.index()
    moving = {(d.object_type, d.key) for d in diffs if d.status in ("added", "changed")}
    extracted = set(source.object_types) & set(target.object_types)
    issues: list[Issue] = []
    for object_type, key in sorted(moving):
        for dep in src_idx[(object_type, key)].depends_on:
            ref = (dep.object_type, dep.key)
            if ref in tgt_idx or ref in moving:
                continue
            if dep.object_type not in extracted:
                issues.append(Issue(
                    severity="warning", code="MISSING_DEPENDENCY", object_type=object_type, key=key,
                    message=f"references {dep.object_type} {dep.key!r} (at {dep.path}); not verified because "
                            f"{dep.object_type} was not extracted from both instances",
                ))
                continue
            where = "exists in neither instance" if ref not in src_idx else f"is missing in {target.instance}"
            issues.append(Issue(
                severity="error", code="MISSING_DEPENDENCY", object_type=object_type, key=key,
                message=f"references {dep.object_type} {dep.key!r} (at {dep.path}), which {where}",
            ))
    return issues
