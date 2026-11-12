"""Adapter for the Anthropic Messages API (Claude models)."""

from __future__ import annotations

import json
import os

from .base import Completion, Message, NotConfigured, ProviderError, ToolCall, ToolSpec, Usage


def _to_anthropic(messages: list[Message], flatten_tools: bool = False) -> tuple[str, list[dict]]:
    """Translate to the Messages API shape.

    flatten_tools: the request carries no `tools` (the JSON-repair turn). The API rejects
    tool_use / tool_result blocks in that case, so earlier tool traffic is rendered as text.
    Empty text is never sent: the API rejects empty text blocks and empty user messages.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "user":
            out.append({"role": "user", "content": m.content or "(empty message)"})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content and m.content.strip():
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                if flatten_tools:
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"[called tool {tc.name} with {json.dumps(tc.arguments)}]",
                        }
                    )
                else:
                    blocks.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    )
            out.append(
                {"role": "assistant", "content": blocks or [{"type": "text", "text": "(no reply)"}]}
            )
        elif m.role == "tool" and flatten_tools:
            out.append(
                {"role": "user", "content": f"[result of tool {m.name or '?'}]\n{m.content}"}
            )
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            # Anthropic requires consecutive tool_results to share one user message.
            if (
                out
                and out[-1]["role"] == "user"
                and isinstance(out[-1]["content"], list)
                and out[-1]["content"]
                and out[-1]["content"][0].get("type") == "tool_result"
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return "\n\n".join(system_parts), out


class AnthropicProvider:
    provider = "anthropic"

    def __init__(self) -> None:
        self._client = None

    def client(self, model: str):
        if self._client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise NotConfigured(model, "ANTHROPIC_API_KEY not set")
            from anthropic import Anthropic

            self._client = Anthropic(timeout=300.0, max_retries=1)
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
        full = f"anthropic/{model_name}"
        client = self.client(full)
        system, msgs = _to_anthropic(messages, flatten_tools=not tools)
        kwargs: dict = {
            "model": model_name,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        try:
            if on_token is None:
                resp = client.messages.create(**kwargs)
            else:
                with client.messages.stream(**kwargs) as stream:
                    for text in stream.text_stream:
                        on_token(text)
                    resp = stream.get_final_message()
        except Exception as e:
            raise _classify(full, e) from e

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if isinstance(block.input, dict) else {},
                    )
                )
        usage = Usage(
            getattr(resp.usage, "input_tokens", 0) or 0,
            getattr(resp.usage, "output_tokens", 0) or 0,
        )
        return Completion(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=calls),
            usage=usage,
            model=full,
            stop_reason=resp.stop_reason or "",
        )


def _classify(model: str, e: Exception) -> ProviderError:
    name = type(e).__name__
    text = str(e)
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return NotConfigured(model, f"auth failed: {text[:120]}")
    if name in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "OverloadedError",
    }:
        return ProviderError(model, f"{name}: {text[:120]}", retryable=True)
    if name == "NotFoundError":
        return ProviderError(model, f"model not found: {text[:120]}", retryable=False)
    if name == "BadRequestError":
        return ProviderError(model, f"bad request: {text[:200]}", retryable=False)
    return ProviderError(model, f"{name}: {text[:120]}", retryable=True)
