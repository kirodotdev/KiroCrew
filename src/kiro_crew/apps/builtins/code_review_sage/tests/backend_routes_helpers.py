"""Shared harness for the Code Review Sage backend-route tests (``test_backend_routes*.py``).

``_load_routes_module`` loads a private copy of ``backend/routes.py`` for one test. The kept
``test_backend_routes.py`` and its planted_links, consolidation, run_lifecycle and followup
parts import it from here, and ``test_review_queue.py`` and ``test_user_repos.py`` reach it
through the kept file. A helper only one part uses stays in that part.
"""

import importlib.util
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _APP_ROOT / "backend" / "routes.py"


def _load_routes_module():
    spec = importlib.util.spec_from_file_location("sage_backend_routes_under_test", str(_ROUTES))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
