"""Detect what this machine can run and recommend local models that actually fit.

Nothing here is precise — VRAM/RAM numbers are read from nvidia-smi and the OS, model sizes
are approximate Q4 weights plus KV-cache headroom. The goal is a sensible default, not a
benchmark. Everything degrades gracefully to "unknown" when a probe fails.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Hardware:
    os: str = platform.system()
    cpu: str = ""
    ram_gb: float | None = None
    gpu: str | None = None
    vram_gb: float | None = None
    ollama_reachable: bool = False
    ollama_models: list[str] = field(default_factory=list)
    keys: dict[str, bool] = field(default_factory=dict)  # provider -> key present
    docker: bool = False
    git: bool = False

    @property
    def usable_gb(self) -> float | None:
        """Budget for a fully GPU-resident model (VRAM minus ~1 GB) or, on CPU/Apple, RAM/2."""
        if self.vram_gb:
            return max(self.vram_gb - 1.0, 0.5)
        if self.ram_gb:
            return self.ram_gb / 2
        return None


# name -> (approx GB at Q4 incl. headroom, quality tier 1-5, tags)
CATALOG: list[tuple[str, float, int, set[str]]] = [
    ("qwen2.5-coder:1.5b", 1.5, 1, {"coder"}),
    ("qwen2.5-coder:3b", 2.5, 2, {"coder"}),
    ("qwen2.5-coder:7b", 5.5, 3, {"coder", "general"}),
    ("qwen3:8b", 6.0, 3, {"general", "reasoner"}),
    ("deepseek-r1:8b", 6.0, 3, {"reasoner"}),
    ("qwen2.5-coder:14b", 10.0, 4, {"coder", "general"}),
    ("deepseek-r1:14b", 10.0, 4, {"reasoner"}),
    ("qwen3:14b", 10.0, 4, {"general", "reasoner"}),
    ("qwen3-coder:30b", 20.0, 5, {"coder", "general"}),
    ("deepseek-r1:32b", 21.0, 5, {"reasoner"}),
    ("qwen3:32b", 21.0, 5, {"general", "reasoner"}),
]


def detect() -> Hardware:
    hw = Hardware()
    hw.cpu = platform.processor() or platform.machine()
    hw.ram_gb = _ram_gb()
    hw.gpu, hw.vram_gb = _nvidia()
    if hw.gpu is None and platform.system() == "Darwin" and platform.machine() == "arm64":
        hw.gpu, hw.vram_gb = "Apple Silicon (unified memory)", None
    hw.ollama_reachable, hw.ollama_models = _ollama()
    hw.keys = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
    }
    hw.docker = shutil.which("docker") is not None
    hw.git = shutil.which("git") is not None
    return hw


def _ram_gb() -> float | None:
    try:
        if hasattr(os, "sysconf") and os.sysconf_names.get("SC_PHYS_PAGES"):
            return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 1)
    except (ValueError, OSError):
        pass
    if platform.system() == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return round(st.ullTotalPhys / 2**30, 1)
        except Exception:
            return None
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
            ).stdout
            return round(int(out.strip()) / 2**30, 1)
        except Exception:
            return None
    return None


def _nvidia() -> tuple[str | None, float | None]:
    if shutil.which("nvidia-smi") is None:
        return None, None
    try:
        out = (
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            .stdout.strip()
            .splitlines()
        )
    except Exception:
        return None, None
    if not out:
        return None, None
    name, _, mem = out[0].partition(",")
    try:
        return name.strip(), round(float(mem.strip()) / 1024, 1)
    except ValueError:
        return name.strip(), None


def _ollama() -> tuple[bool, list[str]]:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/tags", timeout=3)
        return True, [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return False, []


def recommend_local(hw: Hardware) -> dict[str, list[str]]:
    """Pick the best models per slot that fit this machine. Returns ollama/<name> ids."""
    budget = hw.usable_gb
    if budget is None:
        budget = 5.5  # unknown machine: assume a 7b fits
    fits = [c for c in CATALOG if c[1] <= budget]
    if not fits:
        fits = CATALOG[:1]

    def best(tag: str, fallback_tag: str = "general") -> list[str]:
        cands = sorted((c for c in fits if tag in c[3]), key=lambda c: -c[2])
        if not cands:
            cands = sorted((c for c in fits if fallback_tag in c[3]), key=lambda c: -c[2])
        if not cands:
            cands = sorted(fits, key=lambda c: -c[2])  # anything that fits beats nothing
        # prefer already-pulled models when quality ties
        pulled = set(hw.ollama_models)
        cands.sort(key=lambda c: (-c[2], c[0] not in pulled))
        return [f"ollama/{c[0]}" for c in cands[:2]]

    coder = best("coder")
    general = best("general", "coder")
    reasoner = best("reasoner", "general")
    return {
        "frontier": general,
        "coder": coder,
        "reasoner": reasoner,
        "cheap": list(reversed(coder)) or coder,
    }


def build_profile(hw: Hardware, tier: str) -> dict:
    """Compose a profile dict for this machine: local recommendations + cloud fallbacks by tier."""
    local = recommend_local(hw)
    free_cloud = []
    if hw.keys.get("groq"):
        free_cloud.append("groq/llama-3.3-70b-versatile")
    if hw.keys.get("gemini"):
        free_cloud.append("gemini/gemini-2.5-flash")

    if tier == "free":
        slots = {
            "frontier": free_cloud + local["frontier"],
            "coder": local["coder"] + free_cloud[:1],
            "reasoner": (["groq/deepseek-r1-distill-llama-70b"] if hw.keys.get("groq") else [])
            + local["reasoner"],
            "cheap": (["gemini/gemini-2.5-flash"] if hw.keys.get("gemini") else [])
            + local["cheap"],
        }
        limits = {"max_revision_rounds": 3, "max_tool_calls_per_task": 60, "max_usd_per_run": 0}
        desc = "Local-first on this machine" + (" with free cloud fallbacks" if free_cloud else "")
        budget = 0
    elif tier == "lite":
        slots = {
            "frontier": ["anthropic/claude-fable-5-1", "anthropic/claude-sonnet-5"]
            + free_cloud
            + local["frontier"],
            "coder": local["coder"] + ["deepseek/deepseek-chat"],
            "coder_escalation": ["anthropic/claude-sonnet-5", "deepseek/deepseek-chat"],
            "reasoner": ["deepseek/deepseek-reasoner"] + free_cloud + local["reasoner"],
            "cheap": (["gemini/gemini-2.5-flash"] if hw.keys.get("gemini") else [])
            + ["deepseek/deepseek-chat"]
            + local["cheap"],
        }
        limits = {
            "max_revision_rounds": 3,
            "escalate_after": 2,
            "max_tool_calls_per_task": 80,
            "max_usd_per_run": 2.0,
        }
        desc = "Claude plans and reviews; local models code; cheap APIs critique"
        budget = 15
    else:
        slots = {
            "frontier": ["anthropic/claude-fable-5-1", "anthropic/claude-opus-5"],
            "coder": ["anthropic/claude-opus-5", "anthropic/claude-sonnet-5", "openai/gpt-5"],
            "coder_escalation": ["anthropic/claude-fable-5-1"],
            "reasoner": ["openai/gpt-5", "gemini/gemini-2.5-pro", "anthropic/claude-sonnet-5"],
            "cheap": ["gemini/gemini-2.5-flash", "openai/gpt-5-mini", "anthropic/claude-haiku-4-5"],
        }
        limits = {
            "max_revision_rounds": 4,
            "escalate_after": 1,
            "max_tool_calls_per_task": 150,
            "max_usd_per_run": 15.0,
        }
        desc = "Best model in every seat"
        budget = None
    # de-duplicate while keeping order
    for k, v in slots.items():
        seen: list[str] = []
        for m in v:
            if m not in seen:
                seen.append(m)
        slots[k] = seen
    return {
        "name": tier,
        "description": desc,
        "monthly_budget_gbp": budget,
        "slots": slots,
        "limits": limits,
    }


def detect_test_command(root: Path) -> str:
    """Guess how to run this project's tests from the files present."""
    if (
        (root / "pyproject.toml").exists()
        or (root / "setup.py").exists()
        or list(root.glob("test_*.py"))
        or (root / "tests").is_dir()
        or list(root.glob("*.py"))
    ):
        return "python -m pytest -q"
    if (root / "package.json").exists():
        try:
            import json

            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get(
                "scripts", {}
            )
            if "test" in scripts:
                return "npm test --silent"
        except (OSError, ValueError):
            pass
        return "npm test --silent"
    if (root / "Cargo.toml").exists():
        return "cargo test --quiet"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "pom.xml").exists():
        return "mvn -q test"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "gradle test -q"
    if list(root.glob("*.csproj")) or list(root.glob("*.sln")):
        return "dotnet test"
    return "python -m pytest -q"


