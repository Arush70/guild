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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    # Interactive (pair) mode: called with a request dict before a mutating tool runs; returns
    # (approved, reason). None = no gate (the pipeline's engineer works on its own branch).
    approver: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    tool_calls_made: int = 0
    files_written: set[str] = field(default_factory=set)
    files_read: set[str] = field(default_factory=set)

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in [p, *p.parents]:
            raise PermissionError(f"path escapes project root: {rel}")
        return p

    def inside(self, p: Path) -> bool:
        """True when p (after following symlinks) is still inside the project root."""
        try:
            rp = p.resolve()
        except OSError:
            return False
        return self.root.resolve() in [rp, *rp.parents]

    def rel_parts(self, rel: str) -> tuple[str, ...]:
        """Path components relative to the project root, after normalisation."""
        return self.resolve(rel).relative_to(self.root.resolve()).parts

    def is_ignored(self, rel: str) -> bool:
        parts = Path(rel).parts
        for pat in self.ignore:
            if any(fnmatch.fnmatch(part, pat) for part in parts) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    def check_write(self, rel: str) -> None:
        if self.readonly:
            raise PermissionError("this role is read-only")
        parts = self.rel_parts(rel)
        # .git holds hooks and config that git itself executes (outside any sandbox); .guild
        # holds guild's own config, roles, plan and traces. No role may write there.
        if any(part.lower() in PROTECTED_DIRS for part in parts):
            raise PermissionError(f"{rel} is inside a protected directory (.git / .guild)")
        if self.write_allowlist is not None:
            norm = "/".join(parts)
            if not any(fnmatch.fnmatch(norm, pat) for pat in self.write_allowlist):
                raise PermissionError(
                    f"role may only write to {self.write_allowlist}; refused {rel}"
                )


PROTECTED_DIRS = frozenset({".git", ".guild"})


def is_secret_file(name: str) -> bool:
    """dotenv files hold the API keys the sandbox strips from the environment; reading one
    would send them to the model provider and into the trace."""
    n = Path(name).name.lower()
    if n == ".env":
        return True
    return n.startswith(".env.") and not n.endswith((".example", ".sample", ".template", ".dist"))


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
    base = ctx.resolve(path)
    if not base.is_dir():
        return f"not a directory: {path}"
    max = _int(max, 300) or 300
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


@tool(
    "write_file",
    "Create or fully overwrite a file with the given content.",
    {
        "type": "object",
        "required": ["path", "content"],
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    },
    mutating=True,
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
        return (
            f'refused: {path} already exists and you have not read it. Call read_file("{path}") '
            f"first, then either edit_file to change specific parts or write_file with the FULL "
            f"updated content that keeps everything not covered by the task."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(content, str):
        content = str(content)
    if existed and p.is_file() and p.stat().st_size > 0:
        try:  # keep the existing file's line endings (LF vs CRLF)
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
    # "search in this file" is a natural thing for a model to ask
    walk = [(str(base.parent), [], [base.name])] if base.is_file() else os.walk(base)
    for dirpath, dirnames, filenames in walk:
        rel_dir = os.path.relpath(dirpath, ctx.root)
        dirnames[:] = [d for d in dirnames if not ctx.is_ignored(os.path.join(rel_dir, d))]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if ctx.is_ignored(rel) or (glob and not fnmatch.fnmatch(f, glob)):
                continue
            if is_secret_file(f) or not ctx.inside(Path(dirpath) / f):
                continue  # secrets, or a symlink pointing out of the project
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
)
def run_command(ctx: ToolContext, command: str, timeout: int = 120) -> str:
    if ctx.readonly and not _readonly_allowed(command):
        return (
            "refused: this role is read-only; only inspection commands are allowed "
            "(pytest, git log/show/diff/status, grep/rg, ls/cat/head/tail, ruff check, "
            "mypy, pip list/show/audit, bandit)"
        )
    res = ctx.sandbox.run(command, timeout=min(max(_int(timeout, 120) or 120, 1), 600))
    return _clip(f"exit={res.returncode}\n{res.output}")


@tool(
    "run_tests",
    "Run the project's configured test command in the sandbox.",
    {"type": "object", "properties": {"extra_args": {"type": "string", "default": ""}}},
)
def run_tests(ctx: ToolContext, extra_args: str = "") -> str:
    extra_args = str(extra_args or "").strip()
    if ctx.readonly and extra_args and not _readonly_allowed(f"pytest {extra_args}"):
        return "refused: this role is read-only; those extra_args are not allowed"
    cmd = f"{ctx.test_command} {extra_args}".strip()
    res = ctx.sandbox.run(cmd, timeout=600)
    return _clip(f"$ {cmd}\nexit={res.returncode}\n{res.output}")


# Read-only roles may only run inspection commands. An allowlist of command prefixes is used
# instead of a deny-list: "echo x > f", "python -c ...", "sed -i", "del" etc. are all refused.
_READONLY_PREFIXES = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "py -m pytest",
    "python -m unittest",
    "python3 -m unittest",
    "ruff check",
    "ruff format --check",
    "mypy",
    "pyright",
    "flake8",
    "pylint",
    "bandit",
    "pip-audit",
    "pip audit",
    "pip list",
    "pip show",
    "pip freeze",
    "npm test",
    "npm audit",
    "npx tsc --noEmit",
    "cargo test",
    "cargo clippy",
    "go test",
    "go vet",
    "git log",
    "git show",
    "git diff",
    "git status",
    "git blame",
    "git ls-files",
    "git grep",
    "git branch",
    "grep",
    "rg",
    "ls",
    "dir",
    "cat",
    "type",
    "head",
    "tail",
    "wc",
    "find",
    "tree",
    "sort",
    "uniq",
)
# Anything that chains, redirects, substitutes or backgrounds a command. A single "&" and a
# newline are separators too (in sh and in cmd.exe), not only "&&" and ";".
_SHELL_META_RX = re.compile(r"(&|;|>|<|`|\$\(|[\r\n]|\bsudo\b|\bxargs\b)")

