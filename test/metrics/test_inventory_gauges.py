"""Tests for the kirocrew.inventory.* observable instrument contract.

Four layers, mirroring the module's structure:
  * raw readers — a real value from a crafted source, and None (never a raise)
    when the subsystem is absent, with the None-vs-zero split pinned per probe;
  * OTEL registration — a real in-memory pipeline collects every expected metric
    name with the expected attribute shape, a raising reader produces a gap
    rather than an exporter failure, and no MCP server NAME ever reaches the
    wire;
  * the gateway call site — the reporter claim is reached unconditionally, since
    an unclaimed install publishes nothing and says nothing about why;
  * provider wiring — the live build path registers the gauges, and a
    registration blow-up does not take telemetry (or the process gauges) down.
"""

import json
import logging
import sqlite3
from unittest.mock import patch

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kiro_crew.metrics import inventory_gauges as ig
from kiro_crew.metrics.schema import validate_name

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _collect(register=ig.register_inventory_gauges):
    """Build a real SDK pipeline, register, force one collection cycle.

    Claims the install-reporter role, because a process that has not claimed it
    publishes no inventory at all -- that election is asserted separately in
    ``test_only_the_install_reporter_publishes_inventory``.
    """
    ig.mark_install_reporter()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register(provider.get_meter("test"))
    data = reader.get_metrics_data()
    provider.shutdown()
    out: dict[str, list] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


def _cron_record(job_id, **overrides):
    """A record shaped the way the scheduler's own loader requires.

    ``schedule`` is a nested object, not a flat ``every``: a record the loader
    rejects is reported as unloadable, so a flat fixture would silently exercise
    the corrupt-store path instead of the counting path.
    """
    record = {
        "id": job_id,
        "name": job_id,
        "message": "m",
        "schedule": {"kind": "every", "every_secs": 60},
    }
    record.update(overrides)
    return record


def _write_crons(tmp_path, records):
    # The store is an OBJECT holding a `jobs` list; a bare top-level list is a
    # shape failure the reader reports as unloadable.
    (tmp_path / "crons.json").write_text(json.dumps({"jobs": records}), encoding="utf-8")


def _nudge_service(tmp_path, active_flags):
    """A REAL ``AutoNudgeService`` holding REAL ``NudgeLoop`` objects.

    The probe reaches into `service._loops` and reads `loop.active`; a stubbed
    object with those names would keep the test passing after the owner renamed
    either one, leaving production with a permanent gap. Constructing the real
    classes means a rename fails at this line. Construction alone touches no event
    loop -- only `start()` does -- so this stays safe in a sync test.
    """
    from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

    service = AutoNudgeService(base_dir=tmp_path)
    for i, active in enumerate(active_flags):
        loop = NudgeLoop(id=f"loop-{i}", slot_key=f"chat-{i}", message="m", active=active)
        service._loops[loop.id] = loop
    return service


# ---------------------------------------------------------------------------
# raw readers — crons
# ---------------------------------------------------------------------------


def test_active_crons_counts_only_unpaused_jobs(tmp_path, monkeypatch):
    from kiro_crew import cron

    monkeypatch.setattr(cron, "_DEFAULT_DIR", tmp_path, raising=False)
    _write_crons(
        tmp_path,
        [
            _cron_record("a"),
            _cron_record("b", user_paused=True),
            _cron_record("c", auto_paused=True),
            _cron_record("d"),
        ],
    )
    assert ig.read_active_crons() == 2


def test_active_crons_missing_store_is_a_genuine_zero(tmp_path, monkeypatch):
    from kiro_crew import cron

    ig.reset_for_testing()
    try:
        monkeypatch.setattr(cron, "_DEFAULT_DIR", tmp_path, raising=False)
        assert ig.read_active_crons() == 0
        # An ABSENT source is not a fault: a fresh install has no crons, and a
        # failure here would put every new install permanently in the broken bucket.
        assert ig.read_probe_failures() == {}
    finally:
        ig.reset_for_testing()


