"""A single role running one job: prompt → (tool call → result)* → final JSON answer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Role
from .providers import Message, Router, ToolCall
from .schemas import validate
from .tools import ToolContext, dispatch, specs_for
from .trace import Trace


@dataclass
class AgentResult:
    role: str
    model: str
    raw: str
    data: dict[str, Any] | None
    tool_calls: int
    escalated: bool = False
    history: list[Message] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply, tolerating fences and prose."""
    text = text.strip()
    if not text:
        return None
    for cand in (text, *(m.group(1) for m in _JSON_BLOCK.finditer(text))):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    # last resort: outermost braces
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            v = json.loads(text[start : end + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            return None
    return None


_TOOL_KEYS = (
    ("name", "arguments"),
    ("name", "parameters"),
    ("tool", "arguments"),
    ("tool", "args"),
    ("function", "arguments"),
    ("tool_name", "tool_input"),
)


def extract_text_tool_calls(text: str, allowed: set[str]) -> list[ToolCall]:
    """Small models often *write* a tool call as JSON text instead of using the protocol.
    Recognise {"name": "...", "arguments": {...}} (and common variants), possibly several,
    possibly fenced, and turn them into real ToolCalls. Only names in `allowed` count."""
    calls: list[ToolCall] = []
    candidates: list[str] = [m.group(1) for m in _JSON_BLOCK.finditer(text)]
    # also scan for bare top-level objects
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start : i + 1])
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        objs = obj if isinstance(obj, list) else [obj]
        for o in objs:
            if not isinstance(o, dict):
                continue
            for nk, ak in _TOOL_KEYS:
                name = o.get(nk)
                if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
                    name, o = name.get("name"), name
                if isinstance(name, str) and name in allowed and ak in o:
                    args = o.get(ak)
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    calls.append(
                        ToolCall(
                            id=f"text_{len(calls)}",
                            name=name,
                            arguments=args if isinstance(args, dict) else {},
                        )
                    )
                    break
    return calls