# Options that make an otherwise read-only command write files or run another program.
_UNSAFE_ANYWHERE_RX = re.compile(
    r"(?:^|\s)(?:--fix\S*|--delete|--add-noqa|--output(?:-file)?(?:=\S*)?|--ext-diff|"
    r"--basetemp(?:=\S*)?|--junit-?xml(?:=\S*)?|--pre(?:=\S*)?|--open-files-in-pager\S*)"
    r"(?=\s|$)"
)
_FIND_UNSAFE = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
_GIT_BRANCH_SAFE_RX = re.compile(
    r"-a|-r|-v|-vv|--all|--remotes|--verbose|--list|--show-current|--no-color|--color"
    r"|--merged|--no-merged|--contains|--no-contains"
)


def _segment_allowed(seg: str) -> bool:
    if not any(seg == p or seg.startswith(p + " ") for p in _READONLY_PREFIXES):
        return False
    words = seg.split()
    if words[0] == "find" and any(w in _FIND_UNSAFE for w in words):
        return False
    if words[0] in {"sort", "tree"} and any(w == "-o" or w.startswith("-o") for w in words[1:]):
        return False
    if words[0] == "uniq" and len([w for w in words[1:] if not w.startswith("-")]) > 1:
        return False  # "uniq IN OUT" writes OUT
    if "pytest" in words[:3] and any(w == "-p" or w == "-c" or w == "-o" for w in words):
        return False  # load a plugin / another config
    if words[:2] == ["git", "branch"]:
        # listing only: any positional argument or -d/-D/-m/-c/-f creates, moves or deletes
        rest = words[2:]
        if any(not _GIT_BRANCH_SAFE_RX.fullmatch(w) for w in rest if w.startswith("-")):
            return False
        flags_with_value = {"--merged", "--no-merged", "--contains", "--no-contains", "--list"}
        positional = [
            w
            for i, w in enumerate(rest)
            if not w.startswith("-") and not (i and rest[i - 1] in flags_with_value)
        ]
        if positional:
            return False
    return not (words[:2] == ["git", "grep"] and any(w.startswith("-O") for w in words))


def _readonly_allowed(cmd: str) -> bool:
    c = cmd.strip()
    if not c or _SHELL_META_RX.search(c) or _UNSAFE_ANYWHERE_RX.search(c):
        return False
    # plain pipelines like "pytest -q | tail -20" are fine
    return all(_segment_allowed(seg.strip()) for seg in c.split("|"))


