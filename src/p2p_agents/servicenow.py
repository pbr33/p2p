"""ServiceNow Table API client (incidents, groups, work notes)."""

from __future__ import annotations

import os
from typing import Any

import httpx

INCIDENT_FIELDS = (
    "sys_id,number,short_description,description,state,priority,impact,urgency,category,subcategory,"
    "cmdb_ci,assignment_group,assigned_to,opened_at,sys_updated_on,work_notes,comments,close_notes"
)


def _q(text: str) -> str:
    """Make free text safe inside an encoded query (^ separates clauses)."""
    return text.replace("^", " ").strip()


class ServiceNowClient:
    def __init__(self, instance_url: str, user: str | None = None, password: str | None = None,
                 token: str | None = None, transport: httpx.BaseTransport | None = None):
        if not (token or (user and password)):
            raise ValueError("ServiceNow credentials missing: set SN_TOKEN, or SN_USER and SN_PASSWORD")
        self.client = httpx.Client(
            base_url=instance_url.rstrip("/"),
            auth=None if token else (user, password),
            headers={"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
            timeout=30,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> "ServiceNowClient":
        url = os.environ.get("SN_INSTANCE_URL")
        if not url:
            raise ValueError("SN_INSTANCE_URL is not set (e.g. https://yourcompany.service-now.com)")
        return cls(url, os.environ.get("SN_USER"), os.environ.get("SN_PASSWORD"), os.environ.get("SN_TOKEN"))

    def _get(self, table: str, query: str, fields: str, limit: int, display: bool = True) -> list[dict[str, Any]]:
        resp = self.client.get(f"/api/now/table/{table}", params={
            "sysparm_query": query,
            "sysparm_fields": fields,
            "sysparm_limit": limit,
            "sysparm_display_value": "true" if display else "false",
            "sysparm_exclude_reference_link": "true",
        })
        resp.raise_for_status()
        return resp.json().get("result", [])

    def get_incident(self, number: str) -> dict[str, Any]:
        rows = self._get("incident", f"number={_q(number)}", INCIDENT_FIELDS, 1)
        if not rows:
            raise LookupError(f"incident {number} not found")
        return rows[0]

    def new_incidents(self, assignment_group: str, limit: int = 20) -> list[dict[str, Any]]:
        query = f"active=true^state=1^assignment_group.name={_q(assignment_group)}^ORDERBYopened_at"
        return self._get("incident", query, INCIDENT_FIELDS, limit)

    def similar_resolved(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        words = [w for w in _q(text).split() if len(w) > 3][:6]
        if not words:
            return []
        clause = "^OR".join(f"short_descriptionLIKE{w}" for w in words)
        query = f"stateIN6,7^{clause}^ORDERBYDESCresolved_at"
        return self._get("incident", query, "number,short_description,close_notes,assignment_group,resolved_at", limit)

    def group_sys_id(self, name: str) -> str:
        rows = self._get("sys_user_group", f"name={_q(name)}", "sys_id,name", 1, display=False)
        if not rows:
            raise LookupError(f"assignment group {name!r} not found in ServiceNow")
        return rows[0]["sys_id"]

    def update_incident(self, sys_id: str, work_notes: str, assignment_group: str | None = None) -> None:
        body: dict[str, Any] = {"work_notes": work_notes}
        if assignment_group:
            body["assignment_group"] = self.group_sys_id(assignment_group)
        resp = self.client.patch(f"/api/now/table/incident/{sys_id}", json=body)
        resp.raise_for_status()
