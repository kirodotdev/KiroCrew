"""Install-inventory observable instruments -- what this install has configured.

``process_gauges`` answers "how is this process behaving"; nothing answers "what
does this install actually have set up". An operator running Kiro Crew across
more than one machine has no way to see that a host stopped scheduling crons, or
that skills and knowledge documents are piling up on one box and absent on
another: the dashboard shows one machine's counts on request, and nothing samples
them over time, so a config drift between hosts is only visible by opening each
dashboard in turn.

WHAT THESE CANNOT ANSWER, and it matters because the metric names invite the
mistake: they are NOT project-wide feature-adoption analytics. Collection is
off by default, egress is a second opt-in, and OTLP export additionally requires
the ``kirocrew[otlp]`` extra, so any aggregate over these gauges describes one
operator's own opted-in fleet and nothing else. That is not an accident to be
fixed later -- ``beacon.py`` is the project's install-analytics channel, and its
docstring lists four independently disqualifying reasons adoption data cannot
ride the metrics trunk (the redaction guardrail eats an install id, the extra
skews the population, ``telemetry.enabled`` is a published no-egress promise, and
pre-aggregated points cannot be deduped per install). Every instrument here
serves the same job the rest of ``kirocrew.*`` serves: an operator observing
machines they run.

HOST ATTRIBUTION IS BOUNDED BY THE RESOURCE, not by anything in this module, and
the bound is worth knowing before building a per-host dashboard on it. These
gauges carry no host attribute of their own -- deliberately, since a hostname is
free-form and routinely embeds an employee alias -- so separating two machines
depends entirely on the ``Resource`` ``provider.py`` builds. Today that resource
is ``service.name`` plus whatever the SDK's own detector contributes. On the
version this project pins, that includes a ``service.instance.id``, but the SDK
generates it FRESH PER PROCESS: two hosts are distinguishable at any instant,
while a single host's series restarts whenever its gateway does, so
"watch this machine's cron count over time" is not something a reader should
assume works yet. The dependency range also permits SDK versions that contribute
no instance id at all, in which case two hosts are genuinely indistinguishable.
Both are properties of the shared resource -- every ``kirocrew.process.*`` gauge
has them already -- and both are closed by giving the resource a persisted,
install-scoped identity rather than by anything here; these instruments inherit
that the moment the resource gains it.

These instruments close that gap the same way ``process_gauges`` does — OTEL
*observable* (asynchronous) instruments, whose callbacks run only when a
``PeriodicExportingMetricReader`` collects, on that reader's own ticker thread.
No sampler thread of our own, no work at rest, and nothing at all unless
telemetry is enabled: registration happens on ``provider._build_recorder()``'s
live path only, so the ``telemetry.enabled`` consent gate covers these gauges
too.

Instruments (all in the core ``kirocrew.`` namespace, validated against
``schema.validate_name`` at registration):

=========================================  =========  ==============  ==================
name                                       value      attributes      source
=========================================  =========  ==============  ==================
``kirocrew.inventory.crons.active``        count      —               ``crons.json``
``kirocrew.inventory.monitor_loops.active`` count     —               ``autonudge`` service
``kirocrew.inventory.skills.installed``    count      —               skills loader
``kirocrew.inventory.memory.migrated``     0/1        —               ``memory.migrated``
``kirocrew.inventory.knowledge.documents`` count      —               ``knowledge.db``
``kirocrew.inventory.lessons``             count      —               ``lessons.jsonl``
``kirocrew.inventory.mcp.servers``         count      ``class``       MCP roster
``kirocrew.inventory.config.toggle``       0/1        ``key``         config flags
``kirocrew.inventory.probe.failures``      count      ``probe``       this module
=========================================  =========  ==============  ==================

A probe that cannot answer returns ``None`` and its instrument yields **no
observation** for that cycle. That distinction is load-bearing: "the monitor
service is not running in this process" and "the service is running with zero
loops armed" are different facts, and a fake ``0`` would merge them.

But absence has a second reading, and it is the one that would undermine this
module: a series that is not there could equally mean the probe BROKE, or that the
host stopped exporting altogether -- and telling a drifted host from a dark host
is exactly the job these gauges exist to do. That is why a raising probe
increments :data:`GAUGE_PROBE_FAILURES` instead of only logging. A broken probe
then presents as DATA -- the failure series is present and climbing, which a host
that stopped exporting cannot produce -- and the first failure of each probe is
logged at WARNING, which a default install actually collects, rather than at debug
where it would be invisible on precisely the installs that need it.

So ``None`` alone is not the test for whether something is wrong. The line is
whether the SOURCE is absent or unreadable, and each reader draws it explicitly:
an absent source is a silent gap, because nothing is broken (no auto-nudge service
in this process, a knowledge database on an install that never ingested). A source
that is PRESENT and unreadable is a fault, so those readers count a failure before
returning ``None`` -- an unparseable ``crons.json`` is the case in point, and
``cron._read_job_records`` names it a fault in as many words.

That line is drawn wherever the source lets it be drawn, which is not everywhere.
:func:`read_mcp_server_classes` is the one probe that cannot: the discovery loaders
it calls swallow a malformed or unreadable config internally and merge ``{}``, so an
empty roster and a broken config arrive identical and its ``None`` covers both. Its
docstring says so. Reading a silent ``None`` as "nothing to report" is therefore
sound for every probe whose source can distinguish the two, and for the MCP probe it
is the best available answer rather than a guarantee -- which is worth knowing before
building an alert on that series' absence.

Every one of them reports a **raw count**, including the two that read a store
rather than a config field. Publishing those two as a constant 1 under a
magnitude-band attribute is the tempting alternative, on the theory that a precise
document or lesson count is quasi-identifying -- but that theory does not survive
this module's own scope. These metrics go only where the operator points the
exporter: their own collector, about their own machines, and an operator can
already identify their own hosts by far more than a document count. Meanwhile the
band costs exactly what the gauge is for. Growth from 51 to 200 sources is the
drift worth seeing and a band renders it as a flat line; a band attribute is up to
five series where an integer value is one; and fixing the boundaries at emit time
is a one-way door once dashboards read them. A backend can band a raw count at
query time, and cannot recover a count from a band. The product's own thresholds
are still worth knowing when reading these -- 200 is the lesson-store prune
ceiling -- so the operator guide names them instead of the wire format
hard-coding them.

**Cost is the whole design constraint here.** A callback runs on every export
interval (60s by default) and, with an OTLP destination configured, once per
reader — so two ticker threads can enter the same callback concurrently. Every
probe is therefore either O(1) in-memory or explicitly cached, and the two
genuinely expensive sources are the reason :func:`_ttl_cached` exists:

=========================  ===========================================  ==========
probe                      cost                                         cache
=========================  ===========================================  ==========
crons                      one small ``json.loads`` of ``crons.json``   none
monitor loops              in-memory dict snapshot                      none
skills                     recursive ``os.walk`` of the skills tree     300s TTL
memory / config toggles    fingerprint-cached config read (2 stats)     none
knowledge documents        ``sqlite3`` connect + one indexed COUNT(*)   300s TTL
lessons                    mtime-cached JSONL read                      store's own
MCP servers                a few small ``json.loads``                   none
=========================  ===========================================  ==========

Nothing here walks a whole data directory, opens a write connection, or creates a
file. The knowledge probe in particular opens SQLite **read-only** and only when
the database already exists: constructing a ``KnowledgeStore`` would run schema
init and graph load, and would *create* the database on an install that has never
ingested anything — a telemetry gauge must never bring a subsystem into being.

Where the underlying source can only say "empty" rather than distinguishing empty
from unavailable, the docstring on that reader says so.

Cardinality: every metric name is a constant, and the only attribute values are
closed enums -- two ``class`` buckets, the fixed :data:`CONFIG_TOGGLES` key set,
and the fixed :data:`ALL_PROBES` names. Counts ride in the VALUE, which costs no
series at all. An MCP server's NAME is never an attribute, by design: the roster
is user-chosen free-form text, so it is both a cardinality bomb and a disclosure
of what the user has installed. Only the two-way first-party/third-party split
leaves the machine.

First-party imports are deferred into the readers rather than taken at module
scope. ``metrics`` sits below ``cron``/``skills``/``autonudge``/``knowledge`` in
the dependency order, so importing them here would invert it and risk a cycle;
deferring also keeps the default-off path from paying for imports it never uses.
The opentelemetry import is deferred into :func:`register_inventory_gauges` for
the reason ``process_gauges`` gives — the module stays importable when the SDK is
absent.

OSS-CLEAN: opentelemetry (Apache-2.0) + stdlib + first-party helpers only.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Optional

from kiro_crew.metrics.schema import validate_name

if TYPE_CHECKING:  # annotation-only; never imported at runtime
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

logger = logging.getLogger(__name__)

GAUGE_CRONS_ACTIVE = "kirocrew.inventory.crons.active"
GAUGE_MONITOR_LOOPS_ACTIVE = "kirocrew.inventory.monitor_loops.active"
GAUGE_SKILLS_INSTALLED = "kirocrew.inventory.skills.installed"
GAUGE_MEMORY_MIGRATED = "kirocrew.inventory.memory.migrated"
GAUGE_KNOWLEDGE_DOCUMENTS = "kirocrew.inventory.knowledge.documents"
GAUGE_LESSONS = "kirocrew.inventory.lessons"
GAUGE_MCP_SERVERS = "kirocrew.inventory.mcp.servers"
GAUGE_CONFIG_TOGGLE = "kirocrew.inventory.config.toggle"
GAUGE_PROBE_FAILURES = "kirocrew.inventory.probe.failures"

ALL_METRIC_NAMES = (
    GAUGE_CRONS_ACTIVE,
    GAUGE_MONITOR_LOOPS_ACTIVE,
    GAUGE_SKILLS_INSTALLED,
    GAUGE_MEMORY_MIGRATED,
    GAUGE_KNOWLEDGE_DOCUMENTS,
    GAUGE_LESSONS,
    GAUGE_MCP_SERVERS,
    GAUGE_CONFIG_TOGGLE,
    GAUGE_PROBE_FAILURES,
)

#: Gauges here whose reading is a monotonic PROCESS-LIFETIME total rather than a
#: point-in-time state -- the same category as the process CPU/GC readings in
#: :mod:`kiro_crew.metrics.process_gauges`, and declared the same way so the
#: dashboard aggregator reduces them window-relative instead of reporting a
#: running total as a current reading. ``probe.failures`` was an observable
#: COUNTER, which made it the last CUMULATIVE series Kiro Crew exported: one is
#: enough to force every consumer into stateful whole-hour aggregation, since a
#: cumulative counter's hourly increment is last-minus-first across the whole
#: hour per host and per process lifetime. The DELTA route is not the
#: alternative -- an observable callback reads an external lifetime total, so the
#: first collection after a provider rebuild would re-emit the whole total as one
#: giant delta (see :mod:`kiro_crew.metrics.temporality`).
LIFETIME_TOTAL_METRICS = (GAUGE_PROBE_FAILURES,)

#: Probe identities for the ``probe`` attribute on :data:`GAUGE_PROBE_FAILURES`.
#: A closed set, one per reader, so the attribute stays enum-like.
PROBE_CRONS = "crons"
PROBE_MONITOR_LOOPS = "monitor_loops"
PROBE_SKILLS = "skills"
PROBE_MEMORY = "memory"
PROBE_KNOWLEDGE = "knowledge"
PROBE_LESSONS = "lessons"
PROBE_MCP = "mcp"
PROBE_CONFIG_TOGGLES = "config_toggles"

ALL_PROBES = (
    PROBE_CRONS,
    PROBE_MONITOR_LOOPS,
    PROBE_SKILLS,
    PROBE_MEMORY,
    PROBE_KNOWLEDGE,
    PROBE_LESSONS,
    PROBE_MCP,
    PROBE_CONFIG_TOGGLES,
)

#: The knowledge table this module counts. A named constant rather than a literal
#: buried in the query, so a test can assert the real ``KnowledgeStore`` schema
#: still has it: the probe deliberately never constructs a store (that would create
#: the database), which is exactly why nothing else would notice the owner renaming
#: what is counted, and the resulting gap is cached and indistinguishable from
#: "never ingested".
KNOWLEDGE_SOURCES_TABLE = "sources"

#: Attribute values for :data:`GAUGE_MCP_SERVERS`.
MCP_CLASS_FIRST_PARTY = "first_party"
MCP_CLASS_THIRD_PARTY = "third_party"

#: The closed toggle enum for :data:`GAUGE_CONFIG_TOGGLE`, as
#: ``(attribute key, config section, boolean field)``. Adding a row is the only
#: way to widen it, which is what keeps the ``key`` attribute bounded.
#:
#: ``telemetry.enabled`` is deliberately absent: this module only ever runs
#: inside the consent gate, so it could report nothing but 1 — a tautology
#: dressed as a measurement. ``memory`` is absent for the opposite reason: it has
#: its own gauge (see :func:`read_memory_migrated`). Every row is a plain feature
#: switch; none carries a path, an endpoint, or any user-chosen string.
CONFIG_TOGGLES: tuple[tuple[str, str, str], ...] = (
    ("beacon", "telemetry", "beacon_enabled"),
    ("knowledge_auto_ingest", "knowledge", "auto_ingest_artifacts"),
    ("mcp_gateway", "mcp_gateway", "enabled"),
    ("session_sharing", "agent", "session_sharing"),
    ("tool_search", "agent", "tool_search"),
    ("session_control", "agent", "session_control"),
    ("skills_auto_create", "skills", "auto_create_from_sessions"),
    ("session_summary", "session_summary", "enabled"),
    ("instances", "instances", "enabled"),
    ("stt", "stt", "enabled"),
)

#: TTL for the two probes that cannot be made cheap (a recursive skills walk and
#: a SQLite connect). Five minutes rather than the 60s export interval: these
#: answer "what is installed", which changes on the scale of a user action, so a
#: reading up to one TTL stale is indistinguishable in a fleet aggregate while
#: costing a fifth as much on the default interval.
_EXPENSIVE_TTL_SECS = 300.0

# Guards the TTL cache and the lazily-built loader singletons below. A real lock
# rather than "the callbacks all share one thread": each metric reader collects
# on its OWN ticker thread, so an install with an OTLP destination configured
# runs these callbacks on two threads concurrently.
_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}

# Reused across ticks so the recursive skills walk is not re-constructed (and its
# own internal cache not discarded) on every collection. Built lazily under
# ``_lock`` and touched by nothing else in the process.
_skills_loader: Any = None
_lesson_store: Any = None

# Per-probe failure counts, published as GAUGE_PROBE_FAILURES. This exists
# because a broken probe and a host that stopped exporting look IDENTICAL at a
# backend -- both are a series that simply is not there -- and telling those apart
# is the whole job these gauges were added to do. A failure count turns the first
# case into DATA: the series is present and climbing, which a missing host cannot
# produce. Written under ``_lock``.
_probe_failures: dict[str, int] = {}
# Probes already reported at WARNING. The first failure of each probe is loud
# enough for a default install to collect (the journal keeps WARNING and above, so
# a debug line would be invisible on exactly the installs that need it); repeats
# drop to debug so a permanently broken probe cannot flood the log once per export
# interval for the life of the process.
_warned_probes: set[str] = set()


def _note_probe_failure(probe: str) -> None:
    """Record that *probe* could not answer, and say so once at WARNING."""
    with _lock:
        _probe_failures[probe] = _probe_failures.get(probe, 0) + 1
        first = probe not in _warned_probes
        if first:
            _warned_probes.add(probe)
    if first:
        logger.warning(
            "inventory probe %r failed; its gauge will report no value until it "
            "recovers (see %s for the count)",
            probe,
            GAUGE_PROBE_FAILURES,
            exc_info=True,
        )
    else:
        logger.debug("inventory probe %r failed again", probe, exc_info=True)


def read_probe_failures() -> dict[str, int]:
    """Snapshot of per-probe failure counts. Empty until something fails."""
    with _lock:
        return dict(_probe_failures)


# Whether THIS process is the one that publishes install-scoped inventory.
#
# The problem this solves: ``_build_recorder`` runs in every process that touches
# telemetry, and an install runs several at once -- the gateway, the MCP gateway
# daemon, spawned agents and apps (which is why the local exporter shards per
# PID). Process-scoped instruments want exactly that. Install-scoped ones do NOT:
# every process would report the SAME "this install has 14 crons", each under its
# own resource, so an aggregate over installs counts one install once per running
# process -- a fleet sum reads N times the truth, and a fleet average is dragged
# toward whichever installs happen to run the most processes.
#
# Deduplicating downstream is not available: that needs an install-scoped resource
# identity, and the resource carries only a per-process id today (see the module
# docstring). So exactly one process must publish, and it is ELECTED EXPLICITLY by
# the gateway calling :func:`mark_install_reporter` rather than inferred here. An
# inference was the alternative -- ``autonudge.get_instance()`` is non-None only in
# the gateway, so it would work today -- but it silently stops being true if that
# service's startup moves or becomes conditional, and the failure mode is inventory
# quietly disappearing. A named call is a contract a reader can find.
#
# What the election does and does not cover: it elects ONE GATEWAY PROCESS, which is
# one install everywhere the product runs one gateway per data home. Two gateways
# sharing a data home on different ports would both claim and both publish, because
# the dashboard's singleton is per PORT, not per data home -- a pre-existing property
# of that configuration (every install-scoped fact duplicates, not just these), and
# not something a flag can detect: a second gateway double-counts whether or not it
# runs crons. Closing it needs either cross-process arbitration or, more cheaply, the
# persisted install-scoped resource id that lets a consumer dedupe. A POD is NOT this
# case: it boots with its own ``KIROCREW_HOME`` and never copies crons, sessions or
# the databases, so its inventory is a genuinely different install's and publishing
# it is correct.
_is_install_reporter = False


def mark_install_reporter() -> None:
    """Declare THIS process the publisher of install-scoped inventory.

    Called once from the gateway's startup, from a point no branch guards: a
    flag-gated call site means no process claims the role and every
    ``kirocrew.inventory.*`` series is absent, with nothing to say why (see
    ``test_reporter_claim_is_unconditional``). Every other telemetry-enabled
    process in the install leaves it unset and publishes no ``kirocrew.inventory.*``
    series, so install-level aggregates count each install once.

    Idempotent, and deliberately has no "unmark": a process that has claimed the
    role keeps it for its lifetime. Consulted at COLLECT time rather than at
    registration, so it does not matter whether the first recorder is built before
    or after this call -- a recorder built early simply publishes nothing until the
    claim lands, which is at worst one export interval of absence.
    """
    global _is_install_reporter
    with _lock:
        _is_install_reporter = True


def is_install_reporter() -> bool:
    """Whether this process publishes install-scoped inventory."""
    with _lock:
        return _is_install_reporter


def reset_for_testing() -> None:
    """Drop the TTL cache and the loader singletons.

    Exists because both are module state that outlives a test: a cached count
    from one temp home would otherwise be served to the next.
    """
    global _skills_loader, _lesson_store, _is_install_reporter
    with _lock:
        _cache.clear()
        _probe_failures.clear()
        _warned_probes.clear()
        _skills_loader = None
        _lesson_store = None
        _is_install_reporter = False


def _ttl_cached(key: str, ttl: float, produce: Callable[[], "int | None"]) -> "int | None":
    """Return a cached value for *key*, refreshing *produce* no more than once per *ttl*.

    Not a strict "exactly once": *produce* runs OUTSIDE the lock, so two reader
    threads arriving on a cold entry can both call it. That is deliberate --
    holding the lock across a filesystem walk or a SQLite open would make the
    second reader block on the first -- and a benign double-produce is the
    accepted cost.

    Every outcome is cached, including failure. A ``None`` return and a raised
    exception both store ``None``: the reasons an expensive probe fails here (no
    database yet, an unreadable tree) are install-level states, so re-running it
    every cycle would pay the full cost precisely on the installs that can never
    answer. The exception is swallowed rather than propagated because the caller's
    contract is already "None means no observation"; letting it through would
    reach the same gap by a costlier route.
    """
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    try:
        value = produce()
    except Exception:  # noqa: BLE001 -- an unreadable source is a gap, not a crash
        _note_probe_failure(key)
        value = None
    with _lock:
        _cache[key] = (time.monotonic() + ttl, value)
    return value


# ---------------------------------------------------------------------------
# Raw readers — plain callables returning values (or None for "cannot answer"),
# kept SDK-free so they are unit-testable without an OTEL pipeline.
# ---------------------------------------------------------------------------


def read_active_crons() -> Optional[int]:
    """Count of scheduled cron jobs that are neither user- nor auto-paused.

    Reads ``crons.json`` directly instead of asking the live ``CronService``,
    because there is no process-global handle for it (it is owned by the
    dashboard state object) and because ``list_jobs`` re-arms the asyncio timer —
    calling that from a ticker thread with no running loop would raise, and worse,
    it cancels the existing timer first, which would stop every scheduled job.
    The reduction is ``cron.enabled_count_from_disk``, the single owner that
    ``CronService.count_enabled_from_disk`` also calls, so this cannot drift from
    what the scheduler considers enabled. That method keeps only the count; this
    probe needs the ``loadable`` half too.

    Returns ``None`` when the store exists but does not parse, and records a
    ``crons`` probe failure on the way out. Both halves matter: a count would be
    a lie (the scheduler loaded nothing either), and a silent gap would be
    indistinguishable from a host that stopped exporting, which is the one thing
    ``probe.failures`` exists to prevent. ``_read_job_records`` calls this case a
    fault in as many words, and its ``loadable`` flag is what separates it from a
    **missing** store — a genuine 0, since a fresh install has no crons.
    """
    from kiro_crew.cron import _CRONS_FILE, _default_dir, enabled_count_from_disk

    count, loadable = enabled_count_from_disk(_default_dir() / _CRONS_FILE)
    if not loadable:
        _note_probe_failure(PROBE_CRONS)
        return None
    return count


def read_active_monitor_loops() -> Optional[int]:
    """Count of armed monitor / auto-nudge loops in this process.

    Returns ``None`` when the service never started — a spawned agent process, a
    unit test, or a gateway with auto-nudge off — which is the case a fake 0
    would hide. A running service with nothing armed returns a real 0.

    The loop registry is mutated only under an ``asyncio.Lock`` on the event
    loop, which offers this thread no protection and cannot be acquired from it.
    Materialising ``list(...)`` first is what makes the read safe anyway: each
    dict operation is atomic under the GIL, so a snapshot cannot raise "changed
    size during iteration", and a gauge that is stale by one loop is fine.
    """
    from kiro_crew.autonudge import get_instance

    service = get_instance()
    if service is None:
        return None
    return sum(1 for loop in list(service._loops.values()) if loop.active)


def _read_installed_skills_uncached() -> Optional[int]:
    """Uncached skill count — a recursive walk. See :func:`read_installed_skills`."""
    global _skills_loader
    from kiro_crew.skills import SkillsLoader

    with _lock:
        if _skills_loader is None:
            # install_builtins=False: syncing builtins onto disk is a write path
            # with content hashing, and a metrics probe must not perform it.
            _skills_loader = SkillsLoader(install_builtins=False)
        loader = _skills_loader
    return len(loader._iter_visible())


def read_installed_skills() -> Optional[int]:
    """Count of skills visible to the loader, cached for :data:`_EXPENSIVE_TTL_SECS`.

    One total, not a builtin/user split: builtin skills are *copied onto disk*
    into the same tree as user-installed ones, so nothing in the listing
    distinguishes them and any split would be a guess presented as a fact.

    Returns ``None`` only if the walk raises. An empty tree is a real 0.
    """
    return _ttl_cached(PROBE_SKILLS, _EXPENSIVE_TTL_SECS, _read_installed_skills_uncached)


def read_memory_migrated() -> Optional[int]:
    """Whether this install's memory has been migrated to the vector store (0/1).

    Reports ``memory.migrated`` rather than a "memory enabled" switch, because no
    such switch exists: the embedding provider is coerced to a real value on
    load, so memory is structurally always on and an "enabled" gauge could only
    ever read 1.

    Reading 0 is the point, which is why this is not a rollout metric that goes
    quiet once a fleet converges. The gateway flips the bit on the first boot that
    initialises vector memory, fresh installs included, so a healthy install reads
    1 and keeps reading 1; it stays 0 when the subsystem did not come up -- vector
    memory never initialised, migration aborted before the flip, or the flag write
    was skipped because ``config.json`` is unparseable (that write fails closed
    rather than clobber the file, and retries next boot). In a converged fleet a 0
    is therefore an install whose memory subsystem is broken, and no other
    inventory series would show it.

    Returns ``None`` only if the config read raises; the field itself is always a
    concrete bool.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    return int(bool(KiroCrewConfig.load().memory.migrated))


