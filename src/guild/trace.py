"""JSONL trace: every model call, tool call, decision and verification result.

One file per run at .guild/runs/<run_id>/trace.jsonl. Schema is deliberately flat so it can be
loaded with pandas and analysed offline (the same discipline as ASTRA/MACS traces).

Event kinds written to the trace (every one also has seq, ts, run_id):
  run_start, run_end   one pair per Guild object
  phase                a step starts: phase, role (who works on it), task_id, round
  model_call           one completion: role, slot, model, tokens, usd, latency_s
  tool_call            role, tool, args, ok, result_preview
  approval             pair mode: the owner's answer to a proposed edit or command
  verification         the fields of verify.Verification
  review_input         what the reviewers were shown: diff_chars, files_in_diff
  decision             a role's structured verdict
  fallback             a model failed and the router moved to the next candidate
  note, error, task_end

The dashboard server adds its own kinds on the live stream only (see ui/server.py Hub).
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Trace:
    def __init__(self, path: Path, run_id: str, echo=None):
        self.path = path
        self.run_id = run_id
        self.echo = echo  # optional callable(event: dict) for live display
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 — closed in close()
        self._seq = 0

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        ev = {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.run_id, "kind": kind}
        ev.update({k: jsonable(v) for k, v in fields.items()})
        # A trace problem must never take a run down: unknown objects are stringified and a
        # closed/failed file is ignored (late events from a UI thread after close()).
        with contextlib.suppress(OSError, ValueError):
            self._f.write(json.dumps(ev, ensure_ascii=False, default=repr) + "\n")
            self._f.flush()
        if self.echo:
            self.echo(ev)
        return ev

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._f.close()

    def model_call(
        self,
        *,
        role: str,
        slot: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        latency_s: float,
        n_tool_calls: int,
        stop_reason: str,
        content_preview: str,
    ) -> None:
        self.emit(
            "model_call",
            role=role,
            slot=slot,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=round(usd, 6),
            latency_s=round(latency_s, 3),
            n_tool_calls=n_tool_calls,
            stop_reason=stop_reason,
            content_preview=(content_preview or "")[:300],
        )

    def tool_call(self, *, role: str, tool: str, args: dict, result_preview: str, ok: bool) -> None:
        self.emit(
            "tool_call",
            role=role,
            tool=tool,
            args=_short_args(args),
            result_preview=str(result_preview or "")[:300],
            ok=ok,
        )


def _short_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return {"_args": str(args)[:200]}
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"...[{len(v)} chars]"
        else:
            out[k] = v
    return out


def jsonable(v: Any) -> Any:
    """Plain JSON data from guild's own objects (events, job results)."""
    if hasattr(v, "to_dict"):
        return jsonable(v.to_dict())
    if is_dataclass(v) and not isinstance(v, type):
        # field by field, not asdict(): a nested object keeps the say over its own to_dict()
        return {f.name: jsonable(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (set, tuple, list)):
        return [jsonable(x) for x in v]
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Load a trace. Lines that are not complete JSON objects are skipped: the dashboard reads
    traces while a run is still appending to them, so the last line may be half-written."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and "kind" in ev:
                events.append(ev)
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals of one run's trace: what `guild cost`, the dashboard's run list and a run's detail
    view all show."""
    by_role: dict[str, dict[str, Any]] = {}
    for e in events:
        if e["kind"] != "model_call":
            continue
        r = by_role.setdefault(
            e.get("role") or "?",
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "latency_s": 0.0},
        )
        r["calls"] += 1
        for key in ("input_tokens", "output_tokens", "usd", "latency_s"):
            r[key] += e.get(key) or 0
        r.setdefault("models", set()).add(str(e.get("model")))
    for r in by_role.values():
        r["models"] = sorted(r["models"])
        r["usd"] = round(r["usd"], 5)
        r["latency_s"] = round(r["latency_s"], 1)
    start = next((e for e in events if e["kind"] == "run_start"), {})
    return {
        "profile": start.get("profile"),
        "calls": sum(r["calls"] for r in by_role.values()),
        "input_tokens": sum(r["input_tokens"] for r in by_role.values()),
        "output_tokens": sum(r["output_tokens"] for r in by_role.values()),
        "usd": round(sum(r["usd"] for r in by_role.values()), 4),
        "by_role": by_role,
        "phases": [e.get("phase") for e in events if e["kind"] == "phase"],
        "started": events[0].get("ts", 0) if events else 0,
        "ended": events[-1].get("ts", 0) if events else 0,
    }