def test_active_crons_unparseable_store_is_a_gap_AND_a_counted_fault(tmp_path, monkeypatch):
    """A corrupt store cannot answer, and must not do so silently.

    Two assertions, and the second is the one that matters. A 0 would claim the
    user scheduled nothing. But a bare gap is just as bad in the other direction:
    it is indistinguishable from a host that stopped exporting, which is the exact
    confusion probe.failures exists to resolve -- so the present-but-unreadable
    case counts a failure, unlike the missing-store case above.
    """
    from kiro_crew import cron

    ig.reset_for_testing()
    try:
        monkeypatch.setattr(cron, "_DEFAULT_DIR", tmp_path, raising=False)
        (tmp_path / "crons.json").write_text("{not json", encoding="utf-8")
        assert ig.read_active_crons() is None
        assert ig.read_probe_failures() == {ig.PROBE_CRONS: 1}
    finally:
        ig.reset_for_testing()


def test_unparseable_crons_publishes_the_failure_series_and_no_count(tmp_path, monkeypatch):
    """End to end: the fault is visible as DATA, not only as a missing gauge."""
    from kiro_crew import cron

    ig.reset_for_testing()
    try:
        monkeypatch.setattr(cron, "_DEFAULT_DIR", tmp_path, raising=False)
        (tmp_path / "crons.json").write_text("{not json", encoding="utf-8")
        metrics = _collect()
        assert ig.GAUGE_CRONS_ACTIVE not in metrics or not metrics[ig.GAUGE_CRONS_ACTIVE]
        failures = {dp.attributes["probe"]: dp.value for dp in metrics[ig.GAUGE_PROBE_FAILURES]}
        assert failures.get(ig.PROBE_CRONS) == 1, f"crons fault not published: {failures}"
    finally:
        ig.reset_for_testing()


# ---------------------------------------------------------------------------
# raw readers — monitor loops
# ---------------------------------------------------------------------------


def test_monitor_loops_counts_armed_loops_only(tmp_path):
    service = _nudge_service(tmp_path, [True, False, True])
    with patch("kiro_crew.autonudge.get_instance", return_value=service):
        assert ig.read_active_monitor_loops() == 2


def test_monitor_loops_service_absent_yields_none_but_running_empty_yields_zero(tmp_path):
    """The None-vs-zero split this module exists to preserve, on one probe."""
    with patch("kiro_crew.autonudge.get_instance", return_value=None):
        assert ig.read_active_monitor_loops() is None
    with patch("kiro_crew.autonudge.get_instance", return_value=_nudge_service(tmp_path, [])):
        assert ig.read_active_monitor_loops() == 0


def test_monitor_loop_probe_reads_the_real_registry_shape(tmp_path):
    """Pin the two private names the probe reaches into, so a rename fails here."""
    from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

    service = AutoNudgeService(base_dir=tmp_path)
    assert isinstance(service._loops, dict), "AutoNudgeService._loops is no longer a dict"
    loop = NudgeLoop(id="i", slot_key="s", message="m")
    assert isinstance(loop.active, bool), "NudgeLoop.active is no longer a bool flag"


# ---------------------------------------------------------------------------
# raw readers — skills
# ---------------------------------------------------------------------------


def test_installed_skills_counts_the_loader_listing(tmp_path):
    from kiro_crew.skills import SkillsLoader

    for name in ("alpha", "beta", "gamma"):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
    ig.reset_for_testing()
    try:
        ig._skills_loader = SkillsLoader(skills_path=tmp_path, install_builtins=False)
        assert ig.read_installed_skills() == 3
    finally:
        ig.reset_for_testing()


def test_installed_skills_is_cached_within_the_ttl():
    """The walk is the expensive probe; a second tick inside the TTL must not pay."""
    ig.reset_for_testing()
    try:
        with patch.object(ig, "_read_installed_skills_uncached", return_value=7) as uncached:
            assert ig.read_installed_skills() == 7
            assert ig.read_installed_skills() == 7
            assert ig.read_installed_skills() == 7
            uncached.assert_called_once()
    finally:
        ig.reset_for_testing()


