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
import json
import lzma
import re
import shutil
import subprocess
import sys
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


def node_builds(version: str, out_dir: Path) -> dict[str, dict]:
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


def _xz_is_complete(path: Path) -> bool:
    """True when *path* decompresses to its end without error."""
    try:
        with lzma.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
    except (OSError, lzma.LZMAError, EOFError):
        return False
    return True


def engine_platforms(kas_dir: Path, out_dir: Path) -> dict[str, dict]:
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
        if name.endswith(".tar.zst"):
            dest = out_dir / (name[: -len(".tar.zst")] + ".tar.xz")
            # An xz already at the destination is kept only if it is a complete
            # archive AND was made from this exact source: the sidecar records the
            # zst's sha256 at recompression time, so a run against a refreshed
            # upstream drop (same filename, new bytes) regenerates instead of
            # pinning the previous drop's archive under the new manifest.
            src_sha = sha256_of(src)
            sidecar = dest.with_suffix(dest.suffix + ".src-sha256")
            if dest.exists():
                stale = not _xz_is_complete(dest)
                if stale:
                    print(f"  ! {dest.name} is truncated; regenerating")
                elif not (sidecar.is_file() and sidecar.read_text().strip() == src_sha):
                    stale = True
                    print(f"  ! {dest.name} was made from a different {name}; regenerating")
                if stale:
                    dest.unlink()
                    sidecar.unlink(missing_ok=True)
            if not dest.exists():
                print(
                    f"  ~ {name} -> {dest.name} (recompressing; zstd has no stdlib reader on 3.12)"
                )
                _recompress_zst_to_xz(src, dest)
                sidecar.write_text(src_sha + "\n")
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
    args = ap.parse_args()

    release = args.out / "kas" / args.version
    release.mkdir(parents=True, exist_ok=True)

    print(f"Engine tarballs ({args.version}):")
    platforms = engine_platforms(args.kas_dir, release)
    print(f"Node runtime ({args.node_version}):")
    builds = node_builds(args.node_version, release)

    manifest = {
        "version": args.version,
        "package": args.package,
        "platforms": platforms,
        "node": {"version": args.node_version, "builds": builds},
    }
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {release / 'manifest.json'}")
    print("Pin it in src/kiro_crew/acp/kas_pin.py (after testing):")
    print(f'  KAS_PIN_VERSION = "{args.version}"')
    print(f'  KAS_PIN_MANIFEST_SHA256 = "{sha256_of(release / "manifest.json")}"')
    if args.s3:
        print("Upload:")
        upload(args.out, args.version, args.s3, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
