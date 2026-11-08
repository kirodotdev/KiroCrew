"""Stage the Visual C++ runtime DLLs ``kiro-cli.exe`` imports, from a pinned redistributable.

``kiro-cli.exe`` imports ``vcruntime140.dll``, ``vcruntime140_1.dll``,
``msvcp140.dll`` and ``msvcp140_1.dll``.  No Windows edition carries them, and
the upstream MSI the build extracts does not bring them, so a
``BUNDLE_KIRO_CLI=1`` Windows build stages them beside the executable, where
the loader looks first.

They come from Microsoft's own Visual C++ redistributable, the package
Microsoft distributes for exactly this, never from the build host:
``packaging/vc-runtime-pin.json`` names one immutable download URL, the
sha256 of that installer, and the sha256 of each DLL inside it.  Every check
fails closed.  A copy from the build host's ``System32`` would ship whatever
runtime that host happens to carry, and because the application directory wins
over ``System32`` an old host copy would shadow a user's newer runtime.

The redistributable is a WiX Burn bundle.  Nothing here runs it: its attached
payload container is read out of the PE file, and the cabinets inside are
unpacked in-process (both are MSZIP, which ``zlib`` decodes).  Pure stdlib,
so the same code runs on the Windows build host and in the Linux test suite.
The DLL pins are what make the extraction trustworthy: a parser mistake can
only fail the build, never stage different bytes.

This module never downloads: ``build-desktop.sh`` fetches the installer with
the same ``curl`` + cache + sha256 step it uses for kiro-cli, and hands the
file here.

Commands::

    python3 packaging/vc_runtime.py source
    python3 packaging/vc_runtime.py stage <dest> --bundle <VC_redist.x64.exe>
    python3 packaging/vc_runtime.py pin <url> <VC_redist.x64.exe>

``source`` validates the pin file and prints ``<sha256> <url>``.  ``stage``
writes the four DLLs and ``VC-RUNTIME-VERSION`` into ``dest``.  ``pin`` reads
a candidate installer downloaded from ``url`` and prints the JSON for
``vc-runtime-pin.json``; it is the bump procedure (``docs/build/release.md``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import zlib
from pathlib import Path

PIN_FILE = Path(__file__).resolve().parent / "vc-runtime-pin.json"
VERSION_FILE = "VC-RUNTIME-VERSION"
# The architecture suffix the redistributable's x64 runtime cabinet gives each
# file (its MSI File-table key, e.g. ``vcruntime140.dll_amd64``).
_ARCH_SUFFIX = "_amd64"
_BURN_MAGIC = 0x00F14300
_BURN_FORMAT_CAB = 1
_CAB_MAGIC = b"MSCF"
_DOWNLOAD_HOST = "download.visualstudio.microsoft.com"


class VcRuntimeError(Exception):
    """A pin, download or extraction step refused; the build must stop."""


def load_pin(path: Path = PIN_FILE) -> dict:
    """Read and validate the pin file; raise :class:`VcRuntimeError` on any defect."""
    pin = json.loads(path.read_text(encoding="utf-8"))
    redist = pin.get("redistributable") or {}
    dlls = pin.get("dlls") or {}
    url = redist.get("url", "")
    sha = redist.get("sha256", "")
    if not url.startswith(f"https://{_DOWNLOAD_HOST}/"):
        raise VcRuntimeError(f"{path.name}: url must be an https://{_DOWNLOAD_HOST}/ download")
    if not _is_sha256(sha):
        raise VcRuntimeError(f"{path.name}: redistributable sha256 is not 64 lowercase hex digits")
    # Microsoft's immutable download paths carry the installer's own sha256 as
    # one segment; a pin whose url and sha disagree was edited by hand.
    if sha.upper() not in url.split("/"):
        raise VcRuntimeError(f"{path.name}: url does not name the pinned sha256")
    if not re.fullmatch(r"\d+(\.\d+){2,3}", str(redist.get("version", ""))):
        raise VcRuntimeError(f"{path.name}: redistributable version must be its ProductVersion")
    if not dlls or not all(name.endswith(".dll") and _is_sha256(v) for name, v in dlls.items()):
        raise VcRuntimeError(f"{path.name}: dlls must map each .dll name to a sha256")
    return pin


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def burn_attached_container(bundle: bytes) -> bytes:
    """Return the attached payload container (container 1) of a WiX Burn bundle.

    The ``.wixburn`` section names the stub size and each container's size.
    Container 0 (the bootstrapper UI) starts at the stub; the payload container
    follows it, after the engine's original signature when the engine was signed
    before the containers were attached.
    """
    if bundle[:2] != b"MZ":
        raise VcRuntimeError("redistributable is not a PE file")
    pe = struct.unpack_from("<I", bundle, 0x3C)[0]
    if bundle[pe : pe + 4] != b"PE\0\0":
        raise VcRuntimeError("redistributable has no PE header")
    sections = struct.unpack_from("<H", bundle, pe + 6)[0]
    table = pe + 24 + struct.unpack_from("<H", bundle, pe + 20)[0]
    for index in range(sections):
        name, _vsize, _va, _rsize, raw = struct.unpack_from("<8sIIII", bundle, table + 40 * index)
        if name.rstrip(b"\0") == b".wixburn":
            break
    else:
        raise VcRuntimeError("redistributable is not a WiX Burn bundle (no .wixburn section)")
    magic, _version = struct.unpack_from("<II", bundle, raw)
    stub, _checksum, sig_offset, sig_size, fmt, count = struct.unpack_from("<6I", bundle, raw + 24)
    if magic != _BURN_MAGIC or fmt != _BURN_FORMAT_CAB or count < 2:
        raise VcRuntimeError(
            "unexpected .wixburn header: not a cabinet bundle with a payload container"
        )
    sizes = struct.unpack_from(f"<{count}I", bundle, raw + 48)
    start = stub + sizes[0]
    if sig_size:
        start = max(start, sig_offset + sig_size)
    container = bundle[start : start + sizes[1]]
    if container[:4] != _CAB_MAGIC or struct.unpack_from("<I", container, 8)[0] != sizes[1]:
        raise VcRuntimeError("payload container is not where the .wixburn header puts it")
    return container


def cab_files(cab: bytes) -> dict[str, bytes]:
    """Unpack a single-volume MSZIP (or stored) cabinet into ``{name: bytes}``."""
    if cab[:4] != _CAB_MAGIC:
        raise VcRuntimeError("not a cabinet")
    files_off = struct.unpack_from("<I", cab, 16)[0]
    n_folders, n_files, flags = struct.unpack_from("<HHH", cab, 26)
    if flags & 0x3:
        raise VcRuntimeError("multi-volume cabinets are not supported")
    pos = 36
    folder_reserve = data_reserve = 0
    if flags & 0x4:
        header_reserve, folder_reserve, data_reserve = struct.unpack_from("<HBB", cab, pos)
        pos += 4 + header_reserve
    folders = []
    for _ in range(n_folders):
        data_off, n_blocks, compress = struct.unpack_from("<IHH", cab, pos)
        folders.append(_folder_bytes(cab, data_off, n_blocks, compress & 0xF, data_reserve))
        pos += 8 + folder_reserve
    out: dict[str, bytes] = {}
    pos = files_off
    for _ in range(n_files):
        size, offset, folder = struct.unpack_from("<IIH", cab, pos)
        end = cab.index(b"\0", pos + 16)
        name = cab[pos + 16 : end].decode("utf-8", "replace")
        pos = end + 1
        if folder >= len(folders) or offset + size > len(folders[folder]):
            raise VcRuntimeError(f"cabinet entry {name!r} lies outside its folder")
        out[name] = folders[folder][offset : offset + size]
    return out


def _folder_bytes(cab: bytes, pos: int, n_blocks: int, compress: int, reserve: int) -> bytes:
    if compress not in (0, 1):
        raise VcRuntimeError(f"cabinet compression type {compress} is not supported")
    out = bytearray()
    for _ in range(n_blocks):
        packed, unpacked = struct.unpack_from("<HH", cab, pos + 4)
        block = cab[pos + 8 + reserve : pos + 8 + reserve + packed]
        pos += 8 + reserve + packed
        if compress == 0:
            data = block
        else:
            if block[:2] != b"CK":
                raise VcRuntimeError("MSZIP block lacks its CK signature")
            # MSZIP blocks are raw deflate streams that may refer back into the
            # previous block's output, so the last 32 KiB is the dictionary.
            inflater = zlib.decompressobj(-15, zdict=bytes(out[-32768:]))
            data = inflater.decompress(block[2:]) + inflater.flush()
        if len(data) != unpacked:
            raise VcRuntimeError("cabinet block did not decode to its declared size")
        out += data
    return bytes(out)


def runtime_dlls(bundle: bytes) -> dict[str, bytes]:
    """Every x64 runtime DLL in the bundle's cabinets, keyed by its file name."""
    found: dict[str, bytes] = {}
    for payload in cab_files(burn_attached_container(bundle)).values():
        if payload[:4] != _CAB_MAGIC:
            continue
        for key, data in cab_files(payload).items():
            if key.lower().endswith(_ARCH_SUFFIX):
                found.setdefault(key[: -len(_ARCH_SUFFIX)].lower(), data)
    return found


