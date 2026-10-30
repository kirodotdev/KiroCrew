"""Kiro Crew's own KAS engine bundle, through the agent-backend boundary.

The install, lookup and pin live in the ACP layer (:mod:`kiro_crew.acp.kas_bundle`,
:mod:`kiro_crew.acp.kas_pin`). Application code -- the ``kirocrew kas`` CLI, the
dashboard handler and ``kirocrew doctor`` -- reaches them through this module,
so the boundary gate sees one surface and the ACP layout stays private. Only
plain data crosses: paths, version strings, the installer's error text.

Importing this module is cheap by design. ``kirocrew``'s argument parser imports
``cli_kas`` while it is built, and the dashboard's route registrar imports the
handler, both before the gateway is ready and whether or not ``agent.kas_bundle``
is on. Only the pin (its constants, an env lookup and the engine's spec
``permissions`` gate) is imported eagerly; the installer -- tarfile, zipfile, subprocess, urllib and the data-home resolver --
is imported on the first attribute that needs it, through :func:`__getattr__`
(:pep:`562`), so a disabled bundle route costs the boot path nothing
(``no-new-work-on-gateway-boot-path`` item 5).

The installer's names are resolved on EVERY read, through
``importlib.import_module``, and never cached here: ``sys.modules`` is the one
place the owner is stored, and a memo of it (or of a value read from it) would be
a second one that a purge-and-reimport leaves stale. Writes to those names are
forwarded to the owner for the same reason, so ``monkeypatch.setattr`` through this
module patches what every caller reads and its restore puts back one value in one
place (``test_mirrored_owner_storage``).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_pin as _pin

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.acp.kas_bundle import InstalledKasBundle

KAS_PIN_VERSION = _pin.KAS_PIN_VERSION
KAS_PIN_MANIFEST_SHA256 = _pin.KAS_PIN_MANIFEST_SHA256
KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION = _pin.KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION
KAS_VERSION_OVERRIDE_ENV = _pin.KAS_VERSION_OVERRIDE_ENV
pinned_version = _pin.pinned_version
engine_spec_permissions_supported = _pin.engine_spec_permissions_supported

#: Attribute -> the dotted name of the module that owns it. Names, not modules: the
#: owner is looked up in ``sys.modules`` on each read (see the module docstring).
_OWNED: dict[str, str] = {
    name: "kiro_crew.acp.kas_bundle"
    for name in (
        "KAS_BUNDLE_INSTALL_RECORD_NAME",
        "KAS_BUNDLE_MANIFEST_NAME",
        "InstalledKasBundle",
        "KasBundleError",
        "KasBundleManifest",
        "parse_manifest",
        "fetch_manifest",
        "host_node_key",
        "host_platform_key",
        "install_bundle",
        "install_pinned",
        "installed_bundle",
        "installed_versions",
        "kas_engines_root",
    )
}

__all__ = [
    "KAS_BUNDLE_SIGN_IN_REMEDY",
    "KAS_PIN_MANIFEST_SHA256",
    "KAS_PIN_VERSION",
    "KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION",
    "KAS_VERSION_OVERRIDE_ENV",
    "bundle_spawn_argv",
    "engine_spec_permissions_supported",
    "installed_engine_label",
    "kas_bundle_route",
    "pinned_version",
    "sha256_bytes",
    "sha256_file",
    *sorted(_OWNED),
]

#: How a bundle-route host signs in. kiro-cli is never the owner there, so the
#: remedy names only Crew's own sign-in; the relay route's remedy
#: (``host_auth._KAS_REMEDY``) names both because that spawn can draw on either.
KAS_BUNDLE_SIGN_IN_REMEDY = (
    "Sign in from Settings → Agent Harness → Kiro sign-in, then start a new chat. "
    "kiro-cli is not used on this route."
)


def _owner(name: str) -> ModuleType:
    """The module that defines *name*, imported on first use and read from
    ``sys.modules`` on every later one."""
    return importlib.import_module(_OWNED[name])


def __getattr__(name: str) -> Any:
    """Read an installer name from the module that owns it (:pep:`562`)."""
    if name not in _OWNED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), name)


class _ReExportModule(ModuleType):
    """Forward a write to an installer name to its owner.

    Binding it here instead would shadow the owner for the life of the process,
    because ``__getattr__`` runs only for a name this module does not hold; the
    forwarded write leaves one value for a test harness to remember and put back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWNED:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _OWNED:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_OWNED))


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, for comparing a local manifest against the pin."""
    return _owner("installed_bundle")._sha256_file(path)


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of bytes already read, so a caller verifies the buffer it parses."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


def bundle_spawn_argv(installed: InstalledKasBundle) -> list[str]:
    """The exact argv the harness spawns for *installed*, for diagnostics."""
    from kiro_crew.acp.kas_transport import build_kas_bundle_argv

    return build_kas_bundle_argv(str(installed.node_bin), str(installed.acp_server))


def kas_bundle_route() -> bool:
    """Whether the next agent spawn runs Crew's own engine: KAS selected AND the bundle on.

    The one predicate the kiro-cli-facing diagnostics share -- ``kirocrew doctor``,
    the diagnostics bundle's version line, the gateway self-update's ``kiro-cli
    update`` step and the boot-time version warning -- so they agree on when
    kiro-cli is not part of the installation they describe. Positive on both
    halves: ``agent.kas_bundle`` alone says nothing when another harness is
    selected (that harness may well be kiro-cli), and the backend alone is the
    relay route, which IS kiro-cli. Read from the config at call time, never
    remembered: a CLI command loads it once, and the gateway's unattended
    callers run rarely enough that a reload costs nothing. Unreadable config
    reads as the relay route, the answer that keeps every kiro-cli row as it
    was. Blocking (reads config); the gateway's paths call it off the loop.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.config import KiroCrewConfig

    try:
        agent = KiroCrewConfig.load().agent
        return agent.acp_backend == ACP_BACKEND_KAS and bool(agent.kas_bundle)
    except Exception:
        return False


def installed_engine_label() -> str | None:
    """``KAS <version>, Node <version>`` for the engine the bundle route runs, or ``None``.

    ``None`` when nothing is installed for the pinned version (or the lookup
    failed): the caller prints the install command, not a version. One spelling
    for every row that names the engine in the place the relay route gives to
    kiro-cli's version -- doctor's Connectivity row and the diagnostics bundle's
    ``versions.txt`` -- so the two cannot describe one engine two ways.
    """
    try:
        installed = _owner("installed_bundle").installed_bundle()
    except Exception:
        return None
    if installed is None:
        return None
    return f"KAS {installed.version}, Node {installed.node_version or '?'}"


ProgressCallback = Callable[[str, int, int], Any]
