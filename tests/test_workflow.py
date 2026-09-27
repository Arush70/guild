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
DONE = {"status": "done", "summary": "touched", "files_changed": ["notes.txt"], "tests_run": "", "notes_for_reviewer": ""}
# an engineer turn that actually changes a file (the workflow refuses "done" with no changes)
ENG_TOUCH = [[("write_file", {"path": "notes.txt", "content": "touched\n"})], DONE]
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
    # real tests actually ran in the sandbox, without a model in the loop
    ver_ev = [e for e in read_trace(g.trace.path) if e["kind"] == "verification"]
    assert ver_ev and ver_ev[-1]["passed"] is True and ver_ev[-1]["tests_run"] == 2
    assert not any(r == "verifier" for r, _ in fake.calls)


def test_revision_round_then_escalation(project, cfg, profile):
    reject = {"verdict": "request_changes", "summary": "bad",
              "findings": [{"severity": "high", "file": "app.py", "issue": "no test", "suggestion": "add one"}]}
    eng = ENG_TOUCH * 3
    fake = FakeProvider({"engineer": eng, "verifier": [VERIFIER_RUNS[1]] * 3,
                         "critic": [reject, reject, APPROVE], "security": [APPROVE] * 3,
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    assert out.accepted and out.rounds == 3 and out.escalated
    models = [m for r, m in fake.calls if r == "engineer"]
    assert models == ["small", "small", "small", "small", "big", "big"]  # 2 calls per round; escalated after 2 failed rounds


def test_security_block_prevents_acceptance(project, cfg, profile):
    block = {"verdict": "block", "summary": "secret committed",
             "findings": [{"severity": "critical", "file": "app.py", "issue": "API key", "evidence": "x", "fix": "remove"}]}
    eng = ENG_TOUCH * 3
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
    eng = ENG_TOUCH * 2
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


def test_verification_detects_real_failure(project, cfg, profile):
    # engineer "does" nothing but the repo now has a failing test → verification must fail
    (project / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n")
    eng = ENG_TOUCH * 3
    fake = FakeProvider({"engineer": eng, "critic": [APPROVE] * 3, "security": [APPROVE] * 3})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    assert not out.accepted
    assert out.verification["passed"] is False and any("test_bad" in f for f in out.verification["failures"])
    assert "TESTS FAILED" in out.task.notes


def test_text_tool_calls_are_executed(project, cfg, profile):
    """Small models often write {"name": ..., "arguments": ...} as text. It must still run."""
    from guild.providers.base import Completion, Message, Usage

    class TextToolFake:
        def __init__(self):
            self.n = 0
        def complete(self, model_name, messages, tools, max_tokens, temperature):
            self.n += 1
            if self.n == 1:
                txt = 'Sure!\n{"name": "write_file", "arguments": {"path": "new.py", "content": "x = 1\\n"}}'
            else:
                txt = '{"status": "done", "summary": "wrote new.py", "files_changed": ["new.py"], "tests_run": "", "notes_for_reviewer": ""}'
            return Completion(Message("assistant", txt), Usage(10, 5), f"fake/{model_name}", "stop")

    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", TextToolFake())
    res = g.agent("engineer").run("create new.py")
    g.close()
    assert (project / "new.py").read_text() == "x = 1\n"
    assert res.data["status"] == "done"
    ev = [e for e in read_trace(g.trace.path) if e["kind"] == "tool_call"]
    assert ev and ev[0]["tool"] == "write_file" and ev[0]["ok"]


def test_guild_metadata_never_in_commits_or_diff(project, cfg, profile):
    fake = FakeProvider({"engineer": ENGINEER_GOOD, "critic": [APPROVE], "security": [APPROVE],
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    g.save_plan(plan)
    g.run_task(plan, plan.tasks[0])
    g.close()
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=project, capture_output=True, text=True).stdout
    assert ".guild" not in tracked


def test_selected_tasks_continue_after_failure(project, cfg, profile):
    block = {"verdict": "block", "summary": "bad", "findings": []}
    eng = ENG_TOUCH * 6
    fake = FakeProvider({"engineer": eng, "critic": [APPROVE] * 6,
                         "security": [block] * 3 + [APPROVE] * 3, "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    t1 = Task(id="T1", title="a", description="x")
    t2 = Task(id="T2", title="b", description="y")
    t3 = Task(id="T3", title="c", description="z", depends_on=["T1"])
    plan = Plan(goal="g", roadmap=[], tasks=[t1, t2, t3])
    outs = g.run_tasks(plan, ["T1", "T2", "T3"])
    g.close()
    assert [(o.task.id, o.accepted) for o in outs] == [("T1", False), ("T2", True)]  # T3 skipped: depends on T1


def test_untracked_files_do_not_block_branch_and_stay_uncommitted(project, cfg, profile):
    (project / "scratch.txt").write_text("mine")  # untracked, pre-existing
    fake = FakeProvider({"engineer": ENGINEER_GOOD, "critic": [APPROVE], "security": [APPROVE],
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    import subprocess
    assert out.task.branch and out.task.branch.startswith("guild/")
    tracked = subprocess.run(["git", "ls-files"], cwd=project, capture_output=True, text=True).stdout
    assert "scratch.txt" not in tracked and "test_multiply.py" in tracked


def test_dirty_tracked_file_means_no_branch_and_no_commit(project, cfg, profile):
    (project / "app.py").write_text("def add(a, b):\n    return a + b  # edited\n")  # tracked, modified
    import subprocess
    head0 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout
    fake = FakeProvider({"engineer": ENGINEER_GOOD, "critic": [APPROVE], "security": [APPROVE],
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    g.close()
    head1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip()
    assert out.task.branch is None and head0 == head1 and branch == "master"  # nothing committed, still on master


def test_merge_and_discard_helpers(project, cfg, profile):
    fake = FakeProvider({"engineer": ENGINEER_GOOD, "critic": [APPROVE], "security": [APPROVE],
                         "lead": [LEAD_ACCEPT], "docs": [DOCS[1]]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0])
    d = g.branch_diff(out.task.branch)
    assert d["exists"] and "multiply" in d["diff"] and "test_multiply.py" in d["stat"]
    res = g.merge_branch(out.task.branch)
    g.close()
    assert res["ok"], res
    assert g.current_branch() == "master" and "multiply" in (project / "app.py").read_text()
    assert not g._git("rev-parse", "--verify", "--quiet", out.task.branch)  # branch deleted after merge


def test_engineer_done_without_changes_is_pushed_back(project, cfg, profile):
    """A model that writes code as prose and says 'done' gets one more go; if it still changes
    nothing, the task is blocked rather than falsely accepted."""
    from guild.providers.base import Completion, Message, Usage

    class ProseEngineer:
        def __init__(self):
            self.n = 0
        def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
            self.n += 1
            role = messages[0].content
            if "Software Engineer" in role[:300]:
                txt = ('Here is the code:\n```python\ndef multiply(a, b):\n    return a * b\n```\n'
                       '{"status": "done", "summary": "added multiply", "files_changed": ["app.py"], "tests_run": "", "notes_for_reviewer": ""}')
            else:
                txt = '{"verdict": "approve", "findings": [], "summary": "ok"}'
            return Completion(Message("assistant", txt), Usage(1, 1), f"fake/{model_name}", "stop")

    fake = ProseEngineer()
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    out = g.run_task(plan, plan.tasks[0], roles=["engineer", "verifier", "critic"])
    g.close()
    assert not out.accepted and out.task.status == "blocked"
    assert "no file changes" in out.task.notes
    assert "multiply" not in (project / "app.py").read_text()
    notes = [e for e in read_trace(g.trace.path) if e["kind"] == "note"]
    assert any("changed no files" in e["msg"] for e in notes)


def test_run_task_records_traceback_on_crash(project, cfg, profile, monkeypatch):
    fake = FakeProvider({"engineer": ENGINEER_GOOD})
    g = _guild(project, cfg, profile, fake)
    monkeypatch.setattr(g, "verify", lambda: (_ for _ in ()).throw(TypeError("boom")))
    plan = Plan(goal="g", roadmap=[], tasks=[Task(**PLAN["tasks"][0])])
    import pytest
    with pytest.raises(TypeError):
        g.run_task(plan, plan.tasks[0])
    g.close()
    err = [e for e in read_trace(g.trace.path) if e["kind"] == "error"]
    assert err and "boom" in err[0]["error"] and "verify" in err[0]["traceback"]
    assert plan.tasks[0].status == "todo" and "guild error" in plan.tasks[0].notes
