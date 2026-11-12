"""Where a tool may look (`Workspace`) and what the role using it may do (`ToolContext`)."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import PROTECTED_DIRS, windows_name


@dataclass
class Workspace:
    """A project directory and its ignore rules. All path scoping lives here: nothing resolves
    outside `root`, ignored paths are skipped, and a symlink is never followed out."""

    root: Path
    ignore: list[str]

    def resolve(self, rel: str) -> Path:
        if str(rel).startswith(("\\\\", "//")):
            # a network path (\\server\share): even looking at it makes Windows log in to
            # that server with the owner's account
            raise PermissionError(f"path escapes project root: {rel}")
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

    def walk(self, base: Path) -> Iterator[tuple[str, Path]]:
        """(path relative to root, absolute path) of every non-ignored file under `base`, in a
        stable order. `base` may be a single file."""
        if base.is_file():
            rel = os.path.relpath(base, self.root)
            if not self.is_ignored(rel):
                yield rel, base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, self.root)
            dirnames[:] = sorted(
                d for d in dirnames if not self.is_ignored(os.path.join(rel_dir, d))
            )
            for f in sorted(filenames):
                rel = os.path.normpath(os.path.join(rel_dir, f))
                if not self.is_ignored(rel):
                    yield rel, Path(dirpath) / f

    def listing(self, path: str = ".", limit: int = 300) -> str:
        """One line per file with its size; what the list_files tool and the project summary
        show a model."""
        base = self.resolve(path)
        if not base.is_dir():
            return f"not a directory: {path}"
        out: list[str] = []
        for rel, p in self.walk(base):
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append(f"{rel}  ({size} B)")
            if len(out) >= limit:
                out.append(f"...truncated at {limit} entries")
                break
        return "\n".join(out) or "(empty)"


def _is_protected(part: str) -> bool:
    """.git and .guild under every name Windows accepts for them: other case, trailing dots or
    spaces, an NTFS stream suffix, and the 8.3 short name (GIT~1, GUILD~1)."""
    name = windows_name(part)
    return name in PROTECTED_DIRS or bool(re.fullmatch(r"(git|guild)~\d+", name))


@dataclass
class ToolContext(Workspace):
    """A workspace plus one role's permissions and what it has done so far."""

    test_command: str = ""
    lint_command: str | None = None
    sandbox: Any = None  # guild.sandbox.LocalSandbox | DockerSandbox
    write_allowlist: list[str] | None = None
    readonly: bool = False
    # Interactive (pair) mode: called with a request dict before a mutating tool runs; returns
    # (approved, reason). None = no gate (the pipeline's engineer works on its own branch).
    approver: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    tool_calls_made: int = 0
    files_written: set[str] = field(default_factory=set)
    files_read: set[str] = field(default_factory=set)

    def check_write(self, rel: str) -> None:
        if self.readonly:
            raise PermissionError("this role is read-only")
        parts = self.rel_parts(rel)
        if any(_is_protected(part) for part in parts):
            raise PermissionError(f"{rel} is inside a protected directory (.git / .guild)")
        if self.write_allowlist is not None:
            norm = "/".join(parts)
            if not any(fnmatch.fnmatch(norm, pat) for pat in self.write_allowlist):
                raise PermissionError(
                    f"role may only write to {self.write_allowlist}; refused {rel}"
                )
