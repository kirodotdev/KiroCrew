"""Upstream ACP Registry — discovery and distribution for ACP adapters.

The registry (``https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json``)
is the ecosystem's curated index of ACP adapters. It answers "which adapters exist,
what are they called, and how do I launch one". It deliberately does NOT answer
"can Kiro Crew govern this adapter's tool calls" — there is no permission,
approval or capability data in the schema — so this module owns exactly the half
the registry covers and nothing more. The trust half stays in
:mod:`kiro_crew.acp.backends`, keyed by the registry ``id``.

That split is the whole point of consuming the registry rather than hand-listing
adapters: the ecosystem is ~38 entries and growing, so a table of hand-written
rows goes stale by construction, while a routing verdict is a judgement Kiro Crew
must make itself and cannot inherit from anyone.

Schema (registry v1), one entry per adapter::

    id            "codex-acp"          registry identity; our descriptor key
    name          "Codex"              display name
    version       "1.4.0"              pinned release
    description   "ACP adapter for …"
    repository    "https://github.com/…"
    authors       ["OpenAI", …]
    license       "Apache-2.0"         "proprietary" for claude-acp
    distribution  {"npx": {"package": "@agentclientprotocol/codex-acp@1.4.0"}}
    icon          "https://cdn…/codex-acp.svg"

``distribution.npx.package`` carries an EXACT pinned version, which is the
ecosystem's canonical distribution form. Kiro Crew resolves the exact globally
installed package and runs its verified Node entry point directly, so session
startup can use operator-installed code but can never download and execute an
adapter implicitly.

Fetching is OPT-IN and cached on disk. Nothing here runs on the default kiro-cli
path: an operator who never opens the adapter surface never pays a network call,
and a gateway with no outbound access degrades to the bundled snapshot rather
than failing a session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
_REGISTRY_ORIGIN = "cdn.agentclientprotocol.com"

#: How long a cached copy is served before a refresh is attempted. The registry
#: changes on adapter releases, not minutes, and a stale entry is far cheaper
#: than a fetch on every settings render.
CACHE_TTL_SECS = 6 * 60 * 60

#: Ceiling on the downloaded document. The real one is ~49 KB; this bounds a
#: hostile or misconfigured endpoint rather than trusting Content-Length.
_MAX_BYTES = 2 * 1024 * 1024

_FETCH_TIMEOUT_SECS = 10

#: The CDN answers 403 to the default ``Python-urllib/3.x`` User-Agent, so this is
#: required rather than cosmetic — and identifying the client is the right thing
#: to do anyway, since it lets the registry maintainers see which clients fetch.
_USER_AGENT = "Kiro Crew (+https://github.com/kirodotdev/KiroCrew)"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEMVER_PATTERN = (
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SEMVER_RE = re.compile(rf"^{_SEMVER_PATTERN}$")
_NPX_PACKAGE_RE = re.compile(
    rf"^(?:@[a-z0-9][a-z0-9._~-]*/)?" rf"[a-z0-9][a-z0-9._~-]*@(?P<version>{_SEMVER_PATTERN})$"
)
_UVX_PACKAGE_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]*(?:==|@)(?P<version>{_SEMVER_PATTERN})$")
_MAX_PACKAGE_CHARS = 512
_MAX_ARG_COUNT = 64
_MAX_ARG_CHARS = 4096
_NPM_ROOT_TIMEOUT_SECS = 5
_NODE_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")
_WINDOWS_SHELL_SHIM_SUFFIXES = (".bat", ".cmd")
_NPM_BIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROCESS_CONTROL_ENV_KEYS = frozenset(
    {
        "BASH_ENV",
        "COMSPEC",
        "ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "PATHEXT",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "RUBYOPT",
        "SHELLOPTS",
        "SYSTEMROOT",
        "ZDOTDIR",
    }
)
_PROCESS_CONTROL_ENV_PREFIXES = ("DYLD_", "LD_", "NPM_CONFIG_")


def _npm_package_parts(package_name: str) -> tuple[str, ...]:
    """Parse npm's registry-defined scope delimiter independently of the host OS."""
    return PurePosixPath(package_name).parts