def test_ttl_cache_expiry_re_produces():
    ig.reset_for_testing()
    try:
        calls = []

        def produce():
            calls.append(1)
            return len(calls)

        assert ig._ttl_cached("k", 1000.0, produce) == 1
        assert ig._ttl_cached("k", 1000.0, produce) == 1
        # Expire the entry by rewriting its deadline into the past, which is what
        # the monotonic clock advancing past the TTL does.
        with ig._lock:
            _, value = ig._cache["k"]
            ig._cache["k"] = (0.0, value)
        assert ig._ttl_cached("k", 1000.0, produce) == 2
    finally:
        ig.reset_for_testing()


def test_ttl_cache_caches_a_none_answer():
    """An install that can never answer must not re-pay the probe every cycle."""
    ig.reset_for_testing()
    try:
        with patch.object(ig, "_read_knowledge_documents_uncached", return_value=None) as uncached:
            assert ig.read_knowledge_documents() is None
            assert ig.read_knowledge_documents() is None
            uncached.assert_called_once()
    finally:
        ig.reset_for_testing()


def test_ttl_cache_caches_a_raised_failure_as_a_gap():
    """A probe that RAISES must be cached too, not re-run every cycle.

    The None case alone is not enough: the expensive probes fail by raising (an
    unreadable tree, a locked database), and without this an install that can never
    answer would pay the full walk or SQLite open on every export interval forever.
    """
    ig.reset_for_testing()
    try:
        with patch.object(
            ig, "_read_installed_skills_uncached", side_effect=OSError("unreadable")
        ) as uncached:
            assert ig.read_installed_skills() is None
            assert ig.read_installed_skills() is None
            assert ig.read_installed_skills() is None
            uncached.assert_called_once()
    finally:
        ig.reset_for_testing()


def test_ttl_cached_never_propagates_a_probe_exception():
    """The caller's contract is "None means no observation", so nothing escapes."""
    ig.reset_for_testing()
    try:

        def boom():
            raise RuntimeError("probe exploded")

        assert ig._ttl_cached("boom", 1000.0, boom) is None
    finally:
        ig.reset_for_testing()


# ---------------------------------------------------------------------------
# raw readers — memory / config toggles
# ---------------------------------------------------------------------------


def test_memory_migrated_reports_the_config_bit():
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.memory.migrated = True
    with patch.object(KiroCrewConfig, "load", return_value=cfg):
        assert ig.read_memory_migrated() == 1
    cfg.memory.migrated = False
    with patch.object(KiroCrewConfig, "load", return_value=cfg):
        assert ig.read_memory_migrated() == 0


