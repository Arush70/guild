"""Provider-agnostic message and tool types.

Every provider adapter translates to/from these. Keeping this tiny is what lets a role be
served by Ollama one minute and Anthropic the next.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # for role == "tool"
    name: str | None = None  # tool name, for role == "tool"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


@dataclass
class Completion:
    message: Message
    usage: Usage
    model: str  # fully qualified "provider/model" that actually answered
    stop_reason: str = ""


class ProviderError(Exception):
    """Raised when a provider cannot serve a request (network, auth, 429, 5xx...)."""

    def __init__(self, model: str, reason: str, retryable: bool = True):
        super().__init__(f"{model}: {reason}")
        self.model = model
        self.reason = reason
        self.retryable = retryable


class NotConfigured(ProviderError):
    """Provider has no API key / is unreachable — skip silently in a fallback chain."""

    def __init__(self, model: str, reason: str):
        super().__init__(model, reason, retryable=False)


def parse_json_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"_raw": str(raw)}
