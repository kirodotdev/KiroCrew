"""Kiro Crew Auto Triage Pipeline — a full-page view of Kiro Crew's own
auto-triage pipeline.

The pipeline itself is a chain of scheduled jobs that already exist and already
own their state: a scanner labels new issues, a triage pass classifies them, a
dispatcher opens one chat session per accepted item, that session does the work
and opens the pull request, and a cleanup pass reaps what died. This app does not
reimplement any of that. It READS the trail those jobs leave and presents it as
three nested levels:

* the pipeline and how much each step is moving,
* the items sitting inside one step, each carrying its own trail,
* the agent sessions that worked one item, and what each cost.

The app ships a backend, but a strictly read-only one: three GET routes and no
write path of any kind. That boundary is deliberate. The scheduled jobs hold
hard-won operational semantics -- an instance-aware claim protocol, heartbeat
liveness, terminal-event backfill for sessions that exit without reporting,
resume bounds, dispatch budget and jitter -- most of which exist because
something failed in production once. Re-deriving them inside a view would mean
re-earning each lesson, and would give a display surface the power to act on the
repository. It has none.

Listed in ``kiro_crew.apps.builtins.BUILTIN_NAMES`` because
``backend/routes.py`` re-exported here registers routes at gateway startup.
"""

from .backend.routes import register_routes

__all__ = ["register_routes"]
