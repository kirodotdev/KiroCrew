"""``python -m auto_improvement_app.spine`` entry point.

Runs the spine driver CLI. Use ``--dry-run`` to exercise the full pipeline with the
stub profile end-to-end (M0 exit criterion), or ``--go`` once a real Target Profile
is configured (M2/M3).
"""

from __future__ import annotations

import sys

from .driver import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
