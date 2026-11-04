from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from p2p_agents.connectors.base import Connector
from p2p_agents.paths import natural_key
from p2p_agents.settings import InstanceConfig, ObjectTypeConfig, PlatformConfig


def _unflatten(row: dict[str, str]) -> dict[str, Any]:
    """CSV columns named 'a.b' become nested {'a': {'b': ...}}; empty cells are dropped."""
    out: dict[str, Any] = {}
    for column, value in row.items():
        if column is None or value in (None, ""):
            continue
        node = out
        *parents, leaf = column.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{name}."))
        elif isinstance(v, list):
            out[name] = json.dumps(v)
        else:
            out[name] = v
    return out


class FileBundleConnector(Connector):
    """Configuration exchanged as files rather than through a write API.

    Used for SAP Ariba (CIG mapping exports, lookup tables, supplier workspace templates)
    and for PDAP customizing exports. Reads `<bundle_dir>/<file>`; "writes" produce import
    files in `<outbox_dir>/<object_type>.import.(csv|jsonl)` for upload or transport.
    """

    def __init__(self, platform: PlatformConfig, instance: InstanceConfig):
        super().__init__(platform, instance)
        self._pending: dict[str, list[dict[str, Any]]] = {}

    def _bundle_file(self, cfg: ObjectTypeConfig) -> Path:
        if not self.instance.bundle_dir:
            raise ValueError(f"instance {self.instance.name}: bundle_dir is required")
        if not cfg.file:
            raise ValueError(f"{cfg.name}: file not configured")
        return Path(self.instance.bundle_dir) / cfg.file

    def fetch(self, cfg: ObjectTypeConfig) -> Iterator[dict[str, Any]]:
        path = self._bundle_file(cfg)
        if not path.exists():
            raise FileNotFoundError(f"{self.instance.name}: export file missing: {path}")
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    yield _unflatten(row)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            yield from (data if isinstance(data, list) else data.get("items", []))

    def to_item(self, cfg: ObjectTypeConfig, raw: dict[str, Any]):
        item = super().to_item(cfg, raw)
        # Exports carry no platform ID; the natural key is how the import identifies the row
        if item is not None and item.source_id is None:
            item.source_id = item.key
        return item

    def _queue(self, cfg: ObjectTypeConfig, action: str, payload: dict[str, Any]) -> str | None:
        self._pending.setdefault(cfg.name, []).append({"_action": action, **payload})
        return natural_key(payload, cfg.key)

    def create(self, cfg: ObjectTypeConfig, payload: dict[str, Any]) -> str | None:
        return self._queue(cfg, "create", payload)

    def update(self, cfg: ObjectTypeConfig, target_id: str, payload: dict[str, Any]) -> None:
        self._queue(cfg, "update", payload)

    def flush(self) -> list[Path]:
        """Write queued changes as import files. Returns the files written."""
        if not self._pending:
            return []
        if not self.instance.outbox_dir:
            raise ValueError(f"instance {self.instance.name}: outbox_dir is required to write import files")
        outbox = Path(self.instance.outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        written = []
        for object_type, rows in self._pending.items():
            cfg = self.object_config(object_type)
            if cfg.file and cfg.file.lower().endswith(".csv"):
                flat = [_flatten(r) for r in rows]
                columns = sorted({c for r in flat for c in r}, key=lambda c: (c != "_action", c))
                path = outbox / f"{object_type}.import.csv"
                with path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(flat)
            else:
                path = outbox / f"{object_type}.import.jsonl"
                path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            written.append(path)
        self._pending.clear()
        return written

    def close(self) -> None:
        self.flush()
