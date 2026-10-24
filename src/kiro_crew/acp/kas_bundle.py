"""The KAS engine bundle Kiro Crew installs and runs itself.

The KAS relay (:mod:`kiro_crew.acp.kas_transport`) reaches the engine through a
kiro-cli process, so an operator needs kiro-cli installed and kiro-cli decides
which engine build runs. This module is the other route: Kiro Crew downloads a
pinned ``@kiro/agent`` build plus a Node runtime from a manifest it trusts,
installs both under its own data home, and the KAS harness spawns
``node .../acp-server.js`` directly. Nothing of kiro-cli's is involved -- not
its binary, not its on-disk ``kas/`` layout, not its credential store. The
engine asks Kiro Crew for its token (``_kiro/auth/getAccessToken``) and Kiro
Crew answers from its own vault (:mod:`kiro_crew.acp.kas_host_auth`), which is
the only auth owner this route has.

What is on disk, under ``<data home>/engines/kas/``::

    0.66.33/
      install.json                what was installed, and where its two entry points are
      bundle/node_modules/@kiro/agent/dist/server/acp-server.js
      node/node-v22.20.0-linux-x64/bin/node

The desktop app stages the same layout into its resources. A universal Mac app
stages it once, with a Node binary holding both architectures
(:func:`make_universal_node`), trimmed to what runs (:func:`trim_for_shipping`).

The ``engines`` leaf is sealed READ-ONLY inside Kiro Crew's sandbox
(``sandbox._CREW_READONLY_LEAVES``) and write-protected at the file-tool gate
(``security.paths._WRITE_PROTECTED_HOME_PATHS``): a sandboxed agent may read
and execute the engine but cannot rewrite the code its next session runs.
Installation happens in the gateway process (``kirocrew kas install``), outside
both.

Which version runs is not a file on disk: it is the Kiro Crew release's pin
(:mod:`kiro_crew.acp.kas_pin`), and :func:`installed_bundle` looks up exactly
that version's directory. A Kiro Crew update therefore switches engines by
switching which directory is asked for -- the old version's directory stays,
so a process of the previous release still running keeps its engine, and a
rollback finds its engine still installed.

The manifest's own sha256 is pinned beside the version, which makes the
manifest the trust root for everything else. Its shape::

    {
      "version": "0.66.33",
      "package": "@kiro/agent",
      "platforms": {
        "linux-x64": {"file": "kas-linux-x64.tar.xz", "sha256": "...", "size": 25630996},
        ...
      },
      "node": {
        "version": "22.20.0",
        "builds": {
          "linux-x64": {"file": "node-v22.20.0-linux-x64.tar.xz", "sha256": "...", "size": ...},
          ...
        }
      }
    }

``platforms`` is keyed by the engine's own build matrix (``darwin-universal``
is one JS bundle for both Mac architectures); ``node.builds`` is keyed by the
host's ``<os>-<arch>`` because Node ships one binary per architecture. Every
``file`` is a bare name resolved beside the manifest, and every artifact is
sha256-pinned with its exact size, so the transfer engine
(:mod:`kiro_crew.asset_downloader`) installs nothing it did not verify. A
manifest whose ``node`` is the string ``"electron-builtin"`` describes a build
that expected to run inside Kiro's Electron and carries no runtime of its own;
it is refused with the remedy (republish with the Node builds added).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kiro_crew import asset_downloader, platform_compat
from kiro_crew.acp.kas_pin import KAS_PIN_MANIFEST_SHA256, KAS_PIN_VERSION, pinned_version
from kiro_crew.config.paths import data_home
from kiro_crew.constants import BUNDLED_KAS_DIR_ENV

logger = logging.getLogger(__name__)

__all__ = [
    "KAS_BUNDLE_ENGINES_LEAF",
    "KAS_BUNDLE_INSTALL_RECORD_NAME",
    "KAS_BUNDLE_MANIFEST_NAME",
    "ArtifactRef",
    "InstalledKasBundle",
    "KasBundleError",
    "KasBundleManifest",
    "fetch_manifest",
    "host_node_key",
    "host_platform_key",
    "install_bundle",
    "install_pinned",
    "bundled_kas_root",
    "installed_bundle",
    "installed_versions",
    "kas_engines_root",
    "make_universal_node",
    "manifest_url_for",
    "parse_manifest",
    "trim_for_shipping",
]

#: Data-home leaf every installed engine lives under. Also named in
#: ``sandbox._CREW_READONLY_LEAVES`` and ``security.paths`` -- the three must agree.
KAS_BUNDLE_ENGINES_LEAF = "engines"

# ``BUNDLED_KAS_DIR_ENV`` (imported from ``constants``, where ``sandbox`` reads it to
# seal the directory) names the desktop app's staged engine root: set by the Electron
# shell only when the build staged the engine for this architecture
# (``packaging/build-desktop.sh``, ``BUNDLE_KAS``; ``gateway-env.js``,
# ``bundledKasEnvironment``). Like ``KIROCREW_BUNDLED_KIRO_DIR``, and read
# as a SECOND root after the data home's ``engines/kas``: an operator's own
# ``kirocrew kas install`` (or a ``--unpinned`` test install) wins over the copy the
# app was built with, exactly as ``KIROCREW_KIRO_BIN`` wins over the bundled kiro-cli.

#: Object names under ``<base>/kas/`` on the publishing host.
KAS_BUNDLE_MANIFEST_NAME = "manifest.json"

#: npm scopes a package name with "/" ("@kiro/agent"); that is npm's separator,
#: not a filesystem path, and node_modules nests one directory per segment.
_NPM_SCOPE_SEP = "/"

#: The engine's stdio ACP server, relative to the extracted bundle. Read from the
#: package's own ``bin`` map when present; this is the fallback and the check.
_ACP_SERVER_REL = Path("dist") / "server" / "acp-server.js"
_PACKAGE_BIN_NAME = "kiro-agent"

#: Manifest documents are small; a body past this is not a manifest.
_MANIFEST_MAX_BYTES = 256 * 1024
_MANIFEST_TIMEOUT_SECS = 60

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
#: A bare object name: no separators, no traversal, nothing a URL join or a path
#: join could carry out of the manifest's directory.
_FILE_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")

#: The per-version record naming what was installed and where its two entry points
#: (the engine's ACP server and the Node binary) are; ``kirocrew doctor`` prints its path.
KAS_BUNDLE_INSTALL_RECORD_NAME = "install.json"
_STAGING_PREFIX = ".staging-"


class KasBundleError(Exception):
    """A manifest, transfer, extraction or layout problem. ``str(exc)`` is the remedy."""


@dataclass(frozen=True)
class ArtifactRef:
    """One pinned object beside the manifest."""

    file: str
    sha256: str
    size: int


@dataclass(frozen=True)
class KasBundleManifest:
    """A parsed, validated manifest."""

    version: str
    package: str
    platforms: Mapping[str, ArtifactRef]
    node_version: str
    node_builds: Mapping[str, ArtifactRef]

    def engine_for(self, platform_key: str) -> ArtifactRef:
        try:
            return self.platforms[platform_key]
        except KeyError:
            raise KasBundleError(
                f"KAS {self.version} publishes no engine build for {platform_key} "
                f"(has: {', '.join(sorted(self.platforms)) or 'none'})"
            ) from None

    def node_for(self, node_key: str) -> ArtifactRef:
        try:
            return self.node_builds[node_key]
        except KeyError:
            raise KasBundleError(
                f"KAS {self.version} publishes no Node runtime for {node_key} "
                f"(has: {', '.join(sorted(self.node_builds)) or 'none'})"
            ) from None


@dataclass(frozen=True)
class InstalledKasBundle:
    """An engine on disk, ready to spawn."""

    version: str
    node_version: str
    root: Path
    node_bin: Path
    acp_server: Path
    #: True when *root* is the desktop app's own staged copy
    #: (``BUNDLED_KAS_DIR_ENV``), not the data home's ``engines/kas``.
    bundled: bool = False


# ── host keys ──────────────────────────────────────────────────────────────────


def _host_os(system: str) -> str:
    system = system.lower()
    if system == "linux":
        return "linux"
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "win32"
    raise KasBundleError(f"KAS bundles are not published for this operating system ({system})")


def _host_arch(machine: str) -> str:
    machine = machine.lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise KasBundleError(f"KAS bundles are not published for this architecture ({machine})")


def host_node_key(system: str | None = None, machine: str | None = None) -> str:
    """``<os>-<arch>`` for the Node runtime: one binary per architecture."""
    return f"{_host_os(system or platform.system())}-{_host_arch(machine or platform.machine())}"


def _require_loadable_glibc(system: str | None, machine: str | None) -> None:
    """Refuse the install on a Linux host whose glibc is too old to run the bundle.

    The published ``linux-x64`` Node build is linked against glibc 2.28, so a host
    below that floor cannot run the bundled Node at all. The engine also ships a
    native ``onnxruntime`` library (via ``@huggingface/transformers``) linked
    against glibc 2.27, so an Amazon Linux 2-era host below glibc 2.27 additionally
    cannot load that library even with an older Node swapped in. Stop here, before
    any engine or Node artifact is downloaded or extracted, with one message naming
    the found glibc, the minimum, and the reason, rather than letting ``_probe_node``
    surface the raw "GLIBC_2.28 not found" loader line after a download.

    Reuses the glibc reader and floor ``doctor``'s Node remedy uses
    (:func:`kiro_crew.constants._host_glibc_version`,
    :data:`kiro_crew.constants._OFFICIAL_NODE_GLIBC_FLOOR`). A no-op on non-Linux,
    at or above the floor, or when glibc cannot be read.
    """
    from kiro_crew import constants

    if _host_os(system or platform.system()) != "linux":
        return
    glibc = constants._host_glibc_version()
    floor = constants._OFFICIAL_NODE_GLIBC_FLOOR
    if glibc is None or glibc >= floor:
        return
    raise KasBundleError(
        f"this host's glibc ({glibc[0]}.{glibc[1]}) is older than KAS requires "
        f"(glibc {floor[0]}.{floor[1]}+): the bundled Node build needs glibc "
        f"{floor[0]}.{floor[1]}, and Amazon Linux 2-era hosts (glibc below 2.27) "
        f"also lack the glibc 2.27 the engine's onnxruntime library needs. "
        f"Move to a newer base image (Amazon Linux 2023, Ubuntu 22.04+). "
        f"Nothing was installed."
    )


def host_platform_key(system: str | None = None, machine: str | None = None) -> str:
    """The engine's platform key. Mac is one universal JS bundle for both archs."""
    os_name = _host_os(system or platform.system())
    if os_name == "darwin":
        return "darwin-universal"
    return f"{os_name}-{_host_arch(machine or platform.machine())}"


