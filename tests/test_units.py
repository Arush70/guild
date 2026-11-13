from __future__ import annotations

import pytest

from codeguild.agent import extract_json
from codeguild.config import list_available, load_profile, load_role
from codeguild.providers.base import Usage
from codeguild.providers.router import CostTracker
from codeguild.tools.registry import REGISTRY


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


def test_plan_schema_drops_dangling_dependencies():
    from codeguild.schemas import validate

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


def test_cost_is_tokens_times_price_per_million():
    t = CostTracker()
    t.prices["shop/model"] = (3.0, 15.0)
    assert t.price("shop/model", Usage(1_000_000, 2_000_000)) == pytest.approx(33.0)
    assert t.price("shop/model", Usage(500, 0)) == pytest.approx(0.0015)
    assert t.price("ollama/anything", Usage(10**6, 10**6)) == 0.0  # unlisted models are free


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
    from codeguild.plan import vague_done_when

    assert vague_done_when(text) is vague


def test_extract_text_tool_calls_variants():
    from codeguild.agent import extract_text_tool_calls

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
