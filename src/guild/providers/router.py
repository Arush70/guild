"""Model router: resolves a slot to a concrete model by walking the profile's fallback chain,
tracks tokens and estimated cost, and enforces the per-run USD cap.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import Profile
from .anthropic_provider import AnthropicProvider
from .base import Completion, Message, NotConfigured, ProviderError, ToolSpec, Usage
from .openai_compat import ENDPOINTS, OpenAICompatProvider


class Provider(Protocol):
    def complete(self, model_name: str, messages: list[Message], tools: list[ToolSpec] | None,
                 max_tokens: int, temperature: float) -> Completion: ...


# USD per 1M tokens (input, output). Approximate list prices; used for *estimates* only.
# Anything not listed (local models, unknown) is costed at 0. Override in .guild/prices.yaml.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-fable-5-1": (15.0, 75.0),
    "anthropic/claude-opus-5": (15.0, 75.0),
    "anthropic/claude-sonnet-5": (3.0, 15.0),
    "anthropic/claude-haiku-4-5": (1.0, 5.0),
    "openai/gpt-5": (1.25, 10.0),
    "openai/gpt-5-mini": (0.25, 2.0),
    "gemini/gemini-2.5-pro": (1.25, 10.0),
    "gemini/gemini-2.5-flash": (0.30, 2.50),
    "deepseek/deepseek-chat": (0.27, 1.10),
    "deepseek/deepseek-reasoner": (0.55, 2.19),
    "groq/llama-3.3-70b-versatile": (0.59, 0.79),
    "groq/llama-3.1-8b-instant": (0.05, 0.08),
}


class BudgetExceeded(RuntimeError):
    pass


class AllProvidersFailed(RuntimeError):
    def __init__(self, slot: str, errors: list[ProviderError]):
        self.slot = slot
        self.errors = errors
        lines = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"every candidate for slot '{slot}' failed:\n{lines}")


@dataclass
class CallRecord:
    ts: float
    slot: str
    model: str
    usage: Usage
    usd: float
    latency_s: float
    role: str = ""


@dataclass
class CostTracker:
    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    calls: list[CallRecord] = field(default_factory=list)

    def price(self, model: str, usage: Usage) -> float:
        inp, out = self.prices.get(model, (0.0, 0.0))
        return (usage.input_tokens * inp + usage.output_tokens * out) / 1_000_000

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def total_usd(self) -> float:
        return sum(c.usd for c in self.calls)

    @property
    def total_usage(self) -> Usage:
        u = Usage()
        for c in self.calls:
            u = u + c.usage
        return u

    def by_model(self) -> dict[str, tuple[Usage, float, int]]:
        agg: dict[str, list] = defaultdict(lambda: [Usage(), 0.0, 0])
        for c in self.calls:
            a = agg[c.model]
            a[0] = a[0] + c.usage
            a[1] += c.usd
            a[2] += 1
        return {k: (v[0], v[1], v[2]) for k, v in agg.items()}

    def by_role(self) -> dict[str, tuple[Usage, float, int]]:
        agg: dict[str, list] = defaultdict(lambda: [Usage(), 0.0, 0])
        for c in self.calls:
            a = agg[c.role or "?"]
            a[0] = a[0] + c.usage
            a[1] += c.usd
            a[2] += 1
        return {k: (v[0], v[1], v[2]) for k, v in agg.items()}


class Router:
    """Resolve slots → models with fallback. One instance per run."""

    def __init__(self, profile: Profile, tracker: CostTracker | None = None,
                 max_usd: float | None = None, on_fallback: Callable[[str, str, str], None] | None = None):
        self.profile = profile
        self.tracker = tracker or CostTracker()
        self.max_usd = profile.limits.max_usd_per_run if max_usd is None else max_usd
        self.on_fallback = on_fallback
        self._providers: dict[str, Provider] = {}
        self._dead: set[str] = set()       # models that failed non-retryably this run
        self._sticky: dict[str, str] = {}  # slot -> last model that worked
        self._overrides: dict[str, Provider] = {}

    # -- test hook ---------------------------------------------------------
    def register_provider(self, prefix: str, provider: Provider) -> None:
        self._overrides[prefix] = provider

    def _provider_for(self, prefix: str) -> Provider:
        if prefix in self._overrides:
            return self._overrides[prefix]
        if prefix not in self._providers:
            if prefix == "anthropic":
                self._providers[prefix] = AnthropicProvider()
            elif prefix in ENDPOINTS:
                self._providers[prefix] = OpenAICompatProvider(prefix)
            else:
                raise ValueError(f"unknown provider prefix '{prefix}'")
        return self._providers[prefix]

    def candidates(self, slot: str) -> list[str]:
        chain = list(self.profile.chain(slot))
        if slot in self._sticky and self._sticky[slot] in chain:
            chain.remove(self._sticky[slot])
            chain.insert(0, self._sticky[slot])
        return [m for m in chain if m not in self._dead]

    def complete(self, slot: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                 *, max_tokens: int = 4096, temperature: float = 0.2, role: str = "") -> Completion:
        if self.max_usd is not None and self.max_usd > 0 and self.tracker.total_usd >= self.max_usd:
            raise BudgetExceeded(f"run cost ${self.tracker.total_usd:.3f} reached cap ${self.max_usd:.2f}")

        errors: list[ProviderError] = []
        for full in self.candidates(slot):
            prefix, _, model_name = full.partition("/")
            if not model_name:
                errors.append(ProviderError(full, "model id must be 'provider/model'", retryable=False))
                self._dead.add(full)
                continue
            try:
                provider = self._provider_for(prefix)
            except ValueError as e:
                errors.append(ProviderError(full, str(e), retryable=False))
                self._dead.add(full)
                continue

            t0 = time.time()
            try:
                comp = provider.complete(model_name, messages, tools, max_tokens, temperature)
            except NotConfigured as e:
                errors.append(e)
                self._dead.add(full)
                continue
            except ProviderError as e:
                errors.append(e)
                if not e.retryable:
                    self._dead.add(full)
                if self.on_fallback:
                    self.on_fallback(slot, full, e.reason)
                continue

            usd = self.tracker.price(comp.model, comp.usage)
            if self.max_usd is not None and self.max_usd == 0 and usd > 0:
                # free profile: refuse to spend even if a key happens to be set
                self._dead.add(full)
                errors.append(ProviderError(full, "would cost money but profile cap is $0", retryable=False))
                continue
            self.tracker.record(CallRecord(time.time(), slot, comp.model, comp.usage, usd,
                                           time.time() - t0, role))
            self._sticky[slot] = full
            return comp

        raise AllProvidersFailed(slot, errors)
