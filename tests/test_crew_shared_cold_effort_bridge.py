from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kiro_byo import crew_shared_patch


@dataclass
class _Model:
    name: str
    selectable_effort_levels: tuple[str, ...]


class _Config:
    mode_pack = "tiga"
    mode_type = "multi"
    default_model = ""

    def __init__(self, rows: dict[str, _Model]):
        self._rows = rows
        self.models = tuple(rows.values())

    def resolve_model(self, value: str):
        model = self._rows.get(value)
        return (object(), model) if model is not None else None


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


def _load_helper(cfg: _Config, *, route_accepts=None):
    accepted_by_register: list[list[str]] = []

    def load_catalog_config(_path: Path):
        return cfg

    @contextlib.contextmanager
    def use_app_home(_path: Path):
        yield

    def parse_type_selection(value: str):
        prefix = "kiro-byo-type:"
        if not str(value).startswith(prefix):
            return None
        mode, policy = str(value)[len(prefix):].split(":", 1)
        return SimpleNamespace(mode_id=mode, type_id=policy)

    def type_selection_id(mode: str, policy: str) -> str:
        return f"kiro-byo-type:{mode}:{policy}"

    def route_model(_cfg, *, reasoning_effort: str, **_kwargs):
        allowed = True if route_accepts is None else route_accepts(reasoning_effort)
        return object() if allowed else None

    def cap(levels, *, source: str):
        assert source == "Kiro BYO cold catalog"
        return list(dict.fromkeys(str(v) for v in levels))[:32]

    def register(levels):
        values = list(levels)
        accepted_by_register.append(values)
        return values

    fake_modules = {
        "kiro_byo.config": _module("kiro_byo.config", load_catalog_config=load_catalog_config),
        "kiro_byo.mode_router": _module("kiro_byo.mode_router", route_model=route_model),
        "kiro_byo.paths": _module("kiro_byo.paths", use_app_home=use_app_home),
        "kiro_byo.protocol": _module(
            "kiro_byo.protocol",
            parse_type_selection=parse_type_selection,
            type_selection_id=type_selection_id,
        ),
        "kiro_crew": _module("kiro_crew"),
        "kiro_crew.apps": _module("kiro_crew.apps"),
        "kiro_crew.apps.manager": _module(
            "kiro_crew.apps.manager", app_data_dir=lambda _app: Path("/tmp/kiro-byo")
        ),
    }
    ns = {
        "asyncio": asyncio,
        "Any": object,
        "_ChatSlot": object,
        "_normalize_model": lambda value: str(value or "").strip().lower(),
        "cap_effort_capability_levels": cap,
        "register_reasoning_effort_values": register,
        "ACP_BACKEND_KIRO_BYO": "kiro-byo",
        "logger": logging.getLogger("test-cold-effort"),
    }
    with patch.dict(sys.modules, fake_modules):
        exec("from __future__ import annotations\n" + crew_shared_patch._KIRO_BYO_COLD_EFFORT_HELPER, ns)
        fn = ns["_kiro_byo_cold_selection_capabilities"]
    return fn, fake_modules, accepted_by_register


def _run_with_modules(fn, modules, slot):
    with patch.dict(sys.modules, modules):
        return asyncio.run(fn(slot))


def test_cold_concrete_model_publishes_its_exact_effort_vocabulary() -> None:
    cfg = _Config(
        {
            "Model-A": _Model("Model-A", ("minimal", "balanced", "extreme")),
            "Model-B": _Model("Model-B", ()),
        }
    )
    fn, modules, registered = _load_helper(cfg)
    result = _run_with_modules(fn, modules, SimpleNamespace(model="Model-A"))
    assert result == {
        "known": True,
        "backend": "kiro-byo",
        "effort_supported": True,
        "effort_levels": ["minimal", "balanced", "extreme"],
        "model_effort_pair_ids": False,
    }
    assert registered == [["minimal", "balanced", "extreme"]]


def test_cold_model_without_adjustable_effort_is_known_unsupported() -> None:
    cfg = _Config({"Binary-Thinking": _Model("Binary-Thinking", ())})
    fn, modules, _registered = _load_helper(cfg)
    result = _run_with_modules(fn, modules, SimpleNamespace(model="Binary-Thinking"))
    assert result["known"] is True
    assert result["effort_supported"] is False
    assert result["effort_levels"] == []


def test_cold_type_is_known_but_exposes_no_session_effort_control() -> None:
    cfg = _Config(
        {
            "A": _Model("A", ("eco", "normal", "deep")),
            "B": _Model("B", ("normal", "ultra")),
        }
    )
    fn, modules, registered = _load_helper(cfg)
    result = _run_with_modules(fn, modules, SimpleNamespace(model="kiro-byo-type:tiga:power"))
    assert result["known"] is True
    assert result["effort_supported"] is False
    assert result["effort_levels"] == []
    assert registered == [[]]


def test_cold_auto_uses_byo_default_type_and_hides_effort_control() -> None:
    cfg = _Config({"A": _Model("A", ("soft", "hard"))})
    fn, modules, registered = _load_helper(cfg)
    result = _run_with_modules(fn, modules, SimpleNamespace(model=""))
    assert result["known"] is True
    assert result["effort_supported"] is False
    assert result["effort_levels"] == []
    assert registered == [[]]
