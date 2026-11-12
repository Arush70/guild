"""Is each model of a profile usable right now? Shown by `guild doctor` and the dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import Profile
from .anthropic_provider import KEY_ENV as ANTHROPIC_KEY_ENV
from .openai_compat import ENDPOINTS, OLLAMA_HOST


def ollama_models() -> list[str] | None:
    """Names of the models Ollama has pulled; None when Ollama is not reachable."""
    import httpx

    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None


def is_pulled(name: str, pulled: list[str]) -> bool:
    """`qwen2.5-coder` matches a pulled `qwen2.5-coder:7b`: Ollama resolves a bare name to a tag."""
    return name in pulled or name.split(":")[0] in {x.split(":")[0] for x in pulled}


def key_env(provider: str) -> str | None:
    """The environment variable holding a provider's API key, if it needs one."""
    if provider == "anthropic":
        return ANTHROPIC_KEY_ENV
    return ENDPOINTS[provider].key_env if provider in ENDPOINTS else None


@dataclass
class ModelStatus:
    slot: str
    model: str
    status: str
    level: str  # ready | missing (needs a key or a pull) | broken (cannot work as configured)

    @property
    def ok(self) -> bool:
        return self.level == "ready"


def _status(model: str, pulled: list[str] | None) -> tuple[str, str]:
    prefix, _, name = model.partition("/")
    if prefix == "ollama":
        if pulled is None:
            return "ollama not reachable", "broken"
        if is_pulled(name, pulled):
            return "ready", "ready"
        return f"not pulled — ollama pull {name}", "missing"
    if prefix == "anthropic":
        if os.environ.get(ANTHROPIC_KEY_ENV):
            return "key set", "ready"
        return f"{ANTHROPIC_KEY_ENV} missing", "missing"
    if prefix in ENDPOINTS:
        ep = ENDPOINTS[prefix]
        if ep.key_optional or os.environ.get(ep.key_env):
            return "configured", "ready"
        return f"{ep.key_env} missing", "missing"
    return "unknown provider", "broken"


def profile_status(profile: Profile, pulled: list[str] | None) -> list[ModelStatus]:
    return [
        ModelStatus(slot, model, *_status(model, pulled))
        for slot, chain in profile.slots.items()
        for model in chain
    ]