def test_every_declared_toggle_resolves_against_a_real_config():
    """A renamed config field must fail HERE, not silently publish a wrong 0.

    ``read_config_toggles`` omits a key it cannot resolve, which is the right
    runtime behavior (a missing series beats a fabricated "off") but would hide a
    typo forever. This is the ratchet that makes the omission observable.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    for key, section_name, field_name in ig.CONFIG_TOGGLES:
        section = getattr(cfg, section_name, None)
        assert section is not None, f"toggle {key!r}: no config section {section_name!r}"
        value = getattr(section, field_name, None)
        assert isinstance(
            value, bool
        ), f"toggle {key!r}: {section_name}.{field_name} is {value!r}, expected a bool"


def test_config_toggle_keys_are_unique_and_exclude_the_tautologies():
    keys = [key for key, _, _ in ig.CONFIG_TOGGLES]
    assert len(keys) == len(set(keys)), "duplicate toggle key"
    # telemetry.enabled is always true inside the consent gate, and memory has
    # its own gauge — either one here would be a metric that cannot vary.
    paths = {(section, field) for _, section, field in ig.CONFIG_TOGGLES}
    assert ("telemetry", "enabled") not in paths
    assert ("memory", "migrated") not in paths


def test_config_toggles_reports_zero_and_one():
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    with patch.object(KiroCrewConfig, "load", return_value=cfg):
        toggles = ig.read_config_toggles()
    assert toggles
    assert set(toggles) == {key for key, _, _ in ig.CONFIG_TOGGLES}
    assert set(toggles.values()) <= {0, 1}


def test_config_toggles_omits_an_unresolvable_key():
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    with patch.object(ig, "CONFIG_TOGGLES", (("ghost", "telemetry", "no_such_field"),)):
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            assert ig.read_config_toggles() is None


# ---------------------------------------------------------------------------
# raw readers — knowledge / lessons
# ---------------------------------------------------------------------------


def _make_knowledge_db(root, source_count):
    """Build the database THROUGH the real ``KnowledgeStore``, not by hand.

    A hand-written ``CREATE TABLE sources`` would keep agreeing with the probe's
    ``SELECT COUNT(*) FROM sources`` after the owning schema renamed that table --
    probe and test drifting together in silence, while production emitted a
    permanently CACHED gap indistinguishable from "never ingested". Going through
    the real store makes this test the place a schema rename fails.
    """
    from kiro_crew.knowledge.store import KnowledgeStore

    db_dir = root / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "knowledge.db"
    store = KnowledgeStore(str(db_path))
    for i in range(source_count):
        store.add_source(f"doc-{i}.md", "local_file", f"/docs/doc-{i}.md")
    return db_path


def test_knowledge_documents_counts_sources(tmp_path):
    """Counts through a schema the REAL store built, which is the drift ratchet.

    The fixture goes through ``KnowledgeStore``, so this is where a rename on either
    side of the coupling fails: the store renaming its table, or this module's
    ``KNOWLEDGE_SOURCES_TABLE`` pointing at one that no longer exists. That matters
    because the probe deliberately never constructs a store (doing so would create
    the database), so nothing else observes the coupling -- and a drifted probe
    returns a CACHED gap indistinguishable from "never ingested".
    """
    _make_knowledge_db(tmp_path, 12)
    ig.reset_for_testing()
    try:
        with patch("kiro_crew.config.paths.config_dir", return_value=tmp_path):
            assert ig.read_knowledge_documents() == 12, (
                f"the probe could not count {ig.KNOWLEDGE_SOURCES_TABLE!r} in a schema "
                "KnowledgeStore itself created; read_knowledge_documents would gap forever"
            )
    finally:
        ig.reset_for_testing()


def test_knowledge_probe_never_creates_the_database(tmp_path):
    """A telemetry probe must not bring a subsystem into existence."""
    ig.reset_for_testing()
    try:
        with patch("kiro_crew.config.paths.config_dir", return_value=tmp_path):
            assert ig.read_knowledge_documents() is None
        assert not (tmp_path / "workspace" / "knowledge" / "knowledge.db").exists()
        assert not (tmp_path / "workspace").exists()
    finally:
        ig.reset_for_testing()


def test_knowledge_probe_opens_read_only(tmp_path):
    """A write through THE PROBE'S OWN connection must be refused.

    Deliberately not "open a ``mode=ro`` connection here and watch sqlite refuse a
    write" — that asserts sqlite's behaviour and stays green if the probe drops
    ``mode=ro`` entirely, which is the only thing worth guarding. So the write is
    attempted on the connection the probe just obtained, by intercepting
    ``sqlite3.connect`` on the way back out.
    """
    db_path = _make_knowledge_db(tmp_path, 1)
    real_connect = sqlite3.connect
    refused: list[bool] = []

    def _spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        try:
            # Schema-independent on purpose: an INSERT into `sources` would hit that
            # table's NOT NULL constraints on a WRITABLE connection, so the mutation
            # case would fail with an IntegrityError instead of reporting that the
            # write got through. A CREATE is refused by a read-only connection and
            # accepted by a writable one, with nothing else in the way.
            conn.execute("CREATE TABLE _probe_rw_check (x)")
            refused.append(False)
        except sqlite3.OperationalError:
            refused.append(True)
        return conn

    ig.reset_for_testing()
    try:
        with (
            patch("kiro_crew.config.paths.config_dir", return_value=tmp_path),
            patch.object(sqlite3, "connect", _spy),
        ):
            assert ig.read_knowledge_documents() == 1, "the probe did not run"
        assert refused == [True], f"the probe's own connection accepted a write: {refused}"
    finally:
        ig.reset_for_testing()
    assert db_path.exists()


def test_lessons_counts_saved_lessons(tmp_path):
    from kiro_crew.learn import Lesson, LessonStore

    store = LessonStore(base_dir=tmp_path)
    store.save(Lesson(ts="2026-01-01T00:00:00Z", rule="always X", category="preference"))
    store.save(Lesson(ts="2026-01-02T00:00:00Z", rule="always Z", category="tool"))
    ig.reset_for_testing()
    try:
        ig._lesson_store = store
        assert ig.read_lessons() == 2
    finally:
        ig.reset_for_testing()


def test_lessons_absent_file_is_a_genuine_zero(tmp_path):
    from kiro_crew.learn import LessonStore

    ig.reset_for_testing()
    try:
        ig._lesson_store = LessonStore(base_dir=tmp_path)
        assert ig.read_lessons() == 0
    finally:
        ig.reset_for_testing()


# ---------------------------------------------------------------------------
# raw readers — MCP classes
# ---------------------------------------------------------------------------


def test_mcp_classes_split_managed_from_user_added():
    from kiro_crew.mcp_discovery import _MANAGED_SERVER_NAMES

    managed = sorted(_MANAGED_SERVER_NAMES)
    assert managed, "the managed-server constant must not be empty"
    agent_cfg = {"mcpServers": {name: {} for name in managed[:2]}}
    by_source = {"user": {"acme-internal-tool": {}, "some-other-server": {}}}
    with patch("kiro_crew.mcp_discovery._load_agent_config", return_value=agent_cfg):
        with patch("kiro_crew.mcp_discovery._load_mcp_json_by_source", return_value=by_source):
            classes = ig.read_mcp_server_classes()
    assert classes == {ig.MCP_CLASS_FIRST_PARTY: 2, ig.MCP_CLASS_THIRD_PARTY: 2}


def test_mcp_classes_empty_roster_yields_none():
    with patch("kiro_crew.mcp_discovery._load_agent_config", return_value={}):
        with patch("kiro_crew.mcp_discovery._load_mcp_json_by_source", return_value={}):
            assert ig.read_mcp_server_classes() is None


def test_mcp_first_party_set_is_the_installer_constant_not_a_local_copy():
    """Reusing mcp_discovery's constant is what stops a silent drift."""
    import inspect

    from kiro_crew.mcp_discovery import _MANAGED_SERVER_NAMES

    source = inspect.getsource(ig.read_mcp_server_classes)
    assert "_MANAGED_SERVER_NAMES" in source
    for name in _MANAGED_SERVER_NAMES:
        assert name not in inspect.getsource(ig), f"{name!r} is restated in this module"