# ── manifest ───────────────────────────────────────────────────────────────────


def _artifact(section: Any, *, what: str) -> ArtifactRef:
    if not isinstance(section, Mapping):
        raise KasBundleError(f"manifest {what}: expected an object, got {type(section).__name__}")
    file = section.get("file")
    sha256 = section.get("sha256")
    size = section.get("size")
    if not isinstance(file, str) or not _FILE_NAME_RE.match(file):
        raise KasBundleError(f"manifest {what}: 'file' must be a bare object name, got {file!r}")
    if not isinstance(sha256, str) or not _SHA256_RE.match(sha256.lower()):
        raise KasBundleError(f"manifest {what}: 'sha256' must be 64 hex digits")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise KasBundleError(f"manifest {what}: 'size' must be a positive byte count")
    return ArtifactRef(file=file, sha256=sha256.lower(), size=size)


def _artifact_map(section: Any, *, what: str) -> dict[str, ArtifactRef]:
    if not isinstance(section, Mapping) or not section:
        raise KasBundleError(f"manifest {what}: expected a non-empty object")
    out: dict[str, ArtifactRef] = {}
    for key, value in section.items():
        if not isinstance(key, str) or not _FILE_NAME_RE.match(key):
            raise KasBundleError(f"manifest {what}: bad platform key {key!r}")
        out[key] = _artifact(value, what=f"{what}[{key}]")
    return out


