from __future__ import annotations

import pytest

from guild.agent import extract_json
from guild.config import list_available, load_profile, load_role
from guild.providers.base import Usage
from guild.providers.router import CostTracker, DEFAULT_PRICES
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


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Sure!\n```json\n{"a": 1}\n```', {"a": 1}),
    ('prefix {"a": {"b": 2}} suffix', {"a": {"b": 2}}),
    ('no json here', None),
    ('', None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_cost_tracker_prices():
    t = CostTracker()
    inp, out = DEFAULT_PRICES["anthropic/claude-sonnet-5"]
    usd = t.price("anthropic/claude-sonnet-5", Usage(1_000_000, 1_000_000))
    assert usd == pytest.approx(inp + out)
    assert t.price("ollama/anything", Usage(10**6, 10**6)) == 0.0


@pytest.mark.parametrize("text,vague", [
    ("test_multiply passes", False),
    ("pytest exits 0", False),
    ("The interface is designed and documented.", True),
    ("works well", True),
    ("", True),
])
def test_vague_done_when(text, vague):
    from guild.workflow import vague_done_when
    assert vague_done_when(text) is vague