# ---------------------------------------------------------------------------
# OTEL registration + collection
# ---------------------------------------------------------------------------


def test_all_names_pass_core_namespace_validation():
    for name in ig.ALL_METRIC_NAMES:
        assert validate_name(name) == name


def test_lifetime_totals_are_declared_for_the_consumer_that_must_know():
    """A lifetime-total gauge is not interchangeable with a state gauge.

    Its newest sample is "since this process started", so a consumer that reports
    the newest sample as a reading shows a number that only grows. The dashboard
    aggregator differences them instead, and it finds them through this tuple —
    which must therefore name exactly the one whose reading accumulates, and
    must stay a subset of the roster. Adding a lifetime-total gauge without
    listing it here — or leaving a stale name in the tuple after a roster
    change — would silently demote the series to newest-sample, with nothing
    failing; this is the inventory half of the guard the process family already
    has in ``test_process_gauges``.
    """
    assert set(ig.LIFETIME_TOTAL_METRICS) == {ig.GAUGE_PROBE_FAILURES}
    assert set(ig.LIFETIME_TOTAL_METRICS) <= set(ig.ALL_METRIC_NAMES)


def test_collection_yields_every_instrument_with_the_expected_shape():
    with (
        patch.object(ig, "read_active_crons", return_value=3),
        patch.object(ig, "read_active_monitor_loops", return_value=1),
        patch.object(ig, "read_installed_skills", return_value=42),
        patch.object(ig, "read_memory_migrated", return_value=1),
        patch.object(ig, "read_knowledge_documents", return_value=12),
        patch.object(ig, "read_lessons", return_value=300),
        patch.object(
            ig,
            "read_mcp_server_classes",
            return_value={ig.MCP_CLASS_FIRST_PARTY: 4, ig.MCP_CLASS_THIRD_PARTY: 1},
        ),
        patch.object(ig, "read_config_toggles", return_value={"beacon": 1, "stt": 0}),
    ):
        metrics = _collect()

    for name in ig.ALL_METRIC_NAMES:
        if name == ig.GAUGE_PROBE_FAILURES:
            # Absent by design on a healthy install: it publishes only once a probe
            # has failed, so a healthy host adds no series. Covered positively by
            # test_probe_failure_publishes_a_counter_series.
            assert (
                name not in metrics or not metrics[name]
            ), "the failure counter must publish nothing when every probe answers"
            continue
        assert name in metrics, f"{name} missing from collection"
        assert metrics[name], f"{name} produced no data points"

    assert metrics[ig.GAUGE_CRONS_ACTIVE][0].value == 3
    assert metrics[ig.GAUGE_MONITOR_LOOPS_ACTIVE][0].value == 1
    assert metrics[ig.GAUGE_SKILLS_INSTALLED][0].value == 42
    assert metrics[ig.GAUGE_MEMORY_MIGRATED][0].value == 1

    # The store-backed counts ride in the VALUE with no attribute at all. A
    # magnitude-band attribute would cost a series per band and would render
    # growth inside a band -- the drift these exist to show -- as a flat line.
    (docs,) = metrics[ig.GAUGE_KNOWLEDGE_DOCUMENTS]
    assert docs.value == 12
    assert dict(docs.attributes or {}) == {}
    (lessons,) = metrics[ig.GAUGE_LESSONS]
    assert lessons.value == 300
    assert dict(lessons.attributes or {}) == {}

    classes = {dp.attributes["class"]: dp.value for dp in metrics[ig.GAUGE_MCP_SERVERS]}
    assert classes == {ig.MCP_CLASS_FIRST_PARTY: 4, ig.MCP_CLASS_THIRD_PARTY: 1}

    toggles = {dp.attributes["key"]: dp.value for dp in metrics[ig.GAUGE_CONFIG_TOGGLE]}
    assert toggles == {"beacon": 1, "stt": 0}


