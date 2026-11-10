"""Plumb's CUDA wheel choice and the launcher's device fallback.

The driver probe is stubbed. Nothing here runs nvidia-smi or installs torch.
"""

from __future__ import annotations

import ast
import subprocess

import pytest

from kiro_crew.decisions import local_torch
from kiro_crew.decisions.local_runtime import PLUMB_DEVICE_HELPER, SERVERS_DIR
from kiro_crew.decisions.local_servers.plumb_device import resolve_device


@pytest.fixture(autouse=True)
def _fresh_driver_cache():
    local_torch.clear_driver_cuda_cache()
    yield
    local_torch.clear_driver_cuda_cache()


class TestCudaIndex:
    @pytest.mark.parametrize(
        ("version", "marker"),
        [
            ((13, 4), "cu132"),
            ((13, 2), "cu132"),
            ((13, 1), "cu130"),
            ((13, 0), "cu130"),
            ((12, 9), "cu126"),
            ((12, 6), "cu126"),
            ((12, 5), "whl/cpu"),
            ((12, 4), "whl/cpu"),
        ],
    )
    def test_the_newest_published_wheel_the_driver_can_run(self, version, marker):
        assert local_torch.cuda_index_for_driver(version).endswith(marker)

    def test_a_header_line_is_the_drivers_cuda_version(self):
        text = "| NVIDIA-SMI 580.65.06   Driver Version: 580.65.06   CUDA Version: 13.0     |"
        assert local_torch.parse_cuda_version(text) == (13, 0)

    def test_a_report_without_the_line_is_not_a_version(self):
        assert local_torch.parse_cuda_version("NVIDIA-SMI has failed") is None


class TestDriverProbe:
    def test_no_system_binary_does_not_spawn(self, monkeypatch):
        monkeypatch.setattr(
            local_torch.platform_compat, "trusted_system_bin_quiet", lambda name: None
        )
        spawned = []
        monkeypatch.setattr(
            local_torch.subprocess, "run", lambda *a, **k: spawned.append(a) or None
        )
        assert local_torch.driver_cuda_version() is None
        assert spawned == []
        assert local_torch.cuda_unavailable_reason() == (
            "unsupported_platform"
            if not local_torch.sys.platform.startswith("linux")
            else "no_driver"
        )

    def test_a_successful_report_is_cached(self, monkeypatch):
        calls = []

        def _run(argv, **kwargs):
            calls.append(argv)
            assert kwargs["timeout"] == local_torch._NVIDIA_SMI_TIMEOUT_SECS
            return subprocess.CompletedProcess(argv, 0, stdout="CUDA Version: 13.0\n")

        monkeypatch.setattr(
            local_torch.platform_compat,
            "trusted_system_bin_quiet",
            lambda name: "/usr/bin/nvidia-smi",
        )
        monkeypatch.setattr(local_torch.subprocess, "run", _run)
        assert local_torch.driver_cuda_version() == (13, 0)
        assert local_torch.driver_cuda_version() == (13, 0)
        assert calls == [["/usr/bin/nvidia-smi"]]

    def test_a_timeout_reads_as_no_driver(self, monkeypatch):
        def _run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(
            local_torch.platform_compat,
            "trusted_system_bin_quiet",
            lambda name: "/usr/bin/nvidia-smi",
        )
        monkeypatch.setattr(local_torch.subprocess, "run", _run)
        assert local_torch.driver_cuda_version() is None


class TestPlumbChoice:
    def test_off_or_absent_stays_on_the_cpu_index(self, monkeypatch):
        monkeypatch.setattr(local_torch, "plumb_gpu_requested", lambda: False)
        monkeypatch.setattr(local_torch, "driver_cuda_version", lambda: (13, 0))
        assert local_torch.linux_torch_index("plumb-4b") == local_torch.TORCH_CPU_INDEX
        assert local_torch.plumb_use_cuda() is False

    def test_another_preset_stays_on_the_cpu_index_even_when_the_switch_is_on(self, monkeypatch):
        monkeypatch.setattr(local_torch, "plumb_gpu_requested", lambda: True)
        monkeypatch.setattr(local_torch, "driver_cuda_version", lambda: (13, 0))
        monkeypatch.setattr(local_torch.sys, "platform", "linux")
        assert local_torch.linux_torch_index("laya") == local_torch.TORCH_CPU_INDEX

    def test_on_with_a_usable_driver_selects_that_wheel(self, monkeypatch):
        monkeypatch.setattr(local_torch, "plumb_gpu_requested", lambda: True)
        monkeypatch.setattr(local_torch, "driver_cuda_version", lambda: (13, 0))
        monkeypatch.setattr(local_torch.sys, "platform", "linux")
        assert local_torch.linux_torch_index("plumb-4b").endswith("cu130")
        assert local_torch.plumb_use_cuda() is True
        view = local_torch.plumb_gpu_view()
        assert view == {"available": True, "enabled": True, "reason": ""}

    def test_on_without_a_driver_stays_on_cpu_and_says_why(self, monkeypatch):
        monkeypatch.setattr(local_torch, "plumb_gpu_requested", lambda: True)
        monkeypatch.setattr(local_torch, "driver_cuda_version", lambda: None)
        monkeypatch.setattr(local_torch.sys, "platform", "linux")
        assert local_torch.plumb_use_cuda() is False
        assert local_torch.plumb_gpu_view()["reason"] == "no_driver"

    def test_a_non_linux_host_is_not_offered_a_cuda_wheel(self, monkeypatch):
        monkeypatch.setattr(local_torch, "plumb_gpu_requested", lambda: True)
        monkeypatch.setattr(local_torch, "driver_cuda_version", lambda: (13, 0))
        monkeypatch.setattr(local_torch.sys, "platform", "darwin")
        assert local_torch.cuda_unavailable_reason() == "unsupported_platform"
        assert local_torch.plumb_use_cuda() is False


class _Torch:
    def __init__(self, *, available: bool = False, raises: bool = False):
        self.cuda = self
        self._available = available
        self._raises = raises

    def is_available(self) -> bool:
        if self._raises:
            raise RuntimeError("cuda init failed")
        return self._available


class TestLauncherDevice:
    def test_cuda_is_used_only_when_torch_can_see_it(self):
        assert resolve_device("cuda", _Torch(available=True)) == "cuda"
        assert resolve_device("cuda", _Torch(available=False)) == "cpu"
        assert resolve_device("cuda", _Torch(raises=True)) == "cpu"
        assert resolve_device("cpu", _Torch(available=True)) == "cpu"
        assert resolve_device("", _Torch(available=True)) == "cpu"

    def test_the_launcher_imports_the_helper_the_runtime_copies(self):
        source = (SERVERS_DIR / "plumb_cpu.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "plumb_device"
        ]
        assert [alias.name for node in imports for alias in node.names] == ["resolve_device"]
        assert (SERVERS_DIR / PLUMB_DEVICE_HELPER).is_file()

    def test_the_model_is_constructed_with_the_resolved_device(self):
        source = (SERVERS_DIR / "plumb_cpu.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "JevK5"
        ]
        assert len(calls) == 1
        device = next(kw.value for kw in calls[0].keywords if kw.arg == "device")
        assert isinstance(device, ast.Name) and device.id == "device"
