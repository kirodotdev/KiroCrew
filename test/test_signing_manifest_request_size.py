"""Sign-task request size: library entries stay small, and the ceiling fails generation.

The signing service refuses a request body over 100 KiB, and pull-request CI
cannot reach it, so the generator itself must keep the body small and refuse
one that would be too large.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
import sys

import pytest

# The generator runs on the macOS and Linux sign hosts and matches POSIX
# relative paths; on Windows os.path.relpath yields backslashes it never sees.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="signing manifest is generated on POSIX hosts only"
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "packaging" / "signing" / "generate-manifest.py"
TEMPLATE = REPO_ROOT / "packaging" / "signing" / "manifest-template.json"

MACHO_HEAD = struct.pack(">I", 0xFEEDFACF) + struct.pack(">I", 0x0100000C)
SITE = "Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("generate_manifest", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _app(tmp_path: pathlib.Path, rels: "list[str]") -> pathlib.Path:
    app = tmp_path / "KiroCrew.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    for rel in rels:
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MACHO_HEAD + b"\0" * 8)
    return app


def _run(generator, monkeypatch, capsys, app: pathlib.Path) -> "tuple[int, str, str]":
    for var in ("SIGNER_ACCESS_ROLE_ARN", "SIGNING_BUCKET", "INPUT_KEY", "OUTPUT_KEY"):
        monkeypatch.setenv(var, "x" * 128)
    monkeypatch.setattr("sys.argv", ["generate-manifest.py", str(TEMPLATE), str(app)])
    rc = generator.main()
    out, err = capsys.readouterr()
    return rc, out, err


def test_library_entries_are_short_and_executables_keep_entitlements(
    generator, tmp_path, monkeypatch, capsys
):
    lib_a = f"{SITE}/Crypto/Cipher/_raw_aes.abi3.so"
    lib_b = f"{SITE}/Crypto/Hash/_raw_aes.abi3.so"
    exe = "Contents/Resources/backend-dist/kirocrew-backend-arm64/bin/python3.12"
    rc, out, _err = _run(generator, monkeypatch, capsys, _app(tmp_path, [lib_a, lib_b, exe]))
    assert rc == 0
    entries = json.loads(out)["manifest"]["app"]["embedded_requirements"]

    assert set(entries[lib_a]) == {"full_identifier"}
    assert entries[lib_a]["full_identifier"].startswith(f"{generator.APP_ID}.raw-aes-")
    # Same file name in two packages: still distinct identifiers.
    assert entries[lib_a]["full_identifier"] != entries[lib_b]["full_identifier"]

    assert entries[exe] == {
        "full_identifier": generator.identifier_for(exe, generator.APP_ID),
        "signing_args": {"entitlements_path": "SIGNING_METADATA/Entitlements.entitlements"},
    }
    # Compact body: no indentation whitespace.
    assert "\n " not in out


def test_request_over_the_ceiling_fails_generation(generator, tmp_path, monkeypatch, capsys):
    rels = [f"{SITE}/pkg{i:04d}/native/_ext{i:04d}.cpython-312-darwin.so" for i in range(600)]
    rc, out, err = _run(generator, monkeypatch, capsys, _app(tmp_path, rels))
    assert rc == 1
    assert out == ""
    assert "over the" in err and str(generator.MAX_REQUEST_BYTES) in err
    assert generator.MAX_REQUEST_BYTES < 102400
