"""Which platform wheel this host takes, and where its signed manifest lives.

A release publishes the pure ``py3-none-any`` wheel and, for each host the KAS
engine runs on, a ``py3-none-<tag>`` wheel that carries the pinned engine inside the
package (``scripts/kas_platform_wheel.py``). Each platform wheel has its own signed
manifest, ``feed/<channel>/latest-cli-<tag>.json``, in the same schema as
``latest-cli.json``. ``latest-cli.json`` itself is never changed: released clients
refuse any field they do not know, so they keep updating through the pure wheel.

The tags match ``scripts/kas_platform_wheel.py`` ``PLATFORM_TAGS`` and the
``publish-cli.yml`` matrix; a pin test holds the three equal.
"""

from __future__ import annotations

import platform
import urllib.error

#: (platform.system(), normalised machine) -> the wheel tag published for it.
_HOST_TAGS = {
    ("Linux", "x86_64"): "manylinux_2_28_x86_64",
    ("Linux", "aarch64"): "manylinux_2_28_aarch64",
    ("Darwin", "arm64"): "macosx_11_0_arm64",
    ("Darwin", "x86_64"): "macosx_11_0_x86_64",
}
PLATFORM_WHEEL_TAGS = frozenset(_HOST_TAGS.values())
_MACHINE_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "aarch64"}
#: The bundled Node's glibc floor, the ``manylinux_2_28`` in the Linux tags.
_GLIBC_FLOOR = (2, 28)
#: The bundled Node's macOS floor, the ``macosx_11_0`` in the Mac tags.
_MACOS_FLOOR = 11


def _machine(system: str, machine: str) -> str:
    m = _MACHINE_ALIASES.get(machine.lower(), machine.lower())
    if system == "Darwin" and m == "aarch64":
        return "arm64"
    if system == "Linux" and m == "arm64":
        return "aarch64"
    return m


def host_wheel_tag(
    system: str | None = None, machine: str | None = None, mac_version: str | None = None
) -> str | None:
    """The platform wheel tag this host installs, or ``None`` for the pure wheel.

    A Linux host whose C library is not glibc 2.28 or newer (musl, an old glibc, or
    one that cannot be read) gets ``None``, and so does a Mac below macOS 11 (or one
    whose version cannot be read): pip would refuse the ``manylinux_2_28`` or
    ``macosx_11_0`` wheel there, and so must the update. *mac_version* is the
    ``platform.mac_ver()`` release string, read from this host when not given.
    """
    system = system or platform.system()
    tag = _HOST_TAGS.get((system, _machine(system, machine or platform.machine())))
    if tag is None:
        return None
    if system == "Darwin":
        release = platform.mac_ver()[0] if mac_version is None else mac_version
        try:
            major = int(release.split(".")[0])
        except ValueError:
            return None
        return tag if major >= _MACOS_FLOOR else None
    if system != "Linux":
        return tag
    from kiro_crew import constants

    glibc = constants._host_glibc_version()
    return tag if glibc is not None and glibc >= _GLIBC_FLOOR else None


def platform_manifest_url(feed_base: str, channel: str, tag: str) -> str:
    """``<feed>/feed/<channel>/latest-cli-<tag>.json``."""
    if tag not in PLATFORM_WHEEL_TAGS:
        raise ValueError(f"unknown platform wheel tag {tag!r}")
    return f"{feed_base}/feed/{channel}/latest-cli-{tag}.json"


def is_absent(exc: BaseException) -> bool:
    """Whether a fetch failed because the object does not exist (403/404).

    The release CDN answers a missing key with 403 (S3 behind CloudFront with
    origin access control) or 404. Anything else -- a timeout, a 5xx, a signature
    or URL refusal -- is not absence and must not fall back.
    """
    cause = exc.__cause__
    return isinstance(cause, urllib.error.HTTPError) and cause.code in (403, 404)
