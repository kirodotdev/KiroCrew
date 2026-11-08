"""The system monitor must not report a failed embedding runtime as running.

``ollama_running`` drives the Services tab's embedding status. A failed vendored
llama.cpp import never recovers in-process, so it reads False even with the
model file present. This is the Memory tab's check: the import failed AND
vendored libs ship for the platform. An unsupported platform (no libs) is the
designed keyword-only degradation, which that check leaves alone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _run_collect(
    llama_class: object,
    libs_dirname: str | None,
    *,
    ollama: bool = False,
    ready: bool = False,
    model_present: bool = True,
) -> dict:
    """Run ``_collect_system_metrics``; by default a present model file and an idle embedder."""
    from kiro_crew.dashboard import handlers_system

    with (
        patch.object(handlers_system, "_get_static_system_info", return_value={}),
        # The local-IP block opens a real UDP socket; stub it like the sibling tests.
        patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
        # No real child: every subprocess block degrades under its own try/except.
        patch.object(handlers_system.subprocess, "check_output", side_effect=OSError("stub")),
        patch.object(
            handlers_system,
            "get_shared_embedder",
            return_value=SimpleNamespace(is_ready=lambda: ready),
        ),
        patch.object(handlers_system, "model_file_present", return_value=model_present),
        # create=True: if the collector stops reading these names, the
        # failed-import case fails on its assertion rather than on patch setup.
        patch.object(handlers_system, "_load_llama_class", return_value=llama_class, create=True),
        patch.object(
            handlers_system, "_platform_libs_dirname", return_value=libs_dirname, create=True
        ),
        patch.object(handlers_system, "ollama_backend_selected", return_value=ollama, create=True),
    ):
        handlers_system._metrics_cache.clear()
        handlers_system._metrics_cache_ts = 0.0
        handlers_system._proc_scan_cache = {}
        handlers_system._proc_scan_cache_ts = 0.0
        return handlers_system._collect_system_metrics()


@pytest.mark.parametrize(
    ("llama_class", "libs_dirname", "running"),
    [
        pytest.param(None, "linux_x86_64", False, id="import-failed"),
        pytest.param(object(), "linux_x86_64", True, id="importable"),
        pytest.param(None, None, True, id="unsupported-platform"),
    ],
)
def test_ollama_running_reads_the_runtime_load_result(
    llama_class: object, libs_dirname: str | None, running: bool
) -> None:
    assert _run_collect(llama_class, libs_dirname)["ollama_running"] is running


@pytest.mark.parametrize("ready", [True, False])
def test_ollama_backend_running_reads_only_the_server_probe(ready: bool) -> None:
    # A failed native runtime and a missing GGUF file are not the Ollama
    # backend's faults; the server probe alone decides.
    metrics = _run_collect(None, "linux_x86_64", ollama=True, ready=ready, model_present=False)
    assert metrics["ollama_running"] is ready