def _read_knowledge_documents_uncached() -> Optional[int]:
    """Uncached knowledge-source count. See :func:`read_knowledge_documents`."""
    from kiro_crew.config.paths import default_workspace_dir

    db_path = default_workspace_dir() / "knowledge" / "knowledge.db"
    if not db_path.exists():
        # Never ingested: the database is created on first write, and this probe
        # must not be what creates it.
        return None
    # Read-only URI so this cannot create, migrate, or lock the database, and a
    # short timeout so a busy writer degrades to a gap instead of stalling the
    # export cycle.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        # Table name from a module constant, not inlined: see KNOWLEDGE_SOURCES_TABLE.
        # It is a constant of ours, never user input, so it cannot carry injection.
        query = f"SELECT COUNT(*) FROM {KNOWLEDGE_SOURCES_TABLE}"  # noqa: S608
        return int(conn.execute(query).fetchone()[0])
    finally:
        conn.close()


def read_knowledge_documents() -> Optional[int]:
    """Count of knowledge SOURCES, cached for :data:`_EXPENSIVE_TTL_SECS`.

    Sources rather than the ``items`` table: an item is one chunk of a document,
    so counting items would report a chunking artifact as a document count.

    Returns ``None`` when the database does not exist yet (nothing ingested) or
    cannot be read.
    """
    return _ttl_cached(PROBE_KNOWLEDGE, _EXPENSIVE_TTL_SECS, _read_knowledge_documents_uncached)


