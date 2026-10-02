from __future__ import annotations

import pytest

from guild.agent import extract_json
from guild.config import list_available, load_profile, load_role
from guild.providers.base import Usage
from guild.providers.router import DEFAULT_PRICES, CostTracker
from guild.tools.registry import REGISTRY


def test_builtin_profiles_and_roles_load():
    for name in ("free", "lite", "pro"):
        p = load_profile(name)
        assert {"frontier", "coder", "reasoner", "cheap"} <= set(p.slots)
        for chain in p.slots.values():
            for m in chain:
                assert "/" in m, m
    for name in list_available("roles"):
        r = load_role(name)
        unknown = set(r.tools) - set(REGISTRY)
        assert not unknown, f"{name} references unknown tools {unknown}"
        for s in filter(None, [r.slot, r.escalation_slot]):
            for prof in ("free", "lite", "pro"):
                # every slot a role needs must exist in every profile, except escalation in free
                if s == "coder_escalation" and prof == "free":
                    continue
                assert s in load_profile(prof).slots, f"{name}.{s} missing from {prof}"


def test_free_profile_has_no_paid_models():
    p = load_profile("free")
    for chain in p.slots.values():
        for m in chain:
            assert not m.startswith(("anthropic/", "openai/")), m
    assert p.limits.max_usd_per_run == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('Sure!\n```json\n{"a": 1}\n```', {"a": 1}),
        ('prefix {"a": {"b": 2}} suffix', {"a": {"b": 2}}),
        ("no json here", None),
        ("", None),
        # deepseek-r1: reasoning (with braces) before the answer
        ('<think>plan: {step: 1}</think>\n{"verdict": "approve"}', {"verdict": "approve"}),
        ('I think {"a": 1} is wrong, final: {"a": 2}', {"a": 2}),
        (
            '{"status": "done", "summary": "wrote {x}"} ok',
            {"status": "done", "summary": "wrote {x}"},
        ),
    ],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_readonly_command_allowlist():
    from guild.tools.registry import _readonly_allowed

    for ok in ("pytest -q", "python -m pytest -q | tail -20", "git log --oneline", "rg TODO src"):
        assert _readonly_allowed(ok), ok
    for bad in (
        "echo x > app.py",
        "python -c \"open('a','w').write('x')\"",
        "sed -i s/a/b/ app.py",
        "git reset --hard",
        "pip install requests",
        "del app.py",
        "pytest -q; rm -rf src",
        "ruff check --fix .",
    ):
        assert not _readonly_allowed(bad), bad


def test_plan_schema_drops_dangling_dependencies():
    from guild.schemas import validate

    d, err = validate(
        "lead",
        "plan",
        {
            "tasks": [
                {"id": "T1", "title": "a", "depends_on": ["none"]},
                {"id": "T2 ", "title": "b", "depends_on": ["T1 ", "T0", "T2"]},
            ]
        },
    )
    assert err is None
    assert d["tasks"][0]["depends_on"] == []
    assert d["tasks"][1]["id"] == "T2" and d["tasks"][1]["depends_on"] == ["T1"]


def test_cost_tracker_prices():
    t = CostTracker()
    inp, out = DEFAULT_PRICES["anthropic/claude-sonnet-5"]
    usd = t.price("anthropic/claude-sonnet-5", Usage(1_000_000, 1_000_000))
    assert usd == pytest.approx(inp + out)
    assert t.price("ollama/anything", Usage(10**6, 10**6)) == 0.0


@pytest.mark.parametrize(
    "text,vague",
    [
        ("test_multiply passes", False),
        ("pytest exits 0", False),
        ("The interface is designed and documented.", True),
        ("works well", True),
        ("", True),
    ],
)
def test_vague_done_when(text, vague):
    from guild.workflow import vague_done_when

    assert vague_done_when(text) is vague


def test_extract_text_tool_calls_variants():
    from guild.agent import extract_text_tool_calls

    allowed = {"read_file", "edit_file"}
    txt = 'I will read it.\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```\nthen {"tool": "edit_file", "args": {"path": "a.py", "old_text": "x", "new_text": "y"}}'
    calls = extract_text_tool_calls(txt, allowed)
    assert [(c.name, c.arguments["path"]) for c in calls] == [
        ("read_file", "a.py"),
        ("edit_file", "a.py"),
    ]
    # a final-answer JSON must not be mistaken for a tool call
    assert extract_text_tool_calls('{"status": "done", "summary": "ok"}', allowed) == []
    # unknown tools are ignored
    assert extract_text_tool_calls('{"name": "rm_rf", "arguments": {}}', allowed) == []
