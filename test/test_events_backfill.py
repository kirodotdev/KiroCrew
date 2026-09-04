"""Backfill validator tests against fixtures replicating each real store shape."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_dir_link
from kiro_crew.events.backfill import ALL_SOURCES, run_backfill
from kiro_crew.events.kinds import (
    AutonudgeArmed,
    CronRegistered,
    SessionMessage,
    SubagentCompleted,
    SubagentFailed,
    SubagentSpawned,
    TurnUsage,
)


def _fixture_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    # Transcript: two message rows, one metadata row, one corrupt line.
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "dashboard_chat-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "content": "hello",
                        "ts": "2026-08-01T10:00:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "hi there",
                        "ts": "2026-08-01T10:00:05+00:00",
                    }
                ),
                json.dumps({"sig": "abc", "summary": "meta row without role"}),
                "{not json",
            ]
        ),
        encoding="utf-8",
    )
    # Subagent runs: one delivered (tombstone cause="delivered" = SUCCESS),
    # one abnormal (cause="timeout"), one still running (no tombstone, result
    # streaming).
    ok_run = home / "subagents" / "aa11"
    ok_run.mkdir(parents=True)
    (ok_run / "state.json").write_text(
        json.dumps({"task": "survey the repo", "pid": 4242, "turns": 9, "started": 1754000100.0}),
        encoding="utf-8",
    )
    (ok_run / "result.txt").write_text("findings " * 10, encoding="utf-8")
    (ok_run / "tombstone.json").write_text(
        json.dumps({"cause": "delivered", "recovery_action": "delivered", "died": 1754000200.0}),
        encoding="utf-8",
    )
    bad_run = home / "subagents" / "bb22"
    bad_run.mkdir(parents=True)
    (bad_run / "state.json").write_text(
        json.dumps({"task": "doomed", "started": 1754000300.0}), encoding="utf-8"
    )
    (bad_run / "tombstone.json").write_text(
        json.dumps({"cause": "timeout", "recovery_action": "reap", "died": 1754000400.0}),
        encoding="utf-8",
    )
    # An in-flight run: state + streaming result.txt but NO tombstone yet —
    # must produce only spawned, never completed.
    live_run = home / "subagents" / "cc33"
    live_run.mkdir(parents=True)
    (live_run / "state.json").write_text(
        json.dumps({"task": "still running", "started": 1754000500.0}), encoding="utf-8"
    )
    (live_run / "result.txt").write_text("partial stream...", encoding="utf-8")
    # Cron snapshot in the REAL store shape: nested schedule object plus the
    # three-way paused split (enabled / user_paused / auto_paused).
    (home / "crons.json").write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "j1",
                        "name": "daily-report",
                        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                        "enabled": True,
                        "user_paused": False,
                        "auto_paused": False,
                        "created_ts": 1754000000.0,
                    },
                    {
                        "id": "j2",
                        "name": "poller",
                        "schedule": {"kind": "every", "every_secs": 300},
                        "enabled": True,
                        "user_paused": True,
                        "auto_paused": False,
                        "script": "x.py:run",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    # AutoNudge snapshot: the store writer's interval field is idle_secs.
    (home / "autonudge.json").write_text(
        json.dumps({"loops": [{"id": "n1", "idle_secs": 300, "max_cycles": 24}]}),
        encoding="utf-8",
    )
    # Usage shard row exactly as production writes it.
    usage = home / "usage" / "tokens"
    usage.mkdir(parents=True)
    (usage / "2026-08-01.jsonl").write_text(
        json.dumps(
            {
                "_type": "tokens",
                "ts": "2026-08-01T10:00:06+00:00",
                "slot": "chat-1-1784000000",
                "provider": "acp",
                "model": "claude-opus-4.8",
                "input": 0,
                "output": 0,
                "cache_create": 0,
                "cache_read": 0,
                "cost": 0.0,
                "credits": 12.5,
                "turns": 3,
                "duration_ms": 4200,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return home


def _derived(home: Path) -> list:
    """Every event the sources derive, in source order — the validator's subject."""
    events: list = []
    for source in ALL_SOURCES:
        events.extend(source(home).events)
    return events


