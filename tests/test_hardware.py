from pathlib import Path

from typer.testing import CliRunner

from guild import hardware as hw
from guild.cli import app


def _machine(vram=None, ram=None, models=(), keys=None):
    m = hw.Hardware(ram_gb=ram, vram_gb=vram, gpu="GPU" if vram else None,
                    ollama_reachable=bool(models), ollama_models=list(models), git=True)
    m.keys = keys or {}
    return m


def test_recommend_for_8gb_laptop():
    rec = hw.recommend_local(_machine(vram=8, ram=16, models=["qwen2.5-coder:7b"]))
    assert rec["coder"][0] == "ollama/qwen2.5-coder:7b"
    assert rec["reasoner"][0] in {"ollama/deepseek-r1:8b", "ollama/qwen3:8b"}
    assert all("30b" not in m and "14b" not in m for c in rec.values() for m in c)


def test_recommend_for_24gb_gpu_and_tiny_cpu_box():
    big = hw.recommend_local(_machine(vram=24, ram=64))
    assert big["coder"][0] == "ollama/qwen3-coder:30b"
    tiny = hw.recommend_local(_machine(ram=8))
    assert all(m in {"ollama/qwen2.5-coder:3b", "ollama/qwen2.5-coder:1.5b"} for c in tiny.values() for m in c)
    assert all(len(c) >= 1 for c in tiny.values())


def test_build_profile_free_uses_keys_and_no_paid_models():
    p = hw.build_profile(_machine(vram=8, keys={"groq": True, "gemini": True}), "free")
    assert p["slots"]["frontier"][0].startswith("groq/")
    assert p["limits"]["max_usd_per_run"] == 0
    assert not any(m.startswith(("anthropic/", "openai/")) for c in p["slots"].values() for m in c)
    lite = hw.build_profile(_machine(vram=8), "lite")
    assert lite["slots"]["frontier"][0].startswith("anthropic/") and "coder_escalation" in lite["slots"]


def test_missing_pulls():
    p = hw.build_profile(_machine(vram=8, models=["qwen2.5-coder:7b"]), "free")
    miss = hw.missing_pulls(p["slots"], _machine(vram=8, models=["qwen2.5-coder:7b"]))
    assert "qwen2.5-coder:7b" not in miss and miss  # something else still to pull


def test_detect_test_command(tmp_path: Path):
    assert hw.detect_test_command(tmp_path) == "python -m pytest -q"
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert hw.detect_test_command(tmp_path).startswith("npm test")
    (tmp_path / "Cargo.toml").write_text("")
    (tmp_path / "package.json").unlink()
    assert hw.detect_test_command(tmp_path) == "cargo test --quiet"


def test_init_wizard_noninteractive(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hw, "detect", lambda: _machine(vram=8, ram=16, models=["qwen2.5-coder:7b"]))
    r = CliRunner().invoke(app, ["init", str(tmp_path), "-y"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / ".guild" / "config.yaml").exists()
    prof = (tmp_path / ".guild" / "profiles" / "free.yaml").read_text()
    assert "qwen2.5-coder:7b" in prof and "ollama pull" in r.output
    assert ".guild/runs/" in (tmp_path / ".gitignore").read_text()


def test_init_wizard_interactive_answers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hw, "detect", lambda: _machine(vram=8, keys={"anthropic": True}))
    r = CliRunner().invoke(app, ["init", str(tmp_path)], input="lite\npython -m pytest -q\ny\n")
    assert r.exit_code == 0, r.output
    assert "profile: lite" in (tmp_path / ".guild" / "config.yaml").read_text()
    assert (tmp_path / ".guild" / "profiles" / "lite.yaml").exists()
    assert (tmp_path / ".git").is_dir()  # offered and accepted git init
