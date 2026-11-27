"""The Plumb-4B launcher's device (MPS when torch can use it) and dtype choice.

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


def _load(names: set[str], namespace: dict, constants: tuple[str, ...] = ()) -> dict:
    defs: list[ast.stmt] = [
        node for node in _tree().body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {d.name for d in defs if isinstance(d, ast.FunctionDef)} == names
    defs += [
        node
        for node in _tree().body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)
    ]
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
        float32="f32",
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


_DTYPE_FNS = {"_cpu_lacks_native_bf16", "_float32_fits", "_dtype"}
_CONSTANTS = ("FLOAT32_PEAK_BYTES", "FLOAT32_RAM_FACTOR")
_GIB = 1 << 30
# The reporter's CPU: AVX-512 without bf16 (Xeon Platinum 8175M), two cores.
_NO_BF16 = "processor\t: 0\nflags\t\t: fpu sse2 avx2 avx512f avx512bw\n" * 2
# A machine that holds a float32 load: physical memory and a cgroup v2 scope.
_ROOMY = {
    "/proc/cpuinfo": _NO_BF16,
    "/proc/self/cgroup": "0::/user.slice/plumb.scope\n",
    "/sys/fs/cgroup/user.slice/plumb.scope/cgroup.controllers": "memory pids\n",
    "/sys/fs/cgroup/user.slice/plumb.scope/memory.max": f"{120 * _GIB}\n",
    "/sys/fs/cgroup/user.slice/plumb.scope/memory.high": f"{108 * _GIB}\n",
    "/sys/fs/cgroup/user.slice/memory.max": "max\n",
}


def _host(files: dict[str, str], ram_gib: float = 184.7, sysconf: bool = True) -> dict:
    """``open`` and ``os`` for the launcher's reads: *files* by path, nothing else."""

    def fake_open(path: str, *_args: Any, **_kwargs: Any) -> io.StringIO:
        if path not in files:
            raise FileNotFoundError(path)
        return io.StringIO(files[path])

    pages = int(ram_gib * _GIB) // 4096
    fake_os: Any = SimpleNamespace()
    if sysconf:
        fake_os.sysconf = lambda name: {"SC_PHYS_PAGES": pages, "SC_PAGE_SIZE": 4096}[name]
    return {"open": fake_open, "os": fake_os}


def _dtype(device: str, files: dict[str, str], **host: Any) -> str:
    return _load(_DTYPE_FNS, _host(files, **host), _CONSTANTS)["_dtype"](device)


def test_float32_on_a_cpu_without_native_bf16_that_holds_it() -> None:
    assert _dtype("cpu", _ROOMY) == "float32"


@pytest.mark.parametrize(
    ("files", "host"),
    [
        pytest.param(
            {**_ROOMY, "/proc/cpuinfo": _NO_BF16 + "flags\t: avx512_bf16\n"}, {}, id="bf16-core"
        ),
        pytest.param(
            {**_ROOMY, "/proc/cpuinfo": "flags\t: avx2 amx_bf16 amx_tile\n"}, {}, id="amx"
        ),
        pytest.param({**_ROOMY, "/proc/cpuinfo": "Features\t: fp asimd\n"}, {}, id="aarch64"),
        pytest.param(
            {k: v for k, v in _ROOMY.items() if k != "/proc/cpuinfo"}, {}, id="no-cpuinfo"
        ),
        pytest.param(_ROOMY, {"ram_gib": 64}, id="small-machine"),
        pytest.param(_ROOMY, {"sysconf": False}, id="no-sysconf"),
        pytest.param(
            {**_ROOMY, "/sys/fs/cgroup/user.slice/memory.high": f"{20 * _GIB}\n"},
            {},
            id="parent-high",
        ),
        pytest.param(
            {**_ROOMY, "/sys/fs/cgroup/user.slice/plumb.scope/memory.max": "junk\n"},
            {},
            id="unreadable-limit",
        ),
        pytest.param({**_ROOMY, "/proc/self/cgroup": "4:memory:/docker/x\n"}, {}, id="cgroup-v1"),
        pytest.param(
            {k: v for k, v in _ROOMY.items() if not k.endswith("cgroup.controllers")},
            {},
            id="no-cgroup2-dir",
        ),
    ],
)
def test_bfloat16_unless_every_condition_holds(files: dict[str, str], host: dict) -> None:
    assert _dtype("cpu", files, **host) == "bfloat16"


def test_the_apple_gpu_keeps_bfloat16() -> None:
    assert _dtype("mps", _ROOMY) == "bfloat16"


def test_the_float32_factor_is_the_presets_recommendation_factor() -> None:
    from kiro_crew.decisions.local_models import RECOMMEND_RAM_FACTOR

    assert _load(set(), {}, _CONSTANTS)["FLOAT32_RAM_FACTOR"] == RECOMMEND_RAM_FACTOR


def _load_model(
    mps: Any, fail_on: set[str], files: dict[str, str] | None = None
) -> tuple[Any, list[dict], str]:
    calls: list[dict] = []
    log = io.StringIO()
    namespace = {
        "torch": _torch(mps),
        "JevK5": _Model(fail_on, calls),
        "sys": SimpleNamespace(stderr=log),
        **_host(files or {}),
    }
    loaded = _load(_DTYPE_FNS | {"_model_device", "_load_model"}, namespace, _CONSTANTS)
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


def test_a_cpu_load_without_native_bf16_is_float32_and_says_so() -> None:
    model, calls, log = _load_model(False, set(), _ROOMY)

    assert [c["dtype"] for c in calls] == ["f32"]
    assert "device=cpu dtype=float32" in log


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