def test_no_mcp_server_name_reaches_any_attribute():
    """The privacy ratchet: a user's roster classifies, then is discarded."""
    secret = "acme-confidential-internal-mcp"
    with patch(
        "kiro_crew.mcp_discovery._load_agent_config",
        return_value={"mcpServers": {secret: {}}},
    ):
        with patch("kiro_crew.mcp_discovery._load_mcp_json_by_source", return_value={}):
            metrics = _collect()
    points = metrics.get(ig.GAUGE_MCP_SERVERS) or []
    assert points, "the MCP gauge must still publish a count"
    for dp in points:
        for key, value in (dp.attributes or {}).items():
            assert secret not in str(key)
            assert secret not in str(value)
    # And the third-party count did observe it.
    classes = {dp.attributes["class"]: dp.value for dp in points}
    assert classes.get(ig.MCP_CLASS_THIRD_PARTY) == 1


def test_raising_reader_yields_gap_not_failure():
    """A blown-up probe skips its observation; every other metric survives."""
    with patch.object(ig, "read_active_crons", side_effect=RuntimeError("boom")):
        with patch.object(ig, "read_memory_migrated", return_value=1):
            metrics = _collect()
    assert ig.GAUGE_CRONS_ACTIVE not in metrics or not metrics[ig.GAUGE_CRONS_ACTIVE]
    assert metrics[ig.GAUGE_MEMORY_MIGRATED][0].value == 1


