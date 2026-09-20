"""Tools agents can call. Every tool is scoped to the project root and refuses to escape it.

Adding a tool: write a function taking (ctx, **kwargs) -> str, wrap it with @tool(...).
Roles pick tools by name in their YAML `tools:` list.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..providers.base import ToolSpec

MAX_OUTPUT = 12_000  # chars returned to the model per tool call


@dataclass
class ToolContext:
    root: Path
    ignore: list[str]
    test_command: str
    lint_command: str | None
    sandbox: Any  # guild.sandbox.Sandbox
    write_allowlist: list[str] | None = None
    readonly: bool = False
    tool_calls_made: int = 0
    files_written: set[str] = field(default_factory=set)

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in [p, *p.parents]:
            raise PermissionError(f"path escapes project root: {rel}")
        return p

    def is_ignored(self, rel: str) -> bool:
        parts = Path(rel).parts
        for pat in self.ignore:
            if any(fnmatch.fnmatch(part, pat) for part in parts) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    def check_write(self, rel: str) -> None:
        if self.readonly:
            raise PermissionError("this role is read-only")
        if self.write_allowlist is not None:
            if not any(fnmatch.fnmatch(rel, pat) for pat in self.write_allowlist):
                raise PermissionError(f"role may only write to {self.write_allowlist}; refused {rel}")


ToolFn = Callable[..., str]


@dataclass
class Tool:
    spec: ToolSpec
    fn: ToolFn
    mutating: bool = False


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict[str, Any], mutating: bool = False):
    def deco(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = Tool(ToolSpec(name, description, parameters), fn, mutating)
        return fn
    return deco


def _clip(s: str, n: int = MAX_OUTPUT) -> str:
    if len(s) <= n:
        return s
    head = s[: n // 2]
    tail = s[-n // 2:]
    return f"{head}\n...[{len(s) - n} chars omitted]...\n{tail}"


# ---------------------------------------------------------------------------- filesystem

@tool("list_files", "List files in the project (or a subdirectory), respecting ignore rules.",
      {"type": "object", "properties": {"path": {"type": "string", "description": "relative dir, default '.'"},
                                        "max": {"type": "integer", "default": 300}}})
def list_files(ctx: ToolContext, path: str = ".", max: int = 300) -> str:
    base = ctx.resolve(path)
    if not base.is_dir():
        return f"not a directory: {path}"
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, ctx.root)
        dirnames[:] = sorted(d for d in dirnames if not ctx.is_ignored(os.path.join(rel_dir, d)))
        for f in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if ctx.is_ignored(rel):
                continue
            try:
                size = (Path(dirpath) / f).stat().st_size
            except OSError:
                size = 0
            out.append(f"{rel}  ({size} B)")
            if len(out) >= max:
                out.append(f"...truncated at {max} entries")
                return "\n".join(out)
    return "\n".join(out) or "(empty)"


@tool("read_file", "Read a text file. Optionally a line range.",
      {"type": "object", "required": ["path"],
       "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                      "end_line": {"type": "integer"}}})
def read_file(ctx: ToolContext, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = ctx.resolve(path)
    if not p.is_file():
        return f"no such file: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"error: {e}"
    lines = text.splitlines()
    s = max(1, start_line or 1)
    e = min(len(lines), end_line or len(lines))
    numbered = [f"{i:5d}| {lines[i-1]}" for i in range(s, e + 1)]
    return _clip("\n".join(numbered) or "(empty file)")


@tool("write_file", "Create or fully overwrite a file with the given content.",
      {"type": "object", "required": ["path", "content"],
       "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, mutating=True)
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    ctx.check_write(path)
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    ctx.files_written.add(path)
    return f"{'overwrote' if existed else 'created'} {path} ({len(content)} chars)"


@tool("edit_file", "Replace an exact substring in a file with new text. old_text must occur exactly once.",
      {"type": "object", "required": ["path", "old_text", "new_text"],
       "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                      "new_text": {"type": "string"}}}, mutating=True)
def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    ctx.check_write(path)
    p = ctx.resolve(path)
    if not p.is_file():
        return f"no such file: {path}"
    if not old_text:
        return "old_text must not be empty — to append, include the last line of the file in old_text; to create a file, use write_file"
    text = p.read_text(encoding="utf-8")
    n = text.count(old_text)
    if n == 0:
        return "old_text not found — read the file again and copy the exact text"
    if n > 1:
        return f"old_text occurs {n} times; include more context to make it unique"
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    ctx.files_written.add(path)
    return f"edited {path}"


@tool("search", "Search file contents with a regex (like grep -rn). Returns file:line: match.",
      {"type": "object", "required": ["pattern"],
       "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."},
                      "glob": {"type": "string", "description": "e.g. *.py"},
                      "max": {"type": "integer", "default": 100}}})
def search(ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None, max: int = 100) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"bad regex: {e}"
    base = ctx.resolve(path)
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, ctx.root)
        dirnames[:] = [d for d in dirnames if not ctx.is_ignored(os.path.join(rel_dir, d))]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if ctx.is_ignored(rel) or (glob and not fnmatch.fnmatch(f, glob)):
                continue
            try:
                with open(Path(dirpath) / f, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                            if len(hits) >= max:
                                return "\n".join(hits) + f"\n...truncated at {max}"
            except OSError:
                continue
    return "\n".join(hits) or "no matches"


# ---------------------------------------------------------------------------- execution

@tool("run_command", "Run a shell command in the project sandbox (timeout 120s). Use for builds, linters, scripts.",
      {"type": "object", "required": ["command"],
       "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 120}}},
      mutating=True)
def run_command(ctx: ToolContext, command: str, timeout: int = 120) -> str:
    if ctx.readonly and _looks_mutating(command):
        return "refused: this role is read-only and the command looks like it modifies state"
    res = ctx.sandbox.run(command, timeout=min(timeout, 600))
    return _clip(f"exit={res.returncode}\n{res.output}")


@tool("run_tests", "Run the project's configured test command in the sandbox.",
      {"type": "object", "properties": {"extra_args": {"type": "string", "default": ""}}})
def run_tests(ctx: ToolContext, extra_args: str = "") -> str:
    cmd = f"{ctx.test_command} {extra_args}".strip()
    res = ctx.sandbox.run(cmd, timeout=600)
    return _clip(f"$ {cmd}\nexit={res.returncode}\n{res.output}")


_MUTATING_RX = re.compile(r"\b(rm|mv|cp|chmod|chown|git\s+(commit|push|reset|checkout|clean)|pip\s+install|npm\s+install|>|>>|tee)\b")


def _looks_mutating(cmd: str) -> bool:
    return bool(_MUTATING_RX.search(cmd))


# ---------------------------------------------------------------------------- git

def _git(ctx: ToolContext, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=ctx.root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git error: {e}"
    return (r.stdout + r.stderr).strip()


@tool("git_status", "Show git status (short) and current branch.", {"type": "object", "properties": {}})
def git_status(ctx: ToolContext) -> str:
    return _clip(_git(ctx, "status", "--short", "--branch") or "(clean)")


@tool("git_diff", "Show the diff of uncommitted changes (or against a ref).",
      {"type": "object", "properties": {"ref": {"type": "string", "description": "e.g. HEAD, main"},
                                        "stat_only": {"type": "boolean", "default": False}}})
def git_diff(ctx: ToolContext, ref: str = "", stat_only: bool = False) -> str:
    args = ["diff"]
    if stat_only:
        args.append("--stat")
    if ref:
        args.append(ref)
    return _clip(_git(ctx, *args) or "(no changes)")


# ---------------------------------------------------------------------------- web

@tool("web_fetch", "Fetch a URL and return its text (HTML tags stripped). For documentation lookups.",
      {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}})
def web_fetch(ctx: ToolContext, url: str) -> str:
    import httpx
    if not url.startswith(("http://", "https://")):
        return "only http(s) URLs are allowed"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "guild/0.1"})
    except httpx.HTTPError as e:
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


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    t = REGISTRY.get(name)
    if t is None:
        return f"unknown tool: {name}"
    ctx.tool_calls_made += 1
    try:
        return t.fn(ctx, **args)
    except PermissionError as e:
        return f"refused: {e}"
    except TypeError as e:
        return f"bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 — tool errors go back to the model, not up the stack
        return f"tool error ({type(e).__name__}): {e}"
