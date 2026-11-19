"""The team: which roles take part in a project's tasks, and which models play each role.

Two settings, kept apart on purpose:

- **Who is on the team** is the project's `roles_enabled` (`.codeguild/config.yaml`). It holds
  whichever profile is chosen.
- **Which model plays a role** is the profile's: its slots by default, or the role's own
  models (`Profile.roles`). Any profile can have them, a built-in one copied into the project
  or one of the owner's own.

The command line (`codeguild team`, `codeguild profile`) and the dashboard both go through
this module, so they agree on what is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import (
    ConfigError,
    Profile,
    ProjectConfig,
    Role,
    list_available,
    load_profile,
    load_role,
    save_profile,
    where_defined,
)

# What a role does when it is on the team. Reviewers are found by their `review:` line.
_PARTS = {
    "lead": "plans the work; reviews each finished task",
    "engineer": "writes the code for each task",
    "verifier": "runs your tests and lint (a program, not a model)",
    "docs": "updates README and docs after each accepted task",
}
REQUIRED = frozenset({"engineer"})  # without it no task can be done
NOT_A_MODEL = frozenset({"verifier"})


@dataclass
class TeamRole:
    name: str
    title: str
    part: str  # what it does, in a few words
    on_team: bool | None  # None: not a team role (used on demand: ask, chat, pair)
    required: bool
    models: list[str]  # what answers for it now, in order; [] for the verifier
    own_models: bool  # True: the profile names models for this role itself
    slot: str  # the profile slot it uses otherwise

    @property
    def uses_models(self) -> bool:
        return self.name not in NOT_A_MODEL


def _part(role: Role) -> tuple[str, bool]:
    """(what it does, whether it can be on the team)"""
    if role.name in _PARTS:
        return _PARTS[role.name], True
    if role.review:
        # "Review the change for security and privacy." → "reviews each change for security…"
        focus = role.review.rstrip(".").removeprefix("Review the change").strip()
        return ("reviews each change " + focus).strip(), True
    return f"{role.title}: used on demand (chat, pair mode, codeguild ask {role.name})", False


def roles(root: Path | None, cfg: ProjectConfig, profile: Profile) -> list[TeamRole]:
    """Every role this project can use, team roles first, in the order of roles_enabled."""
    out = []
    for name in list_available("roles", root):
        try:
            role = load_role(name, root)
        except (ConfigError, FileNotFoundError):
            continue  # a broken role file is reported where it is used
        part, team = _part(role)
        out.append(
            TeamRole(
                name=name,
                title=role.title,
                part=part,
                on_team=(name in cfg.roles_enabled) if team else None,
                required=name in REQUIRED,
                models=[] if name in NOT_A_MODEL else profile.models_for(role),
                own_models=name in profile.roles,
                slot=role.slot,
            )
        )
    order = {n: i for i, n in enumerate(cfg.roles_enabled)}
    return sorted(out, key=lambda r: (r.on_team is None, order.get(r.name, 99), r.name))


def set_team(cfg: ProjectConfig, root: Path | None, on: list[str]) -> ProjectConfig:
    """`cfg` with exactly these roles on the team. Refuses unknown roles, roles that cannot
    be on a team (researcher, …) and a team without an engineer."""
    known = {r.name: r for r in roles(root, cfg, _any_profile())}
    wanted = list(dict.fromkeys(n.strip() for n in on if n.strip()))
    for name in wanted:
        if name not in known:
            raise ConfigError(f"no role named {name!r} (roles: {', '.join(sorted(known))})")
        if known[name].on_team is None:
            raise ConfigError(
                f"{name} is not part of the task team; use it with `codeguild ask {name}`"
            )
    missing = REQUIRED - set(wanted)
    if missing:
        raise ConfigError(f"the team needs {', '.join(sorted(missing))}: it writes the code")
    # keep the familiar order: lead, engineer, verifier, reviewers…, docs
    first = [n for n in ("lead", "engineer", "verifier") if n in wanted]
    last = [n for n in ("docs",) if n in wanted]
    middle = [n for n in wanted if n not in first and n not in last]
    return cfg.model_copy(update={"roles_enabled": first + middle + last})


def set_role_models(profile: Profile, role: str, models: list[str] | None, root: Path | None):
    """A copy of `profile` where `role` uses `models` (tried in order), or the profile's slot
    again when `models` is empty or None."""
    if role not in list_available("roles", root):
        raise ConfigError(f"no role named {role!r}")
    if role in NOT_A_MODEL:
        raise ConfigError(f"{role} runs your tests; it does not use a model")
    chains = dict(profile.roles)
    cleaned = [m.strip() for m in (models or []) if m.strip()]
    for model in cleaned:
        problem = model_problem(model)
        if problem:
            raise ConfigError(f"{model}: {problem}")
    if cleaned:
        chains[role] = cleaned
    else:
        chains.pop(role, None)
    try:
        return Profile.model_validate({**profile.model_dump(), "roles": chains})
    except ValueError as e:
        raise ConfigError(str(e).splitlines()[-1].strip()) from None


def model_problem(model: str) -> str | None:
    """Why a model id cannot work, or None. Only the provider is checked here: whether a key
    is set or a model is pulled is for `codeguild doctor`, it can change any time."""
    from .providers.openai_compat import ENDPOINTS

    provider, _, name = model.partition("/")
    if not name:
        return "write it as provider/model, e.g. ollama/qwen2.5-coder:7b"
    if provider != "anthropic" and provider not in ENDPOINTS:
        known = ", ".join(sorted({"anthropic", *ENDPOINTS}))
        return f"unknown provider {provider!r} (known: {known})"
    return None


def model_suggestions(root: Path | None, pulled: list[str] | None) -> list[str]:
    """Model ids worth offering in a picker: the ones Ollama has here, every model with a known
    price, and every model any available profile names."""
    from .providers.router import DEFAULT_PRICES

    seen: dict[str, None] = {}
    for name in pulled or []:
        seen[f"ollama/{name}"] = None
    for name in list_available("profiles", root):
        try:
            for chain in load_profile(name, root).all_chains().values():
                seen.update(dict.fromkeys(chain))
        except (ConfigError, FileNotFoundError):
            continue
    seen.update(dict.fromkeys(DEFAULT_PRICES))
    return list(seen)


def save_edited(profile: Profile, root: Path) -> tuple[Path, str]:
    """Save a changed profile where it came from: this project's copy, or the owner's (all
    projects). A built-in profile is copied into the project; codeguild's own files are never
    changed. Returns (file, scope)."""
    scope = where_defined("profiles", profile.name, root)
    scope = "project" if scope == "built-in" else scope
    return save_profile(profile, scope, root), scope


def save_new(profile: Profile, name: str, scope: str, root: Path) -> Path:
    """Save `profile` under a new name, for this project or for all the owner's projects."""
    if name in list_available("profiles", root):
        raise ConfigError(f"a profile named {name!r} exists already; pick another name")
    return save_profile(profile.model_copy(update={"name": name}), scope, root)


def _any_profile() -> Profile:
    return Profile(name="_", slots={})
