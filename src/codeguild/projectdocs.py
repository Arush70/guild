"""The project documents every role reads for context (PRD, architecture, rules, tasks, memory,
decisions, design), and the templates `codeguild init` offers for them."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

# (label, where the document may live — first match wins, template to scaffold or None).
# A scaffolded document is written to the first location.
DOCS: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("PRD", ("docs/PRD.md", "PRD.md"), "PRD.md"),
    ("Architecture", ("docs/ARCHITECTURE.md", "ARCHITECTURE.md"), "ARCHITECTURE.md"),
    ("Rules", ("RULES.md", "docs/RULES.md", "CLAUDE.md", ".cursorrules"), "RULES.md"),
    ("Tasks", ("TASKS.md", "docs/TASKS.md"), "TASKS.md"),
    ("Memory", ("docs/MEMORY.md", "MEMORY.md"), "MEMORY.md"),
    ("Decisions", ("docs/DECISIONS.md", "DECISIONS.md"), "DECISIONS.md"),
    ("Design", ("docs/DESIGN.md", "DESIGN.md"), None),
)


def read(root: Path, per_doc_chars: int = 3000) -> dict[str, str]:
    """{label (path): content} of the documents that exist and are not empty."""
    out: dict[str, str] = {}
    for label, candidates, _ in DOCS:
        path = next((root / rel for rel in candidates if (root / rel).is_file()), None)
        text = path.read_text(encoding="utf-8", errors="replace").strip() if path else ""
        if text:
            cut = "\n…(truncated)" if len(text) > per_doc_chars else ""
            out[f"{label} ({path.relative_to(root).as_posix()})"] = text[:per_doc_chars] + cut
    return out


def scaffold(root: Path) -> list[str]:
    """Write the templates for documents the project does not have yet. Returns what was made."""
    templates = resources.files("codeguild").joinpath("data", "templates")
    created: list[str] = []
    for _, candidates, template in DOCS:
        dst = root / candidates[0]
        if template is None or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(templates.joinpath(template).read_text(encoding="utf-8"), encoding="utf-8")
        created.append(candidates[0])
    return created