def read_lessons() -> Optional[int]:
    """Count of saved lessons.

    Uncached here on purpose: the store already caches by file mtime, so a
    repeated read on an unchanged file is one ``stat``. A second cache would only
    add staleness. The store instance is reused across ticks because that mtime
    cache lives on the instance — a fresh one per tick would re-read every time.

    An absent file returns 0 rather than ``None``: unlike a knowledge database,
    "no lessons file" is the honest steady state of an install that has never
    saved one, not an uninitialised subsystem.
    """
    global _lesson_store
    from kiro_crew.learn import LessonStore

    with _lock:
        if _lesson_store is None:
            _lesson_store = LessonStore()
        store = _lesson_store
    return len(store.load_all())


def read_mcp_server_classes() -> Optional[dict[str, int]]:
    """MCP server counts split into first-party and third-party.

    The first-party set is ``mcp_discovery._MANAGED_SERVER_NAMES``, the roster
    the installer itself manages, reused rather than restated: a second list of
    server names in this module would be a duplicate that drifts silently the
    first time a managed server is added or renamed.

    Server names are read to classify and then discarded. No name reaches an
    attribute — a user's MCP roster is free-form text, so publishing it would be
    both unbounded cardinality and a disclosure of what they have installed.

    Returns ``None`` when the merged roster is empty. The underlying loaders fail
    soft to ``{}``, so an empty result cannot be told apart from an unwritten
    config — and because the managed servers are always present once installed,
    empty means "not configured yet" far more often than "genuinely none".
    """
    from kiro_crew.mcp_discovery import (
        _MANAGED_SERVER_NAMES,
        _load_agent_config,
        _load_mcp_json_by_source,
    )

    names: set[str] = set(_load_agent_config().get("mcpServers", {}) or {})
    for entries in (_load_mcp_json_by_source() or {}).values():
        names.update(entries or {})
    if not names:
        return None
    first_party = len(names & set(_MANAGED_SERVER_NAMES))
    return {
        MCP_CLASS_FIRST_PARTY: first_party,
        MCP_CLASS_THIRD_PARTY: len(names) - first_party,
    }


