"""Adapter for anything that speaks the OpenAI chat-completions API.

Covers: OpenAI, Ollama, Groq, Gemini (OpenAI-compat endpoint), DeepSeek, OpenRouter,
Mistral, and gateways like OmniRoute or LiteLLM. Each is just a base URL + key env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .base import (
    Completion,
    Message,
    NotConfigured,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
    parse_json_args,
)


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    key_env: str | None  # None = no key needed (Ollama)
    default_key: str = "none"


ENDPOINTS: dict[str, Endpoint] = {
    "openai": Endpoint("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "ollama": Endpoint(
        os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/v1", None, "ollama"
    ),
    "groq": Endpoint("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": Endpoint("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "deepseek": Endpoint("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "openrouter": Endpoint("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "mistral": Endpoint("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "omniroute": Endpoint(
        os.environ.get("OMNIROUTE_HOST", "http://localhost:20128").rstrip("/") + "/v1",
        "OMNIROUTE_API_KEY",
        "none",
    ),
    "litellm": Endpoint(
        os.environ.get("LITELLM_HOST", "http://localhost:4000").rstrip("/") + "/v1",
        "LITELLM_API_KEY",
        "none",
    ),
    "custom": Endpoint(
        os.environ.get("CUSTOM_OPENAI_BASE_URL", "http://localhost:8000/v1"),
        "CUSTOM_OPENAI_API_KEY",
        "none",
    ),
}


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": _dumps(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _dumps(d: dict) -> str:
    import json

    return json.dumps(d)


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


class OpenAICompatProvider:
    def __init__(self, provider: str):
        if provider not in ENDPOINTS:
            raise ValueError(f"unknown OpenAI-compatible provider '{provider}'")
        self.provider = provider
        self.endpoint = ENDPOINTS[provider]
        self._client = None

    def _key(self, model: str) -> str:
        ep = self.endpoint
        if ep.key_env is None:
            return ep.default_key
        key = os.environ.get(ep.key_env)
        if not key:
            if ep.default_key != "none" or self.provider in {"omniroute", "litellm", "custom"}:
                return ep.default_key
            raise NotConfigured(model, f"{ep.key_env} not set")
        return key

    def client(self, model: str):
        if self._client is None:
            from openai import OpenAI

            # Local servers (Ollama on a laptop GPU) can take many minutes for a long reply or
            # a deepseek-r1 "think" block; the non-streaming CLI path must not give up at 3 min.
            local = self.provider in {"ollama", "omniroute", "litellm", "custom"}
            self._client = OpenAI(
                base_url=self.endpoint.base_url,
                api_key=self._key(model),
                timeout=float(os.environ.get("GUILD_MODEL_TIMEOUT", 900 if local else 180)),
                max_retries=1,
            )
        return self._client

    def complete(
        self,
        model_name: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        on_token=None,
    ) -> Completion:
        full = f"{self.provider}/{model_name}"
        client = self.client(full)
        kwargs: dict = {
            "model": model_name,
            "messages": _to_openai_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)
        try:
            if on_token is None:
                resp = client.chat.completions.create(**kwargs)
                return self._from_response(resp, full)
            return self._stream(client, kwargs, full, on_token)
        except ProviderError:
            raise
        except Exception as e:
            raise _classify(full, e) from e

    @staticmethod
    def _from_response(resp, full: str) -> Completion:
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(
                id=tc.id or f"call_{i}",
                name=tc.function.name,
                arguments=parse_json_args(tc.function.arguments),
            )
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        usage = (
            Usage(
                getattr(resp.usage, "prompt_tokens", 0) or 0,
                getattr(resp.usage, "completion_tokens", 0) or 0,
            )
            if resp.usage
            else Usage()
        )
        return Completion(
            message=Message(role="assistant", content=msg.content or "", tool_calls=calls),
            usage=usage,
            model=full,
            stop_reason=choice.finish_reason or "",
        )

    @staticmethod
    def _stream(client, kwargs: dict, full: str, on_token) -> Completion:
        """Consume a streamed response, forwarding text deltas and assembling tool calls."""
        text_parts: list[str] = []
        calls: dict[int, dict] = {}  # index -> {id, name, args}
        usage = Usage()
        finish = ""
        stream = client.chat.completions.create(
            stream=True, stream_options={"include_usage": True}, **kwargs
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = Usage(
                    getattr(chunk.usage, "prompt_tokens", 0) or 0,
                    getattr(chunk.usage, "completion_tokens", 0) or 0,
                )
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            if ch.finish_reason:
                finish = ch.finish_reason
            delta = ch.delta
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                text_parts.append(content)
                on_token(content)
            for n, tc in enumerate(delta.tool_calls or []):
                idx = tc.index if getattr(tc, "index", None) is not None else n
                slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = (slot["name"] or "") + fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] = (slot["args"] or "") + fn.arguments
        tool_calls = [
            ToolCall(
                id=c["id"] or f"call_{i}", name=c["name"], arguments=parse_json_args(c["args"])
            )
            for i, c in sorted(calls.items(), key=lambda kv: kv[0])
            if c["name"]
        ]
        return Completion(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls),
            usage=usage,
            model=full,
            stop_reason=finish,
        )


def _classify(model: str, e: Exception) -> ProviderError:
    name = type(e).__name__
    text = str(e)
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return NotConfigured(model, f"auth failed: {text[:120]}")
    if name in {"APIConnectionError", "APITimeoutError"}:
        return ProviderError(model, f"unreachable: {text[:120]}", retryable=True)
    if name == "NotFoundError":
        return ProviderError(model, f"model not found: {text[:120]}", retryable=False)
    if name in {"RateLimitError", "InternalServerError", "APIStatusError"}:
        return ProviderError(model, f"{name}: {text[:120]}", retryable=True)
    if name == "BadRequestError":
        return ProviderError(model, f"bad request: {text[:200]}", retryable=False)
    return ProviderError(model, f"{name}: {text[:120]}", retryable=True)
