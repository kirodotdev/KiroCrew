"""Where the agent-desktop executable is, and whether it is the build we pinned.

agent-desktop (vercel-labs/agent-desktop, Apache-2.0) ships as an npm package whose
tarball bundles one native CLI per OS. Kiro Crew installs it into a private prefix
under the data home rather than globally, so :func:`locate` checks, in order:

1. ``KIROCREW_AGENT_DESKTOP_BIN`` — an operator's explicit choice;
2. ``<data home>/tools/agent-desktop/node_modules/.bin/agent-desktop`` — the prefix
   :func:`INSTALL_PREFIX` names, which the dashboard installer populates;
3. ``agent-desktop`` on ``PATH``.

Finding a file is not trusting it. The private prefix lives in the data home, which
an agent shell can write, and the computer-use sidecar is the process that runs the
binary and believes what it says about secure fields. :func:`verify` therefore
accepts an executable only when its SHA-256 is one of the digests the pinned release
publishes, and does the same for any helper binary installed beside it, because the
CLI execs that helper on the macOS input path.

Platform-free on purpose: nothing here reads a platform flag. The digest set is the
union over every OS, so a binary for the wrong OS still verifies and then fails to
run, which ``AgentDesktopBackend.status`` reports.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

#: The agent-desktop release this driver is written against. The JSON envelope,
#: command names and flags it parses are those of this version.
PINNED_VERSION = "1.0.2"

#: Env var naming an explicit executable. Checked first.
ENV_BIN = "KIROCREW_AGENT_DESKTOP_BIN"

_EXE_NAME = "agent-desktop"

#: SHA-256 of every binary the 1.0.2 npm tarball ships (its own ``.sha256`` files),
#: keyed by the file name in the package's ``bin/``. ``bin/agent-desktop`` and the
#: unsuffixed helper are COPIES of one platform entry, so they verify against the set.
PINNED_SHA256: dict[str, str] = {
    "agent-desktop-darwin-arm64": "bf0a3c9256512f2f57288d37ccd17d7f03665fccc53c2f74011a49b98e61ba59",
    "agent-desktop-darwin-x64": "9684916bb4b5486c02cef8b2d480e55a551639b08358284f90a87cef43b2da8b",
    "agent-desktop-linux-arm64": "529ae49006278f778bcda6b68d7460a55148e2cefb1cb69a23485381d9dbdec0",
    "agent-desktop-linux-x64": "05427281a6746489bc0e15c16698a70a86322125c76c7a9e2f4c2d7a12bf7c5b",
    "agent-desktop-win32-arm64.exe": "5877fbca41fb85806353d0aaf67c23cfdf06a58a77c5d9cba005727a2891034e",
    "agent-desktop-win32-x64.exe": "409e58cbdd6aed47e29771ffa60bc122a9843505a227af488cded85215943988",
    "agent-desktop-macos-helper-darwin-arm64": (
        "522829116cb006b5770c6a82b361f466a4775f794d6af4083e75231a2d6bb551"
    ),
    "agent-desktop-macos-helper-darwin-x64": (
        "ce52467818ac601ae1145a0b9ecaa14aaf04d2e72d24cf6d29d26757340f9c6b"
    ),
    "agent-desktop-linux-clipboard-owner-linux-arm64": (
        "dcd6f2ce9a0e92e42f4d2ee9161ea1b9f2b11c0e4b1878119a19c0a2db3630a6"
    ),
    "agent-desktop-linux-clipboard-owner-linux-x64": (
        "bf17de139902724e86b3616970b7b7f16f5f1fb4143910b532a38dd2182720fa"
    ),
}

#: Helpers the CLI runs from its own directory. Each one present must verify too.
HELPER_NAMES: tuple[str, ...] = (
    "agent-desktop-macos-helper",
    "agent-desktop-linux-clipboard-owner",
)

_KNOWN_DIGESTS = frozenset(PINNED_SHA256.values())


def INSTALL_PREFIX() -> Path:  # noqa: N802 -- the installer imports this exact name
    """The private npm prefix: ``<data home>/tools/agent-desktop``.

    A function rather than a constant because the data home is resolved per call
    (``KIROCREW_HOME`` may be set after import). Not created here.
    """
    from kiro_crew.config.paths import data_home

    return data_home() / "tools" / "agent-desktop"


def _prefix_candidates(prefix: Path) -> list[Path]:
    """The executables an npm install into *prefix* can leave behind.

    ``node_modules/.bin/agent-desktop`` is npm's link on POSIX. On Windows npm writes
    shims there instead: ``.cmd``/``.ps1`` and an extensionless ``sh`` script, which
    ``os.access(X_OK)`` reports as executable because Windows has no execute bit. So
    the package's own ``bin/agent-desktop.exe`` (what the installer places) comes
    FIRST: it exists only on Windows, and taking the shim there made :func:`verify`
    refuse a correct install (measured on Windows Server 2025).
    """
    pkg_bin = prefix / "node_modules" / "agent-desktop" / "bin"
    return [
        pkg_bin / f"{_EXE_NAME}.exe",
        prefix / "node_modules" / ".bin" / _EXE_NAME,
    ]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def locate() -> Optional[str]:
    """Absolute path of the agent-desktop executable, or ``None`` when none is found.

    Says nothing about trust: call :func:`verify` on the result.
    """
    explicit = os.environ.get(ENV_BIN, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if _is_executable(path) else None
    try:
        prefix = INSTALL_PREFIX()
    except Exception:
        prefix = None
    if prefix is not None:
        for candidate in _prefix_candidates(prefix):
            if _is_executable(candidate):
                return str(candidate.resolve())
    found = shutil.which(_EXE_NAME)
    return str(Path(found).resolve()) if found else None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: str) -> Optional[str]:
    """``None`` when *path* and its helpers are pinned builds, else the refusal reason.

    The executable is resolved through symlinks first (npm's ``.bin`` entry is one),
    so the digest is of the file that would actually run.
    """
    real = Path(path).resolve()
    try:
        if sha256_of(real) not in _KNOWN_DIGESTS:
            return (
                f"{real} is not the pinned agent-desktop {PINNED_VERSION} build (SHA-256 mismatch)"
            )
        for name in HELPER_NAMES:
            helper = real.parent / name
            if helper.is_file() and sha256_of(helper) not in _KNOWN_DIGESTS:
                return f"{helper} is not the pinned agent-desktop {PINNED_VERSION} helper (SHA-256 mismatch)"
    except OSError as exc:
        return f"cannot read {real}: {exc.strerror or exc}"
    return None


__all__ = ["ENV_BIN", "INSTALL_PREFIX", "PINNED_SHA256", "PINNED_VERSION", "locate", "verify"]
