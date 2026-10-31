"""Browser Harness under the gateway: install, environment fence, relay, supervision.

The agent browses through ONE driver, `Browser Harness
<https://github.com/browser-use/browser-harness>`_, pinned to one version and run as a
daemon the gateway owns. Nothing in this package calls Harness's CLI, its MCP server,
its ``run.py`` or its update check: the gateway starts ``browser_harness.daemon`` in a
scrubbed environment (:mod:`.env`), hands it a loopback CDP relay (:mod:`.relay`) as if
the relay were the browser, and invokes an allowlist of Harness's own helpers through
Harness's IPC (:mod:`.supervisor`).

Module map, in the order a first run touches them:

* :mod:`.install` -- the pinned venv at ``<home>/browser-harness`` and its lock file.
* :mod:`.chromium` -- the Kiro-owned Chromium download for hosts with no Chrome.
* :mod:`.discover` -- which browser to drive on this host.
* :mod:`.relay` -- the one CDP connection, shared between the daemon and the gateway.
* :mod:`.supervisor` -- browser, relay and daemon lifecycle; the ``status()`` the
  Settings page and the live card read.
* :mod:`.governance` -- ``capabilities.browse`` on the gateway: one deny turns all
  of the above off.

Design: ``docs/request-for-change/rfc-browser-live-harness.md``.
"""

from __future__ import annotations

#: The one Harness release this package drives. A bump re-reads ``telemetry.py``
#: and ``auth.py`` in the new release before it lands, and regenerates
#: ``requirements.lock`` (see :mod:`.install`).
HARNESS_VERSION = "0.1.13"

__all__ = ["HARNESS_VERSION"]
