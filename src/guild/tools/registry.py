"""Tools agents can call. Every tool is scoped to the project root and refuses to escape it.

Adding a tool: write a function taking (ctx, **kwargs) -> str, wrap it with @tool(...).
Roles pick tools by name in their YAML `tools:` list.
"""

from __future__ import annotations

import difflib
import fnmatch
import inspect
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..providers.base import MALFORMED_ARGS, ToolSpec
from ..repo import Repo
from .context import ToolContext
from .policy import (
    READONLY_HINT,
    blocked_url,
    is_secret_file,
    readonly_allowed,
    readonly_test_args_allowed,
)

MAX_OUTPUT = 12_000  # chars returned to the model per tool call


ToolFn = Callable[..., str]


PreviewFn = Callable[..., "dict[str, Any] | None"]


@dataclass
class Tool:
    spec: ToolSpec
    fn: ToolFn
    mutating: bool = False
    # For a mutating tool: what the owner is shown before the call runs in pair mode. Takes the
    # same arguments as the tool. Returns None when there is nothing to approve: the call will
    # be refused or fail anyway (the tool then says why), or it changes nothing.
    preview: PreviewFn | None = None


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    mutating: bool = False,
    preview: PreviewFn | None = None,
):
    def deco(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = Tool(ToolSpec(name, description, parameters), fn, mutating, preview)
        return fn

    return deco


MAX_PREVIEW = 20_000
MAX_PROPOSED = 400_000


def _edit_preview(path: str, old: str, new: str, new_file: bool) -> dict[str, Any]:
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="/dev/null" if new_file else f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    req: dict[str, Any] = {
        "kind": "edit",
        "path": path,
        "new_file": new_file,
        "preview": _clip("\n".join(diff), MAX_PREVIEW),
    }
    if len(new) <= MAX_PROPOSED:
        req["proposed"] = new  # lets an IDE show a real side-by-side diff
    return req


def _clip(s: str, n: int = MAX_OUTPUT) -> str:
    if len(s) <= n:
        return s
    head = s[: n // 2]
    tail = s[-n // 2 :]
    return f"{head}\n...[{len(s) - n} chars omitted]...\n{tail}"


def _int(v: Any, default: int | None) -> int | None:
    """Small models send numbers as strings ("300") or null; never crash a tool on that."""
    if v is None or isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _newline_style(raw: str) -> str:
    """The file's dominant line ending, so edits never flip a whole file between LF and CRLF."""
    crlf = raw.count("\r\n")
    return "\r\n" if crlf and crlf >= raw.count("\n") - crlf else "\n"


def _read_text_raw(p: Path) -> str:
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write_text_raw(p: Path, text: str, newline: str) -> None:
    text = text.replace("\r\n", "\n")
    if newline != "\n":
        text = text.replace("\n", newline)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ---------------------------------------------------------------------------- filesystem


@tool(
    "list_files",
    "List files in the project (or a subdirectory), respecting ignore rules.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "relative dir, default '.'"},
            "max": {"type": "integer", "default": 300},
        },
    },
)
def list_files(ctx: ToolContext, path: str = ".", max: int = 300) -> str:
    return ctx.listing(path, _int(max, 300) or 300)


@tool(
    "read_file",
    "Read a text file. Optionally a line range.",
    {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
    },
)
def read_file(
    ctx: ToolContext, path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    p = ctx.resolve(path)
    if is_secret_file(p.name):
        raise PermissionError(f"{path} holds secrets; read .env.example for the variable names")
    if not p.is_file():
        return f"no such file: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"error: {e}"
    ctx.files_read.add(_norm(path))
    lines = text.splitlines()
    s = max(1, _int(start_line, 1) or 1)
    e = min(len(lines), _int(end_line, len(lines)) or len(lines))
    numbered = [f"{i:5d}| {lines[i - 1]}" for i in range(s, e + 1)]
    return _clip("\n".join(numbered) or "(empty file)")


def _preview_write(ctx: ToolContext, path: str, content: str) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    ctx.check_write(path)
    p, key = ctx.resolve(path), _norm(path)
    if p.is_dir():
        return None
    existed = p.is_file()
    old = _read_text_raw(p).replace("\r\n", "\n") if existed else ""
    if old and key not in ctx.files_read and key not in ctx.files_written:
        return None  # the read-before-overwrite guard refuses this one
    new = content.replace("\r\n", "\n")
    return None if existed and new == old else _edit_preview(key, old, new, not existed)


@tool(
    "write_file",
    "Create or fully overwrite a file with the given content.",
    {
        "type": "object",
        "required": ["path", "content"],
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    },
    mutating=True,
    preview=_preview_write,
)
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    ctx.check_write(path)
    p = ctx.resolve(path)
    existed = p.exists()
    key = _norm(path)
    if (
        existed
        and p.stat().st_size > 0
        and key not in ctx.files_read
        and key not in ctx.files_written
    ):
        # Small models happily overwrite files they never looked at, deleting existing code and
        # tests. Force a read first so the rewrite can preserve what's there.
        raise PermissionError(
            f'{path} already exists and you have not read it. Call read_file("{path}") '
            f"first, then either edit_file to change specific parts or write_file with the FULL "
            f"updated content that keeps everything not covered by the task."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(content, str):
        content = str(content)
    if existed and p.is_file() and p.stat().st_size > 0:
        try:
            _write_text_raw(p, content, _newline_style(_read_text_raw(p)))
        except UnicodeDecodeError:
            p.write_text(content, encoding="utf-8")
    else:
        p.write_text(content, encoding="utf-8")
    ctx.files_written.add(key)
    return f"{'overwrote' if existed else 'created'} {path} ({len(content)} chars)"


def _norm(path: str) -> str:
    """Key used to track read/written files: forward slashes, no leading './'."""
    n = os.path.normpath(path).replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n or path


def _preview_edit(
    ctx: ToolContext, path: str, old_text: str, new_text: str = ""
) -> dict[str, Any] | None:
    ctx.check_write(path)
    p = ctx.resolve(path)
    if not p.is_file() or not old_text:
        return None
    new, _, _ = _apply_edit(p, old_text, new_text)
    old = _read_text_raw(p).replace("\r\n", "\n")
    return None if new is None or new == old else _edit_preview(_norm(path), old, new, False)


@tool(
    "edit_file",
    "Replace an exact substring in a file with new text. old_text must occur exactly once.",
    {
        "type": "object",
        "required": ["path", "old_text", "new_text"],
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
    },
    mutating=True,
    preview=_preview_edit,
)
def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    ctx.check_write(path)
    p = ctx.resolve(path)
    if not p.is_file():
        return f"no such file: {path}"
    if not old_text:
        return (
            "old_text must not be empty — to append, include the last line of the file in "
            "old_text; to create a file, use write_file"
        )
    new, style, error = _apply_edit(p, old_text, new_text)
    if new is None:
        return error
    _write_text_raw(p, new, style)
    ctx.files_written.add(_norm(path))
    return f"edited {path}"


def _apply_edit(p: Path, old_text: str, new_text: str) -> tuple[str | None, str, str]:
    """(new LF-normalised text, the file's newline style, "") or (None, style, error)."""
    raw = _read_text_raw(p)
    style = _newline_style(raw)
    # match on LF-normalised text (read_file shows the model LF lines), write back in the
    # file's own style so a one-line edit stays a one-line diff
    text = raw.replace("\r\n", "\n")
    old_text = str(old_text).replace("\r\n", "\n")
    new_text = str(new_text).replace("\r\n", "\n")
    n = text.count(old_text)
    if n == 0:
        return None, style, "old_text not found — read the file again and copy the exact text"
    if n > 1:
        return None, style, f"old_text occurs {n} times; include more context to make it unique"
    return text.replace(old_text, new_text, 1), style, ""


@tool(
    "search",
    "Search file contents with a regex (like grep -rn). Returns file:line: match.",
    {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "description": "e.g. *.py"},
            "max": {"type": "integer", "default": 100},
        },
    },
)
def search(
    ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None, max: int = 100
) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"bad regex: {e}"
    base = ctx.resolve(path)
    max = _int(max, 100) or 100
    hits: list[str] = []
    for rel, p in ctx.walk(base):
        if glob and not fnmatch.fnmatch(p.name, glob):
            continue
        if is_secret_file(p.name) or not ctx.inside(p):
            continue  # secrets, or a symlink pointing out of the project
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= max:
                            return "\n".join(hits) + f"\n...truncated at {max}"
        except OSError:
            continue
    return "\n".join(hits) or "no matches"


# ---------------------------------------------------------------------------- execution


def _preview_command(ctx: ToolContext, command: str, timeout: int = 120) -> dict[str, Any] | None:
    if not isinstance(command, str) or not command.strip() or readonly_allowed(command):
        return None  # pytest, git status, ls …: nothing to approve
    return {"kind": "command", "command": command, "preview": command}


@tool(
    "run_command",
    "Run a shell command in the project sandbox (timeout 120s). Use for builds, linters, scripts.",
    {
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "default": 120},
        },
    },
    mutating=True,
    preview=_preview_command,
)
def run_command(ctx: ToolContext, command: str, timeout: int = 120) -> str:
    if ctx.readonly and not readonly_allowed(command):
        raise PermissionError(f"this role is read-only; {READONLY_HINT}")
    res = ctx.sandbox.run(command, timeout=min(max(_int(timeout, 120) or 120, 1), 600))
    return _clip(f"exit={res.returncode}\n{res.output}")


