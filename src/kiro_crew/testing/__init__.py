"""Test utilities for KiroCrew consumers.

Everything under ``kiro_crew.testing`` is intended for downstream test
suites that want to exercise code against a populated ``$KIROCREW_HOME``
without hand-rolling setup. The module is in the runtime wheel (not
``dev_requirements``) so third-party packages can ``pip install kirocrew``
and use it immediately.

Public entry points:

- :mod:`kiro_crew.testing.fixtures` — ``seeded_home`` plain context manager
  and ``seeded_home_fixture`` pytest fixture for setting up an isolated
  ``$KIROCREW_HOME`` from a named fixture.
- :mod:`kiro_crew.testing.harness` — ``spawn_feature_gateway`` context
  manager that spins up an isolated, headless gateway from the workspace
  source tree (compose with ``--test-mode`` / ``--json-ready``); yields a
  ``GatewayHandle`` with the dashboard URL and tears down on exit.

Import submodules directly (``from kiro_crew.testing.fixtures import ...``,
``from kiro_crew.testing.harness import ...``); no top-level re-exports,
so the package namespace stays pytest-free for non-pytest consumers.
"""

# Bind submodules onto the package namespace so ``unittest.mock.patch``
# can traverse paths like ``kiro_crew.testing.harness.TERMINATE_GRACE_SECONDS``
# (patch walks dotted paths via getattr; without these binds the lookup
# fails even though the modules are in sys.modules). Both submodules are
# pytest-free at import time, so this doesn't pull pytest into consumers
# that only need the runtime helpers.
from kiro_crew.testing import fixtures, harness  # noqa: E402,F401
