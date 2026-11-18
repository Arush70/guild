"""Reviewers are roles with a `review:` instruction; enabling one adds it to every task."""

from __future__ import annotations

import yaml

from codeguild.config import list_available, load_role
from codeguild.plan import Plan, Task
from codeguild.trace import read_trace
from codeguild.workflow import Guild

from .conftest import FakeProvider

APPROVE = {"verdict": "approve", "findings": [], "summary": "fine"}
WRITE = [("write_file", {"path": "b.py", "content": "y = 2\n"})]
DONE = {"status": "done", "summary": "wrote b.py"}


def test_the_reviewer_roles_that_ship():
    reviewers = {n for n in list_available("roles") if load_role(n).review}
    assert reviewers == {"critic", "security", "performance", "ux", "data-quality"}
    assert load_role("critic").review_phase == "critique"
    assert load_role("ux").readonly and load_role("data-quality").readonly
    assert load_role("engineer").review is None


def test_an_enabled_reviewer_reviews_and_can_stop_a_task(project, cfg, profile):
    cfg.roles_enabled = ["engineer", "critic", "performance"]
    slow = {"verdict": "request_changes", "summary": "quadratic loop", "findings": []}
    fake = FakeProvider(
        {
            "engineer": [WRITE, DONE, WRITE, DONE],
            "critic": [APPROVE, APPROVE],
            "unknown": [slow, APPROVE],  # the scripted provider has no name for this role
        }
    )
    profile.limits.max_revision_rounds = 2
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    assert g.reviewers(cfg.roles_enabled) == ["critic", "performance"]
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    out = g.run_tasks(plan, ["T1"])[0]
    g.close()
    assert out.accepted and out.rounds == 2
    assert set(out.reviews) == {"critic", "performance"}
    phases = [e["phase"] for e in read_trace(g.trace.path) if e["kind"] == "phase"]
    assert phases.count("performance") == 2 and phases.count("critique") == 2
    shape = out.to_dict()
    assert shape["security"] is None and shape["performance"] == APPROVE


def test_a_projects_own_critic_without_a_review_line_still_reviews(project):
    own = load_role("critic").model_dump()
    for key in ("review", "review_phase"):
        own.pop(key)
    (project / ".codeguild" / "roles").mkdir(parents=True)
    (project / ".codeguild" / "roles" / "critic.yaml").write_text(yaml.safe_dump(own))
    role = load_role("critic", project)
    assert role.review == "Review the change." and role.review_phase == "critique"


def test_a_role_listed_in_roles_enabled_that_does_not_exist_is_not_a_reviewer(
    project, cfg, profile
):
    g = Guild(project, cfg, profile)
    assert g.reviewers(["critic", "no-such-role", "engineer"]) == ["critic"]
    g.close()


def test_a_projects_own_reviewer_gets_its_reply_checked_as_a_review(project, cfg, profile):
    (project / ".codeguild" / "roles").mkdir(parents=True)
    own = load_role("ux").model_dump() | {"name": "a11y", "title": "Accessibility Reviewer"}
    (project / ".codeguild" / "roles" / "a11y.yaml").write_text(yaml.safe_dump(own))
    cfg.roles_enabled = ["engineer", "a11y"]
    # first reply is not a review; the repair turn produces one
    fake = FakeProvider({"engineer": [WRITE, DONE], "unknown": [{"looks": "fine"}, APPROVE]})
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    out = g.run_tasks(plan, ["T1"])[0]
    g.close()
    assert out.accepted and out.reviews["a11y"]["verdict"] == "approve"
    assert sum(1 for role, _ in fake.calls if role == "unknown") == 2
