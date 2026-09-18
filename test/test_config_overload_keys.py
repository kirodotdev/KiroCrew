"""Every scalar ``agent.*`` / ``mcp_gateway.*`` key declared in ``sections.py``
must be READ by the loader with the bounds its ``_meta`` help text declares.

The overload-resilience work added ~45 keys to these two sections from eight
different areas, each parsed by hand in ``config/loader.py``. A key that is
declared but never read loads as its dataclass default no matter what the file
says -- a silent no-op that no other test notices, because the schema, the
baseline and the dashboard all read the declaration, not the loader. This
ratchet walks the declarations and drives each one through the real load path.

Three properties per key:

* absent from the file -> the dataclass default (the declaration IS the default);
* written as its own default -> loads equal (the key is read, not dropped);
* a bool flips, an enum accepts every listed value, and a number declaring
  ``Clamped to lo..hi`` loads ``lo``, ``hi`` and the midpoint exactly while a
  value past ``hi`` comes back at most ``hi``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config import sections
from kiro_crew.config.loader import KiroCrewConfig

_CLAMP_RE = re.compile(r"[Cc]lamped to (-?\d+(?:\.\d+)?)\s*(?:s)?\.\.\s*(-?\d+(?:\.\d+)?)")

# Fields whose load path deliberately transforms the written value (platform
# defaults, ceilings resolved from other keys, sentinel normalisation) and that
# predate the overload-resilience work. Listed by name so a NEW key cannot hide
# here without an edit to this file.
_TRANSFORMED: frozenset[str] = frozenset(
    {
        "sandbox_allow_unsandboxed_exec",  # default is a platform probe
        "dangerously_skip_permissions",  # read through _read_skip_permissions
        "subagent_timeout_secs",  # _subagent_timeout_from ceiling
        "yolo_duration",  # normalised duration string
        "max_subagents",  # 0 = auto; ceiling from SUBAGENT_AUTO_MAX_CEILING
        "acp_backend",  # normalised backend alias
        "member_acp_backend",
        "fallback_model",
        "jail",
        "log_level",  # upper-cased
        "bot_name",  # sanitised
        "completion_keep",
        "reasoning_effort",
        "stub_servers",  # resolved from the roster + overrides
        "stub_overrides",
        "socket_path",
        "overlay_dir",
    }
)


# A key whose floor is another key's value: written alongside so the declared
# lower bound is reachable in the probe below.
_FLOOR_COMPANIONS: dict[str, dict[str, object]] = {
    "spawn_concurrency_max": {"spawn_concurrency_min": 1},
    "spawn_concurrency_initial": {"spawn_concurrency_min": 1, "spawn_concurrency_max": 64},
}


def _loaded(tmp_path: Path, data: dict) -> KiroCrewConfig:
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load()


def _scalar_fields(cls: type) -> list[dataclasses.Field]:
    out = []
    for f in dataclasses.fields(cls):
        if f.name.startswith("_") or f.name in _TRANSFORMED:
            continue
        if f.default is dataclasses.MISSING:
            continue
        if isinstance(f.default, (bool, int, float, str)):
            out.append(f)
    return out


_SECTIONS: tuple[tuple[str, type], ...] = (
    ("agent", sections.AgentConfig),
    ("mcp_gateway", sections.McpGatewayConfig),
)

CASES = [(section, f) for section, cls in _SECTIONS for f in _scalar_fields(cls)]


def _ids(v: object) -> str:
    return v.name if isinstance(v, dataclasses.Field) else str(v)


def test_the_inventory_covers_the_overload_keys() -> None:
    """Guards the ratchet: the walk must see the keys this PR added."""
    names = {f.name for _, f in CASES}
    for required in (
        "task_queue_enabled",
        "session_start_concurrency",
        "adaptive_concurrency_mode",
        "recovery_backoff_max_secs",
        "dependency_wake_per_tick",
        "interactive_command_policy",
        "spawn_concurrency_initial",
        "host_budget_max_fds",
    ):
        assert required in names, required
    assert len(CASES) >= 75


@pytest.mark.parametrize("section,field", CASES, ids=_ids)
def test_absent_key_loads_the_declared_default(
    section: str, field: dataclasses.Field, tmp_path: Path
) -> None:
    cfg = _loaded(tmp_path, {section: {}})
    assert getattr(getattr(cfg, section), field.name) == field.default


@pytest.mark.parametrize("section,field", CASES, ids=_ids)
def test_explicit_default_round_trips(
    section: str, field: dataclasses.Field, tmp_path: Path
) -> None:
    cfg = _loaded(tmp_path, {section: {field.name: field.default}})
    assert getattr(getattr(cfg, section), field.name) == field.default


@pytest.mark.parametrize(
    "section,field",
    [(s, f) for s, f in CASES if isinstance(f.default, bool)],
    ids=_ids,
)
def test_a_bool_key_flips(section: str, field: dataclasses.Field, tmp_path: Path) -> None:
    flipped = not field.default
    cfg = _loaded(tmp_path, {section: {field.name: flipped}})
    assert getattr(getattr(cfg, section), field.name) is flipped


@pytest.mark.parametrize(
    "section,field",
    [(s, f) for s, f in CASES if isinstance(f.default, str) and f.metadata.get("enum")],
    ids=_ids,
)
def test_an_enum_key_accepts_every_listed_value(
    section: str, field: dataclasses.Field, tmp_path: Path
) -> None:
    for value in field.metadata["enum"]:
        cfg = _loaded(tmp_path, {section: {field.name: value}})
        assert getattr(getattr(cfg, section), field.name) == value, value


def _clamp(field: dataclasses.Field) -> tuple[float, float] | None:
    m = _CLAMP_RE.search(str(field.metadata.get("help", "")))
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


_CLAMPED = [
    (s, f)
    for s, f in CASES
    if isinstance(f.default, (int, float))
    and not isinstance(f.default, bool)
    and _clamp(f) is not None
]


def test_clamped_inventory_is_not_empty() -> None:
    assert len(_CLAMPED) >= 15


@pytest.mark.parametrize("section,field", _CLAMPED, ids=_ids)
def test_a_declared_clamp_is_the_loader_clamp(
    section: str, field: dataclasses.Field, tmp_path: Path
) -> None:
    lo, hi = _clamp(field)  # type: ignore[misc]
    kind = type(field.default)
    companions = _FLOOR_COMPANIONS.get(field.name, {})
    for value in (kind(lo), kind(hi), kind((lo + hi) / 2)):
        cfg = _loaded(tmp_path, {section: {**companions, field.name: value}})
        got = getattr(getattr(cfg, section), field.name)
        assert got == pytest.approx(value), f"{field.name}={value!r} loaded as {got!r}"
    absurd = kind(hi * 1000 + 12345)
    cfg = _loaded(tmp_path, {section: {field.name: absurd}})
    got = getattr(getattr(cfg, section), field.name)
    assert got <= hi, f"{field.name} loaded {got!r} above its declared ceiling {hi!r}"
    below = kind(lo - 1000)
    cfg = _loaded(tmp_path, {section: {**companions, field.name: below}})
    got = getattr(getattr(cfg, section), field.name)
    assert got >= lo, f"{field.name} loaded {got!r} below its declared floor {lo!r}"


def test_interactive_policy_unknown_value_falls_back_to_cancel(tmp_path: Path) -> None:
    cfg = _loaded(tmp_path, {"agent": {"interactive_command_policy": "auto-yes"}})
    assert cfg.agent.interactive_command_policy == "cancel"
