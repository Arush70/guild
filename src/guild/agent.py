"""A single role running one job: prompt → (tool call → result)* → final JSON answer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Role
from .errors import Cancelled
from .providers import Message, Router, ToolCall
from .schemas import validate
from .tools import ToolContext, ToolResult, dispatch, refusal, specs_for
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

    def to_dict(self) -> dict[str, Any]:
        """Everything but the message history, which is large and already in the trace."""
        return {k: v for k, v in self.__dict__.items() if k != "history"}


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


_THINK_RX = re.compile(r"<think>.*?</think>", re.S)
# strict=False: small models put raw newlines/tabs inside JSON strings (file contents)
_DECODER = json.JSONDecoder(strict=False)


def _json_objects(text: str) -> list[Any]:
    """Every top-level JSON object in free text, found with a real JSON decoder so braces
    inside strings (code!) never confuse the scan."""
    found: list[Any] = []
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            v, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        found.append(v)
        i = end


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply, tolerating fences and prose."""
    text = _THINK_RX.sub("", text or "").strip()
    if not text:
        return None
    for cand in (text, *(m.group(1) for m in _JSON_BLOCK.finditer(text))):
        try:
            v = json.loads(cand, strict=False)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    # last resort: every top-level {...} in the text; the final one is the answer
    # (deepseek-r1 style replies put reasoning, often with braces, before the JSON)
    found = [v for v in _json_objects(text) if isinstance(v, dict)]
    return found[-1] if found else None


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
    # reasoning is not an instruction: a call the model only *considered* must not run
    text = _THINK_RX.sub("", text or "")
    for o in _json_objects(text):
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
                        args = json.loads(args, strict=False)
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


_OUT_OF_BUDGET = "Tool budget exhausted. Reply now with your final JSON."


class Agent:
    MAX_TOKENS = 8192
    MAX_REPAIRS = 2  # extra model calls allowed to fix a malformed / invalid final reply

    def __init__(
        self,
        role: Role,
        router: Router,
        ctx: ToolContext,
        trace: Trace,
        *,
        escalate: bool = False,
        on_token: Callable[[str, str], None] | None = None,
        max_tool_calls: int | None = None,
    ):
        self.role = role
        self.router = router
        self.ctx = ctx
        self.trace = trace
        self.escalate = escalate and role.escalation_slot is not None
        self.slot = role.escalation_slot if self.escalate else role.slot
        self.on_token = on_token
        # tool budget for one run(); a profile can raise/lower it (limits.max_tool_calls_per_task)
        self.max_tool_calls = role.max_tool_calls if max_tool_calls is None else max_tool_calls

    def _dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Run a tool, but only one this role was given. Models can emit a call to any name
        (hallucinated, or copied from a file they read); the registry knows them all."""
        if name not in self.role.tools:
            self.ctx.tool_calls_made += 1
            return refusal(
                f"tool '{name}' is not available to this role "
                f"(available: {', '.join(self.role.tools) or 'none'})"
            )
        if self._cancelled():
            raise Cancelled()
        return dispatch(self.ctx, name, args)

    def _cancelled(self) -> bool:
        return self.router.cancel.is_set()

    def _run_tool(self, call: ToolCall) -> str:
        if self.ctx.tool_calls_made >= self.max_tool_calls:
            # checked per call: one reply may carry any number of calls
            result = ToolResult("tool budget exhausted; not run", ok=False)
        else:
            result = self._dispatch(call.name, call.arguments)
        self.trace.tool_call(
            role=self.role.name,
            tool=call.name,
            args=call.arguments,
            result_preview=result,
            ok=result.ok,
        )
        return result

    def _complete(self, messages: list[Message], tools, *, temperature: float = 0.2):
        def stream(chunk: str) -> None:
            if self._cancelled():  # stop a long generation mid-stream, not only between calls
                raise Cancelled()
            self.on_token(self.role.name, chunk)

        cb = stream if self.on_token else None
        comp = self.router.complete(
            self.slot,
            messages,
            tools,
            max_tokens=self.MAX_TOKENS,
            temperature=temperature,
            role=self.role.name,
            on_token=cb,
        )
        self.trace.model_call(
            role=self.role.name,
            slot=self.slot,
            model=comp.model,
            input_tokens=comp.usage.input_tokens,
            output_tokens=comp.usage.output_tokens,
            usd=comp.usd,
            latency_s=comp.latency_s,
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
        for m in prior or []:
            if m.role in ("user", "assistant") and not m.tool_calls:
                messages.append(Message(m.role, m.content))
        messages.append(Message("user", user_prompt))

        tools = specs_for(self.role.tools) if self.role.tools else None
        self.ctx.tool_calls_made = 0
        model_used = ""
        last_text = ""

        for _ in range(self.max_tool_calls + 2):
            comp = self._complete(messages, tools)
            model_used = comp.model
            messages.append(comp.message)
            last_text = comp.message.content or ""

            if not comp.message.tool_calls and tools:
                text_calls = extract_text_tool_calls(last_text, {t.name for t in tools})
                if text_calls:
                    self.trace.emit(
                        "note",
                        role=self.role.name,
                        msg=f"model wrote {len(text_calls)} tool call(s) as text; executing them",
                    )
                    results = [f"[{tc.name}] {self._run_tool(tc)}" for tc in text_calls]
                    messages.append(
                        Message(
                            "user",
                            "You wrote tool calls as text. I executed them; results:\n\n"
                            + "\n\n".join(results)
                            + "\n\nContinue. Use the tool-calling interface for further tools, "
                            "and reply with your final JSON when done.",
                        )
                    )
                    if self.ctx.tool_calls_made >= self.max_tool_calls:
                        messages.append(Message("user", _OUT_OF_BUDGET))
                    continue

            if not comp.message.tool_calls:
                break

            if self.ctx.tool_calls_made >= self.max_tool_calls:
                # tool results must directly follow the assistant's tool calls (OpenAI and
                # Anthropic reject the request otherwise); the instruction comes after them
                for tc in comp.message.tool_calls:
                    messages.append(
                        Message("tool", "tool budget exhausted", tool_call_id=tc.id, name=tc.name)
                    )
                messages.append(Message("user", _OUT_OF_BUDGET))
                continue

            for tc in comp.message.tool_calls:
                messages.append(
                    Message("tool", self._run_tool(tc), tool_call_id=tc.id, name=tc.name)
                )

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
            last_text = comp.message.content or ""
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