def missing_pulls(profile_slots: dict[str, list[str]], hw: Hardware) -> list[str]:
    """Ollama models the profile wants that aren't pulled yet."""
    have = set(hw.ollama_models) | {m.split(":")[0] for m in hw.ollama_models}
    want: list[str] = []
    for chain in profile_slots.values():
        for m in chain:
            if m.startswith("ollama/"):
                name = m.split("/", 1)[1]
                if name not in have and name.split(":")[0] not in have and name not in want:
                    want.append(name)
    return want


def summary_lines(hw: Hardware) -> list[str]:
    lines = [f"OS: {hw.os}  CPU: {hw.cpu or '?'}  RAM: {hw.ram_gb or '?'} GB"]
    if hw.gpu:
        lines.append(f"GPU: {hw.gpu}" + (f"  VRAM: {hw.vram_gb} GB" if hw.vram_gb else ""))
    else:
        lines.append("GPU: none detected (nvidia-smi not found) — local models will run on CPU")
    if hw.ollama_reachable:
        lines.append(
            f"Ollama: running, {len(hw.ollama_models)} model(s) pulled"
            + (": " + ", ".join(hw.ollama_models[:6]) if hw.ollama_models else "")
        )
    else:
        lines.append(
            "Ollama: not reachable — install from https://ollama.com for free local models"
        )
    keys = [k for k, v in hw.keys.items() if v]
    lines.append("API keys: " + (", ".join(keys) if keys else "none set"))
    git = "yes" if hw.git else "NO — install git; guild needs it for branches"
    lines.append(f"git: {git}  docker: {'yes' if hw.docker else 'no'}")
    return lines