def verified_dlls(bundle: bytes, pin: dict) -> dict[str, bytes]:
    """The pinned DLLs out of a bundle whose own sha256 matches the pin."""
    got = _sha256(bundle)
    if got != pin["redistributable"]["sha256"]:
        raise VcRuntimeError(
            f"redistributable sha256 mismatch: got {got}, pinned {pin['redistributable']['sha256']}"
        )
    available = runtime_dlls(bundle)
    staged: dict[str, bytes] = {}
    for name, want in pin["dlls"].items():
        data = available.get(name.lower())
        if data is None:
            raise VcRuntimeError(f"pinned redistributable carries no x64 {name}")
        if _sha256(data) != want:
            raise VcRuntimeError(f"{name} sha256 mismatch: got {_sha256(data)}, pinned {want}")
        staged[name] = data
    return staged


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a fresh temp file and rename, so a planted symlink is replaced, not followed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vc-runtime.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp creates 0600; the staged files ship inside the app and are
        # read by whoever runs it.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stage(dest: Path, bundle: bytes, pin: dict) -> None:
    """Write the pinned DLLs and the version record; nothing is written unless all verify."""
    staged = verified_dlls(bundle, pin)
    for name, data in staged.items():
        _atomic_write(dest / name, data)
    _atomic_write(dest / VERSION_FILE, f"{pin['redistributable']['version']}\n".encode())


