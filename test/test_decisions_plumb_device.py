"""The Plumb-4B launcher loads on the Apple Silicon GPU (MPS) when torch can use it.

The launcher runs in the model's own environment and imports torch and jevk5 at
module level, so these tests read its source and load only the definitions they
need, with a stand-in torch and model class.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PLUMB = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "decisions"
    / "local_servers"
    / "plumb_cpu.py"
)


def _tree() -> ast.Module:
    return ast.parse(_PLUMB.read_text(encoding="utf-8"))


def _load(names: set[str], namespace: dict) -> dict:
    defs: list[ast.stmt] = [
        node for node in _tree().body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {d.name for d in defs if isinstance(d, ast.FunctionDef)} == names
    code = compile(ast.Module(body=defs, type_ignores=[]), str(_PLUMB), "exec", dont_inherit=True)
    # Executes definitions parsed from a tracked repository file, no external input.
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(code, namespace)  # noqa: S102
    return namespace


def _torch(mps: Any) -> Any:
    def is_available() -> bool:
        if isinstance(mps, BaseException):
            raise mps
        return bool(mps)

    return SimpleNamespace(
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=is_available)),
        bfloat16="bf16",
    )


def _device(mps: Any) -> str:
    return _load({"_model_device"}, {"torch": _torch(mps)})["_model_device"]()


def test_mps_is_chosen_when_torch_can_use_it() -> None:
    assert _device(True) == "mps"


def test_cpu_without_mps() -> None:
    assert _device(False) == "cpu"


def test_cpu_when_the_mps_probe_raises() -> None:
    assert _device(RuntimeError("no Metal device")) == "cpu"


def test_cpu_when_this_torch_has_no_mps_backend() -> None:
    namespace = {"torch": SimpleNamespace(backends=SimpleNamespace())}
    assert _load({"_model_device"}, namespace)["_model_device"]() == "cpu"


class _Model:
    def __init__(self, fail_on: set[str], calls: list[dict]) -> None:
        self.fail_on = fail_on
        self.calls = calls

    def __call__(self, weights: str, **kwargs: Any) -> Any:
        self.calls.append({"weights": weights, **kwargs})
        if kwargs["device"] in self.fail_on:
            raise RuntimeError(f"cannot load on {kwargs['device']}")
        return SimpleNamespace(device=kwargs["device"])


def _load_model(mps: Any, fail_on: set[str]) -> tuple[Any, list[dict], str]:
    calls: list[dict] = []
    log = io.StringIO()
    namespace = {
        "torch": _torch(mps),
        "JevK5": _Model(fail_on, calls),
        "sys": SimpleNamespace(stderr=log),
    }
    loaded = _load({"_model_device", "_load_model"}, namespace)
    return loaded["_load_model"]("/weights"), calls, log.getvalue()


def test_model_loads_on_mps_in_bfloat16_without_graphs() -> None:
    model, calls, log = _load_model(True, set())

    assert model.device == "mps"
    assert calls == [{"weights": "/weights", "device": "mps", "dtype": "bf16", "graphs": False}]
    assert "device=mps" in log


def test_model_loads_on_cpu_without_mps() -> None:
    model, calls, log = _load_model(False, set())

    assert model.device == "cpu"
    assert [c["device"] for c in calls] == ["cpu"]
    assert "device=cpu" in log


def test_a_failed_mps_load_falls_back_to_cpu() -> None:
    model, calls, log = _load_model(True, {"mps"})

    assert model.device == "cpu"
    assert [c["device"] for c in calls] == ["mps", "cpu"]
    assert all(c["graphs"] is False for c in calls)
    assert "device=cpu" in log


def test_a_failed_cpu_load_is_not_retried() -> None:
    with pytest.raises(RuntimeError, match="cannot load on cpu"):
        _load_model(False, {"cpu"})


def test_the_launcher_builds_its_model_through_the_device_choice() -> None:
    source = ast.unparse(_tree())
    assert "model = _load_model(args.weights)" in source
    assert "device='cpu', dtype=torch.bfloat16" not in source.replace('"', "'")
