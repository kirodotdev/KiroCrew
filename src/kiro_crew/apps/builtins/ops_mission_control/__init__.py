"""Ops Mission Control — an autonomous ops first responder.

Polls signal providers (CloudWatch alarms, PagerDuty incidents, Datadog monitors,
GitHub issues, signed webhooks), claims what is firing, investigates it in a real
chat session mirrored to Slack, matches it against a compounding knowledge ledger,
and proposes an action. Read-only by default; write authority is opt-in per signal
pattern.

Provider adapters sit behind four narrow Protocols (``backend/providers/base.py``)
registered through an ADD-only registry, so an out-of-tree companion package
can contribute its own adapters without this core ever importing it or branching
on edition.
"""

# Required re-export: ``dashboard/server.py``'s startup loop imports the PACKAGE
# and checks ``hasattr(_mod, "register_routes")`` on it — not on the
# ``backend.routes`` submodule. Without this line the routes silently never
# register (same re-export as issue_radar and code_review_sage).
from .backend.routes import register_routes  # noqa: F401
