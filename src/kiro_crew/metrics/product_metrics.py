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
        # Turns that hit the timeout ceiling: a closed `path` and two booleans.
        "kirocrew.turn.timeout.cause",
    }
)

# Crew sessions and session control (metrics/crew_log_export.py,
# metrics/session_control_calls.py). `agent_kind`, `actor`, `parent_kind` and
# `caller` fold anything outside their closed sets to `other`; `depth` and
# `nth` are fixed buckets.
_CREW = frozenset(
    {
        "kirocrew.crew.session.started",
        "kirocrew.crew.turn.started",
        "kirocrew.crew.session.tree.depth",
        "kirocrew.crew.session.tree.child",
        "kirocrew.crew.session_control.calls",
    }
)

# Work ledger and Crew Mode (metrics/work.py, metrics/crew_mode.py). Every
# attribute is a tuple-backed enum or a fixed bucket, with `other` as the
# remainder.
_WORK = frozenset(
    {
        "kirocrew.work.created",
        "kirocrew.work.item_closed",
        "kirocrew.work.dispatches",
        "kirocrew.work.reports",
        "kirocrew.crew_mode.turned_on",
        "kirocrew.crew_mode.new_chats",
        "kirocrew.crew_mode.runs_finished",
        "kirocrew.crew_mode.steers_per_run",
        "kirocrew.crew_mode.worker_waits",
    }
)

# Features: did this subsystem do its job, and how often.
_FEATURES = frozenset(
    {
        "kirocrew.cron.fires",
        "kirocrew.artifact.created",
        "kirocrew.workflow.runs",
        "kirocrew.approval.decisions",
        "kirocrew.autonudge.loops.started",
        "kirocrew.autonudge.loops.stopped",
        "kirocrew.autonudge.ticks",
        # A registered monitor kind or `other` (metrics/monitor_loops.py).
        "kirocrew.monitor.decisions",
        # Outcome folded by webhooks.metric_outcome.
        "kirocrew.webhook.requests",
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
        # Crewmates the person created; the count rides in the value.
        "kirocrew.inventory.members",
        # The selected dictation and reply provider (metrics/voice.py labels).
        "kirocrew.inventory.voice.provider",
    }
)

# Voice: dictation and spoken replies (metrics/voice.py). `provider` is a
# built-in name, `registered` for an edition's, or `other`; every other value is
# a tuple-backed enum or a fixed bucket. No transcript, reply text, voice id,
# language or error message is an attribute.
_VOICE = frozenset(
    {
        "kirocrew.voice.stt.started",
        "kirocrew.voice.stt.ended",
        "kirocrew.voice.stt.errors",
        "kirocrew.voice.unavailable",
        "kirocrew.voice.tts.replies",
    }
)

# In-product recommendations: feature tips, the Explore panel (milestones.py)
# and feature videos -- are they used, and does a recommended feature get used.
# Every attribute value is closed at its emit site: tip and video features are
# shipped doc names or Explore ids, milestone values are catalog keys, and
# milestone.reached{recommended_by} is one of tip / explore / video / several /
# none. No tip id, video id or timestamp is an attribute.
_RECOMMENDATIONS = frozenset(
    {
        "kirocrew.tips.feedback",
        "kirocrew.feature_video.status",
        "kirocrew.milestone.panel_opened",
        "kirocrew.milestone.starter_clicked",
        "kirocrew.milestone.reached",
        "kirocrew.milestone.skipped",
        "kirocrew.milestone.mark_all_done",
        "kirocrew.milestone.notice_shown",
        "kirocrew.milestone.features_completed",
    }
)

#: The v1 export allowlist: every metric a product destination may receive.
PRODUCT_METRICS: "frozenset[str]" = (
    _SESSIONS | _CREW | _WORK | _FEATURES | _INVENTORY | _VOICE | _RECOMMENDATIONS
)

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
#   kirocrew.knowledge.searched
#
# Held on the kind oracle:
#   kirocrew.crew.session.tree.roots -- an observable gauge declared outside
#     inventory_gauges / process_gauges, so the kind check would read it as a
#     counter. It joins once the test's gauge population includes it.
#
# Not proposed: kirocrew.crew_log.* is the crew log's own runtime health
# (queue depths, fold timings, lost writes). It stays on the machine.
