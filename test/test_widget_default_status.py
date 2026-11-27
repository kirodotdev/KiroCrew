"""``dashboard.widget_snapshot``: the text sanitizer and the default "Status" widget.

The sanitizer (mobile widgets design, 3.2.3) leaves every snapshot string plain: C0 and
C1 controls and bidi controls removed, whitespace collapsed, cut to 80 grapheme clusters
without splitting one. It runs only on text that was already redacted, so a cut can
never keep part of a credential. The default widget (3.2.4) is there for every Bot,
with or without a dashboard package, draws an unseen fold as ``null`` and never as 0,
and its fold paths and list row key are pinned against ``dashboard_types.describe`` so
a rename in a fold renderer fails here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from kiro_crew import dashboard_types
from kiro_crew.dashboard import widget_snapshot as ws
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard_feed import FOLD_UNSEEN, FieldRead
from kiro_crew.platform.context import redact_via_context
from kiro_crew.testing.synthetic_credentials import github_classic

SLUG = "pebble"
CREW = "Pebble"
NOW = datetime(2026, 11, 18, 0, 30, 0, tzinfo=timezone.utc)
BOT = {"slug": SLUG, "member": CREW, "name": "Pebble"}
TOKEN = github_classic(seed=83)
LIMIT = ws.MAX_TEXT_GRAPHEMES


def _wire(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False)


def _block(snapshot: dict[str, Any], label: str) -> dict[str, Any]:
    return next(b for b in snapshot["blocks"] if b.get("label") == label)


def _default(read: FieldRead, **kw: Any) -> dict[str, Any]:
    manifest = ws.cut_manifest(ws.DEFAULT_TYPES, ws.DEFAULT_WIDGET, slug=SLUG, version=0)
    return ws.shape_snapshot(
        ws.DEFAULT_WIDGET,
        ws.DEFAULT_TYPES,
        read,
        manifest,
        bot=kw.pop("bot", BOT),
        layout_version=0,
        now=NOW,
        **kw,
    )


# ----------------------------------------------------------------- the sanitizer


@pytest.mark.parametrize(
    "raw",
    [
        "a\x00b",
        "a\x07b",
        "a\x1bb",
        "a\x7fb",
        "a\x80b",
        "a\x9bb",
        "a\u202ab",
        "a\u202eb",
        "a\u2066b",
        "a\u2069b",
        "a\u200eb",
        "a\u200fb",
        "a\u061cb",
    ],
)
def test_control_and_bidi_characters_are_removed(raw):
    assert ws.sanitize_text(raw) == "ab"


def test_whitespace_collapses_and_keeps_words_apart():
    assert ws.sanitize_text("  one\ttwo\n\nthree\r\n four\x85five\u2028six  ") == (
        "one two three four five six"
    )
    # A removed character between two spaces leaves one space, not two.
    assert ws.sanitize_text("a \x00 b \u202e c") == "a b c"


def test_text_at_the_limit_is_kept_and_one_more_is_cut_with_an_ellipsis():
    exact = "x" * LIMIT
    assert ws.sanitize_text(exact) == exact
    cut = ws.sanitize_text("x" * (LIMIT + 1))
    assert cut == "x" * (LIMIT - 1) + ws.ELLIPSIS
    assert len(cut) == LIMIT


def test_the_cut_drops_trailing_space_before_the_ellipsis():
    text = "x" * (LIMIT - 2) + " " + "y" * 10
    assert ws.sanitize_text(text) == "x" * (LIMIT - 2) + ws.ELLIPSIS


@pytest.mark.parametrize(
    "cluster",
    [
        "\U0001f44d\U0001f3fd",  # thumbs up, medium skin tone
        "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",  # family ZWJ
        "\U0001f3f3\ufe0f\u200d\U0001f308",  # rainbow flag ZWJ
        "\U0001f1ef\U0001f1f5",  # flag JP
        "e\u0301\u0323",  # e with two combining marks
        "\u0e01\u0e49",  # Thai consonant and tone mark
        "1\ufe0f\u20e3",  # keycap
    ],
)
def test_the_cut_never_splits_a_grapheme_cluster(cluster):
    # One cluster is LIMIT long by count of clusters, however many code points.
    exact = cluster * LIMIT
    assert ws.sanitize_text(exact) == exact
    cut = ws.sanitize_text(cluster * (LIMIT + 3))
    assert cut == cluster * (LIMIT - 1) + ws.ELLIPSIS
    # Landing the cut right after an ASCII prefix still keeps the cluster whole.
    mixed = "a" * (LIMIT - 2) + cluster + "tail"
    assert ws.sanitize_text(mixed) == "a" * (LIMIT - 2) + cluster + ws.ELLIPSIS


def test_every_string_in_a_snapshot_is_sanitized():
    dirty = "\u202eevil\u202c\nline " + "z" * 200
    read = FieldRead(
        fields={
            "waiting_count": dirty,
            "waiting_items": [{"text": dirty}, dirty],
            "workers_total": 1,
        }
    )
    snap = _default(read, bot={"slug": SLUG, "member": CREW, "name": "\u2066Peb\x00ble\u2069"})
    assert snap["bot"]["name"] == "Pebble"
    assert snap["blocks"][0]["text"] == "Pebble"
    expected = ws.sanitize_text(dirty)
    assert expected.startswith("evil line ") and expected.endswith(ws.ELLIPSIS)
    assert _block(snap, "Waiting on you")["value"] == expected
    assert [i["text"] for i in _block(snap, "Waiting")["items"]] == [expected, expected]

    def strings(node: Any) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for k, v in node.items() for s in (*strings(k), *strings(v))]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return []

    for text in strings(snap):
        assert text == ws.plain_text(text)
        assert ws.cut_graphemes(text) == text


def test_status_tone_uses_the_value_before_display_sanitization():
    types = {
        "state": {"type": "enum", "choices": ["Needs  attention"], "source": {"agentic": True}}
    }
    widget = {
        "id": "attention",
        "title": "Attention",
        "sizes": ["small"],
        "blocks": [
            {
                "type": "status",
                "label": "State",
                "field": "state",
                "tones": {"Needs  attention": "bad"},
            }
        ],
        "tap": {"open": "bot"},
    }
    manifest = ws.cut_manifest(types, widget, slug=SLUG, version=1)
    snap = ws.shape_snapshot(
        widget,
        types,
        FieldRead(fields={"state": "Needs  attention"}),
        manifest,
        bot=BOT,
        layout_version=1,
        now=NOW,
    )
    status = _block(snap, "State")
    assert status["text"] == "Needs attention"
    assert status["tone"] == "bad"


# ----------------------------------------------------------------- redaction first


def test_the_cut_runs_after_redaction_so_no_part_of_a_key_survives():
    """A key that straddles the cut: cutting first would keep a prefix no pattern
    matches. The redactor sees the whole key first."""
    text = "x" * (LIMIT - 13) + " " + TOKEN
    unsafe = ws.sanitize_text(text)
    assert (
        TOKEN[:11] in unsafe and redact_via_context(unsafe) == unsafe
    ), "the fixture must leak when the sanitizer runs first"
    snap = _default(FieldRead(fields={"waiting_items": [{"text": text}], "workers_total": 0}))
    assert TOKEN[:8] not in _wire(snap)
    # The Bot's name is not a field value, so only the string pass stands between it
    # and the cut.
    named = _default(
        FieldRead(fields={"workers_total": 0}),
        bot={"slug": SLUG, "member": CREW, "name": text},
    )
    assert TOKEN[:8] not in _wire(named)
    assert named["bot"]["name"].startswith("xxx")


def test_a_key_a_control_character_split_is_caught_before_the_cut():
    """The first pass cannot see a key with a C0 control inside it; removing the
    control joins it, and it is redacted again before the cut."""
    hidden = TOKEN[:12] + "\x07" + TOKEN[12:]
    assert redact_via_context(hidden) == hidden, "the fixture must slip the first pass"
    text = "x" * (LIMIT - 15) + " " + hidden
    snap = _default(FieldRead(fields={"waiting_items": [text], "workers_total": 0}))
    assert TOKEN[:12] not in _wire(snap)


# ----------------------------------------------------------------- the default widget


def test_the_default_widget_constant():
    widget = ws.DEFAULT_WIDGET
    assert widget["id"] == "status"
    assert widget["sizes"] == ["small", "medium"]
    assert widget["tap"] == {"open": "bot"}
    blocks = widget["blocks"]
    assert [b["type"] for b in blocks] == ["title", "metric", "metric", "list"]
    assert (blocks[1]["label"], blocks[1]["field"], blocks[1]["warn_at"]) == (
        "Waiting on you",
        "waiting_count",
        1,
    )
    assert (blocks[2]["label"], blocks[2]["field"]) == ("Workers", "workers_total")
    assert (blocks[3]["label"], blocks[3]["field"]) == ("Waiting", "waiting_items")
    assert (blocks[3]["item_text"], blocks[3]["max_items"]) == ("text", 3)


async def _resolve(monkeypatch, package: dict[str, Any] | None, read: FieldRead) -> dict:
    async def in_force(slug: str, member: str) -> tuple[dict[str, Any] | None, int]:
        return package, 12 if package else 0

    monkeypatch.setattr(ws, "package_in_force", in_force)
    monkeypatch.setattr(md, "read_fields", lambda slug, member, manifest: read)
    snap = await ws.resolve_snapshot(SLUG, CREW, "status", name="Pebble", now=NOW)
    assert snap is not None
    return snap


def _full_read() -> FieldRead:
    return FieldRead(
        fields={
            "waiting_count": 2,
            "waiting_items": [{"text": "Approve the deploy"}, {"text": "Pick a name"}],
            "waiting_omitted": 0,
            "workers_total": 3,
            "_at_waiting": "2026-11-18T00:25:00Z",
        }
    )


def _package() -> dict[str, Any]:
    return {
        "model": {"types": {"open_prs": {"type": "number", "source": {"agentic": True}}}},
        "widgets": [
            {
                "id": "prs",
                "title": "PRs",
                "sizes": ["small"],
                "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
                "tap": {"open": "bot"},
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("package", [None, _package()], ids=["no-package", "package"])
async def test_every_bot_gets_the_default_widget(monkeypatch, package):
    assert ws.widgets_of(package)[-1] is ws.DEFAULT_WIDGET
    snap = await _resolve(monkeypatch, package, _full_read())
    assert snap["widget"] == {
        "id": "status",
        "title": "Status",
        "sizes": ["small", "medium"],
        "default": True,
    }
    assert snap["layout_version"] == 0
    assert snap["tap"] == {"open": "bot", "value": SLUG}
    assert snap["blocks"][0]["text"] == "Pebble"
    assert _block(snap, "Waiting on you")["value"] == 2
    assert _block(snap, "Waiting on you")["tone"] == "warn"
    assert _block(snap, "Workers")["value"] == 3
    waiting = _block(snap, "Waiting")
    assert [i["text"] for i in waiting["items"]] == ["Approve the deploy", "Pick a name"]
    assert waiting["freshness"]["at"] == "2026-11-18T00:25:00Z"
    # A package's own model never feeds the default widget.
    assert "open_prs" not in _wire(snap)


#: Each fold the default widget reads, with the fields it feeds.
_FOLD_FIELDS = {
    "waiting": ("waiting_count", "waiting_items", "waiting_omitted", "_at_waiting"),
    "workers": ("workers_total",),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fold", sorted(_FOLD_FIELDS))
async def test_an_unseen_fold_draws_null_never_zero(monkeypatch, fold):
    read = _full_read()
    for name in _FOLD_FIELDS[fold]:
        read.fields.pop(name, None)
        read.missing.append(name)
        read.unresolved[name] = FOLD_UNSEEN
    snap = await _resolve(monkeypatch, None, read)
    blocks = {"waiting": ("Waiting on you", "Waiting"), "workers": ("Workers",)}[fold]
    for label in blocks:
        block = _block(snap, label)
        assert block["freshness"] == {
            "state": "missing",
            "at": None,
            "stale_after": None,
            "failed_at": None,
            "source": "fold",
        }
        if block["type"] == "metric":
            assert block["value"] is None
            assert block["tone"] == "idle"
        else:
            assert (block["items"], block["omitted"]) == ([], 0)
    # The other fold still resolves.
    other = "Workers" if fold == "waiting" else "Waiting on you"
    assert _block(snap, other)["freshness"]["state"] == "current"
    assert _block(snap, other)["value"] in (2, 3)


@pytest.mark.asyncio
async def test_every_fold_unseen_still_gives_the_widget(monkeypatch):
    names = [n for fields in _FOLD_FIELDS.values() for n in fields]
    read = FieldRead(missing=list(names), unresolved={n: FOLD_UNSEEN for n in names})
    snap = await _resolve(monkeypatch, None, read)
    assert snap["blocks"][0]["text"] == "Pebble"
    assert {_block(snap, label)["value"] for label in ("Waiting on you", "Workers")} == {None}
    assert '"value": 0' not in _wire(snap)


# ----------------------------------------------------------------- pinned fold paths


def _described(fold: str, path: str) -> dict[str, Any]:
    entry = next(t for t in dashboard_types.describe()["types"] if t["name"] == fold)
    node: dict[str, Any] = entry["shape"]
    for part in path.split("."):
        properties = node.get("properties")
        assert isinstance(properties, dict) and part in properties, f"{fold}.{path} is gone"
        node = properties[part]
    return node


def test_the_design_paths_are_the_default_types():
    paths = {
        name: (spec["source"]["fold"], spec["source"]["path"])
        for name, spec in ws.DEFAULT_TYPES.items()
    }
    assert {
        "waiting_count": ("waiting", "count"),
        "waiting_items": ("waiting", "items"),
        "workers_total": ("workers", "total"),
        "milestones_total": ("goal", "milestones_total"),
    }.items() <= paths.items()


@pytest.mark.parametrize("name", sorted(ws.DEFAULT_TYPES))
def test_every_default_path_is_in_dashboard_types_describe(name):
    spec = ws.DEFAULT_TYPES[name]
    node = _described(spec["source"]["fold"], spec["source"]["path"])
    expected = {"number": "number", "array": "array"}[spec["type"]]
    assert node["type"] == expected


def test_text_is_a_published_row_key_of_the_waiting_items():
    node = _described("waiting", "items")
    assert "text" in node["row_keys"]
    assert node["names_the_row"] == "text"
    list_block = next(b for b in ws.DEFAULT_WIDGET["blocks"] if b["type"] == "list")
    assert list_block["item_text"] in node["row_keys"]


def test_the_stamp_path_the_default_widget_reads_is_in_describe():
    assert _described("waiting", ws._STAMPED_FOLDS["waiting"])["type"] == "string"
