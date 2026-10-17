"""Shared builders for the ``test_wheel_engine*`` modules.

The repo root (for ``cli.sh``), the artifact base, a signed-manifest builder and its raw
bytes, and the stub build children the shadow-venv steps run.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable

import pytest

from kiro_crew.platform import wheel_engine

_REPO_ROOT = Path(__file__).resolve().parents[1]

_ARTIFACT_BASE = "https://download.crew.kiro.dev"


def _manifest(
    channel: str = "stable",
    version: str = "9.9.9",
    artifact_base: str = _ARTIFACT_BASE,
    **overrides: object,
) -> dict[str, object]:
    wheel_name = f"kirocrew-{version}-py3-none-any.whl"
    manifest: dict[str, object] = {
        "algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
        "channel": channel,
        "key_id": wheel_engine.CLI_MANIFEST_KEY_ID,
        "pub_date": "2026-01-01T00:00:00Z",
        "python_requires": ">=3.10",
        "schema": "kirocrew-cli-artifact-manifest-v1",
        "sha256": "a" * 64,
        "signature": base64.b64encode(b"not-a-real-signature").decode(),
        "version": version,
        "wheel_url": f"{artifact_base}/cli/{channel}/{version}/{wheel_name}",
    }
    manifest.update(overrides)
    return manifest


def _raw(manifest: dict[str, object]) -> bytes:
    return json.dumps(manifest).encode("utf-8")


def _stub_children(
    monkeypatch: pytest.MonkeyPatch,
    answer: "Callable[[list[str]], tuple[int, bytes, bytes]]" = lambda _argv: (0, b"", b""),
) -> list[tuple[list[str], dict[str, object]]]:
    """Replace the one build-child seam; return the ``(argv, kwargs)`` it saw."""
    seen: list[tuple[list[str], dict[str, object]]] = []

    def fake(
        argv: list[str], timeout: float, step: str, **kwargs: object
    ) -> tuple[int, bytes, bytes]:
        seen.append((list(argv), kwargs))
        return answer(list(argv))

    monkeypatch.setattr(wheel_engine, "_spawn_build_child", fake)
    return seen
