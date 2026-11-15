"""Thin wrapper over the Anthropic SDK used by every agent.

- Streams every request (long documents and agent turns can take minutes).
- Opts into server-side refusal fallbacks, so a false-positive safety decline is retried
  on a fallback model inside the same call.
- Runs a manual tool-use loop with Pydantic-validated tool inputs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import anthropic
from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = os.environ.get("P2P_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def default_client() -> tuple[Any, bool]:
    """(client, first_party). Claude on Microsoft Foundry is used when ANTHROPIC_FOUNDRY_BASE_URL is set;
    P2P_MODEL must then name the Foundry deployment (e.g. claude-opus-4-8)."""
    base_url = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")
    if base_url:
        return anthropic.AnthropicFoundry(api_key=os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"), base_url=base_url), False
    return anthropic.Anthropic(), True


class LLMError(RuntimeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[BaseModel], Any]
    terminal: bool = False  # the loop ends once this tool has run successfully

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args.model_json_schema(),
            "eager_input_streaming": True,
        }


@dataclass
class ToolRun:
    final_text: str
    turns: int
    tool_calls: list[dict[str, Any]]
    messages: list[dict[str, Any]] | None = None  # full conversation, to continue it next turn


class LLM:
    def __init__(self, client: Any | None = None, model: str | None = None, effort: str = "high"):
        # Server-side refusal fallbacks exist only on the first-party API, not on Foundry
        self.client, self.first_party = (client, True) if client is not None else default_client()
        self.model = model or DEFAULT_MODEL
        self.effort = effort

    def _send(self, max_tokens: int, **params: Any) -> Any:
        if self.first_party:
            stream_ctx = self.client.beta.messages.stream(
                model=self.model, max_tokens=max_tokens, output_config={"effort": self.effort},
                betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **params)
        else:
            stream_ctx = self.client.messages.stream(
                model=self.model, max_tokens=max_tokens, output_config={"effort": self.effort}, **params)
        with stream_ctx as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"the model declined this request ({getattr(details, 'category', None) or 'no category'})")
        return response

    @staticmethod
    def _text(response: Any) -> str:
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def write(self, system: str, prompt: str, max_tokens: int = 64000) -> str:
        """One-shot generation (documents, summaries)."""
        response = self._send(max_tokens, system=system, messages=[{"role": "user", "content": prompt}])
        if response.stop_reason == "max_tokens":
            raise LLMError("output hit max_tokens; narrow the input (fewer object types) and retry")
        return self._text(response)

    def run_tools(self, system: str, prompt: str | list[dict[str, Any]], tools: list[Tool], max_turns: int = 15,
                  max_tokens: int = 32000, history: list[dict[str, Any]] | None = None) -> ToolRun:
        """Tool-use loop. Pass `history` (a previous ToolRun.messages) to continue a conversation."""
        by_name = {t.name: t for t in tools}
        definitions = [t.definition() for t in tools]
        messages: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": prompt}]
        calls: list[dict[str, Any]] = []

        for turn in range(1, max_turns + 1):
            response = self._send(max_tokens, system=system, messages=messages, tools=definitions)
            # Append the full content (thinking blocks included) so history stays append-only
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "max_tokens":
                raise LLMError("agent turn hit max_tokens before finishing a tool call")

            uses = [b for b in response.content if b.type == "tool_use"]
            if not uses:
                return ToolRun(final_text=self._text(response), turns=turn, tool_calls=calls, messages=messages)

            results, finished = [], False
            for use in uses:
                tool = by_name.get(use.name)
                if tool is None:
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": f"unknown tool {use.name}"})
                    continue
                try:
                    args = tool.args.model_validate(use.input)
                except ValidationError as e:
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": f"INVALID_INPUT: {e.errors(include_url=False)}"})
                    continue
                try:
                    output = tool.handler(args)
                    ok = True
                except Exception as e:  # noqa: BLE001 - reported back to the model
                    output, ok = f"tool failed: {e}", False
                calls.append({"tool": use.name, "input": use.input, "ok": ok})
                content = output if isinstance(output, str) else json.dumps(output, default=str, ensure_ascii=False)
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": content, "is_error": not ok})
                finished = finished or (ok and tool.terminal)
            messages.append({"role": "user", "content": results})
            if finished:
                return ToolRun(final_text=self._text(response), turns=turn, tool_calls=calls, messages=messages)

        raise LLMError(f"agent did not finish within {max_turns} turns")
