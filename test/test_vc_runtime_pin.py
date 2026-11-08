"""The Windows lane's Visual C++ runtime comes from a sha256-pinned redistributable.

``packaging/vc_runtime.py`` stages ``vcruntime140*.dll`` / ``msvcp140*.dll``
beside the bundled ``kiro-cli.exe`` out of the Microsoft redistributable that
``packaging/vc-runtime-pin.json`` pins.  These tests build a small WiX Burn
bundle in memory -- a PE file with a ``.wixburn`` section and MSZIP cabinets,
the shape the real installer has -- so the extractor and every fail-closed
check run on any platform with no download.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "vc_runtime.py"
SPEC = importlib.util.spec_from_file_location("vc_runtime", SCRIPT)
assert SPEC and SPEC.loader
vc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vc)

# The DLLs kiro-cli.exe imports from the Visual C++ runtime.
KIRO_CLI_IMPORTS = {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mszip_cab(files: dict[str, bytes], block: int = 32768) -> bytes:
    """A one-folder MSZIP cabinet; each block is deflated against the previous 32 KiB."""
    stream = b"".join(files.values())
    blocks = []
    for start in range(0, len(stream), block):
        chunk = stream[start : start + block]
        packer = zlib.compressobj(
            9, zlib.DEFLATED, -15, zdict=stream[max(0, start - 32768) : start]
        )
        blocks.append((b"CK" + packer.compress(chunk) + packer.flush(), len(chunk)))
    entries = []
    offset = 0
    for name, data in files.items():
        entries.append(
            struct.pack("<IIHHHH", len(data), offset, 0, 0, 0, 0x20) + name.encode() + b"\0"
        )
        offset += len(data)
    files_off = 36 + 8
    data_off = files_off + sum(map(len, entries))
    data = b"".join(struct.pack("<IHH", 0, len(b), n) + b for b, n in blocks)
    total = data_off + len(data)
    header = b"MSCF" + struct.pack(
        "<IIIIIBBHHHHH", 0, total, 0, files_off, 0, 3, 1, 1, len(files), 0, 0, 0
    )
    folder = struct.pack("<IHH", data_off, len(blocks), 1)
    return header + folder + b"".join(entries) + data


def _burn_bundle(payloads: dict[str, bytes], signature_gap: int = 24) -> bytes:
    """A PE file whose ``.wixburn`` section names a UI container and a payload container.

    ``signature_gap`` puts bytes between the two containers, where a signed
    engine keeps its original signature, as the real redistributable does.
    """
    ui = _mszip_cab({"u0": b"bootstrapper ui"})
    payload = _mszip_cab(payloads)
    pe = 0x80
    section_table = pe + 24
    raw = 0x200
    stub = 0x400
    sig_offset = stub + len(ui)
    burn = struct.pack("<II16s", 0x00F14300, 2, b"\0" * 16)
    burn += struct.pack("<6I", stub, 0, sig_offset, signature_gap, 1, 2) + struct.pack(
        "<2I", len(ui), len(payload)
    )
    head = bytearray(stub)
    head[0:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, pe)
    head[pe : pe + 4] = b"PE\0\0"
    struct.pack_into("<H", head, pe + 6, 1)
    struct.pack_into("<H", head, pe + 20, 0)
    struct.pack_into("<8sIIII", head, section_table, b".wixburn", len(burn), 0x1000, 0x200, raw)
    head[raw : raw + len(burn)] = burn
    return bytes(head) + ui + b"\xaa" * signature_gap + payload


def _runtime(names: set[str]) -> dict[str, bytes]:
    # Over 32 KiB each, with repetition across blocks, so decoding needs the
    # previous block as its dictionary.
    return {name: (name.encode() * 4096) + bytes(range(256)) * 64 for name in sorted(names)}


def _pin_for(bundle: bytes, dlls: dict[str, bytes]) -> dict:
    sha = _sha(bundle)
    return {
        "redistributable": {
            "name": "test",
            "version": "14.44.35211",
            "url": f"https://download.visualstudio.microsoft.com/download/pr/x/{sha.upper()}/VC_redist.x64.exe",
            "sha256": sha,
        },
        "dlls": {name: _sha(data) for name, data in dlls.items()},
    }


@pytest.fixture
def bundle_and_pin():
    dlls = _runtime(KIRO_CLI_IMPORTS)
    inner = _mszip_cab(
        {f"{name}_amd64": data for name, data in dlls.items()} | {"concrt140.dll_arm64": b"arm"}
    )
    bundle = _burn_bundle({"a0": b"an msi, not a cabinet", "a1": inner})
    return bundle, _pin_for(bundle, dlls), dlls


def test_committed_pin_is_well_formed_and_names_what_kiro_cli_imports():
    pin = vc.load_pin()
    assert set(pin["dlls"]) == KIRO_CLI_IMPORTS


def test_stage_writes_the_pinned_dlls_and_records_the_version(bundle_and_pin, tmp_path):
    bundle, pin, dlls = bundle_and_pin
    vc.stage(tmp_path, bundle, pin)
    for name, data in dlls.items():
        assert (tmp_path / name).read_bytes() == data
    assert (tmp_path / vc.VERSION_FILE).read_text() == "14.44.35211\n"


def test_a_tampered_redistributable_is_refused(bundle_and_pin, tmp_path):
    bundle, pin, _ = bundle_and_pin
    tampered = bundle[:-1] + bytes([bundle[-1] ^ 1])
    with pytest.raises(vc.VcRuntimeError, match="redistributable sha256 mismatch"):
        vc.stage(tmp_path, tampered, pin)
    assert not any(tmp_path.iterdir())


def test_a_dll_whose_hash_does_not_match_the_pin_is_refused(bundle_and_pin, tmp_path):
    bundle, pin, _ = bundle_and_pin
    pin["dlls"]["msvcp140.dll"] = "0" * 64
    with pytest.raises(vc.VcRuntimeError, match="msvcp140.dll sha256 mismatch"):
        vc.stage(tmp_path, bundle, pin)
    assert not any(tmp_path.iterdir())


def test_a_pinned_dll_the_redistributable_lacks_is_refused(bundle_and_pin, tmp_path):
    bundle, pin, _ = bundle_and_pin
    pin["dlls"]["vcomp140.dll"] = "0" * 64
    with pytest.raises(vc.VcRuntimeError, match="carries no x64 vcomp140.dll"):
        vc.stage(tmp_path, bundle, pin)


def test_the_payload_container_is_found_without_a_signature_gap(tmp_path):
    dlls = _runtime({"vcruntime140.dll"})
    bundle = _burn_bundle(
        {"a1": _mszip_cab({f"{n}_amd64": d for n, d in dlls.items()})}, signature_gap=0
    )
    assert vc.runtime_dlls(bundle) == dlls


def test_a_file_that_is_not_a_burn_bundle_is_refused():
    with pytest.raises(vc.VcRuntimeError, match="not a PE file"):
        vc.runtime_dlls(b"not an installer")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("url", "http://download.visualstudio.microsoft.com/x/VC_redist.x64.exe", "https://"),
        ("url", "https://example.com/VC_redist.x64.exe", "https://"),
        ("sha256", "a" * 64, "does not name the pinned sha256"),
        ("sha256", "XYZ", "64 lowercase hex"),
        ("version", "<fill in>", "ProductVersion"),
    ],
)
def test_a_malformed_pin_file_is_refused(tmp_path, field, value, message):
    pin = json.loads(vc.PIN_FILE.read_text(encoding="utf-8"))
    pin["redistributable"][field] = value
    path = tmp_path / "vc-runtime-pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(vc.VcRuntimeError, match=message):
        vc.load_pin(path)


def test_source_prints_the_pinned_sha_and_url(capsys):
    assert vc.main(["source"]) == 0
    pin = vc.load_pin()["redistributable"]
    assert capsys.readouterr().out == f"{pin['sha256']} {pin['url']}\n"


def test_main_reports_a_refusal_with_the_bump_procedure(tmp_path, capsys):
    bundle = tmp_path / "VC_redist.x64.exe"
    bundle.write_bytes(b"not the pinned installer")
    assert vc.main(["stage", str(tmp_path / "out"), "--bundle", str(bundle)]) == 1
    err = capsys.readouterr().err
    assert "redistributable sha256 mismatch" in err
    assert "docs/build/release.md" in err
    assert not (tmp_path / "out").exists()


def test_pin_prints_a_pin_for_a_candidate_installer(bundle_and_pin, tmp_path, capsys):
    bundle, pin, dlls = bundle_and_pin
    path = tmp_path / "VC_redist.x64.exe"
    path.write_bytes(bundle)
    assert vc.main(["pin", pin["redistributable"]["url"], str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["redistributable"]["sha256"] == _sha(bundle)
    assert printed["dlls"] == {name: _sha(data) for name, data in dlls.items()}


def _download_block() -> str:
    """The Windows lane's redistributable fetch, extracted from build-desktop.sh."""
    script = (ROOT / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")
    start = script.index("    local vcrt_cache ")
    end = script.index('    python3 "$ROOT/packaging/vc_runtime.py" stage', start)
    return script[start:end]


# On Windows `bash` may resolve to the WSL launcher, which cannot run this
# harness against Windows paths; the block's logic is the same on every host.
@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)
@pytest.mark.parametrize("source_line_ending", [r"\n", r"\r\n"], ids=["lf", "crlf"])
def test_the_windows_lane_refuses_a_download_that_does_not_match_the_pin(
    tmp_path, source_line_ending
):
    # curl is stubbed to deliver the wrong bytes; the block must stop before
    # anything lands in the cache, with the sha check the kiro-cli fetch uses.
    pin = vc.load_pin()["redistributable"]
    curl_url = tmp_path / "curl-url"
    script = (ROOT / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")
    sha_ok = script[
        script.index("kiro_cli_sha_ok() {") : script.index(
            "\n}\n", script.index("kiro_cli_sha_ok() {")
        )
        + 3
    ]
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f"ROOT={shlex.quote(str(ROOT))}\n"
        f"HOME={shlex.quote(str(tmp_path / 'home'))}\n"
        # Native Windows Python emits CRLF; curl must receive the exact pinned URL.
        f"python3() {{ printf '%s %s%b' {shlex.quote(pin['sha256'])} "
        f"{shlex.quote(pin['url'])} {shlex.quote(source_line_ending)}; }}\n"
        f"CURL_URL={shlex.quote(str(curl_url))}\n"
        'curl() { printf \'%s\' "$5" > "$CURL_URL"; '
        'while [ "$1" != "-o" ]; do shift; done; printf tampered > "$2"; }\n'
        f"{sha_ok}\n"
        f"run() {{\n{_download_block()}}}\n"
        "run\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "VC++ redistributable sha256 mismatch: refusing to bundle" in result.stderr
    assert curl_url.read_bytes() == pin["url"].encode("utf-8")
    assert list((tmp_path / "home" / ".cache" / "kirocrew-build" / "vc-runtime").iterdir()) == []


def test_the_windows_lane_stages_the_runtime_from_the_pin_not_the_build_host():
    script = (ROOT / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")
    assert 'packaging/vc_runtime.py" stage' in script
    assert "System32/$crt" not in script and "crt_dir/$crt" not in script


def test_the_embeddings_runtime_note_states_the_same_redistribution_position():
    # The wheel's embeddings lane does not vendor the runtime; its note must not
    # say redistributing it is off the table while the desktop lane ships it.
    note = (ROOT / "src" / "kiro_crew" / "embeddings_native.py").read_text(encoding="utf-8")
    assert "licensing decision" not in note
    assert "packaging/vc-runtime-pin.json" in note and vc.PIN_FILE.is_file()