def test_report_counts_every_derived_kind_and_writes_nothing(tmp_path: Path) -> None:
    home = _fixture_home(tmp_path)
    report = run_backfill(home)
    assert report["kinds"] == {
        "autonudge/armed": 1,
        "cron/registered": 2,
        "session/message": 2,
        "subagent/completed": 1,
        "subagent/failed": 1,
        "subagent/spawned": 3,
        "turn/usage": 1,
    }
    assert report["failures"]["transcripts"] == 1  # the corrupt line
    assert set(report["samples"]) == set(report["kinds"])
    # The validator is read-only: reporting must leave no store behind.
    assert not (home / "events").exists()


def test_derived_events_carry_typed_payloads(tmp_path: Path) -> None:
    home = _fixture_home(tmp_path)
    events = _derived(home)
    assert len(events) == 11
    assert {type(e) for e in events} == {
        SessionMessage,
        SubagentSpawned,
        SubagentCompleted,
        SubagentFailed,
        CronRegistered,
        AutonudgeArmed,
        TurnUsage,
    }

    usage = [e for e in events if isinstance(e, TurnUsage)]
    assert usage[0].credits == 12.5 and usage[0].key == "chat-1-1784000000"
    # cause="delivered" tombstone is SUCCESS, not failure; ts from `died`.
    completed = [e for e in events if isinstance(e, SubagentCompleted)]
    assert completed[0].key == "subagent:aa11" and completed[0].turns == 9
    assert completed[0].ts_ms == 1754000200000
    # An in-flight run (result.txt streaming, no tombstone) is NEVER completed.
    assert not any(e.key == "subagent:cc33" for e in completed)
    failed = [e for e in events if isinstance(e, SubagentFailed)]
    assert failed[0].key == "subagent:bb22" and failed[0].reason == "timeout"
    assert failed[0].ts_ms == 1754000400000
    spawned = sorted((e for e in events if isinstance(e, SubagentSpawned)), key=lambda e: e.key)
    assert spawned[0].ts_ms == 1754000100000  # from state.json `started`
    # Nudge interval comes from the store's idle_secs field.
    nudges = [e for e in events if isinstance(e, AutonudgeArmed)]
    assert nudges[0].interval_secs == 300 and nudges[0].max_cycles == 24
    crons = sorted((e for e in events if isinstance(e, CronRegistered)), key=lambda e: e.key)
    assert crons[0].kind_label == "llm" and crons[0].schedule == "cron:0 9 * * *"
    assert crons[0].paused is False and crons[0].ts_ms == 1754000000000
    assert crons[1].kind_label == "script" and crons[1].schedule == "every:300"
    assert crons[1].paused is True  # user_paused folds into paused


def test_limit_caps_each_source(tmp_path: Path) -> None:
    home = _fixture_home(tmp_path)
    report = run_backfill(home, limit=1)
    assert report["totals"]["transcripts"] == 1
    assert report["totals"]["usage"] == 1
    # Subagent runs emit up to two events per dir; the cap is per source.
    assert report["totals"]["subagents"] <= 2


def test_missing_stores_are_not_failures(tmp_path: Path) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    report = run_backfill(home)
    assert report["kinds"] == {}
    assert all(count == 0 for count in report["failures"].values())


def test_non_finite_timestamps_do_not_abort(tmp_path: Path) -> None:
    # json.loads accepts Infinity/NaN; a corrupt ts must degrade, not raise.
    from kiro_crew.events.backfill import _to_ms

    assert _to_ms(float("inf")) is None
    assert _to_ms(float("nan")) is None
    assert _to_ms(True) is None
    assert _to_ms(10**400) is None  # float() overflow is caught, not raised
    assert _to_ms("not a date") is None
    assert _to_ms(1754000000.0) == 1754000000000

    home = tmp_path / "home"
    (home / "subagents" / "zz99").mkdir(parents=True)
    (home / "subagents" / "zz99" / "state.json").write_text(
        json.dumps({"task": "corrupt", "started": float("inf")}), encoding="utf-8"
    )
    report = run_backfill(home)
    assert report["kinds"].get("subagent/spawned") == 1  # fell back to mtime