def pin_for(url: str, bundle: bytes) -> dict:
    """The pin-file JSON for a candidate installer downloaded from ``url``."""
    current = load_pin()
    available = runtime_dlls(bundle)
    missing = [name for name in current["dlls"] if name.lower() not in available]
    if missing:
        raise VcRuntimeError(f"candidate carries no x64 {', '.join(missing)}")
    return {
        "redistributable": {
            "name": current["redistributable"].get("name", ""),
            "version": "<fill in: the redistributable's ProductVersion>",
            "url": url,
            "sha256": _sha256(bundle),
        },
        "dlls": {name: _sha256(available[name.lower()]) for name in current["dlls"]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("source", help="validate the pin and print '<sha256> <url>'")
    stage_cmd = sub.add_parser("stage", help="stage the pinned DLLs into DEST")
    stage_cmd.add_argument("dest", type=Path)
    stage_cmd.add_argument("--bundle", type=Path, required=True)
    pin_cmd = sub.add_parser("pin", help="print the pin JSON for a candidate installer")
    pin_cmd.add_argument("url")
    pin_cmd.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "source":
            redist = load_pin()["redistributable"]
            print(f"{redist['sha256']} {redist['url']}")
        elif args.cmd == "stage":
            pin = load_pin()
            stage(args.dest, args.bundle.read_bytes(), pin)
            version = pin["redistributable"]["version"]
            print(f"    VC++ runtime: {version} ({', '.join(pin['dlls'])})")
        else:
            print(json.dumps(pin_for(args.url, args.bundle.read_bytes()), indent=2))
    except VcRuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "       See docs/build/release.md for how to bump packaging/vc-runtime-pin.json.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
