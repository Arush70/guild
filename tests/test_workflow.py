from __future__ import annotations


from guild.trace import read_trace
from guild.workflow import Guild, Plan, Task
from tests.conftest import FakeProvider

PLAN = {"roadmap": ["m1"], "tasks": [
    {"id": "T1", "title": "add multiply", "description": "add multiply(a,b) to app.py with a test",
     "files": ["app.py"], "done_when": "test_multiply passes", "depends_on": []}],
    "risks": [], "questions_for_owner": []}

ENGINEER_GOOD = [
    [("read_file", {"path": "app.py"})],
    [("edit_file", {"path": "app.py", "old_text": "    return a + b\n",
                    "new_text": "    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"}),
     ("write_file", {"path": "test_multiply.py", "content": "from app import multiply\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"})],
    [("run_tests", {})],
    {"status": "done", "summary": "added multiply", "files_changed": ["app.py", "test_multiply.py"],
     "tests_run": "2 passed", "notes_for_reviewer": ""},
]
VERIFIER_RUNS = [[("run_tests", {})], {"passed": True, "tests_run": 2, "failures": [], "output_tail": "2 passed"}]
APPROVE = {"verdict": "approve", "findings": [], "summary": "fine"}
LEAD_ACCEPT = {"decision": "ACCEPT", "notes": "good", "improvements": ["add type hints"]}
DOCS = [[("edit_file", {"path": "README.md", "old_text": "# demo\n", "new_text": "# demo\n\nHas multiply.\n"})],
        {"files_changed": ["README.md"], "summary": "documented multiply"}]


def _guild(project, cfg, profile, fake):
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    return g


def test_plan_saves_tasks(project, cfg, profile):
    fake = FakeProvider({"lead": [[("list_files", {})], PLAN]})
    g = _guild(project, cfg, profile, fake)
    plan = g.plan("add multiply")
    g.close()
    assert [t.id for t in plan.tasks] == ["T1"]
    assert g.load_plan().tasks[0].title == "add multiply"
    kinds = [e["kind"] for e in read_trace(g.trace.path)]
    assert "model_call" in kinds and "tool_call" in kinds and kinds[-1] == "run_end"


def test_happy_path_accepts_and_documents(project, cfg, profile):
    fake = FakeProvider({"engineer": ENGINEER_GOOD, "verifier": VERIFIER_RUNS, "critic": [APPROVE],
                         "security": [APPROVE], "lead": [LEAD_ACCEPT], "docs": DOCS})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    assert out.accepted and out.rounds == 1 and not out.escalated
    assert "multiply" in (project / "app.py").read_text()
    assert "Has multiply" in (project / "README.md").read_text()
    assert out.task.status == "done" and out.task.branch.startswith("guild/t1-")
    # real tests actually ran in the sandbox
    ver_ev = [e for e in read_trace(g.trace.path) if e["kind"] == "tool_call" and e["tool"] == "run_tests"]
    assert ver_ev and "passed" in ver_ev[-1]["result_preview"]


def test_revision_round_then_escalation(project, cfg, profile):
    reject = {"verdict": "request_changes", "summary": "bad",
              "findings": [{"severity": "high", "file": "app.py", "issue": "no test", "suggestion": "add one"}]}
    eng = [{"status": "done", "summary": "r1", "files_changed": [], "tests_run": "", "notes_for_reviewer": ""},
           {"status": "done", "summary": "r2", "files_changed": [], "tests_run": "", "notes_for_reviewer": ""},
           {"status": "done", "summary": "r3", "files_changed": [], "tests_run": "", "notes_for_reviewer": ""}]
    fake = FakeProvider({"engineer": eng, "verifier": [VERIFIER_RUNS[1]] * 3,
                         "critic": [reject, reject, APPROVE], "security": [APPROVE] * 3,
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    assert out.accepted and out.rounds == 3 and out.escalated
    models = [m for r, m in fake.calls if r == "engineer"]
    assert models == ["small", "small", "big"]  # escalated after 2 failed rounds


def test_security_block_prevents_acceptance(project, cfg, profile):
    block = {"verdict": "block", "summary": "secret committed",
             "findings": [{"severity": "critical", "file": "app.py", "issue": "API key", "evidence": "x", "fix": "remove"}]}
    eng = [{"status": "done", "summary": "", "files_changed": [], "tests_run": "", "notes_for_reviewer": ""}] * 3
    fake = FakeProvider({"engineer": eng, "verifier": [VERIFIER_RUNS[1]] * 3, "critic": [APPROVE] * 3,
                         "security": [block] * 3})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    assert not out.accepted and out.task.status == "todo" and "SECURITY" in out.task.notes


def test_fallback_chain_skips_failing_model(project, cfg, profile):
    fake = FakeProvider({"engineer": [{"status": "blocked", "summary": "cannot"}]}, fail_models={"small"})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0], roles=["engineer"])
    g.close()
    assert out.task.status == "blocked"
    assert fake.calls == [("engineer", "small2")]
    assert any(e["kind"] == "fallback" for e in read_trace(g.trace.path))


def test_readonly_roles_cannot_write(project, cfg, profile):
    fake = FakeProvider({"critic": [[("write_file", {"path": "evil.py", "content": "x"})], APPROVE]})
    g = _guild(project, cfg, profile, fake)
    g.agent("critic").run("review")
    g.close()
    assert not (project / "evil.py").exists()
    # write_file isn't even offered to the critic; dispatch reports unknown/refused
    ev = [e for e in read_trace(g.trace.path) if e["kind"] == "tool_call"]
    assert ev and not ev[0]["ok"]


def test_docs_role_write_allowlist(project, cfg, profile):
    fake = FakeProvider({"docs": [[("write_file", {"path": "app.py", "content": "pwned"})],
                                  {"files_changed": [], "summary": ""}]})
    g = _guild(project, cfg, profile, fake)
    g.agent("docs").run("update docs")
    g.close()
    assert "pwned" not in (project / "app.py").read_text()


def test_path_escape_refused(project, cfg, profile):
    fake = FakeProvider({"engineer": [[("read_file", {"path": "../../etc/passwd"})],
                                      {"status": "done", "summary": ""}]})
    g = _guild(project, cfg, profile, fake)
    g.agent("engineer").run("x")
    g.close()
    ev = [e for e in read_trace(g.trace.path) if e["kind"] == "tool_call"][0]
    assert "refused" in ev["result_preview"]


def test_run_tasks_selected_ids_in_order(project, cfg, profile):
    eng = [{"status": "done", "summary": "", "files_changed": [], "tests_run": "", "notes_for_reviewer": ""}] * 2
    fake = FakeProvider({"engineer": eng, "verifier": [VERIFIER_RUNS[1]] * 2, "critic": [APPROVE] * 2,
                         "security": [APPROVE] * 2, "lead": [LEAD_ACCEPT] * 2, "docs": [DOCS[1]] * 2})
    g = _guild(project, cfg, profile, fake)
    t1 = Task(id="T1", title="a", description="x")
    t2 = Task(id="T2", title="b", description="y")
    t3 = Task(id="T3", title="c", description="z")
    plan = Plan(goal="g", roadmap=[], tasks=[t1, t2, t3])
    outs = g.run_tasks(plan, ["T3", "T1"])
    g.close()
    assert [o.task.id for o in outs] == ["T3", "T1"]
    assert t3.status == "done" and t1.status == "done" and t2.status == "todo"