@dataclass(frozen=True)
class RegistryAdapter:
    """One adapter as the upstream registry describes it.

    Carries no capability or routing data ON PURPOSE — see the module docstring.
    A caller that wants to know whether Kiro Crew can govern this adapter asks
    :mod:`kiro_crew.acp.backends`, not this record.
    """

    id: str
    name: str
    version: str
    description: str
    repository: str
    license: str
    icon: str
    #: ``npx`` | ``uvx`` | ``binary``. Measured across registry v1: 19 npx-only,
    #: 15 binary-only, 2 uvx-only, 2 offering both binary and npx.
    kind: str
    #: Pinned package for npx/uvx. Empty for a binary distribution.
    package: str
    #: Extra argv the registry says this adapter needs — several require one
    #: (``agoragentic-mcp`` needs ``--acp``), so dropping it launches the wrong
    #: mode and the handshake fails for a reason nothing explains.
    args: tuple[str, ...]
    #: Environment the registry pins for this adapter (``fast-agent`` sets a
    #: model). Applied on top of the spawn env, never replacing it.
    env: tuple[tuple[str, str], ...]

    @property
    def is_launchable(self) -> bool:
        """Can Kiro Crew start this adapter without installing software itself?

        True for npx distributions whose pinned package can be resolved from a
        persistent global install. False for uvx: ``uvx --offline`` can execute
        an ephemeral environment from cache without ``uv tool install``, so it
        does not prove operator installation. False for a binary distribution:
        that means
        downloading and extracting an archive from a release URL, and a gateway
        that fetches and executes arbitrary binaries is a far larger trust
        surface than one that shells out to a package runner. Their registry
        metadata remains available for discovery, but they are not selectable.
        """
        return self.kind == "npx" and _safe_launch_distribution(
            self.kind,
            self.package,
            self.version,
            self.args,
        )

    @property
    def launch_argv(self) -> list[str]:
        """Canonical runner argv, or empty when Kiro Crew cannot launch it.

        This is the upstream distribution spelling used for disclosure. Npm
        startup uses :meth:`resolve_launch_argv` to consume the global install
        directly; it does not execute this npx form. uvx stays empty because its
        offline cache is not evidence of a persistent operator install.
        """
        if not self.is_launchable:
            return []
        if self.kind == "npx":
            return ["npx", "--offline", "--yes=false", "--", self.package, *self.args]
        return []

    def resolve_launch_argv(
        self,
        npm_resolution: NpmResolutionSnapshot | None = None,
    ) -> list[str]:
        """Installed-only launch argv, resolved to portable absolute files.

        npx does not consume packages installed by ``npm install -g`` when it is
        given an exact package spec; it asks the package cache/registry instead.
        Resolve the documented global install directly and run its verified Node
        entry point. This also avoids Windows ``.cmd`` shims, which CreateProcess
        cannot execute without a shell.
        """
        if not self.is_launchable:
            return []
        if self.kind == "npx":
            return _resolve_global_npm_package(
                self.package,
                self.args,
                npm_resolution=npm_resolution,
            )
        return self.launch_argv

    @property
    def offline_env(self) -> dict[str, str]:
        """Runner settings that make an installed-only launch fail closed.

        Modern npm releases no longer expose npx's historical ``--no-install``
        option. ``npm_config_offline`` is the supported npm-wide control and is
        pinned by the spawn after every operator/registry environment overlay.
        """
        if self.kind == "npx":
            return {"npm_config_offline": "true"}
        return {}

    @property
    def install_command(self) -> str:
        """A global install of the SAME pinned version for npm adapters.

        Offered alongside the launch form because startup verifies and executes
        this installed copy without touching the network.
        """
        if not self.is_launchable:
            return ""
        if self.kind == "npx":
            return f"npm install -g {self.package}"
        return ""


