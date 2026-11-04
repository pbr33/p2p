from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Iterator

from p2p_agents.models import ConfigItem, DependencyRef, Snapshot
from p2p_agents.paths import get_values, natural_key
from p2p_agents.settings import InstanceConfig, ObjectTypeConfig, PlatformConfig

log = logging.getLogger(__name__)


class Connector(ABC):
    """Reads and writes one platform instance's configuration objects."""

    def __init__(self, platform: PlatformConfig, instance: InstanceConfig):
        if instance.platform != platform.name:
            raise ValueError(f"instance {instance.name} belongs to {instance.platform}, not {platform.name}")
        self.platform = platform
        self.instance = instance

    def object_config(self, object_type: str) -> ObjectTypeConfig:
        try:
            return self.platform.objects[object_type]
        except KeyError:
            raise KeyError(
                f"{self.platform.display_name} has no object type {object_type!r}; "
                f"known: {', '.join(self.platform.objects)}"
            ) from None

    def extract(self, object_types: list[str] | None = None) -> Snapshot:
        types = object_types or list(self.platform.objects)
        items: list[ConfigItem] = []
        for object_type in types:
            cfg = self.object_config(object_type)
            count = 0
            for raw in self.fetch(cfg):
                item = self.to_item(cfg, raw)
                if item is None:
                    continue
                items.append(item)
                count += 1
            log.info("%s/%s: extracted %d %s", self.platform.name, self.instance.name, count, object_type)
        return Snapshot(platform=self.platform.name, instance=self.instance.name, object_types=types, items=items)

    def to_item(self, cfg: ObjectTypeConfig, raw: dict[str, Any]) -> ConfigItem | None:
        key = natural_key(raw, cfg.key)
        if key is None:
            log.warning("%s: skipping object without natural key %r: %s", cfg.name, cfg.key, str(raw)[:200])
            return None
        deps = [
            DependencyRef(object_type=ref_type, key=str(value), path=path)
            for path, ref_type in cfg.references.items()
            for value in get_values(raw, path)
        ]
        source_id = raw.get(cfg.id_field)
        return ConfigItem(
            platform=self.platform.name,
            object_type=cfg.name,
            key=key,
            data=raw,
            source_id=None if source_id is None else str(source_id),
            depends_on=deps,
        )

    @abstractmethod
    def fetch(self, cfg: ObjectTypeConfig) -> Iterator[dict[str, Any]]:
        """Yield raw objects of one type."""

    @abstractmethod
    def create(self, cfg: ObjectTypeConfig, payload: dict[str, Any]) -> str | None:
        """Create an object; return its new platform ID when known."""

    @abstractmethod
    def update(self, cfg: ObjectTypeConfig, target_id: str, payload: dict[str, Any]) -> None:
        """Update an existing object in place."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass
