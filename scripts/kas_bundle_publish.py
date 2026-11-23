#!/usr/bin/env python3
"""Publish a KAS engine release the way ``kirocrew kas install`` consumes it.

Takes the engine tarballs a KAS build produced (``kas-<platform>.tar.zst`` for
each of ``linux-x64``, ``linux-arm64``, ``darwin-universal``, ``win32-x64``),
adds one official Node build per host architecture from nodejs.org -- verified
against that release's ``SHASUMS256.txt`` -- and writes the manifest
:mod:`kiro_crew.acp.kas_bundle` reads::

    <out>/kas/<version>/kas-*.tar.xz         (recompressed from the build's .tar.zst)
    <out>/kas/<version>/node-v<N>-*.{tar.xz,zip}
    <out>/kas/<version>/manifest.json

    <out>/kas/<version>/provenance.json      (where each artifact came from)

For an Amazon Linux 2 release (``packaging/kas-al2/``) two optional inputs
replace linux-x64 pieces, each pinned by a sha256 the caller supplies:
``--node-linux-x64`` uses a local Node tarball for the ``linux-x64`` Node key
instead of nodejs.org's (every other key still comes from nodejs.org), and
``--onnx-overlay-linux-x64`` swaps the files in the linux-x64 engine's
``onnxruntime-node/bin/napi-v3/linux/x64/`` for the overlay's. Both are recorded
in ``provenance.json``; the manifest's own shape does not change.

With ``--s3 s3://<bucket>`` the same tree is uploaded with the ``aws`` CLI. The
script never reads a credential itself; the CLI resolves its own. Nothing points
at a "current" release: each Kiro Crew release pins the one version it runs.

Why a Node per architecture when the engine's Mac build is universal: Node
ships no universal binary, so ``node.builds`` is keyed by ``<os>-<arch>`` and
the installer picks the one for its host. The engine's own ``platforms`` map
keeps the engine's build matrix untouched.

Example::

    python3 scripts/kas_bundle_publish.py \\
        --kas-dir ~/kas-build/0.66.33 --version 0.66.33 \\
        --node-version 22.23.3 --out ./publish \\
        --s3 s3://kirocrew-kas
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

NODE_DIST = "https://nodejs.org/dist"
#: ``kirocrew`` host key -> nodejs.org archive name suffix.
NODE_BUILDS = {
    "linux-x64": "linux-x64.tar.xz",
    "linux-arm64": "linux-arm64.tar.xz",
    "darwin-x64": "darwin-x64.tar.xz",
    "darwin-arm64": "darwin-arm64.tar.xz",
    "win32-x64": "win-x64.zip",
}
ENGINE_PLATFORMS = ("linux-x64", "linux-arm64", "darwin-universal", "win32-x64")
#: The engine's declared floor (``@kiro/agent`` ``engines.node``); refuse older.
NODE_MIN = (22, 13, 0)
#: Written beside manifest.json; the installer never reads it.
PROVENANCE_NAME = "provenance.json"


def _script_commit() -> str:
    """This checkout's commit, or ``unknown`` outside git (with ``-dirty`` when modified)."""
    here = Path(__file__).resolve().parent
    try:
        sha = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(here), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return sha + ("-dirty" if dirty else "")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ref(path: Path) -> dict:
    return {"file": path.name, "sha256": sha256_of(path), "size": path.stat().st_size}


def fetch(url: str, dest: Path) -> None:
    # Every url this script opens is NODE_DIST + a path it assembled from a
    # validated version and a fixed archive name; refuse anything else so a
    # future caller cannot hand it a file:// or foreign host.
    if not url.startswith(NODE_DIST + "/"):
        sys.exit(f"refusing to fetch outside {NODE_DIST}: {url}")
    if dest.exists():
        return
    print(f"  GET {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- host pinned to NODE_DIST above, https only, and every byte is checked against SHASUMS256.txt
    resp = urllib.request.urlopen(url, timeout=600)
    with resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)


