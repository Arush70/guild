"""The plan: a goal, a roadmap and tasks. Everything that reads or changes `.guild/plan.json`
goes through this module."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import atomic_write_text

TASK_STATUSES = ("todo", "in_progress", "done", "blocked")


class PlanError(RuntimeError):
    """The plan cannot be read, or a change to it is not valid."""


class UnknownTask(PlanError):
    pass


@dataclass
class Task:
    id: str
    title: str
    description: str
    files: list[str] = field(default_factory=list)
    done_when: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "todo"
    branch: str | None = None
    notes: str = ""
    merged: bool = False  # the owner merged the branch (guild never merges by itself)
    base: str | None = None  # branch the task started from; merges go back into it


_VAGUE = (
    "designed",
    "documented",
    "works well",
    "is complete",
    "is ready",
    "looks good",
    "is implemented",
    "is created",
    "is done",
    "is added",
    "functional",
)
_CONCRETE = (
    "test",
    "pytest",
    "pass",
    "exit",
    "returns",
    "assert",
    "exists",
    "contains",
    "==",
    "file",
    ".py",
    "command",
    "output",
)


def vague_done_when(text: str) -> bool:
    """Heuristic: a done_when that names no test/command/file is probably not checkable."""
    t = (text or "").lower()
    return not any(k in t for k in _CONCRETE) or (
        any(v in t for v in _VAGUE) and not any(k in t for k in ("test", "pass"))
    )


def _string_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise PlanError(f"{name} must be a list of strings")
    return value


@dataclass
class Plan:
    goal: str
    roadmap: list[str]
    tasks: list[Task]
    risks: list[str] = field(default_factory=list)
    questions_for_owner: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ (de)serialisation
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        known = {f.name for f in fields(Task)}
        return cls(
            goal=d["goal"],
            roadmap=d.get("roadmap", []),
            tasks=[Task(**{k: v for k, v in t.items() if k in known}) for t in d.get("tasks", [])],
            risks=d.get("risks", []),
            questions_for_owner=d.get("questions_for_owner", []),
            created=d.get("created", time.time()),
        )

    @classmethod
    def from_lead(cls, goal: str, data: dict, previous: Plan | None = None) -> Plan:
        """Build a plan from the Lead's validated reply. With `previous`, this is a revision:
        a task that keeps its id keeps its branch and notes, and stays done if it was done."""
        old = {t.id: t for t in previous.tasks} if previous else {}
        tasks: list[Task] = []
        for i, t in enumerate(data.get("tasks", [])):
            task = Task(
                id=t.get("id") or f"T{i + 1}",
                title=t.get("title", ""),
                description=t.get("description", ""),
                files=t.get("files", []),
                done_when=t.get("done_when", ""),
                depends_on=t.get("depends_on", []),
            )
            prev = old.get(task.id)
            if prev:
                task.branch, task.notes, task.base = prev.branch, prev.notes, prev.base
                if prev.status == "done":
                    task.status, task.merged = "done", prev.merged
            tasks.append(task)
        return cls(
            goal=goal,
            roadmap=data.get("roadmap", previous.roadmap if previous else []),
            tasks=tasks,
            risks=data.get("risks", []),
            questions_for_owner=data.get("questions_for_owner", []),
            created=previous.created if previous else time.time(),
        )

    # ------------------------------------------------------------------ queries
    def find(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def require(self, *task_ids: str) -> list[Task]:
        missing = [tid for tid in task_ids if self.find(tid) is None]
        if missing:
            known = ", ".join(t.id for t in self.tasks)
            raise UnknownTask(
                f"no task {', '.join(map(str, missing))} in the plan (tasks: {known})"
            )
        return [self.find(tid) for tid in task_ids]

    def unmet_dependencies(self, task: Task) -> list[str]:
        """Ids this task still waits for. Ids that are not in the plan (task deleted, plan.json
        edited by hand) are ignored instead of blocking the task forever."""
        status = {t.id: t.status for t in self.tasks}
        return [d for d in task.depends_on if d in status and d != task.id and status[d] != "done"]

    def next_task(self, skip: set[str] = frozenset()) -> Task | None:
        """The first task that can run now, ignoring ids in `skip` (already tried this run)."""
        # "in_progress" with nobody running it is a leftover from a killed run (Ctrl+C, crash,
        # closed terminal); it must be runnable again, not stuck forever.
        for t in self.tasks:
            runnable = t.status in ("todo", "in_progress") and not self.unmet_dependencies(t)
            if runnable and t.id not in skip:
                return t
        return None

    def warnings(self) -> list[str]:
        out = []
        for t in self.tasks:
            if vague_done_when(t.done_when):
                out.append(
                    f"{t.id}: 'done when' is not checkable ({t.done_when!r}) — "
                    "edit it before running"
                )
            if len(t.description or "") < 40:
                out.append(
                    f"{t.id}: description is very short; the engineer may not know what to build"
                )
        return out

    # ------------------------------------------------------------------ changes by the owner
    def edit_task(self, task_id: str, changes: dict[str, Any]) -> Task:
        (task,) = self.require(task_id)
        for name in ("title", "description", "done_when"):
            if name in changes:
                if not isinstance(changes[name], str):
                    raise PlanError(f"{name} must be a string")
                setattr(task, name, changes[name])
        if "status" in changes:
            if changes["status"] not in TASK_STATUSES:
                raise PlanError(f"status must be one of {list(TASK_STATUSES)}")
            task.status = changes["status"]
        if "files" in changes:
            task.files = _string_list("files", changes["files"])
        if "depends_on" in changes:
            ids = {t.id for t in self.tasks} - {task_id}
            task.depends_on = [
                d for d in _string_list("depends_on", changes["depends_on"]) if d in ids
            ]
        return task

    def delete_task(self, task_id: str) -> None:
        (task,) = self.require(task_id)
        self.tasks.remove(task)
        for t in self.tasks:  # nothing may keep waiting for a task that is gone
            t.depends_on = [d for d in t.depends_on if d != task_id]

    def branch_merged(self, task_id: str) -> None:
        self.require(task_id)[0].merged = True

    def branch_discarded(self, task_id: str) -> None:
        (task,) = self.require(task_id)
        task.branch, task.merged, task.status = None, False, "todo"


class PlanStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Plan | None:
        if not self.path.exists():
            return None
        try:
            return Plan.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise PlanError(f"{self.path} is not a readable plan ({type(e).__name__}: {e})") from e

    def save(self, plan: Plan) -> None:
        atomic_write_text(self.path, json.dumps(plan.to_dict(), indent=2))
