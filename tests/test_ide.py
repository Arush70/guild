"""The IDE-style dashboard: what the page needs from the server beyond the job API."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .conftest import FakeProvider
from .test_pair import make_ui  # noqa: F401 — fixture


def test_the_editor_is_served_from_the_package(make_ui):  # noqa: F811
    ui = make_ui(FakeProvider({}))
    page = ui.get("/").text
    assert '<script src="/vendor/monaco/vs/loader.js">' in page
    loader = ui.get("/vendor/monaco/vs/loader.js")
    assert loader.status_code == 200 and "javascript" in loader.headers["content-type"]
    assert ui.get("/vendor/monaco/vs/editor/editor.main.js").status_code == 200
    assert ui.get("/vendor/../server.py").status_code == 404


def test_saving_a_file_from_the_editor(make_ui, project):  # noqa: F811
    ui = make_ui(FakeProvider({}))
    opened = ui.get("/api/file", params={"path": "app.py"}).json()
    assert opened["editable"] and opened["version"]
    text = opened["content"].replace("a + b", "a + b  # edited")
    saved = ui.post(
        "/api/file", json={"path": "app.py", "content": text, "version": opened["version"]}
    )
    assert saved.status_code == 200 and "# edited" in (project / "app.py").read_text()

    # saved again from the stale copy another window still has open
    stale = ui.post(
        "/api/file", json={"path": "app.py", "content": "x", "version": opened["version"]}
    )
    assert stale.status_code == 409 and "# edited" in (project / "app.py").read_text()

    created = ui.post("/api/file", json={"path": "docs/new.md", "content": "a\r\nb\r\n"})
    assert created.status_code == 200
    assert (project / "docs" / "new.md").read_bytes() == b"a\r\nb\r\n"


def test_the_editor_cannot_write_where_the_agents_cannot(make_ui, project):  # noqa: F811
    ui = make_ui(FakeProvider({}))
    for path in (
        ".git/hooks/pre-commit",
        ".codeguild/config.yaml",
        "../outside.txt",
        ".GIT/config",
    ):
        res = ui.post("/api/file", json={"path": path, "content": "x"})
        assert res.status_code == 403, path
    assert not (project.parent / "from-the-editor.txt").exists()
    assert ui.post("/api/file", json={"path": ".", "content": "x"}).status_code == 400
    assert ui.get("/api/file", params={"path": ".git/config"}).json()["editable"] is False
    binary = project / "legacy.txt"
    binary.write_bytes(b"caf\xe9")
    assert ui.get("/api/file", params={"path": "legacy.txt"}).json()["editable"] is False


def test_a_saved_script_stays_executable(make_ui, project):  # noqa: F811
    import os
    import stat

    script = project / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    ui = make_ui(FakeProvider({}))
    version = ui.get("/api/file", params={"path": "run.sh"}).json()["version"]
    assert (
        ui.post(
            "/api/file", json={"path": "run.sh", "content": "#!/bin/sh\n", "version": version}
        ).status_code
        == 200
    )
    if os.name != "nt":
        assert script.stat().st_mode & stat.S_IXUSR


def _task_branch(project: Path) -> None:
    def git(*args):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    base = subprocess.run(
        ["git", "branch", "--show-current"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    git("checkout", "-q", "-b", "codeguild/t1-x")
    (project / "app.py").write_text("def add(a, b):\n    return b + a\n")
    (project / "new file.py").write_text("y = 2\n")
    git("rm", "-q", "test_app.py")
    git("add", "-A", "--", ".", ":(exclude).codeguild")
    git("commit", "-qm", "task")
    git("checkout", "-q", base)
    task = {"id": "T1", "title": "x", "description": "d", "status": "done"}
    plan = {"goal": "g", "tasks": [{**task, "branch": "codeguild/t1-x", "base": base}]}
    (project / ".codeguild" / "plan.json").write_text(json.dumps(plan))


def test_a_task_branch_can_be_read_file_by_file(make_ui, project):  # noqa: F811
    ui = make_ui(FakeProvider({}))
    _task_branch(project)
    listed = ui.get("/api/branches/T1/files").json()
    assert listed["branch"] == "codeguild/t1-x"
    assert {(f["path"], f["status"]) for f in listed["files"]} == {
        ("app.py", "M"),
        ("new file.py", "A"),
        ("test_app.py", "D"),
    }
    changed = ui.get("/api/branches/T1/file", params={"path": "app.py"}).json()
    assert "a + b" in changed["old"] and "b + a" in changed["new"]
    added = ui.get("/api/branches/T1/file", params={"path": "new file.py"}).json()
    assert added["old"] is None and added["new"] == "y = 2"
    removed = ui.get("/api/branches/T1/file", params={"path": "test_app.py"}).json()
    assert removed["new"] is None and "test_add" in removed["old"]
    assert ui.get("/api/branches/T9/files").status_code == 404
