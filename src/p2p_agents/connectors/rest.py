from __future__ import annotations

import time
from typing import Any, Iterator

import httpx

from p2p_agents.connectors.base import Connector
from p2p_agents.paths import get_value
from p2p_agents.settings import InstanceConfig, ObjectTypeConfig, PlatformConfig

RETRY_STATUS = {429, 500, 502, 503, 504}


class RestConnector(Connector):
    """Generic JSON REST connector (Coupa, ReadSoft Online / Tungsten AP Essentials, PDAP OData)."""

    def __init__(
        self,
        platform: PlatformConfig,
        instance: InstanceConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ):
        super().__init__(platform, instance)
        if not instance.base_url:
            raise ValueError(f"instance {instance.name}: base_url is required for REST connectors")
        self.max_retries = max_retries
        self.client = httpx.Client(
            base_url=instance.base_url.rstrip("/"),
            headers={"Accept": "application/json", **platform.headers},
            timeout=60,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expires = 0.0

    # --- auth -------------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        inst = self.instance
        if inst.auth == "oauth2_client_credentials":
            return {"Authorization": f"Bearer {self._oauth_token()}"}
        if inst.auth == "bearer":
            return {"Authorization": f"Bearer {inst.secret(inst.token_env)}"}
        if inst.auth == "api_key":
            return {inst.api_key_header: inst.secret(inst.api_key_env)}
        return {}

    def _basic_auth(self) -> tuple[str, str] | None:
        inst = self.instance
        if inst.auth == "basic":
            return inst.secret(inst.username_env), inst.secret(inst.password_env)
        return None

    def _oauth_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        inst = self.instance
        if not inst.token_url:
            raise ValueError(f"instance {inst.name}: token_url is required for oauth2_client_credentials")
        data = {
            "grant_type": "client_credentials",
            "client_id": inst.secret(inst.client_id_env),
            "client_secret": inst.secret(inst.client_secret_env),
        }
        if inst.scopes:
            data["scope"] = " ".join(inst.scopes)
        resp = self.client.post(inst.token_url, data=data, headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise RuntimeError(f"{inst.name}: OAuth token request failed with HTTP {resp.status_code}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    # --- HTTP -------------------------------------------------------------------------

    def _path(self, template: str, **extra: str) -> str:
        try:
            return template.format(**self.instance.path_vars, **extra)
        except KeyError as e:
            raise ValueError(f"instance {self.instance.name}: path_vars is missing {e} for {template}") from None

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            resp = self.client.request(method, path, headers=self._auth_headers(), auth=self._basic_auth(), **kwargs)
            if resp.status_code not in RETRY_STATUS or attempt == self.max_retries:
                break
            retry_after = resp.headers.get("retry-after")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{self.instance.name}: {method} {path} failed with HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp

    def _results(self, body: Any) -> list[dict[str, Any]]:
        if self.platform.results_key:
            body = get_value(body, self.platform.results_key)
        if body is None:
            return []
        if not isinstance(body, list):
            raise RuntimeError(f"{self.instance.name}: expected a list of objects, got {type(body).__name__}")
        return body

    # --- Connector API ----------------------------------------------------------------

    def fetch(self, cfg: ObjectTypeConfig) -> Iterator[dict[str, Any]]:
        if not cfg.list_path:
            raise ValueError(f"{cfg.name}: list_path not configured")
        p = self.platform
        path = self._path(cfg.list_path)
        params: dict[str, Any] = dict(cfg.list_params)
        if p.pagination == "none":
            yield from self._results(self._request("GET", path, params=params).json())
            return
        index = 0 if p.pagination == "offset" else 1
        while True:
            page_params = dict(params)
            if p.pagination == "offset":
                page_params[p.offset_param] = index
                page_params[p.limit_param] = p.page_size
            else:
                page_params[p.page_param] = index
                page_params[p.limit_param] = p.page_size
            batch = self._results(self._request("GET", path, params=page_params).json())
            yield from batch
            if len(batch) < p.page_size:
                return
            index += p.page_size if p.pagination == "offset" else 1

    def create(self, cfg: ObjectTypeConfig, payload: dict[str, Any]) -> str | None:
        path = cfg.create_path or cfg.list_path
        if not path:
            raise ValueError(f"{cfg.name}: create_path not configured")
        resp = self._request("POST", self._path(path), json=payload)
        body = resp.json() if resp.content else {}
        new_id = body.get(cfg.id_field) if isinstance(body, dict) else None
        return None if new_id is None else str(new_id)

    def update(self, cfg: ObjectTypeConfig, target_id: str, payload: dict[str, Any]) -> None:
        if not cfg.update_path:
            raise ValueError(f"{cfg.name}: update_path not configured")
        self._request("PUT", self._path(cfg.update_path, id=target_id), json=payload)

    def close(self) -> None:
        self.client.close()