def test_dashboard_transcript_key_is_normalized(tmp_path: Path) -> None:
    home = _fixture_home(tmp_path)
    report = run_backfill(home)
    import json as _json

    sample = _json.loads(report["samples"]["session/message"])
    # dashboard_chat-1.jsonl folds under the bare slot key, matching usage.
    assert sample["key"] == "chat-1"


def test_stopped_subagent_emits_no_events_at_all(tmp_path: Path) -> None:
    # kinds.py registers no neutral "stopped" terminal kind, so a stopped run
    # is skipped whole: a spawned with no terminal would replay forever as active.
    home = tmp_path / "home"
    d = home / "subagents" / "sa-stop"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"id": "sa-stop", "task": "t"}), encoding="utf-8")
    (d / "tombstone.json").write_text(
        json.dumps({"cause": "user_stop", "outcome": "stopped", "died": 1754906400}),
        encoding="utf-8",
    )
    # A stopped run whose partial result was still DELIVERED must not be
    # rewritten as a completion: outcome wins over cause.
    d2 = home / "subagents" / "sa-stop-delivered"
    d2.mkdir(parents=True)
    (d2 / "state.json").write_text(json.dumps({"id": "sa-stop-delivered"}), encoding="utf-8")
    (d2 / "tombstone.json").write_text(
        json.dumps({"cause": "delivered", "outcome": "stopped", "died": 1754906500}),
        encoding="utf-8",
    )
    report = run_backfill(home)
    assert report["kinds"].get("subagent/failed") is None
    assert report["kinds"].get("subagent/completed") is None
    assert report["kinds"].get("subagent/spawned") is None


def test_restart_orphan_emits_no_events(tmp_path: Path) -> None:
    # gateway_restart tombstones mark runs the gateway itself orphaned --
    # possibly delivered. No interrupted kind exists, so skip whole.
    home = tmp_path / "home"
    d = home / "subagents" / "sa-orphan"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"id": "sa-orphan"}), encoding="utf-8")
    (d / "tombstone.json").write_text(
        json.dumps(
            {
                "cause": "gateway_restart",
                "recovery_action": "result_available",
                "died": 1754906400,
            }
        ),
        encoding="utf-8",
    )
    report = run_backfill(home)
    assert report["kinds"].get("subagent/failed") is None
    assert report["kinds"].get("subagent/spawned") is None


def test_samples_redact_credentials(tmp_path: Path) -> None:
    # Samples reach CLI stdout; a credential in a task preview must not
    # survive into the report verbatim.
    home = tmp_path / "home"
    d = home / "subagents" / "sa-cred"
    d.mkdir(parents=True)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    (d / "state.json").write_text(
        json.dumps({"id": "sa-cred", "task": f"use {token} to push"}), encoding="utf-8"
    )
    report = run_backfill(home)
    sample = report["samples"]["subagent/spawned"]
    assert token not in sample


