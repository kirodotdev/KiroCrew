"""Issue Radar's route handlers, one module per responsibility.

``backend.routes`` is the facade over this package. It keeps the request gates
every handler runs -- key parsing, the enabled / connected / write-permission
checks, per-repo store scoping, the SEL audit, the error aliases -- plus
``/connect``, the probe-gated list-poll decision and ``register_routes``, and it
re-exports every name defined here. The handlers live here:

  repositories     connected repos, settings, labels, members, the account reads
  items            issue and pull list / first-page / search / detail / ref reads
  deps             the dependency graph and its per-app background refresh tasks
  ai               the one-shot model adapter, output language, issue + PR summaries
  recommendations  the label-taxonomy proposal and label creation
  tagging          the untagged queue, its batched suggestions and bulk apply
  issue_writes     label, state and assignee writes on one issue
  investigation    the local per-item investigation record
  pr_actions       the privileged pull-request actions and their shared preamble

**What ``backend.routes`` owns, and every monkeypatch seam, is reached through it
at call time.** A handler here does ``from .. import routes`` inside the function
and reads ``routes._connected``, ``routes._st``, ``routes._run_pr_action`` and so
on, so a patch on ``backend.routes`` intercepts the call wherever it is made. The
import is function-local for the reason ``register_routes``'s ``crew_routes``
import is: ``backend.routes`` imports these modules, so a module-scope import back
would be a cycle. Anything else a handler needs from a sibling is imported from
its owning module.
"""