def read_config_toggles() -> Optional[dict[str, int]]:
    """The :data:`CONFIG_TOGGLES` switches as 0/1, keyed by attribute key.

    Config is read ONCE for the whole set rather than per toggle, so the gauge
    costs one fingerprint-cached load per collection instead of ten.

    A key whose field cannot be resolved is omitted rather than reported as 0 —
    a renamed config field must read as a missing series, never as a switch
    someone turned off. ``test_every_declared_toggle_resolves`` is what keeps
    that from being a silent hole.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    out: dict[str, int] = {}
    for key, section_name, field_name in CONFIG_TOGGLES:
        section = getattr(cfg, section_name, None)
        if section is None:
            continue
        value = getattr(section, field_name, None)
        if not isinstance(value, bool):
            continue
        out[key] = int(value)
    return out or None


# ---------------------------------------------------------------------------
# OTEL registration
# ---------------------------------------------------------------------------


def _observations(
    probe: str,
    reader: Callable[[], Optional[int]],
) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    """Wrap a raw reader as an OTEL observable callback.

    A None (or raising) reader yields no observation for the cycle -- a gap in
    the series is honest, a zero would be a lie, and an exception would spam
    SDK error logs every export interval. A raise additionally increments
    *probe*'s failure count, which is what keeps "the probe broke" from being
    indistinguishable from "this host stopped exporting".
    """
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        if not is_install_reporter():
            return
        try:
            value = reader()
        except Exception:  # noqa: BLE001 -- never let a probe break the exporter
            _note_probe_failure(probe)
            return
        if value is None:
            return
        yield Observation(value, attributes={})

    return _callback


def _keyed_observations(
    probe: str,
    reader: Callable[[], Optional[dict[str, int]]],
    attr_name: str,
) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    """Observable callback fanning a mapping out to one observation per key."""
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        if not is_install_reporter():
            return
        try:
            values = reader()
        except Exception:  # noqa: BLE001 -- never let a probe break the exporter
            _note_probe_failure(probe)
            return
        if not values:
            return
        for key, value in values.items():
            yield Observation(value, attributes={attr_name: key})

    return _callback


def _failure_observations() -> "Callable[[CallbackOptions], Iterable[Observation]]":
    """Observable-counter callback for per-probe failure counts.

    Publishes nothing until a probe has actually failed, so a healthy install adds
    no series. Cumulative like the process counters: the aggregator reduces it
    window-relative, and a provider rebuild re-observes the same in-process totals.
    """
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        for probe, count in read_probe_failures().items():
            yield Observation(count, attributes={"probe": probe})

    return _callback


def register_inventory_gauges(meter: "Meter") -> None:
    """Register all install-inventory instruments on *meter*.

    Called once per live ``MeterProvider`` build (instruments die with their
    provider, so a consent-driven rebuild re-registers on the new meter — that
    is per-provider construction, not duplication). Best-effort: a failure
    disables these gauges, never telemetry as a whole.
    """
    try:
        for name in ALL_METRIC_NAMES:
            validate_name(name)

        meter.create_observable_gauge(
            GAUGE_CRONS_ACTIVE,
            callbacks=[_observations(PROBE_CRONS, read_active_crons)],
            unit="1",
            description="Scheduled cron jobs that are neither user- nor auto-paused",
        )
        meter.create_observable_gauge(
            GAUGE_MONITOR_LOOPS_ACTIVE,
            callbacks=[_observations(PROBE_MONITOR_LOOPS, read_active_monitor_loops)],
            unit="1",
            description="Armed monitor / auto-nudge loops in this process",
        )
        meter.create_observable_gauge(
            GAUGE_SKILLS_INSTALLED,
            callbacks=[_observations(PROBE_SKILLS, read_installed_skills)],
            unit="1",
            description="Skills visible to the loader (builtin and user, combined)",
        )
        meter.create_observable_gauge(
            GAUGE_MEMORY_MIGRATED,
            callbacks=[_observations(PROBE_MEMORY, read_memory_migrated)],
            unit="1",
            description="1 when memory has been migrated to the vector store",
        )
        meter.create_observable_gauge(
            GAUGE_KNOWLEDGE_DOCUMENTS,
            callbacks=[_observations(PROBE_KNOWLEDGE, read_knowledge_documents)],
            unit="1",
            description="Knowledge sources this install has ingested",
        )
        meter.create_observable_gauge(
            GAUGE_LESSONS,
            callbacks=[_observations(PROBE_LESSONS, read_lessons)],
            unit="1",
            description="Lessons this install has saved",
        )
        meter.create_observable_gauge(
            GAUGE_MCP_SERVERS,
            callbacks=[_keyed_observations(PROBE_MCP, read_mcp_server_classes, "class")],
            unit="1",
            description="Configured MCP servers per class (names are never published)",
        )
        meter.create_observable_gauge(
            GAUGE_CONFIG_TOGGLE,
            callbacks=[_keyed_observations(PROBE_CONFIG_TOGGLES, read_config_toggles, "key")],
            unit="1",
            description="Feature-switch state (0/1) per key, over a closed key set",
        )
        meter.create_observable_gauge(
            GAUGE_PROBE_FAILURES,
            callbacks=[_failure_observations()],
            unit="1",
            description=(
                "Times a probe could not answer, per probe. Absent on a healthy "
                "install; present and climbing distinguishes a broken probe from a "
                "host that stopped exporting"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break boot
        logger.warning("inventory gauge registration failed: %s", exc)
