"""``dashboard.widget_snapshot``: one phone widget resolved into its wire snapshot.

Pins the block shapes, ``null`` values, the four freshness states (a record exactly at
``stale_after`` is current), tones, the list and sparkline cuts, the manifest cut (a
snapshot holds only the fields its widget binds), package selection through
``package_for_reader`` (no package in force leaves only the default widget), and one
provenance vector per source kind: a fold field, an agentic field with ``stale_after``,
a ``custom.latest`` record with ``failed_at``, ``workers.total`` and an unresolved field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.dashboard import widget_snapshot as ws
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard_feed import FieldRead
from kiro_crew.dashboard_templates import catalog, instance

SLUG = "pebble"
CREW = "Pebble"
NOW = datetime(2026, 11, 18, 0, 30, 0, tzinfo=timezone.utc)
BOT = {"slug": SLUG, "member": CREW, "name": "Pebble"}


def _types() -> dict[str, Any]:
    return {
        "pipeline_state": {
            "type": "enum",
            "choices": ["green", "amber", "red"],
            "source": {"agentic": True},
            "stale_after_s": 3600,
        },
        "open_prs": {"type": "number", "source": {"agentic": True}},
        "milestones_done": {"type": "number", "source": {"agentic": True}},
        "milestones_total": {"type": "number", "source": {"agentic": True}},
        "waiting_items": {"type": "array", "source": {"fold": "waiting", "path": "items"}},
        "accepted_series": {"type": "array", "source": {"fold": "workstreams", "path": "series"}},
        "ci_state": {"type": "text", "source": {"fold": "custom", "path": "latest.ci.data.state"}},
        "unbound_secret": {"type": "text", "source": {"agentic": True}},
    }


def _widget() -> dict[str, Any]:
    return {
        "id": "build-health",
        "title": "Build health",
        "sizes": ["small", "medium", "large"],
        "blocks": [
            {
                "type": "status",
                "label": "Pipeline",
                "field": "pipeline_state",
                "tones": {"green": "ok", "amber": "warn", "red": "bad"},
            },
            {
                "type": "metric",
                "label": "Open PRs",
                "field": "open_prs",
                "format": "integer",
                "warn_at": 10,
                "bad_at": 20,
            },
            {
                "type": "progress",
                "label": "Milestones",
                "field": "milestones_done",
                "total_field": "milestones_total",
            },
            {
                "type": "list",
                "label": "Waiting on you",
                "field": "waiting_items",
                "item_text": "title",
                "item_detail": "age",
                "max_items": 2,
            },
            {
                "type": "sparkline",
                "label": "Accepted per hour",
                "field": "accepted_series",
                "point": "accepted",
                "max_points": 3,
            },
            {"type": "status", "label": "CI", "field": "ci_state"},
        ],
        "tap": {"open": "bot"},
    }


def _package() -> dict[str, Any]:
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": f"crewmate:{SLUG}",
            "model": {"types": _types()},
            "view": {"html": '<p data-dashboard-field="open_prs"></p>'},
            "theme": {"tokens": {}},
            "widgets": [_widget()],
        }
    )


def _read(**over: Any) -> FieldRead:
    read = FieldRead(
        fields={
            "pipeline_state": "amber",
            "open_prs": 7,
            "milestones_done": 3,
            "milestones_total": 5,
            "waiting_items": [
                {"title": "Approve the deploy", "age": "2h"},
                {"title": "Review PR", "age": 5},
                {"title": "Pick a name"},
                {"no_title": True},
            ],
            "accepted_series": [{"accepted": 1}, {"accepted": "x"}, {"accepted": 4}, 2, {}, 3.5],
            "ci_state": "green",
            "_row_ci_state": {
                "data": {"state": "green"},
                "at": "2026-11-18T00:10:00Z",
                "stale_after": "2026-11-18T01:10:00Z",
                "failed_at": "2026-11-18T00:20:00Z",
            },
            "unbound_secret": "LEAKED-VALUE",
            "undeclared": "ALSO-LEAKED",
        },
        written_at={
            "pipeline_state": "2026-11-18T00:21:00+00:00",
            "open_prs": "2026-11-18T00:29:00Z",
            "milestones_done": "2026-11-18T00:10:00Z",
        },
    )
    for key, value in over.items():
        setattr(read, key, value)
    return read


def _snapshot(read: FieldRead | None = None, **kw: Any) -> dict[str, Any]:
    widget, types = _widget(), _types()
    manifest = ws.cut_manifest(types, widget, slug=SLUG, version=12)
    return ws.shape_snapshot(
        widget,
        types,
        read or _read(),
        manifest,
        bot=BOT,
        layout_version=12,
        now=kw.pop("now", NOW),
        **kw,
    )


def _block(snapshot: dict[str, Any], label: str) -> dict[str, Any]:
    return next(b for b in snapshot["blocks"] if b.get("label") == label)


# ----------------------------------------------------------------- envelope


def test_the_envelope_names_the_bot_widget_version_and_tap():
    snap = _snapshot()
    assert snap["v"] == 1
    assert snap["bot"] == BOT
    assert snap["widget"] == {
        "id": "build-health",
        "title": "Build health",
        "sizes": ["small", "medium", "large"],
        "default": False,
    }
    assert snap["layout_version"] == 12
    assert snap["generated_at"] == "2026-11-18T00:30:00Z"
    assert snap["tap"] == {"open": "bot", "value": SLUG}
    assert [b["type"] for b in snap["blocks"]] == [
        "status",
        "metric",
        "progress",
        "list",
        "sparkline",
        "status",
    ]


def test_every_block_carries_all_freshness_keys():
    for block in _snapshot()["blocks"]:
        assert set(block["freshness"]) == {"state", "at", "stale_after", "failed_at", "source"}


# ----------------------------------------------------------------- the manifest cut


def test_the_manifest_reads_only_the_widget_fields_and_their_record_rows():
    manifest = ws.cut_manifest(_types(), _widget(), slug=SLUG, version=12)
    assert set(manifest.fields) == {
        "pipeline_state",
        "open_prs",
        "milestones_done",
        "milestones_total",
        "waiting_items",
        "accepted_series",
        "ci_state",
        "_row_ci_state",
    }
    assert "unbound_secret" not in manifest.fields
    assert manifest.fields["_row_ci_state"].fold == "custom"
    assert manifest.fields["_row_ci_state"].path == "latest.ci"


def test_no_value_outside_the_widget_reaches_the_snapshot():
    body = json.dumps(_snapshot())
    assert "LEAKED-VALUE" not in body
    assert "ALSO-LEAKED" not in body
    # The record companion feeds freshness only; its value is not a block value.
    assert '"data"' not in body


def test_redact_sees_each_bound_value_before_a_block_reads_it():
    seen: list[Any] = []

    def redact(value: Any) -> Any:
        seen.append(value)
        return "[R]" if isinstance(value, str) else value

    snap = _snapshot(redact=redact)
    assert _block(snap, "Pipeline")["text"] == "[R]"
    assert "LEAKED-VALUE" not in seen and "ALSO-LEAKED" not in seen


# ----------------------------------------------------------------- block shapes


def test_status_block_text_and_tone():
    block = _block(_snapshot(), "Pipeline")
    assert (block["text"], block["tone"]) == ("amber", "warn")
    # A value with no tone entry is idle.
    assert _block(_snapshot(), "CI")["tone"] == "idle"


def test_metric_block_shape():
    block = _block(_snapshot(), "Open PRs")
    assert {k: block[k] for k in ("value", "format", "unit", "tone")} == {
        "value": 7,
        "format": "integer",
        "unit": "",
        "tone": "ok",
    }


def test_progress_block_reads_its_total_field():
    block = _block(_snapshot(), "Milestones")
    assert (block["value"], block["total"]) == (3, 5)


def test_list_is_cut_to_max_items_and_counts_the_rest():
    block = _block(_snapshot(), "Waiting on you")
    assert block["items"] == [
        {"text": "Approve the deploy", "detail": "2h"},
        {"text": "Review PR", "detail": "5"},
    ]
    # Three usable rows (one has no text), two shown.
    assert block["omitted"] == 1


def test_list_of_strings():
    read = _read()
    read.fields["waiting_items"] = ["a", "b"]
    assert _block(_snapshot(read), "Waiting on you")["items"] == [
        {"text": "a", "detail": None},
        {"text": "b", "detail": None},
    ]


def test_sparkline_keeps_the_last_numbers_only():
    # Numbers in order: 1, 4, 2, 3.5 (strings, empty rows dropped); last 3 kept.
    assert _block(_snapshot(), "Accepted per hour")["points"] == [4, 2, 3.5]


def test_title_block_literal_and_bound():
    shaped = ws.shape_block({"type": "title", "text": "Hello"}, {}, lambda n: {}, now=NOW)
    assert shaped["text"] == "Hello"
    assert shaped["freshness"]["source"] == "none"
    assert shaped["freshness"]["state"] == "current"
    bound = ws.shape_block(
        {"type": "title", "field": "pipeline_state"},
        {"pipeline_state": "red"},
        lambda n: {"state": "current"},
        now=NOW,
    )
    assert bound["text"] == "red"


# ----------------------------------------------------------------- null values


def test_missing_values_are_null_never_zero():
    read = FieldRead(fields={}, missing=list(_types()))
    snap = _snapshot(read)
    assert _block(snap, "Open PRs")["value"] is None
    assert _block(snap, "Open PRs")["tone"] == "idle"
    assert _block(snap, "Pipeline")["text"] is None
    assert _block(snap, "Milestones")["value"] is None
    assert _block(snap, "Milestones")["total"] is None
    assert _block(snap, "Waiting on you")["items"] == []
    assert _block(snap, "Waiting on you")["omitted"] == 0
    assert _block(snap, "Accepted per hour")["points"] == []
    for block in snap["blocks"]:
        assert block["freshness"] == {
            "state": "missing",
            "at": None,
            "stale_after": None,
            "failed_at": None,
            "source": block["freshness"]["source"],
        }


def test_non_finite_and_bool_numbers_are_null():
    read = _read()
    read.fields["open_prs"] = float("nan")
    read.fields["milestones_done"] = True
    snap = _snapshot(read)
    assert _block(snap, "Milestones")["value"] is None
    assert _block(snap, "Open PRs")["tone"] == "idle"


# ----------------------------------------------------------------- freshness states


@pytest.mark.parametrize(
    ("now", "state"),
    [
        (datetime(2026, 11, 18, 1, 0, 0, tzinfo=timezone.utc), "current"),
        (datetime(2026, 11, 18, 1, 21, 0, tzinfo=timezone.utc), "current"),  # exactly at
        (datetime(2026, 11, 18, 1, 21, 1, tzinfo=timezone.utc), "stale"),
    ],
)
def test_stale_after_boundary(now, state):
    assert (
        ws.freshness(
            source="agentic", at="2026-11-18T00:21:00Z", stale_after="2026-11-18T01:21:00Z", now=now
        )["state"]
        == state
    )


def test_failed_wins_over_current_and_stale():
    fresh = ws.freshness(
        source="record",
        at="2026-11-18T00:21:00Z",
        stale_after="2026-11-18T01:21:00Z",
        failed_at="2026-11-18T00:25:00Z",
        now=NOW,
    )
    assert fresh["state"] == "failed"


def test_missing_state():
    assert ws.freshness(source="fold", missing=True, now=NOW)["state"] == "missing"


def test_an_unparseable_time_is_no_evidence():
    fresh = ws.freshness(source="agentic", at="yesterday", stale_after="soon", now=NOW)
    assert fresh == {
        "state": "current",
        "at": None,
        "stale_after": None,
        "failed_at": None,
        "source": "agentic",
    }


# ----------------------------------------------------------------- tones


@pytest.mark.parametrize(
    ("block", "value", "tone"),
    [
        ({"warn_at": 10, "bad_at": 20}, 9, "ok"),
        ({"warn_at": 10, "bad_at": 20}, 10, "warn"),
        ({"warn_at": 10, "bad_at": 20}, 20, "bad"),
        ({"warn_at": 10, "bad_at": 20, "higher_is_worse": False}, 21, "ok"),
        ({"warn_at": 10, "bad_at": 5, "higher_is_worse": False}, 10, "warn"),
        ({"warn_at": 10, "bad_at": 5, "higher_is_worse": False}, 5, "bad"),
        ({"warn_at": 1}, 0, "ok"),
        ({}, 5, "idle"),
        ({"warn_at": 1}, None, "idle"),
        ({"warn_at": 1}, "7", "idle"),
    ],
)
def test_metric_tones(block, value, tone):
    assert ws.metric_tone(block, value) == tone


def test_status_tone_for_a_bool_field():
    shaped = ws.shape_block(
        {"type": "status", "label": "Up", "field": "up", "tones": {"true": "ok", "false": "bad"}},
        {"up": False},
        lambda n: {},
        now=NOW,
    )
    assert (shaped["text"], shaped["tone"]) == ("false", "bad")


# ----------------------------------------------------------------- provenance vectors


def test_provenance_agentic_field_with_stale_after():
    fresh = _block(_snapshot(), "Pipeline")["freshness"]
    assert fresh == {
        "state": "current",
        "at": "2026-11-18T00:21:00Z",
        "stale_after": "2026-11-18T01:21:00Z",
        "failed_at": None,
        "source": "agentic",
    }
    later = datetime(2026, 11, 18, 2, 0, 0, tzinfo=timezone.utc)
    assert _block(_snapshot(now=later), "Pipeline")["freshness"]["state"] == "stale"


def test_provenance_agentic_field_without_a_limit_takes_the_default_limit():
    from datetime import timedelta

    from kiro_crew.dashboard_agentic import AGENTIC_STALE_AFTER_DEFAULT_S

    fresh = _block(_snapshot(), "Open PRs")["freshness"]
    at = datetime(2026, 11, 18, 0, 29, 0, tzinfo=timezone.utc)
    limit = at + timedelta(seconds=AGENTIC_STALE_AFTER_DEFAULT_S)
    assert (fresh["at"], fresh["stale_after"], fresh["state"]) == (
        "2026-11-18T00:29:00Z",
        ws.iso(limit),
        "current",
    )


def test_provenance_custom_latest_record_with_failed_at():
    fresh = _block(_snapshot(), "CI")["freshness"]
    assert fresh == {
        "state": "failed",
        "at": "2026-11-18T00:10:00Z",
        "stale_after": "2026-11-18T01:10:00Z",
        "failed_at": "2026-11-18T00:20:00Z",
        "source": "record",
    }


def test_provenance_fold_field_has_no_clock_and_is_current_as_read():
    # The feed keeps a fold's value and seq but no write time: at is null, never made up,
    # and the block cannot go stale on a time it does not have.
    late = datetime(2027, 1, 1, tzinfo=timezone.utc)
    fresh = _block(_snapshot(now=late), "Waiting on you")["freshness"]
    assert fresh == {
        "state": "current",
        "at": None,
        "stale_after": None,
        "failed_at": None,
        "source": "fold",
    }


def test_provenance_workers_total_is_current_with_no_at():
    read = FieldRead(fields={"waiting_count": 0, "workers_total": 2, "waiting_items": []})
    manifest = ws.cut_manifest(ws.DEFAULT_TYPES, ws.DEFAULT_WIDGET, slug=SLUG, version=0)
    snap = ws.shape_snapshot(
        ws.DEFAULT_WIDGET, ws.DEFAULT_TYPES, read, manifest, bot=BOT, layout_version=0, now=NOW
    )
    workers = _block(snap, "Workers")
    assert workers["value"] == 2
    assert workers["freshness"]["state"] == "current"
    assert workers["freshness"]["at"] is None


def test_provenance_unresolved_field_is_missing():
    read = _read()
    del read.fields["waiting_items"]
    read.missing = ["waiting_items"]
    read.unresolved = {"waiting_items": "fold_unseen"}
    block = _block(_snapshot(read), "Waiting on you")
    assert block["items"] == []
    assert block["freshness"]["state"] == "missing"
    assert block["freshness"]["at"] is None


# ----------------------------------------------------------------- package selection


@pytest.fixture
def _home(tmp_path, _floor_monkeypatch):
    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    manifest = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    }
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (builtin / "template.html").write_text(
        '<div><b data-dashboard-field="credits"></b></div>', encoding="utf-8"
    )
    monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, config=None: SLUG)


class _Reader:
    def __init__(self, read: FieldRead) -> None:
        self.read = read
        self.manifests: list[Any] = []

    def __call__(self, slug: str, member: str, manifest: Any) -> FieldRead:
        self.manifests.append(manifest)
        return self.read


@pytest.mark.asyncio
async def test_a_pinned_package_serves_its_widget(_home):
    instance.adopt(SLUG, "fixture-board")
    instance.apply_package(
        SLUG, package_slug="pebble-board", package_version=12, content=json.dumps(_package())
    )
    reader = _Reader(_read())
    snap = await ws.resolve_snapshot(SLUG, CREW, "build-health", now=NOW, reader=reader)
    assert snap is not None
    assert snap["layout_version"] == 12
    assert _block(snap, "Open PRs")["value"] == 7
    assert "unbound_secret" not in reader.manifests[0].fields
    # The default widget is there beside the authored one.
    package, _version = await ws.package_in_force(SLUG, CREW)
    assert [w["id"] for w in ws.widgets_of(package)] == ["build-health", "status"]


@pytest.mark.asyncio
async def test_no_package_in_force_gives_only_the_default_widget(_home, monkeypatch):
    calls: list[tuple[str, str]] = []
    real = md.package_for_reader

    async def spy(slug: str, member: str, **kw: Any):
        calls.append((slug, member))
        return await real(slug, member, **kw)

    monkeypatch.setattr(md, "package_for_reader", spy)
    package, version = await ws.package_in_force(SLUG, CREW)
    assert (package, version) == (None, 0)
    assert calls == [(SLUG, CREW)]
    assert [w["id"] for w in ws.widgets_of(package)] == ["status"]
    reader = _Reader(FieldRead(fields={"waiting_count": 1, "workers_total": 0}))
    assert await ws.resolve_snapshot(SLUG, CREW, "build-health", reader=reader) is None
    snap = await ws.resolve_snapshot(SLUG, CREW, "status", name="Pebble", now=NOW, reader=reader)
    assert snap is not None
    assert snap["widget"]["default"] is True
    assert snap["layout_version"] == 0
    assert snap["blocks"][0] == {
        "type": "title",
        "text": "Pebble",
        "freshness": {
            "state": "current",
            "at": None,
            "stale_after": None,
            "failed_at": None,
            "source": "none",
        },
    }
    assert _block(snap, "Waiting on you")["tone"] == "warn"
    assert set(reader.manifests[0].fields) == {"waiting_count", "workers_total", "waiting_items"}


@pytest.mark.asyncio
async def test_a_package_bound_to_another_bot_is_no_package(_home, monkeypatch):
    other = _package()
    other["bound_to"] = "crewmate:someone-else"
    instance.adopt(SLUG, "fixture-board")
    instance.apply_package(
        SLUG, package_slug="pebble-board", package_version=3, content=json.dumps(other)
    )
    assert await ws.package_in_force(SLUG, CREW) == (None, 0)


@pytest.mark.asyncio
async def test_a_selection_that_raises_is_no_package(_home, monkeypatch):
    async def boom(slug: str, member: str, **kw: Any):
        raise OSError("store down")

    monkeypatch.setattr(md, "package_for_reader", boom)
    assert await ws.package_in_force(SLUG, CREW) == (None, 0)


class _Art:
    def __init__(self, content: str, *, kind: str = "dashboard", created: str = "c1") -> None:
        self.slug, self.kind, self.content, self.created_at = "bound-pkg", kind, content, created
        self.source_path, self.source_copy_only = "", False


class _Store:
    def __init__(self, current: _Art, applied: _Art) -> None:
        self.current, self.applied = current, applied
        self.asked: list[Any] = []

    def get(self, slug: str, version: int | None = None) -> _Art:
        self.asked.append((slug, version))
        return self.current if version is None else self.applied


@pytest.mark.parametrize(
    ("current", "applied", "cut", "expect"),
    [
        (_Art("x"), _Art(json.dumps(_package())), 4, 4),
        (_Art("x", kind="markdown"), _Art(json.dumps(_package())), 4, 0),
        (_Art("x"), _Art(json.dumps(_package())), 0, 0),
        (_Art("x"), _Art(json.dumps(_package()), created="c2"), 4, 0),
    ],
    ids=["applied-version", "not-a-package", "never-cut", "record-replaced"],
)
@pytest.mark.asyncio
async def test_an_older_record_serves_only_the_version_a_person_cut(
    _home, monkeypatch, current, applied, cut, expect
):
    async def select(slug: str, member: str, **kw: Any):
        return "bound-pkg", None

    store = _Store(current, applied)
    monkeypatch.setattr(md, "package_for_reader", select)
    monkeypatch.setattr("kiro_crew.artifacts.get_default_store", lambda: store)
    monkeypatch.setattr(md, "applied_package_version", lambda _loaded: cut)
    package, version = await ws.package_in_force(SLUG, CREW)
    assert version == expect
    assert (package is not None) == bool(expect)