def _safe_launch_distribution(
    kind: str,
    package: str,
    version: str,
    args: tuple[str, ...],
) -> bool:
    """Whether registry launch data is an exact, non-option argv prefix.

    ``npm_config_offline`` only protects an npx launch when the first argument
    is the package. An option-like package could otherwise re-enable downloads
    and move the real package into registry-controlled trailing args. Requiring
    one exact package/version spelling also rejects floating tags and mismatches.
    """
    if not _SEMVER_RE.fullmatch(version) or len(package) > _MAX_PACKAGE_CHARS:
        return False
    if len(args) > _MAX_ARG_COUNT:
        return False
    if any(not isinstance(arg, str) or "\x00" in arg or len(arg) > _MAX_ARG_CHARS for arg in args):
        return False

    package_pattern = _NPX_PACKAGE_RE if kind == "npx" else _UVX_PACKAGE_RE
    match = package_pattern.fullmatch(package) if kind in ("npx", "uvx") else None
    return match is not None and match.group("version") == version


@dataclass(frozen=True)
class _NpmToolchain:
    npm_argv: tuple[str, ...]
    node: str
    path: str
    volta_home: Path | None = None
    is_windows: bool = os.name == "nt"


@dataclass(frozen=True)
class NpmResolutionSnapshot:
    """One request's manager discovery, including retryable misses."""

    toolchains: tuple[_NpmToolchain, ...]
    roots: tuple[tuple[str, Path], ...]


_NPM_ROOT_CACHE: dict[tuple[tuple[str, ...], str, str], tuple[str, Path]] = {}
_NPM_ROOT_CACHE_LOCK = threading.Lock()


