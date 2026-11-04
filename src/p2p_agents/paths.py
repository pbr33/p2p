"""Helpers for dotted paths into nested JSON (``a.b``, ``items[].code``)."""

from __future__ import annotations

from typing import Any


def get_values(data: Any, path: str) -> list[Any]:
    """All values at `path`. `[]` fans out over a list; missing segments yield nothing."""
    current = [data]
    for part in path.split("."):
        fan_out = part.endswith("[]")
        name = part[:-2] if fan_out else part
        nxt: list[Any] = []
        for node in current:
            if not isinstance(node, dict) or name not in node:
                continue
            value = node[name]
            if fan_out:
                if isinstance(value, list):
                    nxt.extend(value)
            else:
                nxt.append(value)
        current = nxt
    return [v for v in current if v is not None]


def get_value(data: Any, path: str) -> Any:
    values = get_values(data, path)
    return values[0] if values else None


def natural_key(data: dict[str, Any], spec: str) -> str | None:
    """Natural key for an object. `spec` may join several paths with '+', e.g. 'country.code+code'."""
    parts = [get_value(data, p.strip()) for p in spec.split("+")]
    if any(p is None or p == "" for p in parts):
        return None
    return "|".join(str(p) for p in parts)
