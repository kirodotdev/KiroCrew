"""Install this release's pinned KAS engine from the gateway, one install at a time.

Two callers start an install: a KAS spawn that finds the engine missing
(:meth:`kiro_crew.acp.harness.kas.KasHarness.resolve_spawn`) and the dashboard's
Install button (``POST /api/kas-bundle/install``). Both run here, in the gateway
process, never inside an agent sandbox: the ``engines`` leaf is read-only there
(``sandbox._CREW_READONLY_LEAVES``), so a sandboxed caller is refused with the
command to run instead.

One install at a time per process: a second caller waits for the running one and
then reads what it laid down, so two spawns (or a spawn and a click) never race two
writers into the same staging directory. The state the dashboard shows (running,
bytes so far, the last error) is a snapshot of that one install.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.acp.kas_bundle import InstalledKasBundle

logger = logging.getLogger(__name__)

__all__ = [
    "KAS_ENGINE_RETRY_COMMAND",
    "install_pinned_engine",
    "install_snapshot",
    "offline_install_message",
]

#: What a person runs to retry by hand.
KAS_ENGINE_RETRY_COMMAND = "kirocrew kas install"

#: Serializes installs in this process. A plain lock, not an asyncio one: the
#: install is blocking work on a worker thread, and its two callers live on
#: different threads.
_INSTALL_LOCK = threading.Lock()

#: The dashboard's view of the install. Written under ``_STATE_LOCK`` only.
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "error": "",
}


def install_snapshot() -> dict[str, Any]:
    """A copy of the install state: ``running``, ``done``, ``total``, ``error``."""
    with _STATE_LOCK:
        return {
            "running": bool(_STATE["running"]),
            "done": int(_STATE["done"]),
            "total": int(_STATE["total"]),
            "error": str(_STATE["error"]),
        }


def _update(**fields: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(fields)


def offline_install_message(detail: str) -> str:
    """The error a failed install is reported with: the cause and the retry command."""
    detail = detail.strip().rstrip(".")
    return (
        f"Kiro Crew could not install its KAS engine: {detail}. If this host is offline, "
        f"connect it first. Then retry: run `{KAS_ENGINE_RETRY_COMMAND}` in a terminal, or "
        "press Retry in the dashboard."
    )


def _inside_agent_sandbox() -> bool:
    from kiro_crew.sandbox_launch_mark import SANDBOX_MARKER

    return bool(os.environ.get(SANDBOX_MARKER))


def install_pinned_engine(
    base_url: str,
    *,
    caller: str,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> InstalledKasBundle:
    """Install the pinned engine unless it is there; return it. Blocking.

    *caller* names who asked (``"spawn"`` or ``"dashboard"``) for the log line.
    Raises :class:`kiro_crew.acp.kas_bundle.KasBundleError` with
    :func:`offline_install_message`'s text on any failure, so the error a chat
    shows already says how to retry.
    """
    from kiro_crew.acp import kas_bundle as kas_bundle_mod

    present = kas_bundle_mod.installed_bundle()
    if present is not None:
        return present
    selected = kas_bundle_mod.pinned_version()
    if selected != kas_bundle_mod.KAS_PIN_VERSION:
        # Only the release's pin is installed unattended: a version chosen with
        # the override is laid down by hand, never swapped for the pin.
        raise kas_bundle_mod.KasBundleError(
            f"KAS {selected} is selected but not installed, and only this release's "
            f"pin ({kas_bundle_mod.KAS_PIN_VERSION}) is installed automatically. Run "
            f"`{KAS_ENGINE_RETRY_COMMAND} --version {selected} --unpinned`, or unset "
            "the version override."
        )
    if _inside_agent_sandbox():
        raise kas_bundle_mod.KasBundleError(
            "Kiro Crew's KAS engine is not installed, and it cannot be installed from "
            f"inside an agent sandbox. Run `{KAS_ENGINE_RETRY_COMMAND}` in a terminal."
        )
    with _INSTALL_LOCK:
        # Another caller may have installed it while this one waited.
        present = kas_bundle_mod.installed_bundle()
        if present is not None:
            return present
        totals: dict[str, tuple[int, int]] = {}

        def _progress(label: str, done: int, total: int) -> None:
            totals[label] = (done, total)
            _update(
                done=sum(d for d, _ in totals.values()),
                total=sum(t for _, t in totals.values()),
            )
            if on_progress is not None:
                on_progress(label, done, total)

        _update(running=True, done=0, total=0, error="")
        logger.warning(
            "KAS engine %s is not installed; downloading it now (asked by %s)",
            kas_bundle_mod.pinned_version(),
            caller,
        )
        try:
            installed = kas_bundle_mod.install_pinned(base_url, on_progress=_progress)
        except kas_bundle_mod.KasBundleError as exc:
            message = offline_install_message(str(exc))
            _update(running=False, error=message)
            logger.warning("KAS engine install failed: %s", exc)
            raise kas_bundle_mod.KasBundleError(message) from exc
        except Exception as exc:
            message = offline_install_message(f"{type(exc).__name__}: {exc}")
            _update(running=False, error=message)
            logger.exception("KAS engine install failed")
            raise kas_bundle_mod.KasBundleError(message) from exc
        _update(running=False, error="")
        logger.info("KAS engine %s installed at %s", installed.version, installed.root)
        return installed
