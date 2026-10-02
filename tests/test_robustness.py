"""Output-contract validation, repair retries, and streaming assembly."""

from __future__ import annotations

from guild.providers.base import Completion, Message, Usage
from guild.schemas import validate
from guild.trace import read_trace
from guild.workflow import Guild


def test_validate_normalises_near_misses():
    d, err = validate(
        "critic", "default", {"verdict": "Changes Requested", "findings": "no test", "summary": "x"}
    )
    assert (
        err is None and d["verdict"] == "request_changes" and d["findings"][0]["issue"] == "no test"
    )
    d, err = validate("engineer", "default", {"status": "COMPLETED", "files_changed": "app.py"})
    assert err is None and d["status"] == "done" and d["files_changed"] == ["app.py"]
    d, err = validate("lead", "plan", {"tasks": [{"title": "t"}]})
    assert err is None and d["tasks"][0]["id"] == "T1"
    d, err = validate("lead", "review", {"decision": "accept"})
    assert err is None and d["decision"] == "ACCEPT"


def test_validate_coerces_null_and_non_string_fields():
    # qwen-7b style replies: null summary, numeric tests_run, object notes
    d, err = validate(
        "engineer",
        "default",
        {"status": "done", "summary": None, "tests_run": 2, "notes_for_reviewer": {"ok": True}},
    )
    assert err is None
    assert d["summary"] == "" and d["tests_run"] == "2" and '"ok": true' in d["notes_for_reviewer"]
    d, err = validate(
        "critic", "default", {"verdict": "approve", "findings": None, "summary": None}
    )
    assert err is None and d["findings"] == [] and d["summary"] == ""


def test_validate_reports_errors():
    d, err = validate("lead", "plan", {"roadmap": ["m"], "tasks": []})
    assert d is None and "tasks" in err
    d, err = validate("engineer", "default", {"status": "maybe"})
    assert d is None and "status" in err
    # unknown role: anything goes
    assert validate("mystery", "default", {"whatever": 1}) == ({"whatever": 1}, None)


class ScriptedText:
    """Replies with fixed texts in order."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.n = 0

    def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
        self.n += 1
        t = self.texts.pop(0)
        return Completion(Message("assistant", t), Usage(5, 5), f"fake/{model_name}", "stop")


def test_invalid_reply_is_repaired(project, cfg, profile):
    fake = ScriptedText(
        [
            "Here is my review, hope it helps!",  # no JSON
            '{"verdict": "meh", "findings": []}',  # invalid verdict
            '{"verdict": "approve", "findings": [], "summary": "fine now"}',  # valid
        ]
    )
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    res = g.agent("critic").run("review")
    g.close()
    assert res.data == {"verdict": "approve", "findings": [], "summary": "fine now"}
    assert fake.n == 3
    notes = [e for e in read_trace(g.trace.path) if e["kind"] == "note"]
    assert len(notes) == 2 and "repair" in notes[0]["msg"]


def test_gives_up_after_max_repairs(project, cfg, profile):
    fake = ScriptedText(["nope"] * 5)
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    res = g.agent("critic").run("review")
    g.close()
    assert res.data is None and fake.n == 3  # 1 + MAX_REPAIRS


def test_on_token_streams_to_callback(project, cfg, profile):
    class Streamer:
        def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
            text = '{"verdict": "approve", "findings": [], "summary": "ok"}'
            if on_token:
                for i in range(0, len(text), 7):
                    on_token(text[i : i + 7])
            return Completion(Message("assistant", text), Usage(1, 1), f"fake/{model_name}", "stop")

    got = []
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", Streamer())
    role = g.role("critic")
    from guild.agent import Agent

    res = Agent(
        role, g.router, g._ctx(role), g.trace, on_token=lambda r, c: got.append((r, c))
    ).run("review")
    g.close()
    assert res.data["verdict"] == "approve"
    assert "".join(c for _, c in got) == '{"verdict": "approve", "findings": [], "summary": "ok"}'
    assert all(r == "critic" for r, _ in got)


def test_openai_stream_assembly():
    """Assemble text + split tool-call deltas from a fake streamed response."""
    from types import SimpleNamespace as NS

    from guild.providers.openai_compat import OpenAICompatProvider

    def fn(name=None, arguments=None):
        return NS(name=name, arguments=arguments)

    chunks = [
        NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content="Hel", tool_calls=None))]),
        NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content="lo", tool_calls=None))]),
        NS(
            usage=None,
            choices=[
                NS(
                    finish_reason=None,
                    delta=NS(
                        content=None, tool_calls=[NS(index=0, id="c1", function=fn("read_", None))]
                    ),
                )
            ],
        ),
        NS(
            usage=None,
            choices=[
                NS(
                    finish_reason=None,
                    delta=NS(
                        content=None, tool_calls=[NS(index=0, id=None, function=fn("file", '{"pa'))]
                    ),
                )
            ],
        ),
        NS(
            usage=None,
            choices=[
                NS(
                    finish_reason="tool_calls",
                    delta=NS(
                        content=None,
                        tool_calls=[NS(index=0, id=None, function=fn(None, 'th": "a.py"}'))],
                    ),
                )
            ],
        ),
        NS(usage=NS(prompt_tokens=10, completion_tokens=4), choices=[]),
    ]

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(stream=False, **kw):
                    return iter(chunks)

    tokens = []
    comp = OpenAICompatProvider._stream(FakeClient(), {}, "ollama/x", tokens.append)
    assert comp.message.content == "Hello" and tokens == ["Hel", "lo"]
    assert comp.message.tool_calls[0].name == "read_file"
    assert comp.message.tool_calls[0].arguments == {"path": "a.py"}
    assert comp.usage.input_tokens == 10 and comp.stop_reason == "tool_calls"
