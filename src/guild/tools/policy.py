"""What agents may run and reach. Security rules, kept apart from the tools that apply them so
a rule can be reviewed (and tested) on its own."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from ..paths import CACHE_DIRS, PROTECTED_DIRS, is_secret_file

SKIP_WHEN_SCANNING = frozenset({*PROTECTED_DIRS, *CACHE_DIRS, "node_modules", ".venv", "venv"})

# Read-only roles may only run inspection commands, and in pair mode these are the commands
# that run without asking the owner. An allowlist of command prefixes is used instead of a
# deny-list: "echo x > f", "python -c ...", "sed -i", "del" etc. are all refused.
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
# Never allowed, quoted or not: command substitution, variable expansion (a variable can hold
# an option the checks below would refuse), cmd.exe's escape character and line breaks.
_EXPANSION_RX = re.compile(r"`|\$[({\w@*#?!-]|\^|[\r\n\x00]")
_CMD_VARIABLE_RX = re.compile(r"%([^%\s]+)%")


def _expands_in_cmd(cmd: str) -> bool:
    """cmd.exe replaces %NAME% (and %NAME:~0,1% substrings, enough to spell any option) inside
    quotes too. "%h%n%s" in a git format is left alone: no such variables exist."""
    names = {k.upper() for k in os.environ}
    return any(":" in m or m.upper() in names for m in _CMD_VARIABLE_RX.findall(cmd))


# Separators and redirections. Refused outside double quotes; inside them they are text for
# both sh and cmd.exe (single quotes do not quote in cmd.exe, so they do not count).
_SHELL_META = frozenset("&;<>")

# Options that make an otherwise read-only command write a file, run another program, install
# something or send data away. Matched against each word after unquoting, up to "=".
_UNSAFE_OPTIONS = frozenset(
    {
        "--fix",
        "--fix-only",
        "--unsafe-fixes",
        "--delete",
        "--add-noqa",
        "--output",
        "--output-file",
        "--output-format-file",
        "--ext-diff",
        "--textconv",
        "--no-index",
        "--basetemp",
        "--junitxml",
        "--junit-xml",
        "--pre",
        "--open-files-in-pager",
        "--log-file",
        "--log",
        "--debug",
        "--pastebin",
        "--override-ini",
        "--rootdir",
        "--confcutdir",
        "--cache-dir",
        "--report-log",
        "--html",
        "--init-hook",
        "--load-plugins",
        "--install-types",
        "--createstub",
        "--compress-program",
        "--hostname-bin",
        "--generatetrace",
        "--watch",
        "--config",
        "--config-file",
        "--rcfile",
        "--shadow-file",
        "--python-executable",
        "--files0-from",
        "--write-changes",
        "--tee",
        "-exec",
        "-vettool",
        "-toolexec",
        "-coverprofile",
        "-cpuprofile",
        "-memprofile",
        "-blockprofile",
        "-mutexprofile",
        "-trace",
        "-outputdir",
        "-delete",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)
_UNSAFE_OPTION_RX = re.compile(r"--\w[\w-]*-report$|--cov-report$")  # mypy / pytest-cov reports
# Commands where a short "-o" (alone, with a value, or inside a cluster like "-ro") names an
# output file or overrides configuration. For grep, rg and ls it is a harmless display flag.
_DASH_O_WRITES = frozenset({"sort", "tree", "ruff", "bandit", "pip-audit", "go", "pytest", "pip"})
_GIT_BRANCH_SAFE_RX = re.compile(
    r"-a|-r|-v|-vv|--all|--remotes|--verbose|--list|--show-current|--no-color|--color"
    r"|--merged|--no-merged|--contains|--no-contains"
)


def _segments(cmd: str) -> list[str] | None:
    """Split a command line at unquoted "|". None when it chains, redirects or backgrounds
    anything outside double quotes, or a quote is left open."""
    out, cur, quote = [], [], ""
    for ch in cmd:
        if quote:
            if ch == quote:
                quote = ""
            elif ch in _SHELL_META and quote == "'":
                return None
        elif ch in "\"'":
            quote = ch
        elif ch in _SHELL_META:
            return None
        elif ch == "|":
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if quote:
        return None
    out.append("".join(cur))
    return out


def _words(segment: str) -> list[str] | None:
    """The arguments the program will receive: quotes and backslash escapes removed, as a POSIX
    shell does. Options are checked on these, so `"--output=x"` and `-del""ete` hide nothing."""
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return None


def _unsafe_option(word: str, program: str) -> bool:
    low = word.lower()
    name = low.split("=", 1)[0]
    if name in _UNSAFE_OPTIONS or _UNSAFE_OPTION_RX.search(name):
        return True
    if any(name.startswith(opt) for opt in ("--fix", "--open-files-in-pager", "--pre-")):
        return True
    if program in _DASH_O_WRITES and re.fullmatch(r"-[a-zA-Z]*o.*", word):
        return True
    if program == "pytest" and re.match(r"-[pc]", word):
        return True  # load a plugin / another config file
    return program == "git" and word.startswith("-O")  # git grep -O<pager>


_GLOB_CHARS = frozenset("*?[")


def _reads_outside(word: str, root: Path | None) -> bool:
    """True when an argument names a secret file, or an existing path outside the project.
    Checked on what exists, so a search pattern like "/api/state" is not mistaken for a path."""
    value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    if not value or value.startswith("-"):
        return False
    if value.startswith(("~", "\\\\", "//")):
        return True  # the home folder, or a network share
    path = value.replace("\\", "/").rstrip("/")
    # "HEAD:.env" (git show) names a file after the colon
    names = {path.rsplit("/", 1)[-1], path.rsplit(":", 1)[-1].rsplit("/", 1)[-1]}
    if any(is_secret_file(n) for n in names):
        return True
    try:
        base = (root or Path.cwd()).resolve()
        if _GLOB_CHARS & set(path):
            # the shell expands the pattern; see what it can expand to
            folder, _, pattern = path.rpartition("/")
            target = (base / folder).resolve()
            hits = [n for n in os.listdir(target) if fnmatch.fnmatch(n, pattern)]
            return any(is_secret_file(n) for n in hits) or base not in (target, *target.parents)
        target = (base / value).resolve()
        return base not in (target, *target.parents) and target.exists()
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


# Searching every file of a folder also searches its credential files. The `search` tool
# skips them; these command forms do not.
_GREP_RECURSIVE_RX = re.compile(
    r"-[a-zA-Z]*[rR][a-zA-Z]*|--(dereference-)?recursive|--directories=recurse"
)
_RG_UNFILTERED = frozenset({"--hidden", "--no-ignore", "--unrestricted", "-."})


def _searches_secrets(words: list[str], root: Path | None) -> bool:
    if words[0] == "grep":
        everything = any(_GREP_RECURSIVE_RX.fullmatch(w) for w in words[1:])
    elif words[0] == "rg":
        everything = any(
            w in _RG_UNFILTERED or re.fullmatch(r"-[a-zA-Z]*u.*", w) for w in words[1:]
        )
    else:
        return False
    if not everything:
        return False
    base = root or Path.cwd()
    # the folders named on the command line, or the whole project when none is
    folders = [base / w for w in words[1:] if not w.startswith("-") and (base / w).is_dir()]
    return any(_has_secret_file(f) for f in folders or [base])


def _has_secret_file(root: Path, limit: int = 20_000) -> bool:
    seen = 0
    for _, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_WHEN_SCANNING]
        if any(is_secret_file(f) for f in filenames):
            return True
        seen += len(filenames)
        if seen > limit:
            return True  # too big to be sure: ask
    return False


def _segment_allowed(segment: str, root: Path | None) -> bool:
    words = _words(segment)
    # the same line as cmd.exe sees it: there, single quotes and backslashes are ordinary text
    raw_words = segment.replace('"', "").split()
    if not words or not raw_words:
        return False
    normal = " ".join(words)
    if not any(normal == p or normal.startswith(p + " ") for p in _READONLY_PREFIXES):
        return False
    if words[0] != raw_words[0]:
        return False  # the program name itself was quoted or escaped
    program = "pytest" if "pytest" in words[:3] else words[0]
    for word in {*words[1:], *raw_words[1:]}:
        if _unsafe_option(word, program) or _reads_outside(word, root):
            return False
    if _searches_secrets(words, root):
        return False
    if words[:2] == ["npm", "audit"] and len(words) > 2 and not words[2].startswith("-"):
        return False  # "npm audit fix"
    if program == "uniq" and len([w for w in words[1:] if not w.startswith("-")]) > 1:
        return False  # "uniq IN OUT" writes OUT
    if program == "tree" and "-R" in words:
        return False  # with -H, writes an index file into every directory
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
    return True


# Shown to a read-only role whose command was refused.
READONLY_HINT = (
    "only inspection commands on files of this project are allowed (pytest, git "
    "log/show/diff/status, grep/rg, ls/cat/head/tail, ruff check, mypy, pip list/show/audit, "
    "bandit), without options that write files or run other programs, and not on credential "
    "files (.env); to search the whole project use the search tool"
)


def readonly_test_args_allowed(extra_args: str, root: Path | None = None) -> bool:
    """Extra arguments added to the project's test command. They are judged as test-runner
    arguments: no chaining, no redirection, no plugin or config loading, no report files."""
    return readonly_allowed(f"pytest {extra_args}", root) and "|" not in extra_args


def readonly_allowed(cmd: str, root: Path | None = None) -> bool:
    """True when `cmd` only inspects the project at `root`: a read-only role may run it, and
    pair mode runs it without asking the owner."""
    c = cmd.strip() if isinstance(cmd, str) else ""
    if not c or _EXPANSION_RX.search(c) or _expands_in_cmd(c):
        return False
    if re.search(r"\b(sudo|xargs)\b", c):
        return False
    segments = _segments(c)
    # plain pipelines like "pytest -q | tail -20" are fine
    return bool(segments) and all(_segment_allowed(seg.strip(), root) for seg in segments)


def vetted_address(url: str) -> tuple[str | None, str | None]:
    """(address to connect to, None) for a public http(s) URL, or (None, reason to refuse).

    web_fetch is for public documentation: it must not reach this machine (the dashboard and
    Ollama listen on localhost without auth), the LAN, or cloud metadata addresses, whatever
    a web page or a model asks for. The caller connects to the returned address, not to the
    name: a name that is looked up twice can answer "public" first and "127.0.0.1" second.
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None, "malformed URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None, "only http(s) URLs are allowed"
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, port or 0, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None, f"cannot resolve host {host}"
    addresses = [info[4][0].split("%")[0] for info in infos]
    for address in addresses:
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 is 127.0.0.1
        if not (mapped or ip).is_global or ip.is_multicast:
            return None, f"{host} is a local or private address"
    if not addresses:
        return None, f"cannot resolve host {host}"
    return addresses[0], None


def blocked_url(url: str) -> str | None:
    """Reason to refuse a URL, or None."""
    return vetted_address(url)[1]
