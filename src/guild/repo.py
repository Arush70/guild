"""Git. Every git command guild runs goes through `Repo`; the git state of one task run (its
branch, where it started, what was untracked before) is a `TaskBranch`.

Safety rules that live here: guild commits only on a branch it created, never deletes a branch
that is not `guild/…`, and merges only when a human asks.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .paths import CACHE_DIRS, GUILD_DIR, ProjectPaths, is_secret_file

BRANCH_PREFIX = "guild/"
_NOT_GUILD = f":(exclude){GUILD_DIR}"
# Caches created while guild runs tests must never end up in its commits.
_NOT_CACHES = (
    *(f":(exclude,glob){pre}{d}/**" for d in CACHE_DIRS for pre in ("**/", "")),
    ":(exclude)*.pyc",
)
_NO_IDENTITY_RX = re.compile(r"tell me who you are|auto-detect email|empty ident")
_NEW_FILE_BYTES = 20_000


def slug(text: str, limit: int) -> str:
    """Lower-case; every run of other characters becomes one '-'. Always a valid ref component
    (model-written ids like "Task 1: set~up" are not)."""
    cleaned = "".join(c if c.isalnum() else "-" for c in str(text).lower())
    return re.sub(r"-{2,}", "-", cleaned)[:limit].strip("-")


def files_in_diff(diff: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/.+? b/(.+)$", diff, flags=re.M)))


@dataclass
class CommitResult:
    committed: bool
    warning: str = ""  # committed, but the owner should know something
    error: str = ""  # not committed


class Repo:
    def __init__(self, root: Path):
        self.root = root

    @property
    def exists(self) -> bool:
        return ProjectPaths(self.root).has_git

    # ------------------------------------------------------------------ running git
    def run(self, *args: str, timeout: float | None = None) -> tuple[int, str]:
        """(exit code, stdout+stderr). Use the exit code whenever an error message must not be
        mistaken for data (a diff, a branch name)."""
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as e:  # git missing, or hung
            return 127, f"git error: {e}"
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    def out(self, *args: str, timeout: float | None = None) -> str:
        return self.run(*args, timeout=timeout)[1]

    @classmethod
    def init(cls, root: Path) -> Repo | None:
        repo = cls(root)
        repo.run("init", "-q", "-b", "main")
        return repo if repo.exists else None

    # ------------------------------------------------------------------ state
    def head(self) -> str | None:
        """Commit id of HEAD, or None in a repository with no commits yet."""
        rc, out = self.run("rev-parse", "--verify", "--quiet", "HEAD")
        return out if rc == 0 and re.fullmatch(r"[0-9a-f]{40,64}", out) else None

    def current_branch(self) -> str | None:
        """Branch name, or None when HEAD is detached. Works with no commits."""
        rc, out = self.run("symbolic-ref", "--short", "-q", "HEAD")
        return out if rc == 0 and out else None

    def branch_exists(self, name: str | None) -> bool:
        return (
            bool(name)
            and not name.startswith("-")
            and self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0
        )

    def default_branch(self) -> str:
        for cand in ("main", "master"):
            if self.branch_exists(cand):
                return cand
        return self.current_branch() or "main"

    def untracked(self) -> set[str]:
        # -z: plain names; without it git quotes non-ASCII paths and they never match again
        listing = self.out("ls-files", "-z", "--others", "--exclude-standard")
        return set(filter(None, listing.split("\0")))

    def is_dirty(self) -> bool:
        """Uncommitted changes to tracked files. Untracked files, .guild and .gitignore (which
        `guild init` edits) do not count."""
        return bool(
            self.out(
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                ".",
                _NOT_GUILD,
                ":(exclude).gitignore",
            )
        )

    # ------------------------------------------------------------------ diffs
    def diff(self, *args: str) -> str | None:
        """A diff with guild's own files left out, or None when git reports an error (an error
        message is not a diff)."""
        rc, out = self.run("diff", "--no-ext-diff", *args, "--", ".", _NOT_GUILD)
        return out if rc == 0 else None

    def show(self, commit: str) -> str:
        return self.out("show", "--format=%h %s%n", commit, "--", ".", _NOT_GUILD)

    def new_files_diff(self, known_untracked: set[str]) -> str:
        """Untracked files that were not there before, rendered as a diff. Plain `git diff`
        never shows untracked files."""
        chunks: list[str] = []
        skip = (*CACHE_DIRS, GUILD_DIR, ".git")
        for rel in sorted(self.untracked() - known_untracked):
            if rel.endswith(".pyc") or any(part in skip for part in Path(rel).parts):
                continue
            if is_secret_file(rel):
                continue  # a new .env is not shown to the reviewing models
            try:
                raw = (self.root / rel).read_bytes()[:_NEW_FILE_BYTES]
            except OSError:
                continue
            head = f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n"
            if b"\x00" in raw:
                chunks.append(head + "(binary file)")
            else:
                text = raw.decode("utf-8", errors="replace")
                chunks.append(head + "\n".join("+" + ln for ln in text.splitlines()))
        return "\n".join(chunks)

    # ------------------------------------------------------------------ commits
    def commit_all(self, message: str, *, leave_untracked: set[str] = frozenset()) -> CommitResult:
        """Commit every change except guild's files, caches and `leave_untracked` (files that
        were already untracked before guild started and are not its to commit)."""
        self.run("add", "-A", "--", ".", _NOT_GUILD, *_NOT_CACHES)
        added = self.out("diff", "--cached", "--name-only", "--diff-filter=A", "-z").split("\0")
        # a credentials file that appeared during the task (the owner's new .env) stays out
        for f in {*leave_untracked, *(a for a in added if a and is_secret_file(a))}:
            self.run("reset", "-q", "--", f":(literal){f}")
        if self.run("diff", "--cached", "--quiet")[0] == 0:
            return CommitResult(False)  # nothing staged
        rc, out = self.run("commit", "-q", "-m", message)
        if rc == 0:
            return CommitResult(True)
        if _NO_IDENTITY_RX.search(out):
            # A fresh git install has no user.name/user.email. Commit as "guild" rather than
            # leave an accepted task with an empty branch.
            identity = ("-c", "user.name=guild", "-c", "user.email=guild@localhost")
            rc, out = self.run(*identity, "commit", "-q", "-m", message)
            if rc == 0:
                return CommitResult(
                    True,
                    warning="git has no user.name/user.email; committed as 'guild'. Set them "
                    'with: git config --global user.name "You" && '
                    'git config --global user.email "you@example.com"',
                )
        return CommitResult(False, error=out)

    # ------------------------------------------------------------------ branches, for the owner
    def _base_for(self, base: str | None) -> str:
        """The branch a guild branch is compared with and merged into: where the task started,
        or the default branch when that is unknown or gone (a stacked guild branch that was
        merged and deleted)."""
        return base if self.branch_exists(base) else self.default_branch()

    def branch_diff(self, branch: str, base: str | None = None) -> dict:
        base = self._base_for(base)
        if not self.branch_exists(branch):
            return {"branch": branch, "base": base, "exists": False, "diff": "", "stat": ""}
        rng = f"{base}...{branch}"
        return {
            "branch": branch,
            "base": base,
            "exists": True,
            "stat": self.diff("--stat", rng) or "",
            "diff": self.diff(rng) or "",
            "commits": self.out("log", "--oneline", f"{base}..{branch}"),
        }

    def merge_branch(self, branch: str, base: str | None = None) -> dict:
        """Merge a guild branch into its base. Refuses on a dirty tree or a conflict."""
        base = self._base_for(base)
        if base == branch:
            return {"ok": False, "error": f"cannot merge {branch} into itself (no base branch)"}
        if not self.branch_exists(branch):
            return {"ok": False, "error": f"branch {branch} does not exist"}
        if self.is_dirty():
            return {"ok": False, "error": "working tree is dirty; commit or stash first"}
        rc, out = self.run("checkout", "-q", base)
        if rc != 0:
            return {"ok": False, "error": f"could not switch to {base}:\n{out}"}
        rc, out = self.run("merge", "--no-ff", "-q", "-m", f"Merge {branch} (guild)", branch)
        if "CONFLICT" in out or self.out("ls-files", "-u"):
            self.run("merge", "--abort")
            return {"ok": False, "error": f"merge conflict; resolve manually:\n{out}"}
        if rc != 0:  # the exit code, not the words "error"/"fatal": a branch may be named that
            return {"ok": False, "error": out or f"git merge exited {rc}"}
        deleted = self.out("branch", "-d", branch)
        return {"ok": True, "base": base, "merged": branch, "output": f"{out}\n{deleted}".strip()}

    def discard_branch(self, branch: str) -> dict:
        # `git branch -D` is unrecoverable for unmerged work: only ever on guild's own branches
        if not str(branch).startswith(BRANCH_PREFIX):
            return {"ok": False, "output": f"refusing to delete {branch}: not a guild/ branch"}
        if self.current_branch() == branch:
            rc, out = self.run("checkout", "-q", self.default_branch())
            if rc != 0:
                return {"ok": False, "output": f"could not leave {branch}:\n{out}"}
        rc, out = self.run("branch", "-D", "--", branch)
        return {"ok": rc == 0, "output": out}


class TaskBranch:
    """The git side of one task run.

    `start()` puts the task on `guild/<id>-<title>` when that is safe. When it is not (no git,
    no commits yet, uncommitted changes, dry run) the task still runs, in the working tree, and
    guild commits nothing: it never commits to a branch it did not create.
    """

    def __init__(self, repo: Repo, warn: Callable[[str], None]):
        self.repo = repo
        self.warn = warn
        self.name: str | None = None  # the guild/ branch, when one was created
        self.started_from: str | None = None  # branch to return to if the task is not accepted
        self.base_ref: str | None = None  # commit the task started at; the review diffs from it
        self.untracked_before: set[str] = set()

    @classmethod
    def start(
        cls, repo: Repo, task_id: str, title: str, *, dry_run: bool, warn: Callable[[str], None]
    ) -> TaskBranch:
        tb = cls(repo, warn)
        if not repo.exists:
            return tb
        tb.base_ref = repo.head()
        tb.untracked_before = repo.untracked()
        if dry_run:
            return tb
        if tb.base_ref is None:
            warn(
                "git repository has no commits yet; guild will not create a branch and will "
                'NOT commit. Run: git add -A && git commit -m "initial commit"'
            )
            return tb
        if repo.is_dirty():
            warn(
                "you have uncommitted changes to tracked files; guild will not create a "
                "branch and will NOT commit. Commit or stash first for a clean branch."
            )
            return tb
        name = f"{BRANCH_PREFIX}{slug(task_id, 30) or 'task'}-{slug(title, 40)}".rstrip("-")
        tb.started_from = repo.current_branch()
        if tb.started_from != name and repo.branch_exists(name):
            # checkout -B moves the branch here; say where an earlier attempt can be found
            lost = repo.out("rev-list", "--count", f"HEAD..refs/heads/{name}")
            if lost.isdigit() and int(lost):
                old = repo.out("rev-parse", "--short", f"refs/heads/{name}")
                warn(
                    f"branch {name} already had {lost} commit(s) from an earlier run that are "
                    f"not on {tb.started_from or 'this commit'}; it now restarts from here. "
                    f"To get the earlier work back: git branch {name}-earlier {old}"
                )
        rc, out = repo.run("checkout", "-B", name)
        if rc != 0:
            raise RuntimeError(f"git checkout -B {name} failed: {out}")
        tb.name = name
        return tb

    @property
    def _on_branch(self) -> bool:
        return self.name is not None and self.repo.current_branch() == self.name

    def commit(self, message: str) -> str:
        """Commit the task's work. Returns the git error when the commit failed, else ""."""
        if not self._on_branch:
            return ""
        res = self.repo.commit_all(message, leave_untracked=self.untracked_before)
        if res.warning:
            self.warn(res.warning)
        if res.error:
            self.warn(f"git commit failed — the work is NOT on the branch: {res.error[:300]}")
        return res.error

    def leave(self) -> None:
        """After a task that was not accepted (or crashed): return to where it started, so the
        next task does not build on broken work. The attempt stays on its branch for
        inspection. Uncommitted work is never discarded: with a dirty tree we stay put."""
        if not self._on_branch or not self.started_from or self.repo.is_dirty():
            return
        self.repo.run("checkout", "-q", self.started_from)

    def review_diff(self) -> str:
        """Everything the task changed: committed, uncommitted and new files."""
        if not self.repo.exists:
            return "(no git; review working tree)"
        repo = self.repo
        tracked = repo.diff(self.base_ref or "HEAD~1") or repo.diff() or repo.diff("--cached")
        new = repo.new_files_diff(self.untracked_before)
        return "\n".join(x for x in (tracked, new) if x) or "(no diff)"
