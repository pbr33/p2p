"""Core data model shared by all agents."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class DependencyRef(BaseModel):
    """A reference from one config object to another, by natural key."""

    object_type: str
    key: str
    path: str  # where in the referencing object the reference was found


class ConfigItem(BaseModel):
    """One configuration object (an approval chain, a tax code, an extraction profile...).

    `key` is the natural key that identifies the same object across instances; platform
    IDs differ between sandbox and production, so they are kept only as `source_id`.
    """

    platform: str
    object_type: str
    key: str
    data: dict[str, Any]
    source_id: str | None = None
    depends_on: list[DependencyRef] = Field(default_factory=list)


class Snapshot(BaseModel):
    platform: str
    instance: str
    taken_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    object_types: list[str] = Field(default_factory=list)
    items: list[ConfigItem] = Field(default_factory=list)

    def index(self) -> dict[tuple[str, str], ConfigItem]:
        return {(i.object_type, i.key): i for i in self.items}

    def fingerprint(self) -> str:
        """Content hash, used to tell whether generated docs are stale."""
        payload = sorted((i.object_type, i.key, json.dumps(i.data, sort_keys=True, default=str)) for i in self.items)
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


class FieldChange(BaseModel):
    path: str
    source: Any = None
    target: Any = None


class ItemDiff(BaseModel):
    object_type: str
    key: str
    status: Literal["added", "removed", "changed", "unchanged"]
    changes: list[FieldChange] = Field(default_factory=list)


IssueCode = Literal[
    "MISSING_DEPENDENCY",
    "SCHEMA_CONFLICT",
    "IMMUTABLE_FIELD_CHANGE",
    "DEPENDENCY_CYCLE",
    "DUPLICATE_KEY",
    "MISSING_TARGET_ID",
]


class Issue(BaseModel):
    severity: Literal["error", "warning"]
    code: IssueCode
    object_type: str
    key: str
    message: str


class DiffReport(BaseModel):
    platform: str
    source_instance: str
    target_instance: str
    diffs: list[ItemDiff]
    issues: list[Issue] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
        for d in self.diffs:
            out[d.status] += 1
        return out

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


class PlannedAction(BaseModel):
    action: Literal["create", "update"]
    item: ConfigItem
    target_id: str | None = None  # set for updates
    before: dict[str, Any] | None = None  # target state before the change, for rollback


class ReplicationPlan(BaseModel):
    platform: str
    source_instance: str
    target_instance: str
    actions: list[PlannedAction]
    issues: list[Issue] = Field(default_factory=list)
    skipped_removals: list[str] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


class ActionResult(BaseModel):
    action: Literal["create", "update"]
    object_type: str
    key: str
    ok: bool
    target_id: str | None = None
    error: str | None = None
