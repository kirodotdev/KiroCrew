#!/usr/bin/env python3
"""Build and check the per-platform Kiro Crew wheels that carry the pinned KAS engine.

``build`` takes the pure ``<stem>-<v>-py3-none-any.whl`` the release just built and
writes ``<stem>-<v>-py3-none-<tag>.whl``: the same files plus the engine tree this
release pins, at ``kiro_crew/_kas_engine/<pin>/`` (the layout ``kirocrew kas
install`` lays down under ``engines/kas/<pin>/``). The tree is staged by the
backend's own installer (``kas_bundle.install_bundle``, every byte checked against
the pinned manifest) and trimmed by ``kas_bundle.trim_for_shipping``, exactly as
``packaging/build-desktop.sh`` stages the desktop app's copy. Run it on the host
the wheel is for (``build-platform-wheels.yml``'s matrix): the installer runs the
staged Node once, so a build that cannot run its own engine fails here.

``verify`` is the release gate: it opens a platform wheel and fails unless it holds
the pinned engine and a Node for the right host, with the recorded digests equal to
the pinned manifest's. With ``--pure`` it also fails unless every file outside the
engine is byte-identical to the pure wheel it was built from.

Only the standard library and the backend under ``src/`` are used. Example::

    python3 scripts/kas_platform_wheel.py build dist/kirocrew-0.14.0-py3-none-any.whl \\
        --tag manylinux_2_28_x86_64 --out dist/
    python3 scripts/kas_platform_wheel.py verify dist/kirocrew-0.14.0-py3-none-manylinux_2_28_x86_64.whl
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiro_crew.acp import kas_bundle  # noqa: E402
from kiro_crew.acp.kas_pin import KAS_PIN_MANIFEST_SHA256, KAS_PIN_VERSION  # noqa: E402
from kiro_crew.config.sections import KAS_BUNDLE_DEFAULT_URL  # noqa: E402

#: Wheel platform tag -> (platform.system(), platform.machine()) the engine is staged for.
#: glibc 2.28 is the bundled Node's own floor (``kas_bundle._require_loadable_glibc``);
#: macOS 11 is Node 22's.
PLATFORM_TAGS: dict[str, tuple[str, str]] = {
    "manylinux_2_28_x86_64": ("Linux", "x86_64"),
    "manylinux_2_28_aarch64": ("Linux", "aarch64"),
    "macosx_11_0_arm64": ("Darwin", "arm64"),
    "macosx_11_0_x86_64": ("Darwin", "x86_64"),
}
# shortcut: no win_amd64 wheel yet. Windows has no managed-venv update route and its
# deep node_modules paths need long-path support in pip's target; it keeps the pure
# wheel + `kirocrew kas install`. Add it once a Windows install updates from a feed.

_PURE_WHEEL_RE = re.compile(
    r"^(?P<stem>kirocrew(?:_lts)?)-(?P<version>[A-Za-z0-9][A-Za-z0-9._+]{0,127})-py3-none-any\.whl$"
)
_PLATFORM_WHEEL_RE = re.compile(
    r"^(?P<stem>kirocrew(?:_lts)?)-(?P<version>[A-Za-z0-9][A-Za-z0-9._+]{0,127})"
    r"-py3-none-(?P<tag>[a-z0-9_]+)\.whl$"
)
_ENGINE_PREFIX = f"kiro_crew/{kas_bundle.PACKAGED_KAS_DIR_NAME}"


class GateError(Exception):
    """The artifact does not carry the engine this release pins."""


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _flatten_links(root: Path) -> int:
    """Replace every symlink under *root* with a copy of its target; return how many.

    A wheel is a zip, and pip writes no symlinks out of one. A link whose target is
    outside *root*, missing, or a directory is refused: copying it would ship bytes
    the pinned manifest never named.
    """
    count = 0
    real_root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            target = path.resolve()
            if real_root not in target.parents or not target.is_file():
                raise GateError(f"{path.relative_to(root)} links outside the engine tree")
            path.unlink()
            shutil.copy2(target, path)
            count += 1
    return count


def stage_engine(dest: Path, tag: str, base_url: str) -> kas_bundle.InstalledKasBundle:
    """Stage and trim the pinned engine for *tag* under *dest*/<pin>/."""
    system, machine = PLATFORM_TAGS[tag]
    manifest, url = kas_bundle.fetch_manifest(
        base_url, version=KAS_PIN_VERSION, sha256=KAS_PIN_MANIFEST_SHA256
    )
    installed = kas_bundle.install_bundle(
        manifest, source=url, root=dest, system=system, machine=machine
    )
    freed = kas_bundle.trim_for_shipping(installed)
    links = _flatten_links(installed.root)
    print(
        f"  staged KAS {installed.version} for {tag}; trimmed {freed / 1048576:.0f} MiB, {links} link(s) copied"
    )
    return installed


def _entry_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) | stat.S_IFREG


def build(pure_wheel: Path, tag: str, out_dir: Path, base_url: str) -> Path:
    """Write the *tag* platform wheel next to *pure_wheel*'s contents; return its path."""
    match = _PURE_WHEEL_RE.match(pure_wheel.name)
    if not match:
        raise GateError(f"{pure_wheel.name} is not a pure Kiro Crew wheel")
    if tag not in PLATFORM_TAGS:
        raise GateError(f"unknown platform tag {tag!r} (known: {', '.join(PLATFORM_TAGS)})")
    stem, version = match["stem"], match["version"]
    dist_info = f"{stem}-{version}.dist-info"
    out = out_dir / f"{stem}-{version}-py3-none-{tag}.whl"

    with tempfile.TemporaryDirectory(prefix="kas-wheel-") as tmp:
        engine_root = Path(tmp) / "engine"
        installed = stage_engine(engine_root, tag, base_url)
        records: list[str] = []
        part = out.with_suffix(".whl.part")
        with (
            zipfile.ZipFile(pure_wheel) as src,
            zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as dst,
        ):
            for info in src.infolist():
                name = info.filename
                if name.startswith(_ENGINE_PREFIX + "/"):
                    raise GateError(f"{pure_wheel.name} already carries {name}")
                if name == f"{dist_info}/RECORD":
                    continue
                data = src.read(info)
                if name == f"{dist_info}/WHEEL":
                    lines = [
                        ln for ln in data.decode("utf-8").splitlines() if not ln.startswith("Tag:")
                    ]
                    lines.append(f"Tag: py3-none-{tag}")
                    data = ("\n".join(lines) + "\n").encode("utf-8")
                    info = zipfile.ZipInfo(name, date_time=info.date_time)
                    info.external_attr = (0o644 | stat.S_IFREG) << 16
                dst.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                records.append(f"{name},{_record_hash(data)},{len(data)}")
            home = installed.root
            for dirpath, dirnames, filenames in os.walk(home):
                dirnames.sort()
                for fname in sorted(filenames):
                    path = Path(dirpath) / fname
                    rel = PurePosixPath(_ENGINE_PREFIX, home.name, *path.relative_to(home).parts)
                    data = path.read_bytes()
                    zinfo = zipfile.ZipInfo(str(rel), date_time=(2020, 1, 1, 0, 0, 0))
                    zinfo.external_attr = _entry_mode(path) << 16
                    dst.writestr(zinfo, data, compress_type=zipfile.ZIP_DEFLATED)
                    records.append(f"{rel},{_record_hash(data)},{len(data)}")
            records.append(f"{dist_info}/RECORD,,")
            dst.writestr(
                zipfile.ZipInfo(f"{dist_info}/RECORD", date_time=(2020, 1, 1, 0, 0, 0)),
                "\n".join(records) + "\n",
                compress_type=zipfile.ZIP_DEFLATED,
            )
        part.replace(out)
    print(f"  wrote {out.name} ({out.stat().st_size / 1048576:.0f} MiB)")
    return out


