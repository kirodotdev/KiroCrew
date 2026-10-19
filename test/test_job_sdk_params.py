"""The ``start`` params channel: what reaches the runner, what is refused and what a record keeps.

A runner reads the caller's params off its handle as a copy, the record carries the same
strings, and an adopted run keeps the first caller's map. A shape or value the channel will
not carry, a credential-shaped one included, is refused at the call site before any state is
touched, and a broken map on disk costs only its bad entries, with a dropped overflow
counted in the log. The lifecycle and the process-wide registry live in ``test_job_sdk.py``,
the async twins and the defensive invariants in ``test_job_sdk_invariants.py``, what a
runner hands back in ``test_job_sdk_returns.py``, and the ``sdk`` fixture and the bounded
waits in ``job_sdk_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest
from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    sdk_fixture,
)

from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import DONE, JobRun, JobSDK, JobStore

# ---------------------------------------------------------------------------
# 25. start(params=...) / JobHandle.params
# ---------------------------------------------------------------------------


class TestParamsReachTheRunner:
    """A caller names the work, and the runner reads the name off its handle.

    The channel exists so a runner stops having to re-read its own record to
    learn what it was started for, and so a starter that is not the browser can
    say which account, target or mode a run is about.
    """

    def test_a_runner_reads_its_params_off_the_handle(self, sdk: JobSDK) -> None:
        seen: list[dict[str, str]] = []
        sdk.register("work", lambda h: seen.append(h.params))
        run_id = sdk.start("work", params={"account": "123456789012", "mode": "full"})
        assert _wait_terminal(sdk, run_id).status == DONE
        assert seen == [{"account": "123456789012", "mode": "full"}]

    def test_the_record_carries_the_same_strings_the_runner_saw(self, sdk: JobSDK) -> None:
        """Read back off DISK, so a resume sees what the runner saw."""
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work", params={"account": "123456789012"})
        _wait_terminal(sdk, run_id)
        assert sdk.get(run_id).params == {"account": "123456789012"}

    def test_naming_nothing_is_an_empty_map_not_a_missing_field(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work")
        _wait_terminal(sdk, run_id)
        assert sdk.get(run_id).params == {}
        assert sdk.get(run_id).to_dict()["params"] == {}

    def test_the_handle_hands_back_a_copy_so_a_runner_cannot_edit_the_record(
        self, sdk: JobSDK
    ) -> None:
        """The record keeps its single writer.

        A runner holding the record's own mapping could change a field the SDK is
        about to serialize, which is a second writer reached through the handle.
        """
        sdk.register("work", lambda h: h.params.update({"account": "999999999999"}))
        run_id = sdk.start("work", params={"account": "123456789012"})
        _wait_terminal(sdk, run_id)
        assert sdk.get(run_id).params == {"account": "123456789012"}

    def test_the_async_twin_carries_them_too(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: None)
        run_id = asyncio.run(sdk.start_async("work", params={"account": "1"}))
        _wait_terminal(sdk, run_id)
        assert sdk.get(run_id).params == {"account": "1"}

    def test_an_adopted_run_keeps_the_first_callers_params(self, sdk: JobSDK) -> None:
        """Adoption returns the run already executing, so its parameters stand.

        Merging the second caller's map would mean rewriting a running run's
        record, which is exactly the second writer the module refuses.
        """
        release = threading.Event()
        started = threading.Event()

        def runner(h):
            started.set()
            release.wait(5.0)

        sdk.register("work", runner)
        first = sdk.start("work", dedupe_key="k", params={"account": "111"})
        assert started.wait(5.0)
        second = sdk.start("work", dedupe_key="k", params={"account": "222"})
        assert second == first
        release.set()
        _wait_terminal(sdk, first)
        assert sdk.get(first).params == {"account": "111"}


class TestParamsAreRefusedNotTrimmed:
    """Every shape and every value the channel will not carry, and what happens
    instead.

    A caller handing a shape this map cannot hold, or a value the scrub would
    rewrite, has made a mistake at the call site, so it is refused THERE.
    Trimming or rewriting to fit would hand the runner a silently incomplete or
    silently different set of parameters, and a runner acting on half its
    parameters, or on a string its caller never wrote, does the wrong work while
    reporting success.
    """

    #: (params, the exception the call site gets). One row per case the check
    #: owes an answer for: a container that is not a mapping, too many entries, a
    #: key that is not a string, an empty key, an over-long key, a value that is
    #: not a string (including a bool, which is an int and not a string), and an
    #: over-long value.
    CASES = [
        ([], TypeError),
        ("account=1", TypeError),
        (5, TypeError),
        ({str(i): "v" for i in range(17)}, ValueError),
        ({1: "v"}, TypeError),
        ({"": "v"}, ValueError),
        ({"k" * 65: "v"}, ValueError),
        ({"k": 1}, TypeError),
        ({"k": True}, TypeError),
        ({"k": None}, TypeError),
        ({"k": {"nested": "v"}}, TypeError),
        ({"k": ["v"]}, TypeError),
        ({"k": "v" * 513}, ValueError),
    ]

    def test_each_wrong_shape_raises_at_the_call_site(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h: None)
        for params, expected in self.CASES:
            with pytest.raises(expected):
                sdk.start("work", params=params)  # type: ignore[arg-type]

    def test_a_refused_call_starts_nothing(self, sdk: JobSDK) -> None:
        """The check runs before any state is touched.

        A refusal that had already claimed a dedupe key or written a record would
        leave a run nothing finishes.
        """
        sdk.register("work", lambda h: None)
        with pytest.raises(TypeError):
            sdk.start("work", dedupe_key="k", params={"k": 1})  # type: ignore[arg-type]
        assert sdk.list_active() == []
        assert sdk.list_recent() == []
        # The key was never claimed, so a correct call still gets a fresh run.
        run_id = sdk.start("work", dedupe_key="k", params={"k": "1"})
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_a_credential_shaped_value_is_refused_not_quietly_rewritten(self, sdk: JobSDK) -> None:
        """The one content check refuses, like every shape check above it.

        The channel names work; it is not a way to pass a secret through a
        durable record. Scrubbing would hand the runner a string its caller never
        wrote with nothing raised anywhere, which is the silent-wrong-data
        failure this whole class exists to prevent.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        seen: list[dict[str, str]] = []
        sdk.register("work", lambda h: seen.append(h.params))
        with pytest.raises(ValueError):
            sdk.start("work", params={"cmd": f"aws_secret_access_key={secret}"})
        assert seen == []
        assert sdk.list_recent() == []
        on_disk = "".join(p.read_text(encoding="utf-8") for p in sdk.store.dir.glob("*.json"))
        assert secret not in on_disk

    def test_a_value_the_scrub_leaves_alone_is_carried_through_unchanged(self, sdk: JobSDK) -> None:
        """Only a value the scrub WOULD change is refused.

        The check compares against the scrub rather than running it, so an
        ordinary value reaches the runner and the record as the caller wrote it.
        """
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work", params={"cmd": "aws s3 sync s3://bucket ."})
        _wait_terminal(sdk, run_id)
        assert sdk.get(run_id).params == {"cmd": "aws s3 sync s3://bucket ."}

    def test_the_largest_accepted_map_stays_well_inside_the_record_bound(self, sdk: JobSDK) -> None:
        """The bounds are what keep a caller from growing a record past the size
        the disable scan refuses to open."""
        biggest = {f"{'k' * 63}{i:x}": "v" * 512 for i in range(16)}
        sdk.register("work", lambda h: None)
        run_id = sdk.start("work", params=biggest)
        _wait_terminal(sdk, run_id)
        size = (sdk.store.dir / f"{run_id}.json").stat().st_size
        assert size < job_sdk._MAX_RECORD_BYTES


