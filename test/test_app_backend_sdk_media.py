"""The backend SDK's media-type sniff (``kiro_crew.apps.backend_sdk.media``).

What is pinned is what an app cannot check for itself: the public names answer with
the gateway's own raster table (the same types and the same refusals), the function
is looked up at call time so nothing is a copy, the sniff window is bound from the
gateway's own value rather than a literal, both names are published at seam level 14,
and a spawned child reaches them without loading the gateway's security stack.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.messaging import raster
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SRC = Path(__file__).resolve().parents[1] / "src"

_MEDIA_NAMES = ("RASTER_SNIFF_BYTES", "sniff_raster_mime")

#: The first sixteen bytes of a file of each type.
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01"
_GIF87 = b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
_GIF89 = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
_WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L"
_BMP = b"BM\x3a\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00\x28\x00"


def test_the_media_names_are_listed_at_seam_level_fourteen() -> None:
    for name in _MEDIA_NAMES:
        assert name in sdk.__all__, name
        assert name in dir(sdk), name
        assert sdk._LAZY_EXPORTS[name] == "media", name
        assert sdk._EXPORT_LEVELS[name] == 14, name
    assert sdk.SDK_LEVEL >= 14


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        pytest.param(_PNG, "image/png", id="png"),
        pytest.param(_JPEG, "image/jpeg", id="jpeg"),
        pytest.param(_GIF87, "image/gif", id="gif87a"),
        pytest.param(_GIF89, "image/gif", id="gif89a"),
        pytest.param(_WEBP, "image/webp", id="webp"),
        pytest.param(_BMP, "image/bmp", id="bmp"),
    ],
)
def test_sniff_raster_mime_answers_with_the_gateway_table(head: bytes, expected: str) -> None:
    assert sdk.sniff_raster_mime(head) == raster.sniff_raster_mime(head) == expected


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"<svg><script>alert(1)</script></svg>", id="svg"),
        pytest.param(b"RIFF\x24\x00\x00\x00WAVEfmt ", id="riff-wave"),
        pytest.param(b"RIFF\x24\x00\x00\x00WEB", id="riff-cut-before-its-form-tag"),
        pytest.param(b"#!/bin/sh\necho hello\n", id="shell-script"),
        pytest.param(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", id="pdf"),
        pytest.param(b"", id="empty"),
        pytest.param(_PNG[:4], id="truncated-png-signature"),
    ],
)
def test_sniff_raster_mime_refuses_what_the_gateway_refuses(head: bytes) -> None:
    assert raster.sniff_raster_mime(head) is None
    assert sdk.sniff_raster_mime(head) is None


def test_sniff_raster_mime_delegates_at_call_time(monkeypatch) -> None:
    seen: list[bytes] = []

    def recorder(head: bytes) -> str:
        seen.append(head)
        return "image/x-probe"

    monkeypatch.setattr("kiro_crew.messaging.raster.sniff_raster_mime", recorder)
    head = b"\x00probe bytes"
    assert sdk.sniff_raster_mime(head) == "image/x-probe"
    assert len(seen) == 1 and seen[0] is head


def test_the_sniff_window_is_the_gateway_window() -> None:
    assert sdk.RASTER_SNIFF_BYTES == raster.SNIFF_BYTES
    # The WebP form tag sits at offset 8-12, so a read cut to the window still holds it.
    whole_file = _WEBP + b"\x00" * 64
    assert sdk.sniff_raster_mime(whole_file[: sdk.RASTER_SNIFF_BYTES]) == "image/webp"
    assert sdk.sniff_raster_mime(whole_file) == "image/webp"


def _bound_names(node: ast.stmt) -> list[str]:
    targets: list[ast.expr]
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    else:
        return []
    return [target.id for target in targets if isinstance(target, ast.Name)]


def test_the_sniff_window_is_bound_from_the_gateway_value() -> None:
    # Equal values cannot tell the gateway's binding from a hand-copied 16; the
    # module source can.
    media = importlib.import_module("kiro_crew.apps.backend_sdk.media")
    tree = ast.parse(Path(media.__file__).read_text(encoding="utf-8"))
    values = [
        ast.unparse(node.value) for node in tree.body if "RASTER_SNIFF_BYTES" in _bound_names(node)
    ]
    assert values == ["raster.SNIFF_BYTES"], values


def test_a_spawned_child_sniffs_without_loading_the_security_stack(tmp_path) -> None:
    unused = (
        "kiro_crew.hooks",
        "kiro_crew.security",
        "kiro_crew.sandbox",
        "kiro_crew.messaging.attachments",
    )
    probe = (
        "import sys\n"
        "import kiro_crew.apps.backend_sdk as sdk\n"
        "print(sdk.sniff_raster_mime(bytes.fromhex(sys.argv[1])))\n"
        "print(' '.join(str(name in sys.modules) for name in sys.argv[2:]))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "probe-app",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe, _PNG.hex(), *unused],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    mime, loaded = done.stdout.split("\n")[:2]
    assert mime == "image/png"
    assert loaded == " ".join("False" for _ in unused), dict(zip(unused, loaded.split()))