def parse_manifest(data: Any) -> KasBundleManifest:
    """Validate a decoded manifest document. Raises :class:`KasBundleError`."""
    if not isinstance(data, Mapping):
        raise KasBundleError("manifest: expected a JSON object at the top level")
    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise KasBundleError(f"manifest: bad 'version' {version!r}")
    package = data.get("package", "@kiro/agent")
    if not isinstance(package, str) or not package or ".." in package or package.startswith("/"):
        raise KasBundleError(f"manifest: bad 'package' {package!r}")
    platforms = _artifact_map(data.get("platforms"), what="platforms")

    node = data.get("node")
    if isinstance(node, str):
        raise KasBundleError(
            f"manifest for KAS {version} carries no Node runtime (node={node!r}): this "
            "build expected to run inside Kiro's own Electron. Republish it with "
            "'node.builds' added (scripts/kas_bundle_publish.py does that)."
        )
    if not isinstance(node, Mapping):
        raise KasBundleError("manifest: 'node' must be an object with 'version' and 'builds'")
    node_version = node.get("version")
    if not isinstance(node_version, str) or not _VERSION_RE.match(node_version):
        raise KasBundleError(f"manifest: bad node 'version' {node_version!r}")
    node_builds = _artifact_map(node.get("builds"), what="node.builds")
    return KasBundleManifest(
        version=version,
        package=package,
        platforms=platforms,
        node_version=node_version,
        node_builds=node_builds,
    )


def _require_https(url: str, *, what: str) -> None:
    if not url.lower().startswith("https://"):
        raise KasBundleError(
            f"{what} must be an https:// url, got {asset_downloader.redact_url(url)}"
        )


def manifest_url_for(base_url: str, version: str) -> str:
    """``<base>/kas/<version>/manifest.json``. Every caller names a version: the
    harness runs the release's pin, so there is no "current" pointer to follow."""
    base = base_url.rstrip("/")
    if not _VERSION_RE.match(version):
        raise KasBundleError(f"bad version {version!r}")
    return f"{base}/kas/{version}/{KAS_BUNDLE_MANIFEST_NAME}"


