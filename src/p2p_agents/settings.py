"""Platform and instance configuration.

Platform files (config/platforms/<name>.toml) describe *what* to replicate for a platform:
its object types, endpoints, natural keys and cross-object references. Instance entries
(config/instances.toml) describe *where*: base URL, auth style and the environment
variables holding credentials. Secrets never live in config files.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

CONFIG_DIR = Path(os.environ.get("P2P_CONFIG_DIR", Path.cwd() / "config"))
# Platform definitions shipped with the repo; used when a config dir doesn't override them
BUILTIN_PLATFORMS = Path(__file__).resolve().parents[2] / "config" / "platforms"


class ObjectTypeConfig(BaseModel):
    name: str
    description: str = ""
    # REST connectors
    list_path: str | None = None
    create_path: str | None = None
    update_path: str | None = None  # may contain {id}
    list_params: dict[str, str] = Field(default_factory=dict)
    # File-bundle connectors: file name inside the bundle directory (json or csv)
    file: str | None = None
    # Identity
    key: str  # dotted path of the natural key, e.g. "code" or "name"; "a+b" joins several
    id_field: str = "id"
    # Cross-object references: dotted path (use [] for lists) -> referenced object type
    references: dict[str, str] = Field(default_factory=dict)
    # Fields ignored when diffing (system-managed); merged with platform defaults
    volatile: list[str] = Field(default_factory=list)
    # Fields that cannot change once the object exists in the target
    immutable: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    name: str
    display_name: str
    connector: Literal["rest", "file_bundle"]
    pagination: Literal["offset", "page", "none"] = "offset"
    page_size: int = 50
    offset_param: str = "offset"
    limit_param: str = "limit"
    page_param: str = "page"
    results_key: str | None = None  # key holding the list in a wrapped response, e.g. "d.results"
    default_volatile: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    objects: dict[str, ObjectTypeConfig]


class InstanceConfig(BaseModel):
    name: str
    platform: str
    base_url: str = ""
    path_vars: dict[str, str] = Field(default_factory=dict)  # fills {placeholders} in endpoint paths
    bundle_dir: str | None = None  # file_bundle: where exports are read from
    outbox_dir: str | None = None  # file_bundle: where import files are written
    auth: Literal["oauth2_client_credentials", "bearer", "basic", "api_key", "none"] = "none"
    token_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    client_id_env: str | None = None
    client_secret_env: str | None = None
    token_env: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    api_key_env: str | None = None
    api_key_header: str = "x-api-key"
    is_production: bool = False

    def secret(self, env_name: str | None) -> str:
        if not env_name:
            raise ValueError(f"instance {self.name}: credential env var not configured")
        value = os.environ.get(env_name)
        if not value:
            raise ValueError(f"instance {self.name}: environment variable {env_name} is not set")
        return value


def load_platform(name: str, config_dir: Path = CONFIG_DIR) -> PlatformConfig:
    path = config_dir / "platforms" / f"{name}.toml"
    if not path.exists():
        path = BUILTIN_PLATFORMS / f"{name}.toml"
    if not path.exists():
        raise FileNotFoundError(f"no platform config at {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    objects = {k: ObjectTypeConfig(name=k, **v) for k, v in raw.pop("objects", {}).items()}
    return PlatformConfig(name=name, objects=objects, **raw)


def load_instances(config_dir: Path = CONFIG_DIR) -> dict[str, InstanceConfig]:
    path = config_dir / "instances.toml"
    if not path.exists():
        raise FileNotFoundError(f"no instance config at {path} (copy config/instances.example.toml)")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {name: InstanceConfig(name=name, **cfg) for name, cfg in raw.items()}


def load_instance(name: str, config_dir: Path = CONFIG_DIR) -> InstanceConfig:
    instances = load_instances(config_dir)
    if name not in instances:
        raise KeyError(f"unknown instance {name!r}; configured: {', '.join(sorted(instances))}")
    return instances[name]
