"""Snapshots on disk: snapshots/<platform>/<instance>/<timestamp>.json."""

from __future__ import annotations

import os
from pathlib import Path

from p2p_agents.models import Snapshot

SNAPSHOT_DIR = Path(os.environ.get("P2P_SNAPSHOT_DIR", Path.cwd() / "snapshots"))


def save(snapshot: Snapshot, root: Path = SNAPSHOT_DIR) -> Path:
    folder = root / snapshot.platform / snapshot.instance
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{snapshot.taken_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def load(path: Path) -> Snapshot:
    return Snapshot.model_validate_json(Path(path).read_text(encoding="utf-8"))


def latest(platform: str, instance: str, root: Path = SNAPSHOT_DIR) -> Snapshot | None:
    folder = root / platform / instance
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    return load(files[-1]) if files else None


def latest_any(platform: str, root: Path = SNAPSHOT_DIR) -> list[Snapshot]:
    """Latest snapshot of every instance of a platform."""
    folder = root / platform
    if not folder.exists():
        return []
    snaps = [latest(platform, d.name, root) for d in sorted(folder.iterdir()) if d.is_dir()]
    return [s for s in snaps if s is not None]
