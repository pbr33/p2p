from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from p2p_agents.models import ConfigItem, DependencyRef, Snapshot
from p2p_agents.settings import ObjectTypeConfig, PlatformConfig


@pytest.fixture
def platform() -> PlatformConfig:
    return PlatformConfig(
        name="coupa",
        display_name="Coupa",
        connector="rest",
        default_volatile=["id", "updated-at"],
        objects={
            "account_types": ObjectTypeConfig(name="account_types", key="name", immutable=["currency"]),
            "approval_rules": ObjectTypeConfig(
                name="approval_rules", key="name", references={"account-type.name": "account_types"}),
        },
    )


def item(object_type: str, key: str, data: dict[str, Any], deps: list[tuple[str, str]] = (), sid: str | None = None):
    return ConfigItem(platform="coupa", object_type=object_type, key=key, data={"name": key, **data},
                      source_id=sid, depends_on=[DependencyRef(object_type=t, key=k, path="x") for t, k in deps])


def snap(instance: str, items: list[ConfigItem], types=("account_types", "approval_rules")) -> Snapshot:
    return Snapshot(platform="coupa", instance=instance, object_types=list(types), items=items)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeAnthropic:
    """Replays scripted responses for client.beta.messages.stream and records each request."""

    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        # The agent keeps appending to the same list; record what was actually sent
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.responses.pop(0))


def text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def tool_response(*calls: tuple[str, str, dict]) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="tool_use", id=cid, name=name, input=args) for cid, name, args in calls])
