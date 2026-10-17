"""Kiro Crew's own KAS engine bundle, through the agent-backend boundary.

The install, lookup and pin live in the ACP layer (:mod:`kiro_crew.acp.kas_bundle`,
:mod:`kiro_crew.acp.kas_pin`). Application code -- the ``kirocrew kas`` CLI, the
dashboard handler and ``kirocrew doctor`` -- reaches them through this module,
so the boundary gate sees one surface and the ACP layout stays private. Only
plain data crosses: paths, version strings, the installer's error text.

Importing this module is cheap by design. ``kirocrew``'s argument parser imports
``cli_kas`` while it is built, and the dashboard's route registrar imports the
handler, both before the gateway is ready and whether or not ``agent.kas_bundle``
is on. Only the pin (two constants and an env lookup) is imported eagerly; the
installer -- tarfile, zipfile, subprocess, urllib and the data-home resolver --
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
KAS_VERSION_OVERRIDE_ENV = _pin.KAS_VERSION_OVERRIDE_ENV
pinned_version = _pin.pinned_version

#: Attribute -> the dotted name of the module that owns it. Names, not modules: the
#: owner is looked up in ``sys.modules`` on each read (see the module docstring).
_OWNED: dict[str, str] = {
    name: "kiro_crew.acp.kas_bundle"
    for name in (
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
    "KAS_PIN_MANIFEST_SHA256",
    "KAS_PIN_VERSION",
    "KAS_VERSION_OVERRIDE_ENV",
    "bundle_spawn_argv",
    "pinned_version",
    "sha256_bytes",
    "sha256_file",
    *sorted(_OWNED),
]


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


ProgressCallback = Callable[[str, int, int], Any]
