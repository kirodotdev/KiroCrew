"""``PRODUCT_METRICS`` -- the reviewed metric names a product destination may get.

The local JSONL sink and an operator's own collector receive everything the core
collects. A PRODUCT destination -- one that reports how Kiro Crew is used, back to
whoever builds it -- receives only the names below, carried by
``OtlpDestination.allow``. Everything else stays on the machine: most instruments
exist for local debugging, and the larger the exported set, the larger the privacy
question each release has to answer.

Two rules this set obeys, both enforced by ``test_product_metrics.py``:

* **Counters and gauges only.** A fixed-bucket histogram's buckets may not survive
  a receiving backend, so a distribution is exported as a bucket-labelled counter
  instead. An up/down counter is excluded for the same reason a gauge replaces it:
  "how many right now" is read from live state, not accumulated.
* **Every name is a real instrument.** Each entry is a full instrument name,
  ``kirocrew.`` prefix included, that some module actually declares. The test
  resolves each one against the declaring constant, so a typo or a renamed
  instrument fails there rather than silently exporting nothing.

Names are spelled as literals rather than imported from the declaring modules on
purpose. This set is read where a destination is BUILT, which is downstream of
``metrics.provider``; ``metrics.turns`` imports that module at module scope, so
importing the declaring constants from here would close an import cycle through
it. Keeping this module stdlib-only avoids the question entirely, and the test
does the import and asserts the two agree -- the same guarantee, one step later.

A name joins this set only in a reviewed change that also updates the exported-
metrics list in ``docs/guides/telemetry-otlp-export.md``.
"""

from __future__ import annotations

# Sessions, turns and session trees.
_SESSIONS = frozenset(
    {
        "kirocrew.session.started",
        "kirocrew.session.idle_expired",
        "kirocrew.subagent.spawned",
        "kirocrew.subagent.completed",
        "kirocrew.context.compactions",
        # Finished turns (metrics/turn_completed.py): closed outcome,
        # trigger_source, origin and harness only.
        "kirocrew.turn.completed",
    }
)

# Features: did this subsystem do its job, and how often.
_FEATURES = frozenset(
    {
        "kirocrew.cron.fires",
        "kirocrew.artifact.created",
        "kirocrew.workflow.runs",
        "kirocrew.approval.decisions",
        "kirocrew.autonudge.loops.stopped",
        # Feature-use counters (metrics/feature_usage.py).
        "kirocrew.memory.recalled",
        "kirocrew.lesson.injected",
        "kirocrew.skill.lazy_load.count",
        "kirocrew.app.task.completed",
        "kirocrew.channel.message.received",
        "kirocrew.ui.control.clicked",
    }
)

# What is installed and switched on. Every one of these is an observable gauge
# read from live state at collection time.
_INVENTORY = frozenset(
    {
        "kirocrew.inventory.crons.active",
        "kirocrew.inventory.monitor_loops.active",
        "kirocrew.inventory.skills.installed",
        "kirocrew.inventory.memory.migrated",
        "kirocrew.inventory.knowledge.documents",
        "kirocrew.inventory.lessons",
        "kirocrew.inventory.mcp.servers",
        "kirocrew.inventory.config.toggle",
        "kirocrew.inventory.probe.failures",
    }
)

#: The v1 export allowlist: every metric a product destination may receive.
PRODUCT_METRICS: "frozenset[str]" = _SESSIONS | _FEATURES | _INVENTORY

# PENDING -- reviewed for export but NOT exportable yet. Each one joins
# PRODUCT_METRICS in the change that clears its reason, not before.
#
# Held on an OPEN attribute value, not on a missing instrument:
#   kirocrew.turn.tokens -- the instrument exists and the metric is wanted, but
#     its `model` attribute is the configured model id, which is not drawn from
#     any set this repo controls: on a non-Kiro backend it can be a provider
#     resource id, so its range is whatever that provider names and whatever the
#     person pointed the backend at. `provider` is configured the same way. Every
#     attribute value on an exported series has to come from a closed enum, so
#     this one waits for a bucketed or enumerated stand-in for those two. A
#     negative guard in test_product_metrics.py keeps it out, so putting it back
#     is a deliberate change rather than an edit nobody reads.
#
# Held on the instrument KIND:
#   kirocrew.turn.completed.duration -- every attribute is closed, but it is a
#     histogram, and this allowlist takes counters and gauges only. It joins
#     when the export path carries native histograms; until then the outcome
#     mix is read from kirocrew.turn.completed.
#
# Held on a missing instrument -- a name here with no instrument would export
# nothing while reading as if it did:
#
# Session trees and session control (open change):
#   kirocrew.crew.session.started, kirocrew.crew.turn.started,
#   kirocrew.crew.session.tree.depth, kirocrew.crew.session.tree.child,
#   kirocrew.crew.session.tree.roots, kirocrew.crew.session_control.calls
# Features (proposed):
#   kirocrew.autonudge.loops.started, kirocrew.autonudge.ticks,
#   kirocrew.monitor.decisions, kirocrew.knowledge.searched,
#   kirocrew.webhook.requests, kirocrew.work.created,
#   kirocrew.work.item_closed, kirocrew.work.dispatches, kirocrew.work.reports
# Inventory (proposed):
#   kirocrew.inventory.webhooks.registered
# Crew log health (proposed):
#   kirocrew.crew_log.write.lost, kirocrew.crew_log.fold.apply,
#   kirocrew.crew_log.fold.start, kirocrew.crew_log.savepoint.write,
#   kirocrew.crew_log.append.bytes, kirocrew.crew_log.append.batch,
#   kirocrew.crew_log.eager.queue_depth, kirocrew.crew_log.writer.buffered,
#   kirocrew.crew_log.bus.subscriber_error, kirocrew.crew_log.fold.lag