def _read_json_url(url: str, opener: Any, *, sha256: str | None = None) -> Any:
    _require_https(url, what="manifest url")
    try:
        with opener.open(url, timeout=_MANIFEST_TIMEOUT_SECS) as resp:
            body = resp.read(_MANIFEST_MAX_BYTES + 1)
    except (OSError, ValueError) as exc:
        # ValueError is urllib's answer to a url it cannot parse (an unclosed
        # IPv6 bracket in a hand-typed --url), the same refusal as an unreachable one.
        raise KasBundleError(f"could not fetch {asset_downloader.redact_url(url)}: {exc}") from exc
    if len(body) > _MANIFEST_MAX_BYTES:
        raise KasBundleError(f"{asset_downloader.redact_url(url)} is too large to be a manifest")
    if sha256 is not None and hashlib.sha256(body).hexdigest() != sha256:
        raise KasBundleError(
            f"{asset_downloader.redact_url(url)} does not match the sha256 this Kiro Crew "
            "release pins for it; refusing to install. The publishing host was changed "
            "after this release was cut -- install a matching Kiro Crew, or pass "
            "--unpinned to accept the host's manifest as-is"
        )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise KasBundleError(f"{asset_downloader.redact_url(url)} is not JSON: {exc}") from exc


def fetch_manifest(
    base_url: str,
    *,
    version: str,
    sha256: str | None = None,
    opener: Any | None = None,
) -> tuple[KasBundleManifest, str]:
    """Fetch ``<base>/kas/<version>/manifest.json``; return it with the url it came from.

    With *sha256*, the manifest's raw bytes must hash to it before anything is
    parsed. The returned url is what artifact names are resolved beside.
    """
    _require_https(base_url, what="KAS bundle base url")
    opener = opener or asset_downloader.build_opener()
    url = manifest_url_for(base_url, version)
    manifest = parse_manifest(_read_json_url(url, opener, sha256=sha256))
    if manifest.version != version:
        raise KasBundleError(
            f"asked for KAS {version} but the manifest at that path says {manifest.version}"
        )
    return manifest, url


# ── install ────────────────────────────────────────────────────────────────────


def kas_engines_root(home: Path | None = None) -> Path:
    """``<data home>/engines/kas``."""
    return (home or data_home()) / KAS_BUNDLE_ENGINES_LEAF / "kas"


def bundled_kas_root() -> Path | None:
    """The desktop app's staged engine root, or ``None`` when not bundled.

    Read from ``BUNDLED_KAS_DIR_ENV``; whitespace-only and unset both read as "not
    bundled", the blank an unbundled build leaves behind.
    """
    value = os.environ.get(BUNDLED_KAS_DIR_ENV, "").strip()
    return Path(value) if value else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_artifact(
    ref: ArtifactRef,
    *,
    source: str,
    dest: Path,
    label: str,
    on_progress: Callable[[str, int, int], None] | None,
) -> None:
    """Put the verified bytes of *ref* at *dest*.

    *source* is the manifest's own location: an ``https://.../manifest.json`` url
    (the artifact is fetched from beside it, sha256-pinned by the transfer engine)
    or a local directory (the artifact is copied from beside it and hashed here).
    """
    if source.lower().startswith("https://"):
        url = urllib.parse.urljoin(source, ref.file)

        def _progress(done: int, total: int) -> None:
            if on_progress is not None:
                on_progress(label, done, total or ref.size)

        ok, err = asset_downloader.download_to(
            dest,
            url,
            sha256=ref.sha256,
            size=ref.size,
            resume=True,
            label=label,
            on_progress=_progress,
        )
        if not ok:
            raise KasBundleError(err)
        return

    # Copy FIRST, then verify the copy. Hashing the source and copying it
    # afterwards is two reads of a file in a directory the installer does not
    # own, and the bytes could change between them; the staged copy is in the
    # gateway-owned staging directory, so what is hashed is what is extracted.
    src = Path(source) / ref.file
    if not src.is_file():
        raise KasBundleError(f"{label}: {src} is not a file")
    shutil.copyfile(src, dest)
    size = dest.stat().st_size
    if size != ref.size:
        raise KasBundleError(f"{label}: {src} is {size} bytes, manifest says {ref.size}")
    if _sha256_file(dest) != ref.sha256:
        raise KasBundleError(f"{label}: sha256 of {src} does not match the manifest")
    if on_progress is not None:
        on_progress(label, ref.size, ref.size)