def _npm_invocation_for_path(npm: str, node: str) -> tuple[str, ...] | None:
    """Executable npm argv, unwrapping only shell-only Windows shims."""
    npm_path = Path(npm)
    if npm_path.suffix.lower() not in _WINDOWS_SHELL_SHIM_SUFFIXES:
        return (npm,)
    candidates = (
        npm_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
        npm_path.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return (node, str(candidate.resolve()))
        except OSError:
            continue
    return None


def _volta_home_for_npm(npm: str) -> Path | None:
    """Volta home when *npm* is one of its shims, otherwise ``None``."""
    home_candidates: list[Path] = []
    configured = os.environ.get("VOLTA_HOME")
    if configured:
        home_candidates.append(Path(configured).expanduser())
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            home_candidates.append(Path(local_app_data) / "Volta")
    else:
        home_candidates.append(Path.home() / ".volta")

    npm_parent = os.path.normcase(os.path.abspath(str(Path(npm).parent)))
    install_dirs: list[Path] = []
    configured_install = os.environ.get("VOLTA_INSTALL_DIR")
    if configured_install:
        install_dirs.append(Path(configured_install).expanduser())
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            install_dirs.append(Path(program_files) / "Volta")

    for candidate in home_candidates:
        home = Path(os.path.abspath(str(candidate)))
        allowed_parents = (home / "bin", *install_dirs)
        if any(
            npm_parent == os.path.normcase(os.path.abspath(str(parent)))
            for parent in allowed_parents
        ):
            return home
    return None


def _npm_toolchains() -> tuple[_NpmToolchain, ...]:
    """Operator-PATH npm first, then every supported manager/toolchain dir."""
    from kiro_crew.env import augmented_path

    inherited = os.environ.get("PATH", "")
    search = os.pathsep.join(part for part in (inherited, augmented_path("")) if part)
    directories: list[str] = []
    seen_dirs: set[str] = set()
    for raw_dir in search.split(os.pathsep):
        directory = os.path.abspath(raw_dir) if raw_dir else ""
        key = os.path.normcase(directory)
        if not directory or key in seen_dirs:
            continue
        seen_dirs.add(key)
        directories.append(directory)

    toolchains: list[_NpmToolchain] = []
    seen_npm: set[str] = set()
    for directory in directories:
        npm = shutil.which("npm", path=directory)
        if not npm:
            continue
        npm_key = os.path.normcase(os.path.abspath(npm))
        if npm_key in seen_npm:
            continue
        node_path = os.pathsep.join((directory, inherited)) if inherited else directory
        node = shutil.which("node", path=node_path)
        if not node:
            continue
        npm_argv = _npm_invocation_for_path(npm, node)
        if npm_argv is None:
            continue
        seen_npm.add(npm_key)
        toolchains.append(
            _NpmToolchain(
                npm_argv=npm_argv,
                node=node,
                path=node_path,
                volta_home=_volta_home_for_npm(npm),
                is_windows=os.name == "nt",
            )
        )
    return tuple(toolchains)


def _query_npm_global_root(toolchain: _NpmToolchain) -> Path | None:
    """Existing global root for one concrete toolchain; misses are retryable."""
    from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv

    cleanup: str | None = None
    try:
        base_env = {**os.environ, "PATH": toolchain.path}
        argv, env, cleanup = sandboxed_spawn_argv(
            [*toolchain.npm_argv, "root", "-g"],
            mode="standard",
            env=base_env,
            strip_python_env=True,
        )
        result = run_limited(
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NPM_ROOT_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        logger.debug("Could not query npm's global package root", exc_info=True)
        return None
    finally:
        if cleanup:
            try:
                Path(cleanup).unlink()
            except OSError:
                pass
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) != 1:
        return None
    root = Path(lines[0])
    if not root.is_absolute():
        return None
    try:
        return root.resolve(strict=True) if root.is_dir() else None
    except OSError:
        return None


def _npm_global_roots(
    toolchains: tuple[_NpmToolchain, ...] | None = None,
) -> tuple[tuple[str, Path], ...]:
    """Every existing global root, caching successes but retrying every miss."""
    roots: list[tuple[str, Path]] = []
    seen_roots: set[str] = set()
    if toolchains is None:
        toolchains = _npm_toolchains()
    for toolchain in toolchains:
        # Volta intercepts global installs into per-package images. Its npm root
        # describes the selected Node image, not those installed package tools.
        if toolchain.volta_home is not None:
            continue
        key = (toolchain.npm_argv, toolchain.node, toolchain.path)
        with _NPM_ROOT_CACHE_LOCK:
            cached = _NPM_ROOT_CACHE.get(key)
        if cached is None:
            root = _query_npm_global_root(toolchain)
            if root is None:
                continue
            cached = (toolchain.node, root)
            with _NPM_ROOT_CACHE_LOCK:
                _NPM_ROOT_CACHE[key] = cached
        root_key = os.path.normcase(str(cached[1]))
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        roots.append(cached)
    return tuple(roots)


def _clear_npm_root_cache() -> None:
    with _NPM_ROOT_CACHE_LOCK:
        _NPM_ROOT_CACHE.clear()


def npm_resolution_snapshot() -> NpmResolutionSnapshot:
    """Resolve each npm toolchain once for one multi-adapter operation.

    Successful roots keep their process-wide cache. Failed root queries live
    only in this value, so another request retries after an operator installs or
    repairs a toolchain without multiplying one timeout by every adapter row.
    """
    toolchains = _npm_toolchains()
    return NpmResolutionSnapshot(
        toolchains=toolchains,
        roots=_npm_global_roots(toolchains),
    )


def _npm_bin_entry(manifest: dict[str, Any], package_name: str) -> tuple[str, str] | None:
    """Unambiguous npm ``(command, path)`` using npx's selection rule."""
    raw = manifest.get("bin")
    if isinstance(raw, str) and raw:
        return package_name.rsplit("/", 1)[-1], raw
    if not isinstance(raw, dict) or not raw:
        return None
    preferred = package_name.rsplit("/", 1)[-1]
    candidate = raw.get(preferred)
    if isinstance(candidate, str) and candidate:
        return preferred, candidate
    if len(raw) == 1:
        command, only = next(iter(raw.items()))
        if isinstance(command, str) and isinstance(only, str) and command and only:
            return command, only
    return None


def _node_runnable(path: Path) -> bool:
    if path.suffix.lower() in _NODE_SCRIPT_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"node" in first


def _resolve_global_npm_package(
    package: str,
    args: tuple[str, ...],
    *,
    npm_resolution: NpmResolutionSnapshot | None = None,
) -> list[str]:
    """Run one exact globally-installed npm package without npx or a shell."""
    match = _NPX_PACKAGE_RE.fullmatch(package)
    if match is None:
        return []
    split_at = package.rfind("@")
    package_name = package[:split_at]
    resolution = npm_resolution or npm_resolution_snapshot()
    for toolchain in resolution.toolchains:
        if toolchain.volta_home is None:
            continue
        argv = _resolve_volta_package(
            toolchain,
            package_name,
            match.group("version"),
            args,
        )
        if argv:
            return argv
    for node, root in resolution.roots:
        argv = _resolve_npm_package_in_root(
            node,
            root,
            package_name,
            match.group("version"),
            args,
        )
        if argv:
            return argv
    return []


def _resolve_volta_package(
    toolchain: _NpmToolchain,
    package_name: str,
    version: str,
    args: tuple[str, ...],
) -> list[str]:
    """Verified argv for a package in Volta's persistent package image."""
    home = toolchain.volta_home
    if home is None:
        return []
    package_parts = _npm_package_parts(package_name)
    image = home.joinpath("tools", "image", "packages", *package_parts)
    root = image / "node_modules" if toolchain.is_windows else image / "lib" / "node_modules"
    verified = _verified_npm_package(root, package_name, version)
    if verified is None:
        return []
    entrypoint, command = verified
    node = _verified_volta_node(
        home,
        package_name,
        version,
        command,
        is_windows=toolchain.is_windows,
    )
    if node is None:
        return []
    return [str(node), str(entrypoint), *args]


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _verified_volta_node(
    home: Path,
    package_name: str,
    version: str,
    command: str,
    *,
    is_windows: bool,
) -> Path | None:
    """Node image pinned to one exact Volta package/bin registration."""
    if not _NPM_BIN_NAME_RE.fullmatch(command):
        return None
    package_parts = _npm_package_parts(package_name)
    package_config_path = home.joinpath(
        "tools",
        "user",
        "packages",
        *package_parts[:-1],
        f"{package_parts[-1]}.json",
    )
    bin_config_path = home / "tools" / "user" / "bins" / f"{command}.json"
    package_config = _read_json_object(package_config_path)
    bin_config = _read_json_object(bin_config_path)
    if package_config is None or bin_config is None:
        return None
    platform = package_config.get("platform")
    bins = package_config.get("bins")
    if (
        package_config.get("name") != package_name
        or package_config.get("version") != version
        or package_config.get("manager") != "Npm"
        or not isinstance(platform, dict)
        or not isinstance(bins, list)
        or command not in bins
        or any(not isinstance(item, str) for item in bins)
    ):
        return None
    if (
        bin_config.get("name") != command
        or bin_config.get("package") != package_name
        or bin_config.get("version") != version
        or bin_config.get("manager") != "Npm"
        or bin_config.get("platform") != platform
    ):
        return None
    node_version = platform.get("node")
    if not isinstance(node_version, str) or not _SEMVER_RE.fullmatch(node_version):
        return None
    node_image = home / "tools" / "image" / "node" / node_version
    node_path = node_image / "node.exe" if is_windows else node_image / "bin" / "node"
    try:
        resolved_image = node_image.resolve(strict=True)
        resolved_node = node_path.resolve(strict=True)
        resolved_node.relative_to(resolved_image)
        if not resolved_node.is_file() or (
            not is_windows and not os.access(resolved_node, os.X_OK)
        ):
            return None
    except (OSError, ValueError):
        return None
    return resolved_node


def _verified_npm_package(
    root: Path,
    package_name: str,
    version: str,
) -> tuple[Path, str] | None:
    """Verified ``(entrypoint, command)`` below one node_modules root."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return None
    package_dir = resolved_root.joinpath(*_npm_package_parts(package_name))
    try:
        resolved_package_dir = package_dir.resolve(strict=True)
        resolved_package_dir.relative_to(resolved_root)
        manifest_raw = json.loads(
            (resolved_package_dir / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(manifest_raw, dict):
        return None
    if manifest_raw.get("name") != package_name or manifest_raw.get("version") != version:
        return None
    bin_entry = _npm_bin_entry(manifest_raw, package_name)
    if bin_entry is None:
        return None
    command, rel_bin = bin_entry
    try:
        entrypoint = (resolved_package_dir / rel_bin).resolve(strict=True)
        entrypoint.relative_to(resolved_package_dir)
    except (OSError, ValueError):
        return None
    if not entrypoint.is_file() or not _node_runnable(entrypoint):
        return None
    return entrypoint, command


def _resolve_npm_package_in_root(
    node: str,
    root: Path,
    package_name: str,
    version: str,
    args: tuple[str, ...],
) -> list[str]:
    """Verified launch argv for one package under one paired Node/npm root."""
    verified = _verified_npm_package(root, package_name, version)
    if verified is None:
        return []
    entrypoint, _command = verified
    return [node, str(entrypoint), *args]


def _dist_fields(dist: dict[str, Any], version: str) -> tuple[str, str, tuple[str, ...], dict]:
    """Pick one distribution and normalise it.

    npx is preferred over binary when an adapter offers both (2 do), because a
    package runner is the cheaper and more auditable path.
    """
    for kind in ("npx", "uvx"):
        block = dist.get(kind)
        if isinstance(block, dict) and isinstance(block.get("package"), str):
            raw_args = block.get("args")
            if raw_args is not None and not isinstance(raw_args, list):
                continue
            args = tuple(raw_args) if isinstance(raw_args, list) else ()
            if not _safe_launch_distribution(kind, block["package"], version, args):
                continue
            raw_env = block.get("env")
            env = raw_env if isinstance(raw_env, dict) else {}
            return kind, block["package"], args, env
    if isinstance(dist.get("binary"), dict):
        return "binary", "", (), {}
    return "", "", (), {}


def _safe_registry_env(env: dict[Any, Any]) -> tuple[tuple[str, str], ...]:
    """Registry-declared adapter settings excluding process-control hooks.

    The registry may describe ordinary adapter feature flags, but it does not
    get to replace executable lookup, inject a language-runtime startup hook, or
    override npm's installed-only posture. Operator ``extra_env`` remains the
    explicit path for those advanced settings.
    """
    safe: list[tuple[str, str]] = []
    for raw_key, raw_value in env.items():
        if not isinstance(raw_key, str) or not _ENV_NAME_RE.fullmatch(raw_key):
            continue
        key = raw_key.upper()
        if key in _PROCESS_CONTROL_ENV_KEYS or key.startswith(_PROCESS_CONTROL_ENV_PREFIXES):
            logger.warning("Ignoring unsafe ACP registry environment key %s", raw_key)
            continue
        if not isinstance(raw_value, (str, int, float, bool)):
            continue
        safe.append((raw_key, str(raw_value)))
    return tuple(safe)


def _parse(document: Any) -> dict[str, RegistryAdapter]:
    """Convert a registry document into adapters, skipping anything malformed.

    Skipping rather than raising: one bad entry upstream must not remove every
    other adapter from the surface. An entry with no npx distribution is dropped
    because Kiro Crew has no other way to launch it, and listing an adapter it
    cannot start would be worse than omitting it.
    """
    out: dict[str, RegistryAdapter] = {}
    agents = document.get("agents") if isinstance(document, dict) else None
    if not isinstance(agents, list):
        logger.debug("ACP registry document has no agents list")
        return out

    for entry in agents:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        version = entry.get("version")
        if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
            continue
        dist = entry.get("distribution")
        kind, package, args, env = _dist_fields(dist if isinstance(dist, dict) else {}, version)
        if not kind:
            # No distribution Kiro Crew recognises. Keep it out rather than
            # listing an adapter with no way to obtain it at all.
            continue
        out[ident] = RegistryAdapter(
            id=ident,
            name=str(entry.get("name") or ident),
            version=version,
            description=str(entry.get("description") or ""),
            repository=str(entry.get("repository") or ""),
            license=str(entry.get("license") or ""),
            icon=str(entry.get("icon") or ""),
            kind=kind,
            package=package,
            args=args,
            env=_safe_registry_env(env),
        )
    return out


def _cache_path() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / "acp-registry.json"


def _read_cache(max_age_secs: int) -> dict[str, RegistryAdapter] | None:
    path = _cache_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    if max_age_secs >= 0 and (time.time() - stat.st_mtime) > max_age_secs:
        return None
    try:
        return _parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        logger.debug("Unreadable ACP registry cache at %s", path, exc_info=True)
        return None


def fetch(force: bool = False) -> dict[str, RegistryAdapter]:
    """Adapters from the registry, cache-first.

    Never raises. A network failure, a timeout, an oversized body or unparseable
    JSON all fall back to whatever cache exists, and to an empty mapping if there
    is none. The adapter surface degrades to "we could not reach the registry",
    which is a legible state; a raised exception here would take out a settings
    page over a transient DNS failure.
    """
    if not force:
        cached = _read_cache(CACHE_TTL_SECS)
        if cached is not None:
            return cached

    try:
        parsed_url = urllib.parse.urlsplit(REGISTRY_URL)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != _REGISTRY_ORIGIN
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.port is not None
        ):
            raise ValueError("ACP registry URL must use the pinned HTTPS origin")
        request = urllib.request.Request(  # noqa: S310 - fixed https CDN URL
            REGISTRY_URL,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- the URL is rejected unless it uses the pinned HTTPS origin above  # noqa: E501
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECS) as response:  # noqa: S310
            raw = response.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            logger.warning("ACP registry document exceeded %d bytes", _MAX_BYTES)
            return _read_cache(-1) or {}
        document = json.loads(raw.decode("utf-8"))
    except (
        urllib.error.URLError,
        OSError,
        ValueError,
        UnicodeDecodeError,
    ):
        logger.debug("Could not fetch the ACP registry", exc_info=True)
        # Serve a stale cache rather than nothing: an adapter list from this
        # morning is far more useful than an empty surface.
        return _read_cache(-1) or {}

    adapters = _parse(document)
    if adapters:
        try:
            path = _cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Readers use this file to decide which executable identity may be
            # selected. Publish through replace so they see the prior complete
            # registry or the new complete registry, never a truncated middle.
            atomic_write(
                path,
                json.dumps(document, indent=2),
                restrict_to_owner=True,
            )
        except OSError:
            logger.debug("Could not cache the ACP registry", exc_info=True)
    return adapters


def lookup(registry_id: str) -> RegistryAdapter | None:
    """One adapter by registry id, cache-first and never raising."""
    return fetch().get(registry_id)


def cached() -> dict[str, RegistryAdapter]:
    """Adapters already cached on disk, without performing network I/O."""
    return _read_cache(-1) or {}


__all__ = [
    "CACHE_TTL_SECS",
    "REGISTRY_URL",
    "RegistryAdapter",
    "cached",
    "fetch",
    "lookup",
]