def test_symlinked_store_files_are_refused(tmp_path: Path) -> None:
    # A symlink planted where a store file belongs must be refused at open
    # (counted as a source failure), never followed -- parsed values surface
    # in report samples, so a followed link could exfiltrate content from a
    # protected path.
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    secret = secret_dir / "secret.jsonl"
    secret.write_text(
        json.dumps({"role": "user", "content": "hunter2", "ts": "2026-08-01T10:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    make_dir_link(sessions / "dashboard_chat-1.jsonl", secret_dir)
    report = run_backfill(home)
    assert report["kinds"].get("session/message") is None
    assert report["failures"].get("transcripts", 0) >= 1


def test_mapped_dashboard_key_emits_bare_slot(tmp_path: Path) -> None:
    # A dashboard session WITH a session_map entry must emit the same bare
    # key as the unmapped stem-strip fallback and as usage slots, or one
    # session's events split across two correlation keys.
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (home / "session_map.json").write_text(
        json.dumps({"dashboard:chat-9-123": {"sid": "s-1"}}), encoding="utf-8"
    )
    (sessions / "dashboard_chat-9-123.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "role": "user",
                "ts": "2026-08-01T10:00:00+00:00",
                "content": "hi",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = run_backfill(home)
    sample = json.loads(report["samples"]["session/message"])
    assert sample["key"] == "chat-9-123"


def test_pathological_json_counts_as_parse_failure(tmp_path: Path) -> None:
    # A huge integer literal is VALID JSON grammar but json.loads raises a
    # bare ValueError (int_max_str_digits), not JSONDecodeError; it must be
    # a counted failure, not a crash.
    home = tmp_path / "home"
    usage = home / "usage" / "tokens"
    usage.mkdir(parents=True)
    (usage / "2026-08-01.jsonl").write_text('{"n": ' + "9" * 5000 + "}\n", encoding="utf-8")
    report = run_backfill(home)
    assert report["failures"].get("usage", 0) >= 1


def test_boolean_numeric_fields_degrade_to_none_and_stay_typed(tmp_path: Path) -> None:
    # bool subclasses int: idle_secs true must not serialize into a typed
    # event the reader's field validation would then degrade to RawEvent.
    home = tmp_path / "home"
    home.mkdir()
    (home / "autonudge.json").write_text(
        json.dumps({"loops": [{"id": "b1", "active": True, "idle_secs": True}]}),
        encoding="utf-8",
    )
    report = run_backfill(home)
    sample = json.loads(report["samples"]["autonudge/armed"])
    assert sample["data"]["interval_secs"] is None
    # Round-trip: the serialized sample parses back as the TYPED kind.
    from kiro_crew.events.base import RawEvent, parse

    parsed = parse(report["samples"]["autonudge/armed"])
    assert parsed is not None
    assert not isinstance(parsed.event, RawEvent)


def test_inactive_autonudge_loop_is_not_armed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "autonudge.json").write_text(
        json.dumps(
            {
                "loops": [
                    {"id": "live", "active": True, "idle_secs": 300},
                    {"id": "legacy"},  # no flag: writer default is live
                    {"id": "stopped", "active": False, "idle_secs": 300},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = run_backfill(home)
    assert report["kinds"].get("autonudge/armed") == 2


def test_corrupt_usage_numerics_degrade_to_none(tmp_path: Path) -> None:
    home = tmp_path / "home"
    usage = home / "usage" / "tokens"
    usage.mkdir(parents=True)
    (usage / "2026-08-01.jsonl").write_text(
        json.dumps(
            {
                "_type": "tokens",
                "ts": "2026-08-01T10:00:06+00:00",
                "slot": "chat-2",
                "credits": int("9" * 400),  # float() overflows
                "cost": True,  # bool is not a number here
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = run_backfill(home)
    assert report["kinds"].get("turn/usage") == 1
    import json as _json

    sample = _json.loads(report["samples"]["turn/usage"])
    assert sample["data"]["credits"] is None
    assert sample["data"]["cost"] is None


def test_transcript_keys_resolve_via_session_map_and_archive_folds(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    # A channel session whose sanitized stem differs from its raw key.
    from kiro_crew.history import transcript_stem

    raw_key = "slack:1723456789.123456"
    stem = transcript_stem(raw_key)
    assert stem != raw_key  # sanitization actually changes it
    (home / "session_map.json").write_text(
        json.dumps({raw_key: "some-cli-session-id"}), encoding="utf-8"
    )
    row = json.dumps({"role": "user", "content": "hi", "ts": "2026-08-01T10:00:00+00:00"})
    (sessions / f"{stem}.jsonl").write_text(row + "\n", encoding="utf-8")
    # A rotated archive segment for the same session.
    archive = sessions / "archive"
    archive.mkdir()
    (archive / f"{stem}__20260801-090000.jsonl").write_text(row + "\n", encoding="utf-8")

    report = run_backfill(home)
    assert report["kinds"]["session/message"] == 2  # live + archived
    import json as _json

    sample = _json.loads(report["samples"]["session/message"])
    assert sample["key"] == raw_key  # canonical key, not the sanitized stem


def test_legacy_slack_stem_resolves_to_canonical_key(tmp_path: Path) -> None:
    # A Slack thread predating the canonical slack:<ts> key logs under its
    # bare thread_ts stem; transcript_stems covers that alias, so backfill
    # must fold it under the canonical key too.
    from kiro_crew.history import transcript_stems

    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    raw_key = "slack:1723456789.123456"
    stems = transcript_stems(raw_key)
    assert len(stems) > 1  # canonical + legacy alias
    legacy_stem = stems[-1]
    (home / "session_map.json").write_text(
        json.dumps({raw_key: "cli-session-id"}), encoding="utf-8"
    )
    row = json.dumps({"role": "user", "content": "hi", "ts": "2026-08-01T10:00:00+00:00"})
    (sessions / f"{legacy_stem}.jsonl").write_text(row + "\n", encoding="utf-8")
    report = run_backfill(home)
    import json as _json

    sample = _json.loads(report["samples"]["session/message"])
    assert sample["key"] == raw_key


def test_corrupt_tombstone_yields_no_terminal_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run = home / "subagents" / "tt55"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"task": "unknown fate"}), encoding="utf-8")
    (run / "tombstone.json").write_text("{corrupt json", encoding="utf-8")
    report = run_backfill(home)
    # An unreadable tombstone proves neither success nor failure: spawned only.
    assert report["kinds"].get("subagent/spawned") == 1
    assert "subagent/failed" not in report["kinds"]
    assert "subagent/completed" not in report["kinds"]
    assert report["failures"]["subagents"] == 1


def test_archive_stem_with_dunder_in_key_resolves_right(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / "sessions"
    archive = sessions / "archive"
    archive.mkdir(parents=True)
    row = json.dumps({"role": "user", "content": "x", "ts": "2026-08-01T10:00:00+00:00"})
    # A stem that itself contains __ : only the LAST segment is the rotation
    # suffix.
    (archive / "app__task__20260801-090000.jsonl").write_text(row + "\n", encoding="utf-8")
    report = run_backfill(home)
    import json as _json

    sample = _json.loads(report["samples"]["session/message"])
    assert sample["key"] == "app__task"


def test_spawned_carries_parent_correlation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run = home / "subagents" / "pp77"
    run.mkdir(parents=True)
    (run / "state.json").write_text(
        json.dumps({"task": "child", "parent_session": "dashboard:chat-9"}),
        encoding="utf-8",
    )
    report = run_backfill(home)
    import json as _json

    sample = _json.loads(report["samples"]["subagent/spawned"])
    assert sample["data"]["parent_key"] == "chat-9"


def test_only_conversational_roles_become_session_messages(tmp_path: Path) -> None:
    # A transcript interleaves conversation turns with tool records, injected
    # nudges, and error/notice rows -- and in real transcripts `tool` rows
    # OUTNUMBER real messages several to one. Counting them as session/message
    # would inflate the validator's totals with events whose payload contract
    # does not hold, so only an actual conversational turn may be emitted.
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    rows = [
        {"role": "user", "content": "hi", "ts": "2026-08-01T10:00:00+00:00"},
        {"role": "assistant", "content": "hello", "ts": "2026-08-01T10:00:01+00:00"},
        # Every one of these is a real shape observed in live transcripts.
        {"role": "tool", "content": "tool output", "ts": "2026-08-01T10:00:02+00:00"},
        {"role": "nudge", "content": "auto-nudge", "ts": "2026-08-01T10:00:03+00:00"},
        {"role": "inject", "content": "injected", "ts": "2026-08-01T10:00:04+00:00"},
        {"role": "error", "content": "boom", "ts": "2026-08-01T10:00:05+00:00"},
        {"role": "system", "content": "sys", "ts": "2026-08-01T10:00:06+00:00"},
        {"role": "subagent", "content": "sa", "ts": "2026-08-01T10:00:07+00:00"},
        {"role": "file", "content": "f", "ts": "2026-08-01T10:00:08+00:00"},
        {"role": "notice", "content": "n", "ts": "2026-08-01T10:00:09+00:00"},
        {"role": "mcp_oauth", "content": "o", "ts": "2026-08-01T10:00:10+00:00"},
        {"sig": "abc", "summary": "metadata row with no role at all"},
    ]
    (sessions / "dashboard_chat-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    report = run_backfill(home)
    # Only the user + assistant turns, and no parse failures: a non-message
    # row is a deliberate skip, not corrupt input.
    assert report["kinds"] == {"session/message": 2}
    assert report["failures"]["transcripts"] == 0
    roles = sorted(
        e.role for e in _derived(home) if isinstance(e, SessionMessage)  # type: ignore[attr-defined]
    )
    assert roles == ["assistant", "user"]


def test_report_measures_field_fill_not_just_failures(tmp_path: Path) -> None:
    # "Zero parse failures" is close to structurally guaranteed: every payload
    # field is optional and degrades to None. So the report must also say how
    # much of each kind was actually POPULATED, or it measures the schema's
    # tolerance rather than the store's fit.
    home = _fixture_home(tmp_path)
    report = run_backfill(home)
    fill = report["fill"]
    assert fill["session/message"]["role"] == 2
    assert fill["session/message"]["content_chars"] == 2
    assert fill["cron/registered"]["name"] == 2
    # `pid` is present on the kind but absent from one of the fixture's runs,
    # so a partially-filled field must report its real count, not 100%.
    assert fill["subagent/spawned"]["pid"] < report["kinds"]["subagent/spawned"]
    for kind, fields in fill.items():
        assert fields, f"{kind} reported no field fill at all"
        for name, count in fields.items():
            assert 0 <= count <= report["kinds"][kind], f"{kind}.{name} out of range"


def test_report_counts_mtime_timestamp_fallbacks(tmp_path: Path) -> None:
    # A timestamp invented from a file mtime is not evidence the store carries
    # one, so the fallback is counted rather than silently absorbed.
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "dashboard_chat-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {"role": "user", "content": "has ts", "ts": "2026-08-01T10:00:00+00:00"}
                ),
                json.dumps({"role": "assistant", "content": "no ts at all"}),
            ]
        ),
        encoding="utf-8",
    )
    report = run_backfill(home)
    assert report["totals"]["transcripts"] == 2
    assert report["failures"]["transcripts"] == 0
    # Exactly the one row lacking a usable ts fell back to the file mtime.
    assert report["ts_fallbacks"]["transcripts"] == 1


def test_backfill_must_be_deleted_once_a_real_consumer_lands() -> None:
    """A tripwire, not a style check.

    ``backfill.py`` declares itself disposable: it hand-reads five store
    formats instead of using their owning readers, so its parsers can drift
    from the stores silently. "Delete it later" is worth nothing if nothing
    enforces it, and the failure mode is specific -- if the emit-site PR slips,
    this module quietly becomes the historical-migration path its own docstring
    forbids.

    So this test fails the moment ``kiro_crew.events`` gains a real (non-test,
    outside-the-package) importer while the validator is still present. At that
    point the schema HAS a consumer, the validator has done its job, and
    whoever landed the emit site must delete it and this test together.
    """
    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    pkg = src / "kiro_crew" / "events"
    backfill = pkg / "backfill.py"
    if not backfill.exists():
        return  # already deleted -- the contract was honoured
    importers: list[str] = []
    kiro_root = src / "kiro_crew"
    for path in src.rglob("*.py"):
        if pkg in path.parents or path.parent == pkg:
            continue
        if "test" in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Absolute imports are unambiguous. A RELATIVE `from .events import`
        # only refers to this package when the importer sits directly in
        # kiro_crew/ -- `kiro_crew/workflows/events.py` is a different module
        # with the same leaf name, and matching it would make this tripwire cry
        # wolf forever.
        hit = "kiro_crew.events" in text or (
            path.parent == kiro_root and "from .events import" in text
        )
        if hit:
            importers.append(str(path.relative_to(src)))
    assert not importers, (
        "kiro_crew.events now has real consumers "
        f"({', '.join(sorted(importers))}), so the disposable validator has "
        "served its purpose: delete src/kiro_crew/events/backfill.py, its "
        "tests, and this tripwire in the same change."
    )