class Agent:
    MAX_REPAIRS = 2  # extra model calls allowed to fix a malformed / invalid final reply

    def __init__(
        self,
        role: Role,
        router: Router,
        ctx: ToolContext,
        trace: Trace,
        *,
        escalate: bool = False,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        on_token: Callable[[str, str], None] | None = None,
    ):
        self.role = role
        self.router = router
        self.ctx = ctx
        self.trace = trace
        self.escalate = escalate and role.escalation_slot is not None
        self.slot = role.escalation_slot if self.escalate else role.slot
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.on_token = on_token  # (role, text_chunk) -> None; used for live streaming

    def _complete(self, messages: list[Message], tools, *, temperature: float | None = None):
        cb = (lambda chunk: self.on_token(self.role.name, chunk)) if self.on_token else None
        comp = self.router.complete(
            self.slot,
            messages,
            tools,
            max_tokens=self.max_tokens,
            temperature=self.temperature if temperature is None else temperature,
            role=self.role.name,
            on_token=cb,
        )
        rec = self.router.tracker.calls[-1]
        self.trace.model_call(
            role=self.role.name,
            slot=self.slot,
            model=comp.model,
            input_tokens=comp.usage.input_tokens,
            output_tokens=comp.usage.output_tokens,
            usd=rec.usd,
            latency_s=rec.latency_s,
            n_tool_calls=len(comp.message.tool_calls),
            stop_reason=comp.stop_reason,
            content_preview=comp.message.content,
        )
        return comp

    def run(
        self,
        user_prompt: str,
        *,
        context_blocks: dict[str, str] | None = None,
        job: str = "default",
        prior: list[Message] | None = None,
        expect_json: bool = True,
    ) -> AgentResult:
        messages: list[Message] = [Message("system", self.role.system_prompt)]
        if context_blocks:
            ctx_text = "\n\n".join(f"### {k}\n{v}" for k, v in context_blocks.items() if v)
            messages.append(Message("user", f"Context:\n\n{ctx_text}"))
            messages.append(Message("assistant", "Understood. Waiting for the task."))
        for m in prior or []:  # earlier conversation turns (chat)
            if m.role in ("user", "assistant") and not m.tool_calls:
                messages.append(Message(m.role, m.content))
        messages.append(Message("user", user_prompt))

        tools = specs_for(self.role.tools) if self.role.tools else None
        self.ctx.tool_calls_made = 0
        model_used = ""
        last_text = ""

        for _ in range(self.role.max_tool_calls + 2):
            comp = self._complete(messages, tools)
            model_used = comp.model
            messages.append(comp.message)
            last_text = comp.message.content

            if not comp.message.tool_calls and tools:
                # fallback: tool call written as plain text
                text_calls = extract_text_tool_calls(last_text, {t.name for t in tools})
                if text_calls:
                    self.trace.emit(
                        "note",
                        role=self.role.name,
                        msg=f"model wrote {len(text_calls)} tool call(s) as text; executing them",
                    )
                    results = []
                    for tc in text_calls:
                        result = dispatch(self.ctx, tc.name, tc.arguments)
                        ok = not result.startswith(
                            ("refused:", "unknown tool:", "tool error", "bad arguments")
                        )
                        self.trace.tool_call(
                            role=self.role.name,
                            tool=tc.name,
                            args=tc.arguments,
                            result_preview=result,
                            ok=ok,
                        )
                        results.append(f"[{tc.name}] {result}")
                    messages.append(
                        Message(
                            "user",
                            "You wrote tool calls as text. I executed them; results:\n\n"
                            + "\n\n".join(results)
                            + "\n\nContinue. Use the tool-calling interface for further tools, "
                            "and reply with your final JSON when done.",
                        )
                    )
                    if self.ctx.tool_calls_made >= self.role.max_tool_calls:
                        messages.append(
                            Message(
                                "user", "Tool budget exhausted. Reply now with your final JSON."
                            )
                        )
                    continue

            if not comp.message.tool_calls:
                break

            if self.ctx.tool_calls_made >= self.role.max_tool_calls:
                messages.append(
                    Message("user", "Tool budget exhausted. Reply now with your final JSON.")
                )
                for tc in comp.message.tool_calls:
                    messages.append(
                        Message("tool", "tool budget exhausted", tool_call_id=tc.id, name=tc.name)
                    )
                continue

            for tc in comp.message.tool_calls:
                result = dispatch(self.ctx, tc.name, tc.arguments)
                ok = not result.startswith(
                    ("refused:", "unknown tool:", "tool error", "bad arguments")
                )
                self.trace.tool_call(
                    role=self.role.name,
                    tool=tc.name,
                    args=tc.arguments,
                    result_preview=result,
                    ok=ok,
                )
                messages.append(Message("tool", result, tool_call_id=tc.id, name=tc.name))

        if not expect_json:
            return AgentResult(
                role=self.role.name,
                model=model_used,
                raw=last_text,
                data={"text": last_text},
                tool_calls=self.ctx.tool_calls_made,
                escalated=self.escalate,
                history=messages,
            )

        # ---- final reply: parse, validate against the role's contract, repair if needed
        data, error = self._parse_and_validate(last_text, job)
        repairs = 0
        while data is None and repairs < self.MAX_REPAIRS:
            repairs += 1
            self.trace.emit(
                "note",
                role=self.role.name,
                msg=f"invalid final reply ({error}); asking for repair {repairs}",
            )
            messages.append(
                Message(
                    "user",
                    "Your reply did not match the required JSON contract:\n"
                    + (error or "no JSON object found")
                    + "\n\nReply again with ONLY the corrected JSON object, exactly as specified "
                    "in your instructions.",
                )
            )
            comp = self._complete(messages, None, temperature=0.0)
            messages.append(comp.message)
            last_text = comp.message.content
            data, error = self._parse_and_validate(last_text, job)

        return AgentResult(
            role=self.role.name,
            model=model_used,
            raw=last_text,
            data=data,
            tool_calls=self.ctx.tool_calls_made,
            escalated=self.escalate,
            history=messages,
        )

    def _parse_and_validate(self, text: str, job: str):
        raw = extract_json(text)
        if raw is None:
            return None, "no JSON object found in the reply"
        return validate(self.role.name, job, raw)