def _pinned_digests(base_url: str, system: str, machine: str) -> tuple[str, str, str]:
    """``(engine_sha256, node_sha256, node_key)`` the pinned manifest names for a host."""
    manifest, _url = kas_bundle.fetch_manifest(
        base_url, version=KAS_PIN_VERSION, sha256=KAS_PIN_MANIFEST_SHA256
    )
    engine = manifest.engine_for(kas_bundle.host_platform_key(system, machine))
    node_key = kas_bundle.host_node_key(system, machine)
    return engine.sha256, manifest.node_for(node_key).sha256, node_key


def _check_record(
    record: dict, names: set[str], *, prefix: str, want: tuple[str, str, str]
) -> None:
    engine_sha, node_sha, node_key = want
    if record.get("version") != KAS_PIN_VERSION:
        raise GateError(
            f"engine is KAS {record.get('version')!r}, this release pins {KAS_PIN_VERSION}"
        )
    if record.get("engine_sha256") != engine_sha:
        raise GateError("engine_sha256 does not match the pinned manifest")
    if record.get("node_key") != node_key or record.get("node_sha256") != node_sha:
        raise GateError(
            f"Node is {record.get('node_key')!r}, expected {node_key} from the pinned manifest"
        )
    for key in ("acp_server", "node_bin"):
        rel = record.get(key)
        if not isinstance(rel, str) or f"{prefix}/{rel}" not in names:
            raise GateError(f"install.json names {key}={rel!r}, which is not in the artifact")


