"""Install the pinned KAS engine when the gateway starts and finds none.

A platform wheel and the desktop apps carry the engine; a pure wheel, a git
checkout or a ``--no-kas`` install does not, and a wheel cannot run code when pip
installs it. So the gateway's first start is where the engine guarantee holds for
those installs: with no engine for this release's pin, the start downloads it
through the same single-flight installer a KAS spawn and the dashboard's Install
button use (:func:`kas_engine_install.install_pinned_engine`), whatever
``agent.kas_bundle`` says.

A failure does not stop the gateway: the dashboard must stay up to show why and
offer Retry. It is loud instead: logged as an error, held as the install state's
``error`` (the Agent Harness panel shows it and the next KAS chat refuses with it),
and ``kirocrew kas status`` and ``kirocrew doctor`` report the engine missing. A
host the engine cannot run on, or a version override the release does not pin, is
not an install to attempt; the installer's own refusal is logged once.

``KIROCREW_SKIP_KAS_ENGINE_INSTALL=1`` turns the start-time install off, for the test
harness's gateways (``testing.harness.harness_environment``), which boot in fresh
homes and must not fetch the engine at every start. The first KAS chat still
installs it.
"""

from __future__ import annotations

import logging
import os

#: Set to ``1`` to skip the start-time install (the test harness's gateways).
SKIP_ENGINE_INSTALL_ENV = "KIROCREW_SKIP_KAS_ENGINE_INSTALL"

logger = logging.getLogger(__name__)


def ensure_engine_at_startup() -> None:
    """Install this release's pinned engine if no root has it. Blocking; never raises."""
    if os.environ.get(SKIP_ENGINE_INSTALL_ENV) == "1":
        return
    from kiro_crew.acp import kas_bundle, kas_engine_install
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        if kas_bundle.installed_bundle() is not None:
            return
        if kas_bundle.pinned_version() != kas_bundle.host_pin_version():
            return
        base_url = KiroCrewConfig.load().agent.kas_bundle_url
        kas_engine_install.install_pinned_engine(base_url, caller="startup")
    except kas_bundle.KasHostUnsupported as exc:
        logger.warning("%s", exc)
    except Exception as exc:  # noqa: BLE001 - the gateway keeps serving; the state holds the error
        logger.error("Kiro Crew started without its KAS engine: %s", exc)
