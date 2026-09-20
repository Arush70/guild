"""JSONL trace: every model call, tool call, decision and verification result.

One file per run at .guild/runs/<run_id>/trace.jsonl. Schema is deliberately flat so it can be
loaded with pandas and analysed offline (the same discipline as ASTRA/MACS traces).

Event kinds:
  run_start, run_end, phase, model_call, tool_call, decision, verification, fallback, error, note
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, is_dataclass
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
        self._f = open(self.path, "a", encoding="utf-8")
        self._seq = 0

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        ev = {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.run_id, "kind": kind}
        ev.update({k: _jsonable(v) for k, v in fields.items()})
        self._f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._f.flush()
        if self.echo:
            self.echo(ev)
        return ev

    def close(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass

    # convenience -----------------------------------------------------------
    def model_call(self, *, role: str, slot: str, model: str, input_tokens: int, output_tokens: int,
                   usd: float, latency_s: float, n_tool_calls: int, stop_reason: str,
                   content_preview: str) -> None:
        self.emit("model_call", role=role, slot=slot, model=model, input_tokens=input_tokens,
                  output_tokens=output_tokens, usd=round(usd, 6), latency_s=round(latency_s, 3),
                  n_tool_calls=n_tool_calls, stop_reason=stop_reason,
                  content_preview=content_preview[:300])

    def tool_call(self, *, role: str, tool: str, args: dict, result_preview: str, ok: bool) -> None:
        self.emit("tool_call", role=role, tool=tool, args=_short_args(args),
                  result_preview=result_preview[:300], ok=ok)


def _short_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"...[{len(v)} chars]"
        else:
            out[k] = v
    return out


def _jsonable(v: Any) -> Any:
    if is_dataclass(v) and not isinstance(v, type):
        return asdict(v)
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (set, tuple, list)):
        return [_jsonable(x) for x in v]
    return v


def read_trace(path: Path) -> list[dict[str, Any]]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