def test_raising_store_and_keyed_readers_also_yield_gaps():
    with (
        patch.object(ig, "read_lessons", side_effect=RuntimeError("boom")),
        patch.object(ig, "read_config_toggles", side_effect=RuntimeError("boom")),
        patch.object(ig, "read_memory_migrated", return_value=0),
    ):
        metrics = _collect()
    assert ig.GAUGE_LESSONS not in metrics or not metrics[ig.GAUGE_LESSONS]
    assert ig.GAUGE_CONFIG_TOGGLE not in metrics or not metrics[ig.GAUGE_CONFIG_TOGGLE]
    assert metrics[ig.GAUGE_MEMORY_MIGRATED][0].value == 0


def test_unavailable_reader_yields_no_observation():
    with patch.object(ig, "read_active_monitor_loops", return_value=None):
        metrics = _collect()
    assert (
        ig.GAUGE_MONITOR_LOOPS_ACTIVE not in metrics or not metrics[ig.GAUGE_MONITOR_LOOPS_ACTIVE]
    )


def test_probe_failure_publishes_a_counter_series():
    """A broken probe must present as DATA, not merely as a missing gauge.

    This is what separates "the probe broke" from "this host stopped exporting" at
    a backend: the failure series is present and climbing, which a host that went
    dark cannot produce.
    """
    ig.reset_for_testing()
    try:
        with patch.object(ig, "read_active_crons", side_effect=RuntimeError("boom")):
            metrics = _collect()
        assert ig.GAUGE_CRONS_ACTIVE not in metrics or not metrics[ig.GAUGE_CRONS_ACTIVE]
        points = metrics.get(ig.GAUGE_PROBE_FAILURES) or []
        assert points, "a failed probe published no failure series"
        counts = {p.attributes["probe"]: p.value for p in points}
        assert counts == {ig.PROBE_CRONS: 1}
    finally:
        ig.reset_for_testing()


def test_probe_failure_attribute_values_are_the_closed_probe_set():
    ig.reset_for_testing()
    try:
        for probe in ig.ALL_PROBES:
            ig._note_probe_failure(probe)
        assert set(ig.read_probe_failures()) == set(ig.ALL_PROBES)
        assert len(set(ig.ALL_PROBES)) == len(ig.ALL_PROBES), "duplicate probe name"
    finally:
        ig.reset_for_testing()


def test_first_probe_failure_warns_then_falls_back_to_debug(caplog):
    """A default install collects WARNING and above, so the first failure must be loud.

    Repeats drop to debug: a permanently broken probe would otherwise emit one
    WARNING per export interval for the life of the process.
    """
    ig.reset_for_testing()
    try:
        with caplog.at_level(logging.WARNING, logger=ig.logger.name):
            ig._note_probe_failure(ig.PROBE_SKILLS)
            assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1
            ig._note_probe_failure(ig.PROBE_SKILLS)
            ig._note_probe_failure(ig.PROBE_SKILLS)
            assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1
        assert ig.read_probe_failures()[ig.PROBE_SKILLS] == 3
    finally:
        ig.reset_for_testing()


def test_ttl_cached_failure_counts_against_its_probe():
    """The cached-failure path must also increment, not just the wrapper path."""
    ig.reset_for_testing()
    try:
        with patch.object(ig, "_read_knowledge_documents_uncached", side_effect=OSError("locked")):
            assert ig.read_knowledge_documents() is None
        assert ig.read_probe_failures() == {ig.PROBE_KNOWLEDGE: 1}
    finally:
        ig.reset_for_testing()


def test_only_the_install_reporter_publishes_inventory():
    """A process that never claimed the role must publish NO inventory series.

    This is what keeps an install from being counted once per telemetry-enabled
    process. The gateway claims the role; gatewayd, spawned agents and apps do not,
    and their recorders must stay silent on install-scoped facts even though they
    register the same instruments.
    """
    ig.reset_for_testing()
    try:
        assert not ig.is_install_reporter(), "the role must default to unclaimed"
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        ig.register_inventory_gauges(provider.get_meter("test"))
        data = reader.get_metrics_data()
        provider.shutdown()
        published = set()
        if data is not None:
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        if metric.data.data_points:
                            published.add(metric.name)
        assert not published, f"unclaimed process published inventory: {sorted(published)}"
    finally:
        ig.reset_for_testing()