def _check_same_as_pure(zf: zipfile.ZipFile, pure: Path, dist_info: str) -> None:
    """Every file outside the engine is the pure wheel's file, byte for byte.

    ``RECORD`` and ``WHEEL`` are rewritten by :func:`build`, so they are the two
    exceptions; the tag line is checked by the caller.
    """
    skip = {f"{dist_info}/RECORD", f"{dist_info}/WHEEL"}
    with zipfile.ZipFile(pure) as base:
        want = {n for n in base.namelist() if n not in skip}
        have = {
            n for n in zf.namelist() if n not in skip and not n.startswith(_ENGINE_PREFIX + "/")
        }
        if want != have:
            extra, missing = sorted(have - want)[:3], sorted(want - have)[:3]
            raise GateError(f"files differ from the pure wheel (extra {extra}, missing {missing})")
        for name in sorted(want):
            if zf.read(name) != base.read(name):
                raise GateError(f"{name} differs from the pure wheel")


def verify_wheel(wheel: Path, base_url: str, *, pure: Path | None = None) -> str:
    match = _PLATFORM_WHEEL_RE.match(wheel.name)
    if not match or match["tag"] not in PLATFORM_TAGS:
        raise GateError(f"{wheel.name} is not a Kiro Crew platform wheel")
    system, machine = PLATFORM_TAGS[match["tag"]]
    prefix = f"{_ENGINE_PREFIX}/{KAS_PIN_VERSION}"
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        try:
            record = json.loads(zf.read(f"{prefix}/{kas_bundle.KAS_BUNDLE_INSTALL_RECORD_NAME}"))
        except KeyError:
            raise GateError(f"{wheel.name} carries no KAS {KAS_PIN_VERSION} engine") from None
        if not isinstance(record, dict):
            raise GateError("install.json is not an object")
        _check_record(
            record,
            names,
            prefix=prefix,
            want=_pinned_digests(base_url, system, machine),
        )
        node = zf.getinfo(f"{prefix}/{record['node_bin']}")
        if system != "Windows" and not (node.external_attr >> 16) & stat.S_IXUSR:
            raise GateError("the bundled node is not executable")
        tag_line = f"Tag: py3-none-{match['tag']}"
        wheel_meta = zf.read(f"{match['stem']}-{match['version']}.dist-info/WHEEL").decode()
        if tag_line not in wheel_meta.splitlines():
            raise GateError(f"WHEEL metadata does not say {tag_line!r}")
        if pure is not None:
            _check_same_as_pure(zf, pure, f"{match['stem']}-{match['version']}.dist-info")
    return f"{wheel.name}: KAS {KAS_PIN_VERSION} + Node {record.get('node_version')} ({record['node_key']})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--kas-base-url", default=KAS_BUNDLE_DEFAULT_URL)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="Write one platform wheel from the pure wheel")
    b.add_argument("pure_wheel", type=Path)
    b.add_argument("--tag", required=True, choices=sorted(PLATFORM_TAGS))
    b.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify", help="Fail unless each platform wheel carries the pinned engine")
    v.add_argument("wheels", type=Path, nargs="+")
    v.add_argument(
        "--pure",
        type=Path,
        default=None,
        help="The pure wheel each platform wheel was built from; every file outside "
        "the engine must be byte-identical to it",
    )
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            args.out.mkdir(parents=True, exist_ok=True)
            build(args.pure_wheel, args.tag, args.out, args.kas_base_url)
        else:
            for wheel in args.wheels:
                print(f"✓ {verify_wheel(wheel, args.kas_base_url, pure=args.pure)}")
    except (GateError, kas_bundle.KasBundleError, OSError, zipfile.BadZipFile) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
