"""Mochi — desktop companion builtin app.

Mochi is a desktop pet that lives on the user's screen: it chats through a
dedicated KiroCrew session, tells the user when a turn finishes or needs them,
watches things on their behalf, and plans its own day.

Architecture (see the migration notes on the Mochi builtin issue):

* This Python package owns the **autonomous half** — the task queue, the watch
  list, planning/replanning, reminders and stats. It runs in-process, like
  ``auto_research`` and ``issue_radar``, so it can use the gateway's subagent,
  cron and notification machinery directly with no HTTP hop and no app token.
* ``website/src/apps/mochi/`` owns the **renderer** — the pet sprite, chat UI
  and settings, served by the gateway and therefore same-origin with the
  dashboard (so plain ``fetch(..., {credentials: 'same-origin'})`` authenticates,
  exactly like the ``workflows`` builtin page).
* ``website/electron/mochi/`` owns the **window layer** — the transparent,
  always-on-top, click-through overlay per display, plus the tray and global
  shortcuts. These need Electron main-process APIs, so they live in KiroCrew's
  existing shell rather than shipping a second Electron runtime.

Porting discipline: modules moved from the original TypeScript implementation
are ported behaviour-first, pinned by a differential harness that runs both
implementations over identical inputs. That harness was migration-only tooling
(it needed the TypeScript tree present) and is not part of this package; the
behaviour it pinned is now covered by the ``test/test_mochi_*.py`` suites. Edge
cases that look like bugs are preserved deliberately — the TypeScript behaviour
is the specification until it is intentionally changed.
"""

# Required re-export: dashboard/server.py's startup route registration imports
# the PACKAGE and checks hasattr(_mod, "register_routes") — same convention as
# issue_radar/__init__.py (verified against the real call site at
# server.py:1956).
from kiro_crew.apps.builtins.mochi.backend.routes import register_routes  # noqa: F401,E402