@tool(
    "run_tests",
    "Run the project's configured test command in the sandbox.",
    {"type": "object", "properties": {"extra_args": {"type": "string", "default": ""}}},
)
def run_tests(ctx: ToolContext, extra_args: str = "") -> str:
    extra_args = str(extra_args or "").strip()
    if ctx.readonly and extra_args and not readonly_test_args_allowed(extra_args):
        raise PermissionError("this role is read-only; those extra_args are not allowed")
    cmd = f"{ctx.test_command} {extra_args}".strip()
    res = ctx.sandbox.run(cmd, timeout=600)
    return _clip(f"$ {cmd}\nexit={res.returncode}\n{res.output}")


# ---------------------------------------------------------------------------- git


@tool(
    "git_status",
    "Show git status (short) and current branch.",
    {"type": "object", "properties": {}},
)
def git_status(ctx: ToolContext) -> str:
    return _clip(Repo(ctx.root).out("status", "--short", "--branch", timeout=60) or "(clean)")


@tool(
    "git_diff",
    "Show the diff of uncommitted changes (or against a ref).",
    {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "e.g. HEAD, main"},
            "stat_only": {"type": "boolean", "default": False},
        },
    },
)
def git_diff(ctx: ToolContext, ref: str = "", stat_only: bool = False) -> str:
    ref = str(ref or "").strip()
    if ref.startswith("-") or any(c.isspace() for c in ref):
        # the ref is passed straight to git: "--output=<file>" would write anywhere on disk
        raise PermissionError("ref must be a branch, tag or commit, not an option")
    args = ["diff", "--no-ext-diff"]
    if stat_only:
        args.append("--stat")
    if ref:
        args.append(ref)
    args.append("--")
    return _clip(Repo(ctx.root).out(*args, timeout=60) or "(no changes)")


