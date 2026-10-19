"""Whether the installed ``playwright-cli`` still carries the upstream seams Kiro Crew uses.

Two features depend on hooks in ``playwright-core`` that are upstream layout rather than a
declared API: the lifecycle environment variables that keep the CLI's sockets out of
scratch (:func:`cli_lifecycle_env_support`), and the ``show`` dashboard's socket the
Browser panel reveals (:func:`cli_dashboard_socket_support`). Each verdict reads the bundle
of the ``playwright-core`` tree that serves the resolved launcher, without a subprocess,
and is three-valued (:class:`SeamSupport`), so a bundle that was never read is not
reported as a missing capability.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from pathlib import Path

from kiro_crew.browser_cli.install_launcher import (
    _cli_package_for_launcher,
    _manifest_for_cli_package,
    cli_path,
)
from kiro_crew.browser_cli.install_runner import redact_install_output

_LIFECYCLE_SOURCE_MAX_BYTES = 32 * 1024 * 1024


@lru_cache(maxsize=8)
def _source_contains(path_text: str, mtime_ns: int, size: int, needle: bytes) -> bool | None:
    """Whether one version-pinned installed source file contains *needle*.

    ``None`` when the file was never read: empty, past the size ceiling, or an
    I/O failure. That is a different answer from ``False``, which means the bytes
    were searched and the needle is not in them. Collapsing the two lets a
    caller report a capability the CLI was never measured for.
    """
    del mtime_ns  # cache-key only: invalidates the answer after an upgrade
    if size <= 0 or size > _LIFECYCLE_SOURCE_MAX_BYTES:
        return None
    try:
        return needle in Path(path_text).read_bytes()
    except OSError:
        return None


class SeamSupport(enum.StrEnum):
    """Verdict on one upstream seam the installed CLI is pinned against.

    ``UNVERIFIED`` is not a softer ``UNSUPPORTED``. It means no bundle was read,
    so the CLI's capability is unknown and the finding is about THIS host's path
    attribution. Collapsing the two makes every gate report a capability gap and
    sends the operator to upgrade a CLI that already carries the seam.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


# Named by the path callers import it from: pickle records the enum as
# ``module.qualname``, so the owner module must not leak into it.
SeamSupport.__module__ = "kiro_crew.browser_cli.install"


#: What an operator can do about an ``UNVERIFIED`` verdict. ``PATH`` is not a
#: launcher source (see :func:`cli_path`), so a version-manager shim there is
#: refused by design and re-admitting it is not the remedy.
ATTRIBUTION_REMEDY = (
    "install the CLI where Kiro Crew resolves it -- the Browser panel's install "
    "action, or the standalone installer -- so its @playwright/cli package "
    "directory is attributable"
)


def _serving_core_root() -> tuple[Path | None, str]:
    """The ``playwright-core`` tree that will serve the resolved launcher.

    Anchored on the launcher ALONE, not on :func:`_cli_package_dirs`. That list
    falls back to the standalone install prefix, which is right for a revision
    lookup (a fallback revision beats no revision) and wrong here: a seam verdict
    is a claim about the CLI that will actually run, so a package belonging to a
    different install must not answer for a launcher whose own package could not
    be found. It would put back the same misreport in a narrower case.

    ``(None, detail)`` when nothing could be attributed, *detail* naming what was
    resolved and what was missing beside it. Callers report that as attribution,
    never as a measured capability.
    """
    cli = cli_path()
    if cli is None:
        return None, "no trusted playwright-cli launcher resolved on this host"
    package = _cli_package_for_launcher(Path(cli))
    if package is None:
        return None, (
            "no @playwright/cli package directory is attributable to the resolved "
            f"launcher {redact_install_output(cli)}"
        )
    manifest = _manifest_for_cli_package(package)
    if manifest is None:
        return None, (
            "no playwright-core tree serves the CLI package "
            f"{redact_install_output(str(package))}"
        )
    return manifest.parent, ""


def _seams_support(seams: tuple[tuple[Path, bytes], ...]) -> tuple[SeamSupport, str]:
    """Read *seams* in order, stopping at the first that is absent or unreadable.

    An unreadable bundle is ``UNVERIFIED``, not ``UNSUPPORTED``: the needle was
    never searched for, so nothing was learned about the CLI.
    """
    for path, needle in seams:
        try:
            info = path.stat()
        except OSError as exc:
            return SeamSupport.UNVERIFIED, redact_install_output(
                f"the serving bundle {path} could not be read ({exc})"
            )
        found = _source_contains(str(path), info.st_mtime_ns, info.st_size, needle)
        if found is None:
            return SeamSupport.UNVERIFIED, redact_install_output(
                f"the serving bundle {path} could not be read"
            )
        if not found:
            return SeamSupport.UNSUPPORTED, ""
    return SeamSupport.SUPPORTED, ""


def cli_lifecycle_env_support() -> tuple[SeamSupport, str]:
    """Whether the installed CLI honors both stable lifecycle env variables.

    The variable names are upstream test-prefixed seams rather than a declared
    compatibility API. Checking the package that will actually launch the
    daemon turns a future rename/removal into a loud fail-back instead of
    silently putting sockets under scratch again.

    Three-valued on purpose. ``UNSUPPORTED`` means a bundle was read and its
    hook is gone -- a real capability gap upstream. ``UNVERIFIED`` means the
    serving tree could not be attributed to the resolved launcher, or a bundle
    could not be read, and comes with the detail a caller needs to say so.
    """
    core_root, detail = _serving_core_root()
    if core_root is None:
        return SeamSupport.UNVERIFIED, detail
    return _seams_support(
        (
            (
                core_root / "lib" / "tools" / "cli-client" / "registry.js",
                b"process.env.PWTEST_DAEMON_SESSION_DIR",
            ),
            (core_root / "lib" / "coreBundle.js", b"process.env.PWTEST_SOCKETS_DIR ||"),
        )
    )


def cli_dashboard_socket_support() -> tuple[SeamSupport, str]:
    """Whether the installed CLI's ``show`` dashboard listens where the panel expects.

    The dashboard app claims one singleton socket at
    ``makeSocketPath("dashboard", "app")`` under ``PWTEST_SOCKETS_DIR``, and the
    Browser panel's launcher sends its reveal request there. Both halves are
    upstream layout rather than a declared API, so they are pinned the same way
    :func:`cli_lifecycle_env_support` pins the socket-root hook: by reading the
    serving ``playwright-core`` bundle. A rename upstream turns the reveal into a
    logged skip instead of a connect to a path nothing listens on.

    Three-valued for the same reason as the lifecycle gate: an unattributable
    launcher is ``UNVERIFIED``, so the log line can name attribution instead of
    claiming the CLI lacks the layout.
    """
    core_root, detail = _serving_core_root()
    if core_root is None:
        return SeamSupport.UNVERIFIED, detail
    bundle = core_root / "lib" / "coreBundle.js"
    return _seams_support(
        (
            (bundle, b'makeSocketPath("dashboard", "app")'),
            (bundle, b"process.env.PWTEST_SOCKETS_DIR ||"),
        )
    )