class TestABrokenParamsMapOnDiskCostsOnlyItsBadEntries:
    """A hand-edited or foreign record must stay readable.

    The record is data the gateway re-reads across upgrades, so one unusable
    entry must not make an app's whole run history unreadable -- the same rule
    the rest of ``from_dict`` follows.
    """

    def test_entries_that_break_the_shape_are_dropped_and_the_rest_survive(self) -> None:
        raw = {
            "run_id": "a" * 32,
            "app": "x",
            "kind": "k",
            "params": {
                "good": "kept",
                "bad_value": 5,
                7: "bad_name",
                "": "empty name",
                "k" * 65: "over long name",
                "over_long_value": "v" * 513,
            },
        }
        assert JobRun.from_dict(raw).params == {"good": "kept"}

    def test_a_params_field_that_is_not_a_map_falls_back_to_empty(self) -> None:
        for body in ("account=1", 5, [], None):
            run = JobRun.from_dict({"run_id": "a" * 32, "app": "x", "kind": "k", "params": body})
            assert run.params == {}, f"{type(body).__name__} did not fall back"

    def test_a_map_over_the_count_bound_is_cut_to_the_bound(self) -> None:
        raw = {
            "run_id": "a" * 32,
            "app": "x",
            "kind": "k",
            "params": {str(i): "v" for i in range(40)},
        }
        assert len(JobRun.from_dict(raw).params) == job_sdk._MAX_PARAMS

    def test_the_dropped_overflow_is_counted_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bound that drops entries says how many it dropped.

        The reconciliation pass rewrites what came back, so the tail is gone for
        good. ``_validated_params`` refuses a map this large, so an over-count
        record means a foreign writer -- and that is worth a line an operator can
        find rather than a silent shrink.
        """
        over = 7
        raw = {
            "run_id": "a" * 32,
            "app": "x",
            "kind": "k",
            "params": {f"k{i:02d}": "v" for i in range(job_sdk._MAX_PARAMS + over)},
        }
        with caplog.at_level(logging.WARNING, logger=job_sdk.logger.name):
            read = JobRun.from_dict(raw).params
        assert len(read) == job_sdk._MAX_PARAMS
        assert str(over) in caplog.text, f"overflow count absent from {caplog.text!r}"
        assert "params" in caplog.text

    def test_the_record_supplied_id_cannot_forge_a_line_in_that_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The id naming the over-count record is off disk, so it is tamed first.

        Reaching the overflow branch already means a foreign writer produced the
        file, and this line is durable and served to an operator. So the id is
        redacted, clamped to ``_RUN_ID_LEN`` and repr-quoted: a newline inside it
        cannot start a second line, and a megabyte of it cannot pad the ring.
        """
        forged = "aa\n2026-01-01 WARNING kiro_crew.security: policy check passed"
        raw = {
            "run_id": forged + "b" * 4096,
            "app": "x",
            "kind": "k",
            "params": {f"k{i:02d}": "v" for i in range(job_sdk._MAX_PARAMS + 3)},
        }
        with caplog.at_level(logging.WARNING, logger=job_sdk.logger.name):
            assert len(JobRun.from_dict(raw).params) == job_sdk._MAX_PARAMS
        assert len(caplog.records) == 1, f"expected one line, got {caplog.text!r}"
        line = caplog.records[0].getMessage()
        assert "\n" not in line, f"the id started a second line: {line!r}"
        assert "policy check passed" not in line, f"forged text survived: {line!r}"
        assert len(line) < 200, f"the id padded the line to {len(line)}"

    def test_a_map_inside_the_count_bound_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = {
            "run_id": "a" * 32,
            "app": "x",
            "kind": "k",
            "params": {f"k{i:02d}": "v" for i in range(job_sdk._MAX_PARAMS)},
        }
        with caplog.at_level(logging.WARNING, logger=job_sdk.logger.name):
            assert len(JobRun.from_dict(raw).params) == job_sdk._MAX_PARAMS
        assert caplog.text == "", f"a conforming map warned: {caplog.text!r}"

    def test_a_broken_entry_does_not_cost_a_valid_one_its_place(self) -> None:
        """The count bound applies to the entries that SURVIVE.

        Cutting to the bound before dropping the broken ones would let one broken
        entry at the front spend a slot, so a record holding the bound's worth of
        valid entries behind it would come back one short, and that loss is
        durable once the reconciliation pass rewrites the record.
        """
        params: dict[object, object] = {"broken": 5}
        params.update({f"good{i:02d}": "v" for i in range(job_sdk._MAX_PARAMS)})
        raw = {"run_id": "a" * 32, "app": "x", "kind": "k", "params": params}
        read = JobRun.from_dict(raw).params
        assert len(read) == job_sdk._MAX_PARAMS
        assert "broken" not in read
        assert read[f"good{job_sdk._MAX_PARAMS - 1:02d}"] == "v"

    def test_a_record_written_with_params_round_trips_through_the_store(
        self, tmp_path: Path
    ) -> None:
        store = JobStore(tmp_path)
        run = JobRun(run_id="b" * 32, app="x", kind="k", status=DONE)
        run.params = {"account": "123456789012"}
        store.write(run)
        assert store.read(run.run_id).params == {"account": "123456789012"}