# ---------------------------------------------------------------------------- web


@tool(
    "web_fetch",
    "Fetch a URL and return its text (HTML tags stripped). For documentation lookups.",
    {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
)
def web_fetch(ctx: ToolContext, url: str) -> str:
    import httpx

    url = str(url).strip()
    try:
        r = None
        for _ in range(6):  # follow redirects by hand so every hop is checked
            why = blocked_url(url)
            if why:
                raise PermissionError(why)
            r = httpx.get(
                url,
                timeout=30,
                follow_redirects=False,
                headers={"User-Agent": f"guild/{__version__}"},
            )
            if r.is_redirect and r.headers.get("location"):
                url = str(r.url.join(r.headers["location"]))
                continue
            break
        else:
            return "fetch failed: too many redirects"
    except (httpx.HTTPError, ValueError) as e:
        return f"fetch failed: {e}"
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return _clip(f"HTTP {r.status_code}\n{text.strip()}")


# ---------------------------------------------------------------------------- dispatch


def specs_for(names: list[str]) -> list[ToolSpec]:
    missing = [n for n in names if n not in REGISTRY]
    if missing:
        raise KeyError(f"unknown tools: {missing}")
    return [REGISTRY[n].spec for n in names]


def approval_request(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """What to show the owner before `name(**args)` runs, or None when nothing needs approval."""
    t = REGISTRY.get(name)
    if t is None or t.preview is None:
        return None
    try:
        req = t.preview(ctx, **args)
    except (PermissionError, OSError, UnicodeDecodeError, ValueError, TypeError):
        return None  # the tool itself will refuse or report the problem
    return None if req is None else {"tool": name, **req}


class ToolResult(str):
    """What a tool call returns to the model: text, plus whether the call ran. `ok` is False
    when it was refused, declined, malformed or crashed."""

    ok: bool

    def __new__(cls, text: str, ok: bool = True) -> ToolResult:
        self = super().__new__(cls, text)
        self.ok = ok
        return self


def refusal(reason: str) -> ToolResult:
    return ToolResult(f"refused: {reason}", ok=False)


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolResult:
    t = REGISTRY.get(name)
    if t is None:
        return ToolResult(f"unknown tool: {name}", ok=False)
    ctx.tool_calls_made += 1
    if not isinstance(args, dict):
        args = {}
    # Small models add keys the tool does not take ("explanation", "reason"); drop them rather
    # than fail the call. Arguments that could not be parsed at all are still reported.
    if MALFORMED_ARGS not in args:
        accepted = set(inspect.signature(t.fn).parameters) - {"ctx"}
        args = {k: v for k, v in args.items() if k in accepted}
    if ctx.approver is not None and t.mutating and not ctx.readonly:
        req = approval_request(ctx, name, args)
        if req is not None:
            approved, why = ctx.approver(req)
            if not approved:
                return refusal(
                    "the owner declined this action"
                    + (f" ({why})" if why else "")
                    + ". Do not try it again; ask them what they want instead."
                )
    try:
        return ToolResult(t.fn(ctx, **args))
    except PermissionError as e:
        return refusal(str(e))
    except TypeError as e:
        return ToolResult(f"bad arguments for {name}: {e}", ok=False)
    except Exception as e:
        return ToolResult(f"tool error ({type(e).__name__}): {e}", ok=False)