def _expect_sha256(path: Path, want: str, what: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        sys.exit(f"{what}: expected sha256 must be 64 lowercase hex digits")
    actual = sha256_of(path)
    if actual != want:
        sys.exit(f"{what}: {path.name} has sha256 {actual}, expected {want}")
    return actual


def _check_node_tarball(path: Path, version: str) -> None:
    """Refuse a linux-x64 Node tarball the installer would not accept.

    The installer finds ``bin/node`` under the archive's one top directory and
    requires ``node --version`` to print exactly ``v<version>``, the layout and
    answer of nodejs.org's ``node-v<version>-linux-x64.tar.xz``. The binary runs
    here only on a linux-x64 host; elsewhere this refuses rather than publish an
    unverified Node.
    """
    top = f"node-v{version}-linux-x64"
    try:
        with tarfile.open(path, "r:xz") as tar:
            names = tar.getnames()
            member = tar.getmember(f"{top}/bin/node")
            if not member.isfile():
                sys.exit(f"{path.name}: {top}/bin/node is not a regular file")
            if any(n != top and not n.startswith(top + "/") for n in names):
                sys.exit(f"{path.name}: every member must sit under {top}/")
            if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
                sys.exit(f"{path.name}: checking 'node --version' needs a linux-x64 host")
            fh = tar.extractfile(member)
            assert fh is not None
            with tempfile.TemporaryDirectory() as tmp:
                node = Path(tmp) / "node"
                with fh, open(node, "wb") as out:
                    shutil.copyfileobj(fh, out)
                node.chmod(0o755)
                got = subprocess.run(
                    [str(node), "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                ).stdout.strip()
    except (tarfile.TarError, KeyError, OSError, lzma.LZMAError) as exc:
        sys.exit(f"{path.name}: not a {top}.tar.xz layout ({exc})")
    if got != f"v{version}":
        sys.exit(f"{path.name}: node --version printed {got!r}, expected 'v{version}'")


def node_builds(
    version: str, out_dir: Path, local_linux_x64: tuple[Path, str] | None = None
) -> dict[str, dict]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"Node version must look like 22.23.3, got {version!r}")
    parts = tuple(int(x) for x in version.split("."))
    if parts < NODE_MIN:
        sys.exit(f"Node {version} is below the engine's floor {'.'.join(map(str, NODE_MIN))}")
    base = f"{NODE_DIST}/v{version}"
    sums_path = out_dir / f"SHASUMS256-v{version}.txt"
    fetch(f"{base}/SHASUMS256.txt", sums_path)
    sums = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    builds: dict[str, dict] = {}
    for key, suffix in NODE_BUILDS.items():
        name = f"node-v{version}-{suffix}"
        dest = out_dir / name
        if key == "linux-x64" and local_linux_x64 is not None:
            src, want = local_linux_x64
            _expect_sha256(src, want, "--node-linux-x64")
            _check_node_tarball(src, version)
            # Published under nodejs.org's name, so the installer's lookup is unchanged.
            tmp = dest.with_suffix(dest.suffix + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
            builds[key] = ref(dest)
            if builds[key]["sha256"] != want:
                sys.exit(f"{name}: copy does not hash to {want}")
            print(f"  ✓ {name} (local build, sha256 {want})")
            continue
        fetch(f"{base}/{name}", dest)
        actual = sha256_of(dest)
        if sums.get(name) != actual:
            sys.exit(f"{name}: sha256 {actual} does not match nodejs.org SHASUMS256.txt")
        builds[key] = {"file": name, "sha256": actual, "size": dest.stat().st_size}
        print(f"  ✓ {name}")
    sums_path.unlink()
    return builds


def _recompress_zst_to_xz(src: Path, dest: Path) -> None:
    """Rewrite a ``.tar.zst`` engine tarball as ``.tar.xz``, byte-identical inside.

    The KAS build emits zstd, but CPython 3.12 cannot read zstd without a third
    party package or a ``zstd`` executable, and stock macOS and Windows have
    neither. xz is read by ``tarfile`` on every supported interpreter with nothing
    but the standard library, so that is what the published manifest names. The
    tar stream is not re-packed: zstd is decoded and lzma encoded over the same
    bytes, so member names, modes and contents are exactly the KAS build's.
    """
    zstd = shutil.which("zstd")
    if not zstd:
        sys.exit(f"recompressing {src.name} needs a 'zstd' executable on PATH")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with (
        subprocess.Popen(
            [zstd, "-d", "-c", "--", str(src)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc,
        lzma.open(tmp, "wb", preset=6) as out,
    ):
        assert proc.stdout is not None
        shutil.copyfileobj(proc.stdout, out, 1 << 20)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        sys.exit(f"zstd exited {proc.returncode} while reading {src.name}")
    tmp.replace(dest)


#: Where onnxruntime-node keeps its linux-x64 native files inside the engine.
ONNX_X64_DIR = "node_modules/onnxruntime-node/bin/napi-v3/linux/x64/"


def read_onnx_overlay(path: Path, want_sha256: str) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    """The overlay tarball's members by name: flat regular files and symlinks only.

    ``packaging/kas-al2/build-onnxruntime.sh`` writes it: ``libonnxruntime.so.1``,
    the ``libonnxruntime.so.<ver>`` symlink and ``onnxruntime_binding.node``.
    """
    _expect_sha256(path, want_sha256, "--onnx-overlay-linux-x64")
    entries: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(path, "r:*") as tar:
        for m in tar.getmembers():
            name = m.name.removeprefix("./")
            if not name or "/" in name or name in ("..", "."):
                sys.exit(f"overlay member {m.name!r} must be a bare file name")
            if m.issym():
                if "/" in m.linkname:
                    sys.exit(f"overlay symlink {name} -> {m.linkname} must stay in its directory")
                entries[name] = (m, b"")
            elif m.isfile():
                fh = tar.extractfile(m)
                assert fh is not None
                entries[name] = (m, fh.read())
            else:
                sys.exit(f"overlay member {name} is neither a file nor a symlink")
    if not entries:
        sys.exit(f"{path.name}: overlay is empty")
    return entries


def _repack_with_overlay(
    src: Path, dest: Path, overlay: dict[str, tuple[tarfile.TarInfo, bytes]]
) -> None:
    """Rewrite *src* (``.tar.zst``) as ``.tar.xz`` with onnxruntime's x64 files swapped.

    Unlike :func:`_recompress_zst_to_xz` the tar stream IS re-packed, so the
    result is not byte-identical inside: every member keeps its name, mode,
    owner and mtime, and only the overlaid ones get new contents (a regular file
    stays a regular file, a symlink a symlink, or this refuses). Every overlay
    name must replace at least one member.
    """
    zstd = shutil.which("zstd")
    if not zstd:
        sys.exit(f"repacking {src.name} needs a 'zstd' executable on PATH")
    hits = dict.fromkeys(overlay, 0)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with (
        subprocess.Popen(
            [zstd, "-d", "-c", "--", str(src)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc,
        lzma.open(tmp, "wb", preset=6) as raw,
    ):
        assert proc.stdout is not None
        with (
            tarfile.open(fileobj=proc.stdout, mode="r|") as tin,
            tarfile.open(fileobj=raw, mode="w|", format=tin.format) as tout,
        ):
            for m in tin:
                head, _, base = m.name.removeprefix("./").rpartition("/")
                in_dir = head + "/" == ONNX_X64_DIR or (head + "/").endswith("/" + ONNX_X64_DIR)
                if in_dir:
                    if base in overlay:
                        new, data = overlay[base]
                        if new.issym() != m.issym() or new.isfile() != m.isfile():
                            sys.exit(f"{m.name}: overlay changes the member's type")
                        hits[base] += 1
                        out = tarfile.TarInfo(m.name)
                        for attr in ("mode", "uid", "gid", "uname", "gname", "mtime", "type"):
                            setattr(out, attr, getattr(m, attr))
                        if m.issym():
                            out.linkname = new.linkname
                            tout.addfile(out)
                        else:
                            out.size = len(data)
                            tout.addfile(out, io.BytesIO(data))
                        continue
                tout.addfile(m, tin.extractfile(m) if m.isfile() else None)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        sys.exit(f"zstd exited {proc.returncode} while reading {src.name}")
    missing = sorted(n for n, c in hits.items() if c == 0)
    if missing:
        tmp.unlink(missing_ok=True)
        sys.exit(f"{src.name}: no {ONNX_X64_DIR}{', '.join(missing)} to replace")
    tmp.replace(dest)


def _xz_is_complete(path: Path) -> bool:
    """True when *path* decompresses to its end without error."""
    try:
        with lzma.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
    except (OSError, lzma.LZMAError, EOFError):
        return False
    return True


def engine_platforms(
    kas_dir: Path,
    out_dir: Path,
    onnx_overlay_x64: tuple[dict[str, tuple[tarfile.TarInfo, bytes]], str] | None = None,
    sources: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Write one ``.tar.xz`` per engine platform; fill *sources* for provenance.

    *onnx_overlay_x64* is ``(read_onnx_overlay(...), its sha256)``: applied to
    ``linux-x64`` only, after that tarball passed the upstream manifest's sha256.
    """
    sources = {} if sources is None else sources
    platforms: dict[str, dict] = {}
    upstream = kas_dir / "manifest.json"
    declared = {}
    if upstream.is_file():
        declared = json.loads(upstream.read_text(encoding="utf-8")).get("platforms") or {}
    for key in ENGINE_PLATFORMS:
        name = (declared.get(key) or {}).get("file") or f"kas-{key}.tar.zst"
        src = kas_dir / name
        if not src.is_file():
            print(f"  ! no engine tarball for {key} ({src.name}); skipping that platform")
            continue
        if declared.get(key) and declared[key].get("sha256") != sha256_of(src):
            sys.exit(f"{name}: sha256 differs from the upstream manifest.json beside it")
        overlay = onnx_overlay_x64 if key == "linux-x64" else None
        if overlay is not None and not name.endswith(".tar.zst"):
            sys.exit(f"--onnx-overlay-linux-x64 needs the KAS build's .tar.zst, got {name}")
        src_sha = sha256_of(src)
        sources[key] = {"engine_upstream": name, "engine_upstream_sha256": src_sha}
        if name.endswith(".tar.zst"):
            dest = out_dir / (name[: -len(".tar.zst")] + ".tar.xz")
            # An xz already at the destination is kept only if it is a complete
            # archive AND was made from this exact source (and overlay): the
            # sidecar records them at recompression time, so a run against a
            # refreshed upstream drop (same filename, new bytes) regenerates
            # instead of pinning the previous drop's archive under the new manifest.
            made_from = src_sha if overlay is None else f"{src_sha} onnx-overlay {overlay[1]}"
            sidecar = dest.with_suffix(dest.suffix + ".src-sha256")
            if dest.exists():
                stale = not _xz_is_complete(dest)
                if stale:
                    print(f"  ! {dest.name} is truncated; regenerating")
                elif not (sidecar.is_file() and sidecar.read_text().strip() == made_from):
                    stale = True
                    print(f"  ! {dest.name} was made from a different input; regenerating")
                if stale:
                    dest.unlink()
                    sidecar.unlink(missing_ok=True)
            if not dest.exists():
                if overlay is None:
                    print(f"  ~ {name} -> {dest.name} (recompressing; no stdlib zstd on 3.12)")
                    _recompress_zst_to_xz(src, dest)
                else:
                    print(f"  ~ {name} -> {dest.name} (repacking with the onnxruntime overlay)")
                    _repack_with_overlay(src, dest, overlay[0])
                sidecar.write_text(made_from + "\n")
            if overlay is not None:
                sources[key]["onnx_overlay_sha256"] = overlay[1]
                sources[key]["onnx_overlay_replaced"] = [
                    ONNX_X64_DIR + n for n in sorted(overlay[0])
                ]
        else:
            dest = out_dir / name
            # Through a sibling and one rename, so an interrupted copy never leaves a
            # partial file at the final name for a rerun to pin; and a destination that
            # is already there is checked against its source, never trusted.
            if dest.exists() and sha256_of(dest) != sha256_of(src):
                dest.unlink()
            if not dest.exists():
                tmp = dest.with_suffix(dest.suffix + ".part")
                shutil.copyfile(src, tmp)
                tmp.replace(dest)
        platforms[key] = ref(dest)
        print(f"  ✓ {dest.name}")
    if not platforms:
        sys.exit(f"no kas-*.tar.zst found in {kas_dir}")
    if onnx_overlay_x64 is not None and "linux-x64" not in platforms:
        sys.exit("--onnx-overlay-linux-x64 given but there is no linux-x64 engine tarball")
    return platforms


def upload(out_root: Path, version: str, s3_base: str, *, dry_run: bool) -> None:
    s3_base = s3_base.rstrip("/")
    release = out_root / "kas" / version
    # The .src-sha256 sidecars are local recompression bookkeeping, not release files.
    files = sorted(
        p for p in release.iterdir() if p.is_file() and not p.name.endswith(".src-sha256")
    )
    cmds = [["aws", "s3", "cp", str(p), f"{s3_base}/kas/{version}/{p.name}"] for p in files]
    for cmd in cmds:
        print("  $", " ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--kas-dir", required=True, type=Path, help="directory holding kas-*.tar.zst")
    ap.add_argument("--version", required=True, help="engine version, e.g. 0.66.33")
    ap.add_argument("--node-version", required=True, help="Node version to bundle, e.g. 22.23.3")
    ap.add_argument("--out", required=True, type=Path, help="output root (gets kas/<version>/)")
    ap.add_argument("--package", default="@kiro/agent")
    ap.add_argument("--s3", default=None, help="s3://bucket[/prefix] to upload to with the aws CLI")
    ap.add_argument("--dry-run", action="store_true", help="print the upload commands only")
    ap.add_argument(
        "--node-linux-x64",
        type=Path,
        help="local node-v<N>-linux-x64.tar.xz to publish instead of nodejs.org's (e.g. an AL2 build)",
    )
    ap.add_argument("--node-linux-x64-sha256", help="expected sha256 of --node-linux-x64")
    ap.add_argument(
        "--onnx-overlay-linux-x64",
        type=Path,
        help="tarball of onnxruntime-node linux/x64 files to swap into the linux-x64 engine",
    )
    ap.add_argument("--onnx-overlay-sha256", help="expected sha256 of --onnx-overlay-linux-x64")
    args = ap.parse_args()
    for path, sha, flag in (
        (args.node_linux_x64, args.node_linux_x64_sha256, "--node-linux-x64"),
        (args.onnx_overlay_linux_x64, args.onnx_overlay_sha256, "--onnx-overlay-linux-x64"),
    ):
        if (path is None) != (sha is None):
            ap.error(f"{flag} and its sha256 flag go together")
        if path is not None and not path.is_file():
            ap.error(f"{flag}: no file at {path}")

    release = args.out / "kas" / args.version
    release.mkdir(parents=True, exist_ok=True)

    overlay = None
    if args.onnx_overlay_linux_x64 is not None:
        overlay = (
            read_onnx_overlay(args.onnx_overlay_linux_x64, args.onnx_overlay_sha256),
            args.onnx_overlay_sha256,
        )
    sources: dict[str, dict] = {}
    print(f"Engine tarballs ({args.version}):")
    platforms = engine_platforms(args.kas_dir, release, overlay, sources)
    print(f"Node runtime ({args.node_version}):")
    local_node = None
    if args.node_linux_x64 is not None:
        local_node = (args.node_linux_x64, args.node_linux_x64_sha256)
    builds = node_builds(args.node_version, release, local_node)

    manifest = {
        "version": args.version,
        "package": args.package,
        "platforms": platforms,
        "node": {"version": args.node_version, "builds": builds},
    }
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {release / 'manifest.json'}")
    provenance = {
        "version": args.version,
        "publisher": f"scripts/kas_bundle_publish.py at {_script_commit()}",
        "engine": sources,
        "node": {
            key: (
                "local --node-linux-x64"
                if key == "linux-x64" and local_node is not None
                else f"{NODE_DIST}/v{args.node_version}/ (SHASUMS256.txt)"
            )
            for key in builds
        },
    }
    (release / PROVENANCE_NAME).write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {release / PROVENANCE_NAME}")
    print("Pin it in src/kiro_crew/acp/kas_pin.py (after testing):")
    print(f'  KAS_PIN_VERSION = "{args.version}"')
    print(f'  KAS_PIN_MANIFEST_SHA256 = "{sha256_of(release / "manifest.json")}"')
    if local_node is not None and overlay is not None:
        print('  KAS_PIN_LINUX_GLIBC_FLOOR["linux-x64"] = (2, 26)  # only if both are AL2 builds')
    if args.s3:
        print("Upload:")
        upload(args.out, args.version, args.s3, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