def _looks_mutating(cmd: str) -> bool:
    """Kept for callers/tests: True when a read-only role would be refused."""
    return not _readonly_allowed(cmd)


# ---------------------------------------------------------------------------- git


def _git(ctx: ToolContext, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=ctx.root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git error: {e}"
    return ((r.stdout or "") + (r.stderr or "")).strip()


@tool(
    "git_status",
    "Show git status (short) and current branch.",
    {"type": "object", "properties": {}},
)
def git_status(ctx: ToolContext) -> str:
    return _clip(_git(ctx, "status", "--short", "--branch") or "(clean)")


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
    return _clip(_git(ctx, *args) or "(no changes)")


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
            why = _blocked_url(url)
            if why:
                return f"refused: {why}"
            r = httpx.get(
                url, timeout=30, follow_redirects=False, headers={"User-Agent": "guild/0.1"}
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


def _blocked_url(url: str) -> str | None:
    """Reason to refuse a URL, or None. web_fetch is for public documentation: it must not
    reach this machine (the dashboard and Ollama listen on localhost without auth), the LAN,
    or cloud metadata addresses, whatever a web page or a model asks for."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "malformed URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "only http(s) URLs are allowed"
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, parts.port or 0, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return f"cannot resolve host {host}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or (ip.is_multicast or ip.is_unspecified)
        ):
            return f"{host} is a local or private address"
    return None


# ---------------------------------------------------------------------------- approval

MAX_PREVIEW = 20_000
MAX_PROPOSED = 400_000


def _unified(old: str, new: str, path: str, new_file: bool) -> str:
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="/dev/null" if new_file else f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return _clip("\n".join(lines), MAX_PREVIEW)


def approval_request(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """What to show the owner before a mutating tool runs, or None when there is nothing to
    approve: the call is going to be refused or fail anyway (the tool then says why), it
    changes nothing, or it is an inspection command a read-only role could run."""
    try:
        if name in ("write_file", "edit_file"):
            path = args.get("path")
            if not isinstance(path, str) or not path:
                return None
            ctx.check_write(path)
            p = ctx.resolve(path)
            key = _norm(path)
            existed = p.is_file()
            old = _read_text_raw(p).replace("\r\n", "\n") if existed else ""
            if name == "write_file":
                content = args.get("content")
                if not isinstance(content, str) or p.is_dir():
                    return None
                if existed and old and key not in ctx.files_read and key not in ctx.files_written:
                    return None  # the read-before-overwrite guard refuses this one
                new = content.replace("\r\n", "\n")
            else:
                if not existed or not args.get("old_text"):
                    return None
                new, _, _ = _apply_edit(p, args.get("old_text"), args.get("new_text", ""))
                if new is None:
                    return None
            if new == old and existed:
                return None
            req: dict[str, Any] = {
                "kind": "edit",
                "tool": name,
                "path": key,
                "new_file": not existed,
                "preview": _unified(old, new, key, not existed),
            }
            if len(new) <= MAX_PROPOSED:
                req["proposed"] = new  # lets an IDE show a real side-by-side diff
            return req
        if name == "run_command":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return None
            if _readonly_allowed(command):
                return None  # pytest, git status, ls …: nothing to approve
            return {"kind": "command", "tool": name, "command": command, "preview": command}
    except (PermissionError, OSError, UnicodeDecodeError, ValueError):
        return None
    return None


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
    if not isinstance(args, dict):
        args = {}
    # Small models add keys the tool does not take ("explanation", "reason"); drop them rather
    # than fail the call. A truly malformed call (parse_json_args' "_raw") is still reported.
    if "_raw" not in args:
        accepted = set(inspect.signature(t.fn).parameters) - {"ctx"}
        args = {k: v for k, v in args.items() if k in accepted}
    if ctx.approver is not None and t.mutating and not ctx.readonly:
        req = approval_request(ctx, name, args)
        if req is not None:
            approved, why = ctx.approver(req)
            if not approved:
                return (
                    "refused: the owner declined this action"
                    + (f" ({why})" if why else "")
                    + ". Do not try it again; ask them what they want instead."
                )
    try:
        return t.fn(ctx, **args)
    except PermissionError as e:
        return f"refused: {e}"
    except TypeError as e:
        return f"bad arguments for {name}: {e}"
    except Exception as e:
        return f"tool error ({type(e).__name__}): {e}"
