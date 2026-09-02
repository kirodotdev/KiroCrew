"""Playwright CLI browser backend (``@playwright/cli``).

Three concerns, one per module, with no shared mutable state:

- :mod:`kiro_crew.browser_cli.install` — detection, installation, and the
  capability gate. Presence of ``playwright-cli`` is the gate; there is no
  toggle, flag, or consent file anywhere in this package.
- :mod:`kiro_crew.browser_cli.os_deps` — which Linux hosts the browser download's
  ``--with-deps`` flag can serve, and the command handed to the operator on the
  ones it cannot. Reads ``/etc/os-release``; never runs a package manager.
- :mod:`kiro_crew.browser_cli.view` — supervises ``playwright-cli show``, the
  CLI's own dashboard, as a long-lived loopback-only child process.
- :mod:`kiro_crew.browser_cli.snapshots` — retention for the timestamped YAML
  the CLI writes per command, which it never prunes itself.

Every helper here is synchronous and several spawn subprocesses or sleep, so a
caller on the gateway event loop offloads them (``asyncio.to_thread``).
"""

from __future__ import annotations