@contextlib.contextmanager
def _open_tar(path: Path) -> Iterator[tarfile.TarFile]:
    """Open a ``.tar.{gz,xz,bz2,zst}`` for the duration of a ``with`` block.

    zstd goes through whichever decoder this host has: the stdlib on 3.14+, the
    ``zstandard`` package, else a ``zstd`` executable on PATH. Whatever was opened
    to feed the tar -- a file handle, a decompressor stream, a child process -- is
    closed and waited on before the block returns, so the archive directory can be
    removed right after extraction (Windows refuses to delete a file a live handle
    still holds) and a decoder that failed mid-stream is reported, not ignored.
    """
    name = path.name.lower()
    if not name.endswith((".tar.zst", ".tzst")):
        with tarfile.open(path, mode="r:*") as tf:
            yield tf
        return
    if hasattr(tarfile.TarFile, "zstopen"):  # Python 3.14+ (PEP 784)
        with tarfile.open(path, mode="r:zst") as tf:  # type: ignore[call-overload]
            yield tf
        return
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        zstandard = None
    if zstandard is not None:
        with open(path, "rb") as fh:
            with zstandard.ZstdDecompressor().stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tf:
                    yield tf
        return
    zstd = shutil.which("zstd")
    if not zstd:
        raise KasBundleError(
            f"{path.name} is zstd-compressed and this host has neither the 'zstandard' "
            "Python package nor a 'zstd' executable; install one, or publish the bundle as .tar.xz"
        )
    proc = subprocess.Popen(
        [zstd, "-d", "-c", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            yield tf
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        raise KasBundleError(f"{path.name}: zstd exited with status {returncode}")


def _extract_archive(archive: Path, into: Path) -> None:
    """Unpack *archive* under *into*, refusing members that would land outside it."""
    into.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            root = into.resolve()
            for info in zf.infolist():
                target = (into / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise KasBundleError(
                        f"{archive.name}: member {info.filename!r} escapes the archive"
                    )
            zf.extractall(into)
        return
    try:
        with _open_tar(archive) as tf:
            # "data" refuses absolute names, traversal and links out of the tree and
            # strips setuid/setgid; owner exec bits survive, which node needs.
            tf.extractall(into, filter="data")
    except tarfile.TarError as exc:
        raise KasBundleError(f"{archive.name}: {exc}") from exc


def _locate_acp_server(bundle_dir: Path, package: str) -> Path:
    pkg_dir = bundle_dir / "node_modules" / Path(*package.split(_NPM_SCOPE_SEP))
    rel = _ACP_SERVER_REL
    pkg_json = pkg_dir / "package.json"
    if pkg_json.is_file():
        try:
            bins = json.loads(pkg_json.read_text(encoding="utf-8")).get("bin")
        except (OSError, ValueError):
            bins = None
        declared = bins.get(_PACKAGE_BIN_NAME) if isinstance(bins, Mapping) else None
        if isinstance(declared, str) and declared and ".." not in declared:
            rel = Path(declared)
    server = pkg_dir / rel
    if not server.is_file():
        raise KasBundleError(f"engine bundle has no {package}/{rel.as_posix()}")
    return server


def _locate_node(node_dir: Path) -> Path:
    want = "node.exe" if platform_compat.IS_WINDOWS else "node"
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(node_dir):
        depth = len(Path(dirpath).relative_to(node_dir).parts)
        if depth > 3:
            dirnames[:] = []
            continue
        if want in filenames and (platform_compat.IS_WINDOWS or Path(dirpath).name == "bin"):
            found.append(Path(dirpath) / want)
    if len(found) != 1:
        raise KasBundleError(f"expected exactly one {want} in the Node archive, found {len(found)}")
    node = found[0]
    if not platform_compat.IS_WINDOWS:
        mode = node.stat().st_mode
        if not mode & stat.S_IXUSR:
            node.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return node


_NODE_PROBE_TIMEOUT_SECS = 30


def _probe_node(node_bin: Path, expected_version: str, *, launcher: tuple[str, ...] = ()) -> None:
    """Run the extracted Node once, the way the harness will, and check its version.

    One probe covers every way an archive can be dead on this host: the wrong
    architecture, a libc it cannot link against, an exec bit the archive lost.
    Failing here keeps a broken runtime from being published at its version's name instead of
    surfacing as a handshake timeout on the first session. *launcher* is prepended
    to the argv (``arch -x86_64``, to run one slice of a universal binary).
    """
    try:
        proc = subprocess.run(
            [*launcher, str(node_bin), "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_NODE_PROBE_TIMEOUT_SECS,
            cwd=str(node_bin.parent),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KasBundleError(
            f"the extracted Node runtime does not run on this host: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise KasBundleError(
            f"the extracted Node runtime exited {proc.returncode}"
            + (f": {detail[-1]}" if detail else "")
        )
    reported = proc.stdout.strip().lstrip("v")
    if reported != expected_version:
        raise KasBundleError(
            f"the extracted Node reports v{reported}, but the manifest says {expected_version}"
        )


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_bundle(
    manifest: KasBundleManifest,
    *,
    source: str,
    root: Path | None = None,
    force: bool = False,
    system: str | None = None,
    machine: str | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> InstalledKasBundle:
    """Install the engine and runtime *manifest* names for this host; make it current.

    *source* is where the manifest came from: its ``https://`` url, or the local
    directory holding it and its artifacts (``kirocrew kas install --from DIR``).
    Everything lands in a staging directory first and is published into place
    with one rename, so an interrupted install leaves no half-engine at the
    version's name. Raises :class:`KasBundleError`.
    """
    root = root or kas_engines_root()
    _require_loadable_glibc(system, machine)
    platform_key = host_platform_key(system, machine)
    node_key = host_node_key(system, machine)
    engine_ref = manifest.engine_for(platform_key)
    node_ref = manifest.node_for(node_key)

    final = root / manifest.version
    if final.exists() and not force:
        raise KasBundleError(
            f"KAS {manifest.version} is already installed at {final} (use --force to replace it)"
        )

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}{manifest.version}-", dir=root))
    try:
        downloads = staging / "downloads"
        downloads.mkdir()
        engine_archive = downloads / engine_ref.file
        node_archive = downloads / node_ref.file
        _fetch_artifact(
            engine_ref,
            source=source,
            dest=engine_archive,
            label="KAS engine",
            on_progress=on_progress,
        )
        _fetch_artifact(
            node_ref,
            source=source,
            dest=node_archive,
            label="Node runtime",
            on_progress=on_progress,
        )

        bundle_dir = staging / "bundle"
        node_dir = staging / "node"
        _extract_archive(engine_archive, bundle_dir)
        _extract_archive(node_archive, node_dir)
        shutil.rmtree(downloads)

        acp_server = _locate_acp_server(bundle_dir, manifest.package)
        node_bin = _locate_node(node_dir)
        _probe_node(node_bin, manifest.node_version)
        _write_json(
            staging / KAS_BUNDLE_INSTALL_RECORD_NAME,
            {
                "version": manifest.version,
                "package": manifest.package,
                "platform": platform_key,
                "node_key": node_key,
                "node_version": manifest.node_version,
                "engine_sha256": engine_ref.sha256,
                "node_sha256": node_ref.sha256,
                "acp_server": acp_server.relative_to(staging).as_posix(),
                "node_bin": node_bin.relative_to(staging).as_posix(),
            },
        )
        # Only now, with the replacement fetched, verified and probed, does an
        # existing directory at this version go, and only on --force: a fetch,
        # extraction or probe failure above leaves the working engine untouched.
        # The old tree is moved aside first so the publish rename has an absent
        # target; it is removed after the rename and restored if the rename fails.
        # Without --force nothing is retired: a directory that appeared since the
        # check at the top is a concurrent install of the same version that won,
        # and the no-replace publish turns that into a refusal, never a swap.
        retired: Path | None = None
        if force and final.exists():
            retired = Path(tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}retired-", dir=root))
            retired.rmdir()
            os.rename(final, retired)
        try:
            platform_compat.publish_dir_noreplace(staging, final)
        except FileExistsError as exc:
            if retired is not None and not final.exists():
                os.rename(retired, final)
            raise KasBundleError(
                f"KAS {manifest.version} was installed by another process while this "
                f"install ran; it is at {final} (re-run with --force to replace it)"
            ) from exc
        except BaseException:
            if retired is not None and not final.exists():
                os.rename(retired, final)
            raise
        if retired is not None:
            shutil.rmtree(retired, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    installed = installed_bundle(root=root, version=manifest.version)
    assert installed is not None
    return installed


def installed_versions(*, root: Path | None = None) -> list[str]:
    """Every engine version with a directory under *root*, sorted by name."""
    root = root or kas_engines_root()
    try:
        names = [p.name for p in root.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return []
    return sorted(n for n in names if _VERSION_RE.match(n) and not n.startswith("."))


def install_pinned(
    base_url: str,
    *,
    root: Path | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> InstalledKasBundle:
    """Install this release's pinned engine if it is not already there; return it.

    The digest check runs on the manifest's bytes before anything is parsed or
    fetched, so a host serving a different manifest under the pinned version's
    name installs nothing.
    """
    root = root or kas_engines_root()
    present = installed_bundle(root=root, version=KAS_PIN_VERSION)
    if present is not None:
        return present
    manifest, url = fetch_manifest(
        base_url, version=KAS_PIN_VERSION, sha256=KAS_PIN_MANIFEST_SHA256
    )
    # A directory at the pin's name that did not resolve above is a damaged
    # install (an interrupted --force, a hand edit): replace it rather than refuse.
    return install_bundle(
        manifest,
        source=url,
        root=root,
        force=(root / KAS_PIN_VERSION).exists(),
        on_progress=on_progress,
    )


# ── shipping a staged copy (packaging/build-desktop.sh) ─────────────────────────

#: Node keys of the two Mac architectures, and the ``arch(1)`` name of each.
_DARWIN_NODE_SLICES = {"darwin-arm64": "arm64", "darwin-x64": "x86_64"}

#: The one file of the Node distribution kept beside the binary: its license.
#: The trim lists below were checked against the engine at KAS 0.66.33 (no spawn
#: of the distribution's npm/npx/corepack, no load of the dropped files); a pin
#: bump re-reads ``@kiro/agent/dist`` for both before shipping.
_NODE_KEEP_NAMES = frozenset({"LICENSE"})
#: Engine files Node never loads: C/C++ sources and headers native addons were
#: built from, and source maps (the engine runs without ``--enable-source-maps``).
_ENGINE_DROP_SUFFIXES = (".c", ".cc", ".h", ".map")
#: Engine directories Node never loads: ``<parent>/<name>``. web-tree-sitter's
#: debug build is an export the engine does not import.
_ENGINE_DROP_DIRS = frozenset({("web-tree-sitter", "debug")})


def _read_record(home: Path) -> dict[str, Any]:
    try:
        record = json.loads((home / KAS_BUNDLE_INSTALL_RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KasBundleError(
            f"no readable {KAS_BUNDLE_INSTALL_RECORD_NAME} under {home}: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise KasBundleError(f"{home / KAS_BUNDLE_INSTALL_RECORD_NAME} is not an object")
    return record


def _run_tool(argv: list[str], *, what: str) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, encoding="utf-8", errors="replace", timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KasBundleError(f"{what}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise KasBundleError(
            f"{what} exited {proc.returncode}" + (f": {detail[-1]}" if detail else "")
        )
    return proc.stdout


def make_universal_node(
    manifest: KasBundleManifest,
    installed: InstalledKasBundle,
    *,
    source: str,
    lipo: str = "lipo",
    arch: str = "arch",
) -> InstalledKasBundle:
    """Turn a Mac install's Node into one binary that runs on both architectures.

    *installed* was laid down by :func:`install_bundle` for one Mac architecture;
    the other architecture's Node build is fetched (sha256-pinned by *manifest*,
    from *source*), and ``lipo -create`` writes one Mach-O holding both slices
    over the installed binary's path. The engine is already one darwin-universal
    tree, so the version directory then serves either architecture with no
    second copy of anything. Each slice is probed through ``arch -<name>``. The
    record is left as :func:`install_bundle` wrote it: its ``node_bin`` is the
    same path, so every reader of it is unchanged. Raises :class:`KasBundleError`.
    """
    home = installed.root
    record = _read_record(home)
    have = record.get("node_key")
    if have not in _DARWIN_NODE_SLICES:
        raise KasBundleError(
            f"{home} holds Node for {have!r}; a universal Node needs one of "
            f"{', '.join(sorted(_DARWIN_NODE_SLICES))} to start from"
        )
    other = next(k for k in _DARWIN_NODE_SLICES if k != have)
    other_ref = manifest.node_for(other)
    node_bin = installed.node_bin

    work = Path(tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}lipo-", dir=home.parent))
    try:
        archive = work / other_ref.file
        _fetch_artifact(
            other_ref,
            source=source,
            dest=archive,
            label=f"Node runtime ({other})",
            on_progress=None,
        )
        _extract_archive(archive, work / "node")
        other_bin = _locate_node(work / "node")
        merged = node_bin.with_name(f".{node_bin.name}.universal")
        want = set(_DARWIN_NODE_SLICES.values())
        try:
            _run_tool(
                [lipo, "-create", "-output", str(merged), str(node_bin), str(other_bin)],
                what="lipo -create",
            )
            archs = set(_run_tool([lipo, "-archs", str(merged)], what="lipo -archs").split())
            if not want <= archs:
                raise KasBundleError(
                    f"lipo wrote a Node with slices {sorted(archs)}, expected {sorted(want)}"
                )
            merged.chmod(0o755)
            os.replace(merged, node_bin)
        except BaseException:
            merged.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)

    for slice_name in sorted(want):
        _probe_node(node_bin, manifest.node_version, launcher=(arch, f"-{slice_name}"))
    again = _read_installed(home.parent, installed.version, bundled=installed.bundled)
    if again is None or again.node_bin != node_bin:
        raise KasBundleError(f"{home} does not read back after the universal Node was written")
    return again


def _remove(path: Path) -> int:
    """Delete *path* (a file, a link, or a whole directory); return the bytes freed."""
    if path.is_symlink() or not path.is_dir():
        size = path.lstat().st_size
        path.unlink()
        return size
    size = sum(p.lstat().st_size for p in path.rglob("*") if not p.is_dir() or p.is_symlink())
    shutil.rmtree(path)
    return size


def _prune_node_dist(node_bin: Path, node_root: Path) -> int:
    """Keep only *node_bin* and the distribution's license under *node_root*."""
    rel = node_bin.relative_to(node_root).parts
    dist_root = node_root / rel[0] if len(rel) > 1 else node_root
    keep = {node_bin} | {dist_root / name for name in _NODE_KEEP_NAMES}
    # The directories on the way down to the binary are walked, never removed.
    path_dirs = {dist_root, *(p for p in node_bin.parents if dist_root in p.parents)}
    freed = 0
    pending = [dist_root]
    while pending:
        here = pending.pop()
        for entry in here.iterdir():
            if entry in keep:
                continue
            if entry in path_dirs and not entry.is_symlink():
                pending.append(entry)
                continue
            freed += _remove(entry)
    return freed


def _prune_engine(bundle_dir: Path, acp_server: Path) -> int:
    freed = 0
    for dirpath, dirnames, filenames in os.walk(bundle_dir):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here.name, name) in _ENGINE_DROP_DIRS:
                dirnames.remove(name)
                freed += _remove(here / name)
        for name in filenames:
            path = here / name
            if name.endswith(_ENGINE_DROP_SUFFIXES) and path != acp_server:
                freed += _remove(path)
    return freed


def trim_for_shipping(installed: InstalledKasBundle) -> int:
    """Remove what the engine never runs from an installed tree; return bytes freed.

    For a copy that ships inside an app. The Node distribution keeps its binary
    and its license: the harness spawns ``node`` directly and the engine starts
    every child it runs through ``process.execPath`` or the user's own ``PATH``,
    never through the distribution's npm, npx or corepack. The engine loses its
    C/C++ sources, headers and source maps, and web-tree-sitter's debug build.
    Nothing reads these files at run time and no reader re-hashes the extracted
    tree, so the record stays valid as written. Raises :class:`KasBundleError`.
    """
    home = installed.root
    freed = _prune_node_dist(installed.node_bin, home / "node")
    freed += _prune_engine(home / "bundle", installed.acp_server)
    again = _read_installed(home.parent, installed.version, bundled=installed.bundled)
    if again is None or again != installed:
        raise KasBundleError(f"{home} does not read back after trimming")
    return freed


def installed_bundle(
    *, root: Path | None = None, version: str | None = None
) -> InstalledKasBundle | None:
    """The engine this release runs (*version*, default the pin), or ``None``.

    With no *root*, two roots are asked in order: the data home's ``engines/kas``
    (an operator's own install, which wins), then the desktop app's staged copy
    (:func:`bundled_kas_root`) when the shell exported one. An explicit *root* is
    asked alone.

    Never raises: a damaged record reads as "not installed" and is logged, so a
    spawn path can ask without a try/except.
    """
    version = version or pinned_version()
    if not _VERSION_RE.match(version):
        logger.warning("KAS engine version %r is not a version name; ignoring it", version)
        return None
    if root is not None:
        return _read_installed(root, version, bundled=False)
    found = _read_installed(kas_engines_root(), version, bundled=False)
    if found is not None:
        return found
    bundled = bundled_kas_root()
    if bundled is None:
        return None
    return _read_installed(bundled, version, bundled=True)


def _stays_inside(rel: str) -> bool:
    """Whether the record entry *rel*, joined onto its version directory, stays in it.

    ``home / rel`` discards ``home`` for an absolute *rel*, so an absolute form is
    refused in every spelling a record could carry whatever the host: POSIX, a
    drive letter or a rooted Windows path. A ``..`` segment is refused too.
    """
    if not rel or PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        return False
    if rel.startswith(("/", "\\")):
        return False
    return ".." not in PureWindowsPath(rel).parts


def _read_installed(root: Path, version: str, *, bundled: bool) -> InstalledKasBundle | None:
    try:
        home = root / version
        record = json.loads((home / KAS_BUNDLE_INSTALL_RECORD_NAME).read_text(encoding="utf-8"))
        if not isinstance(record, Mapping):
            logger.warning("KAS bundle record for %s is not an object; ignoring it", version)
            return None
        acp_rel = record.get("acp_server")
        node_rel = record.get("node_bin")
        node_version = record.get("node_version")
        if not (isinstance(acp_rel, str) and isinstance(node_rel, str)):
            return None
        if not (_stays_inside(acp_rel) and _stays_inside(node_rel)):
            logger.warning(
                "KAS bundle record under %s names an entry outside its root; ignoring it", home
            )
            return None
        acp_server = home / acp_rel
        node_bin = home / node_rel
        if not acp_server.is_file() or not platform_compat.is_executable_file(node_bin):
            logger.warning("KAS bundle %s at %s is incomplete; ignoring it", version, home)
            return None
        return InstalledKasBundle(
            version=version,
            node_version=node_version if isinstance(node_version, str) else "",
            root=home,
            node_bin=node_bin,
            acp_server=acp_server,
            bundled=bundled,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("KAS bundle record under %s is unreadable: %s", root, exc)
        return None
