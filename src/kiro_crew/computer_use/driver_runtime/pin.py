"""The pinned driver release: one version, one archive per (os, arch).

Every value here is the authority the installer checks a download against. The
release's own ``SHA256SUMS`` and ``release-manifest.json`` were read when the pin
was set and agree byte for byte, but neither is fetched at install time: a file
served next to the archive cannot vouch for the archive.

A version bump re-reads, in the new release, the telemetry opt-out order, the
update check switch, the process model (``mcp`` direct vs proxied) and the tool
schemas :mod:`..backend` relies on, because each of them has changed between
upstream releases before.

Linux and Windows use the ``-binary`` archives (the driver alone). macOS has no
entry: the upstream driver is not offered on macOS yet, and macOS keeps Kiro
Crew's own native driver.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass

#: The upstream driver release this build runs. ``cua-driver --version`` must
#: print exactly this before the binary is ever used for a desktop action.
DRIVER_VERSION = "0.47.8"

#: The release tag the archives are published under.
RELEASE_TAG = f"cua-driver-rs-v{DRIVER_VERSION}"

#: Where the archives are downloaded from. HTTPS only; redirects must stay HTTPS.
RELEASE_BASE_URL = f"https://github.com/trycua/cua/releases/download/{RELEASE_TAG}"

#: Authenticode signer the Windows binaries must carry.
WINDOWS_SIGNER = "Cua AI, Inc."

OS_LINUX = "linux"
OS_MACOS = "macos"
OS_WINDOWS = "windows"

ARCH_X86_64 = "x86_64"
ARCH_ARM64 = "arm64"


@dataclass(frozen=True)
class PinnedAsset:
    """One release archive, with the size and digest it must have."""

    os: str
    arch: str
    name: str
    size: int
    sha256: str
    #: Path of the driver executable inside the extracted archive, POSIX form.
    binary: str
    #: sha256 of that executable. Checked at install AND before every start, so a
    #: binary replaced on disk never runs even if the install record was rewritten
    #: to match it.
    binary_sha256: str = ""

    @property
    def url(self) -> str:
        return f"{RELEASE_BASE_URL}/{self.name}"

    @property
    def is_zip(self) -> bool:
        return self.name.endswith(".zip")


def _asset(
    os_id: str, arch: str, suffix: str, size: int, sha256: str, binary: str, binary_sha256: str
) -> PinnedAsset:
    return PinnedAsset(
        os=os_id,
        arch=arch,
        name=f"cua-driver-rs-{DRIVER_VERSION}-{suffix}",
        size=size,
        sha256=sha256,
        binary=binary,
        binary_sha256=binary_sha256,
    )


#: The whole pin table. Keys are ``(os, arch)``.
ASSETS: dict[tuple[str, str], PinnedAsset] = {
    (OS_LINUX, ARCH_X86_64): _asset(
        OS_LINUX,
        ARCH_X86_64,
        "linux-x86_64-binary.tar.gz",
        30172921,
        "d9abbe5f1db278210ff88e14fcefdc099dbc767b41849457cfc02838ba4180ec",
        "cua-driver",
        "ed2746b13669758a42c8b9d03fc93af38c899100f07a959b1bda26c86fec3ac4",
    ),
    (OS_LINUX, ARCH_ARM64): _asset(
        OS_LINUX,
        ARCH_ARM64,
        "linux-arm64-binary.tar.gz",
        28060736,
        "9ae1f662f3664b75fb1849f669e35b3919929d1d0a79bb1d9177dadb219e071a",
        "cua-driver",
        "2c222b5094532110771c79cf8a7a0a32068ac4b42ac6588d62729aa901ff6165",
    ),
    (OS_WINDOWS, ARCH_X86_64): _asset(
        OS_WINDOWS,
        ARCH_X86_64,
        "windows-x86_64-binary.zip",
        32778455,
        "19d9ffb1844b33a9a7ee5fbe9a3fac56be6740a0c947fd55eb08ee4b3f1f3866",
        "cua-driver.exe",
        "75781926ea2a78046ec938aa47b037841933f6c60558d65ff67bb8e84e4e191b",
    ),
    (OS_WINDOWS, ARCH_ARM64): _asset(
        OS_WINDOWS,
        ARCH_ARM64,
        "windows-arm64-binary.zip",
        30557761,
        "c600678090baca6d141cc7d58e10d2882cf226298fb6e2b3b6f71dba853557eb",
        "cua-driver.exe",
        "b23f60ee7772d98b564552ef176d440508e9d3e7d25165b37d0a66141ec88892",
    ),
}

#: ``platform.machine()`` spellings mapped onto the archive architectures.
_MACHINE_ALIASES: dict[str, str] = {
    "x86_64": ARCH_X86_64,
    "amd64": ARCH_X86_64,
    "x64": ARCH_X86_64,
    "aarch64": ARCH_ARM64,
    "arm64": ARCH_ARM64,
}


def normalize_arch(machine: str) -> str:
    """The archive architecture for a ``platform.machine()`` value, or ``""``."""
    return _MACHINE_ALIASES.get((machine or "").strip().lower(), "")


def asset_for(os_id: str, machine: "str | None" = None) -> "PinnedAsset | None":
    """The pinned archive for *os_id* on *machine* (this host's when omitted).

    ``None`` for macOS: no upstream driver is offered there.
    """
    arch = normalize_arch(_platform.machine() if machine is None else machine)
    if not arch:
        return None
    return ASSETS.get((os_id, arch))


__all__ = [
    "ARCH_ARM64",
    "ARCH_X86_64",
    "ASSETS",
    "DRIVER_VERSION",
    "OS_LINUX",
    "OS_MACOS",
    "OS_WINDOWS",
    "PinnedAsset",
    "RELEASE_BASE_URL",
    "RELEASE_TAG",
    "WINDOWS_SIGNER",
    "asset_for",
    "normalize_arch",
]