def test_claiming_the_role_is_idempotent_and_enables_publication():
    ig.reset_for_testing()
    try:
        ig.mark_install_reporter()
        ig.mark_install_reporter()
        assert ig.is_install_reporter()
        # _collect claims the role itself, so this is the positive counterpart of
        # the test above: the same registration now does publish.
        metrics = _collect()
        assert metrics.get(ig.GAUGE_MEMORY_MIGRATED), "reporter published nothing"
    finally:
        ig.reset_for_testing()


def test_registration_failure_is_swallowed():
    """register_inventory_gauges never raises, even on a hostile meter."""

    class _HostileMeter:
        def __getattr__(self, name):
            raise RuntimeError("no instruments for you")

    ig.register_inventory_gauges(_HostileMeter())  # must not raise


# ---------------------------------------------------------------------------
# gateway call site
# ---------------------------------------------------------------------------


def test_reporter_claim_is_unconditional():
    """The gateway's claim must be reached without passing through a branch.

    The election has no runtime enforcement: nothing raises when a process forgets
    to claim the role, and an unclaimed process's callbacks return before probing,
    so ``probe.failures`` stays empty too. The whole subsystem then reads exactly
    like a host that stopped exporting -- silent, and indistinguishable from
    healthy. That makes the call SITE the invariant, so it is pinned here instead
    of left to review: it shipped inside ``_init_autonudge()``, which returns early
    under ``KIROCREW_AUTONUDGE=0``, and one env var darkened all eight gauges.

    ``try`` is allowed (telemetry is best-effort and must not block boot); a
    conditional or a loop is not, because that is the shape a feature flag arrives
    in.
    """
    import ast
    from pathlib import Path

    from kiro_crew.slack import gateway as gateway_mod

    tree = ast.parse(Path(gateway_mod.__file__).read_text(encoding="utf-8"))
    branching = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Match)
    sites: list[tuple[str, bool]] = []

    def _walk(node, func: str, branched: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name, False)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "mark_install_reporter"
            ):
                sites.append((func, branched))
            _walk(child, func, branched or isinstance(child, branching))

    _walk(tree, "<module>", False)

    assert sites == [("run", False)], (
        "mark_install_reporter() must be called exactly once, unconditionally, from "
        f"GatewayOrchestrator.run(); found (function, is_conditional) = {sites}"
    )


# ---------------------------------------------------------------------------
# provider wiring
# ---------------------------------------------------------------------------


def _enable_telemetry(monkeypatch):
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")


def test_live_build_registers_inventory_gauges(monkeypatch):
    from kiro_crew.metrics import provider as provider_mod

    _enable_telemetry(monkeypatch)
    provider_mod.reset_for_testing()
    try:
        with patch("kiro_crew.metrics.inventory_gauges.register_inventory_gauges") as register:
            rec = provider_mod.get_recorder()
            assert rec.enabled
            register.assert_called_once()
    finally:
        provider_mod.reset_for_testing()


def test_gauge_registration_failure_keeps_telemetry_alive(monkeypatch):
    from kiro_crew.metrics import provider as provider_mod

    _enable_telemetry(monkeypatch)
    provider_mod.reset_for_testing()
    try:
        with patch(
            "kiro_crew.metrics.inventory_gauges.register_inventory_gauges",
            side_effect=RuntimeError("boom"),
        ):
            rec = provider_mod.get_recorder()
            assert rec.enabled, "gauge failure must not disable telemetry"
    finally:
        provider_mod.reset_for_testing()


def test_inventory_failure_does_not_cost_the_process_gauges(monkeypatch):
    """Separate try blocks: one subsystem's probe cannot dark the other's."""
    from kiro_crew.metrics import provider as provider_mod

    _enable_telemetry(monkeypatch)
    provider_mod.reset_for_testing()
    try:
        with patch(
            "kiro_crew.metrics.inventory_gauges.register_inventory_gauges",
            side_effect=RuntimeError("boom"),
        ):
            with patch("kiro_crew.metrics.process_gauges.register_process_gauges") as process:
                rec = provider_mod.get_recorder()
                assert rec.enabled
                process.assert_called_once()
    finally:
        provider_mod.reset_for_testing()
