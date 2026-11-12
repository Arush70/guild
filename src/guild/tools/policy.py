"""What agents may run and reach. Security rules, kept apart from the tools that apply them so
a rule can be reviewed (and tested) on its own."""

from __future__ import annotations

import re
from pathlib import Path


def is_secret_file(name: str) -> bool:
    """dotenv files hold the API keys the sandbox strips from the environment; reading one
    would send them to the model provider and into the trace."""
    n = Path(name).name.lower()
    if n == ".env":
        return True
    return n.startswith(".env.") and not n.endswith((".example", ".sample", ".template", ".dist"))


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


# Shown to a read-only role whose command was refused.
READONLY_HINT = (
    "only inspection commands are allowed (pytest, git log/show/diff/status, grep/rg, "
    "ls/cat/head/tail, ruff check, mypy, pip list/show/audit, bandit)"
)


def readonly_test_args_allowed(extra_args: str) -> bool:
    """Extra arguments a read-only role adds to the project's test command. They are judged as
    test-runner arguments: no chaining, no redirection, no plugin or config loading."""
    return readonly_allowed(f"pytest {extra_args}")


def readonly_allowed(cmd: str) -> bool:
    c = cmd.strip()
    if not c or _SHELL_META_RX.search(c) or _UNSAFE_ANYWHERE_RX.search(c):
        return False
    # plain pipelines like "pytest -q | tail -20" are fine
    return all(_segment_allowed(seg.strip()) for seg in c.split("|"))


def blocked_url(url: str) -> str | None:
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
