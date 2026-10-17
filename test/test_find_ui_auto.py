"""find_ui's auto tier: unregistered controls whose label and page are proven
from source, answered only on a full label match of more than one word, and
only when nothing proven whole matches.

The auto tier is a BUILD-TIME artifact (``static/dist/ui-index.auto.json``),
read beside the committed index when present. Synthetic file pairs pin the
loader and the ranking rules; a pair the real generator writes into tmp_path
pins that the tier exists, is well formed, and answers a few labels only it
carries. No test here reads a built dashboard bundle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import mcp_guide, ui_index

_WEBSITE = Path(__file__).resolve().parents[1] / "website"


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ui_index._cache.update(key=None, index=None)
    # Never the checkout's own dashboard build: each test names its auto file.
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _placement(route: str, parents: list[str]) -> dict[str, Any]:
    return {
        "surface_id": "knowledge",
        "route": route,
        "parent_ids": parents,
        "entry_kind": "content",
        "requires": [],
    }


def _auto(key: str, kind: str = "button", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"auto:page.knowledge:{key}",
        "kind": kind,
        "tier": "auto",
        "conditions_unknown": True,
        "label_key": key,
        "placements": [_placement("/knowledge", ["page.knowledge"])],
        **extra,
    }


_AUTO_EN = {
    "k.reindex": "Reindex sources",
    "k.export": "Export graph",
    "k.open": "Open it",
    "k.signin": "Open sign-in page",
    "k.back": "Back",
    "k.name": "Name",
    "k.short": "Re-run",
}
_AUTO_ZH = {"k.reindex": "重新索引来源", "k.open": "打开", "k.back": "返回", "k.name": "名称"}


def _files(
    tmp_path: Path,
    *,
    curated: dict[str, str] | None = None,
    auto_extra: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """(committed index, auto artifact) in ``tmp_path``.

    ``curated`` maps a curated control's label key to its English label;
    ``auto_extra`` is merged into the Reindex sources auto entry; ``artifact``
    overrides top-level artifact fields.
    """
    labels = {"k.page": "Knowledge"}
    locs: list[dict[str, Any]] = [
        {
            "id": "page.knowledge",
            "kind": "page",
            "tier": "generated",
            "label_key": "k.page",
            "placements": [{**_placement("/knowledge", []), "entry_kind": "rail"}],
        },
    ]
    for n, (key, label) in enumerate((curated or {}).items()):
        labels[key] = label
        locs.append(
            {
                "id": f"knowledge.curated{n}",
                "kind": "button",
                "tier": "curated",
                "label_key": key,
                "placements": [_placement("/knowledge", ["page.knowledge"])],
            }
        )
    committed = {
        "schema_version": 1,
        "input_digest": "sha256:committed",
        "coverage": {"scope": "test scope"},
        "locales": ["en", "zh-CN"],
        "surfaces": ["knowledge"],
        "locations": locs,
        "labels": {"en": labels, "zh-CN": {"k.page": "知识库"}},
    }
    auto_locs = [
        _auto("k.reindex", **(auto_extra or {})),
        _auto("k.export"),
        _auto("k.open", "link"),
        _auto("k.signin", "link"),
        _auto("k.back"),
        _auto("k.name"),
        _auto("k.short"),
    ]
    auto = {
        "schema_version": 1,
        "artifact": "auto",
        "base_input_digest": "sha256:committed",
        "input_digest": "sha256:auto",
        "coverage": {"scope": "auto-indexed test controls", "auto_controls": len(auto_locs)},
        "locales": ["en", "zh-CN"],
        "locations": auto_locs,
        "labels": {"en": dict(_AUTO_EN), "zh-CN": dict(_AUTO_ZH)},
        **(artifact or {}),
    }
    p, a = tmp_path / "ui-index.json", tmp_path / "ui-index.auto.json"
    p.write_text(json.dumps(committed), encoding="utf-8")
    a.write_text(json.dumps(auto), encoding="utf-8")
    return p, a


def _find(q: str, lang: str | None, files: tuple[Path, Path]) -> dict[str, Any]:
    return ui_index.find_ui(q, lang, path=files[0], auto_path=files[1])


def test_an_auto_entry_answers_its_full_label_hedged(tmp_path: Path) -> None:
    files = _files(tmp_path)
    for q in ("reindex sources", "where is the reindex sources button?"):
        d = _find(q, "en", files)
        assert d["status"] == "ok", (q, d)
        assert d["auto_tier"] == "available" and "auto_tier_reason" not in d
        assert d["coverage"] == "test scope; plus auto-indexed test controls"
        top = d["results"][0]
        assert top["id"] == "auto:page.knowledge:k.reindex"
        assert top["tier"] == "auto" and top["conditions_unknown"] is True
        assert [s["label"] for s in top["placements"][0]["path"]] == [
            "Knowledge",
            "Reindex sources",
        ]
    zh = _find("重新索引来源在哪里", "zh-CN", files)
    assert zh["results"][0]["label"] == "重新索引来源"


def test_a_weak_overlap_never_returns_an_auto_entry(tmp_path: Path) -> None:
    files = _files(tmp_path)
    # One shared word, a partial phrase, or a longer question the label only
    # half explains: a curated location could answer these; an auto one cannot.
    for q in (
        "sources",
        "reindex",
        "reindex all sources now",
        # The whole label, plus a word it does not explain (2/3 coverage would
        # answer for a curated location).
        "reindex sources now",
        "reindex sources and export graph",
        # Every word, but not the label's phrase: a token match, not the label.
        "sources reindex",
        # Only part of the label, whose other words are all question words.
        "sign",
        "sign in",
    ):
        assert _find(q, "en", files)["status"] == "no_match", q
    assert _find("open sign-in page", "en", files)["status"] == "ok"
    assert _find("重新索引", "zh-CN", files)["status"] == "no_match"
    # A label made only of question words ("Open it", "打开") names nothing to match.
    assert _find("open it", "en", files)["status"] == "no_match"
    assert _find("打开", "zh-CN", files)["status"] == "no_match"


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("back", "en"),
        ("name", "en"),
        ("where is the back button", "en"),
        ("Name", None),
        ("返回", "zh-CN"),
        ("名称在哪里", "zh-CN"),
    ],
)
def test_a_one_word_auto_label_never_answers(query: str, lang: str | None, tmp_path: Path) -> None:
    # The whole label IS the question, which answers for a longer auto label;
    # one word on one page names any of many look-alike controls.
    d = _find(query, lang, _files(tmp_path))
    assert d["status"] == "no_match", d


def test_the_one_word_rule_counts_words_not_characters(tmp_path: Path) -> None:
    # "Re-run" folds to two words; a two-character CJK label is one word.
    d = _find("re-run", "en", _files(tmp_path))
    assert d["status"] == "ok" and d["results"][0]["id"] == "auto:page.knowledge:k.short"


def test_the_one_word_rule_leaves_curated_locations_alone(tmp_path: Path) -> None:
    files = _files(tmp_path, curated={"k.cback": "Back"})
    d = _find("back", "en", files)
    assert [r["id"] for r in d["results"]] == ["knowledge.curated0"]


def test_curated_beats_auto_on_the_same_label(tmp_path: Path) -> None:
    files = _files(tmp_path, curated={"k.curated": "Reindex sources"})
    d = _find("reindex sources", "en", files)
    assert [r["id"] for r in d["results"]] == ["knowledge.curated0"]
    assert "tier" not in d["results"][0] and d["ambiguous"] is False
    # Even a weaker curated match outranks an exact auto one.
    files = _files(tmp_path, curated={"k.curated": "Reindex the knowledge sources"})
    d = _find("reindex sources", "en", files)
    assert d["results"][0]["id"] == "knowledge.curated0"
    assert all(r.get("tier") != "auto" for r in d["results"])


def test_an_auto_entry_is_never_the_sole_verb_control(tmp_path: Path) -> None:
    d = _find("export button", "en", _files(tmp_path))
    assert d["status"] == "no_match" and "needs_object" in d


def test_without_an_auto_file_the_committed_tiers_answer_alone(tmp_path: Path) -> None:
    committed, auto = _files(tmp_path)
    auto.unlink()
    d = _find("reindex sources", "en", (committed, auto))
    assert d["status"] == "no_match"
    assert d["auto_tier"] == "unavailable" and "not built" in d["auto_tier_reason"]
    assert d["coverage"] == "test scope"
    page = _find("knowledge", "en", (committed, auto))
    assert page["status"] == "ok" and page["results"][0]["id"] == "page.knowledge"
    assert page["auto_tier"] == "unavailable"


def test_the_default_auto_file_is_the_dashboard_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed, auto = _files(tmp_path)
    monkeypatch.setattr(ui_index, "INDEX_PATH", committed)
    monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", auto)
    d = ui_index.find_ui("reindex sources", "en")
    assert d["status"] == "ok" and d["auto_tier"] == "available"
    # An explicit committed index never picks up the default auto file.
    ui_index._cache.update(key=None, index=None)
    d = ui_index.find_ui("reindex sources", "en", path=committed)
    assert d["status"] == "no_match" and d["auto_tier"] == "unavailable"


@pytest.mark.parametrize(
    ("auto_extra", "artifact", "raw"),
    [
        ({"terms": {"en": ["rebuild index"]}}, None, None),
        ({"conditions_unknown": False}, None, None),
        ({"tier": "guessed"}, None, None),
        ({"tier": "curated"}, None, None),
        ({"placements": [_placement("/knowledge", ["page.elsewhere"])]}, None, None),
        (None, {"base_input_digest": "sha256:another-build"}, None),
        (None, {"artifact": "committed"}, None),
        (None, {"locales": ["en"]}, None),
        (None, {"labels": {"en": {**_AUTO_EN, "k.page": "Not Knowledge"}, "zh-CN": {}}}, None),
        (None, {"locations": "not a list"}, None),
        (None, None, "{not json"),
    ],
)
def test_a_bad_auto_file_disables_only_the_auto_tier(
    tmp_path: Path,
    auto_extra: dict[str, Any] | None,
    artifact: dict[str, Any] | None,
    raw: str | None,
) -> None:
    files = _files(tmp_path, auto_extra=auto_extra, artifact=artifact)
    if raw is not None:
        files[1].write_text(raw, encoding="utf-8")
    d = _find("reindex sources", "en", files)
    assert d["status"] == "no_match", d
    assert d["auto_tier"] == "unavailable" and d["auto_tier_reason"]
    # The committed index is untouched: its page still answers, in both locales.
    page = _find("knowledge", "en", files)
    assert page["status"] == "ok" and page["results"][0]["id"] == "page.knowledge"
    assert _find("知识库", "zh-CN", files)["results"][0]["label"] == "知识库"


def test_a_committed_index_carrying_auto_entries_is_unavailable(tmp_path: Path) -> None:
    committed, auto = _files(tmp_path)
    raw = json.loads(committed.read_text(encoding="utf-8"))
    raw["locations"].append(_auto("k.page"))
    committed.write_text(json.dumps(raw), encoding="utf-8")
    assert _find("knowledge", "en", (committed, auto))["status"] == "unavailable"


def test_a_changed_auto_file_is_reread(tmp_path: Path) -> None:
    committed, auto = _files(tmp_path)
    assert _find("reindex sources", "en", (committed, auto))["status"] == "ok"
    auto.write_text("{}", encoding="utf-8")
    assert _find("reindex sources", "en", (committed, auto))["status"] == "no_match"


# ── the real committed index and a real generated auto tier ──


def _real() -> dict[str, Any]:
    return json.loads(ui_index.INDEX_PATH.read_text(encoding="utf-8"))


def test_the_committed_index_carries_no_auto_tier() -> None:
    real = _real()
    assert {loc.get("tier") for loc in real["locations"]} == {"generated", "curated"}
    assert "auto" not in real["coverage"]["tiers"] and "auto_controls" not in real["coverage"]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """(index, auto tier) the real generator writes into a scratch dir.

    ``--out`` keeps the committed index untouched (its freshness is
    ``gen:ui --check``'s job). Needs node and ``website/node_modules``: without
    them it skips, except where ``KIROCREW_UI_GENERATOR_REQUIRED`` is set (the
    CI frontend-lint job, which has both), where a skip would hide a broken
    toolchain and so fails instead.
    """
    node = shutil.which("node")
    if not node or not (_WEBSITE / "node_modules" / "vite").is_dir():
        reason = "building the auto tier needs node and website/node_modules (npm ci)"
        if os.environ.get("KIROCREW_UI_GENERATOR_REQUIRED"):
            pytest.fail(reason + "; KIROCREW_UI_GENERATOR_REQUIRED is set")
        pytest.skip(reason)
    out = tmp_path_factory.mktemp("ui-auto")
    index, auto = out / "ui-index.generated.json", out / "ui-index.auto.json"
    r = subprocess.run(
        [node, "scripts/gen-ui-index.mjs", "--out", str(index), "--auto-out", str(auto)],
        cwd=_WEBSITE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert r.returncode == 0, r.stderr[-4000:]
    return index, auto


def test_the_generated_auto_tier_is_well_formed(built: tuple[Path, Path]) -> None:
    index = json.loads(built[0].read_text(encoding="utf-8"))
    auto = json.loads(built[1].read_text(encoding="utf-8"))
    assert auto["artifact"] == "auto" and auto["base_input_digest"] == index["input_digest"]
    assert auto["locales"] == index["locales"]
    by_id = {loc["id"]: loc for loc in index["locations"]}
    shown = {
        k for loc in index["locations"] for k in (loc["label_key"], *loc.get("alias_keys", []))
    }
    assert len(auto["locations"]) >= 100
    assert auto["coverage"]["auto_controls"] == len(auto["locations"])
    assert 0 < auto["coverage"]["core_coverage_pct"] <= 100
    for loc in auto["locations"]:
        assert loc["tier"] == "auto" and loc["id"] not in by_id
        # A label a generated or curated location already shows is left to it.
        assert loc["label_key"] not in shown, loc["id"]
        assert loc["conditions_unknown"] is True and "terms" not in loc, loc["id"]
        for locale in auto["locales"]:
            assert auto["labels"][locale].get(loc["label_key"]), (loc["id"], locale)
        for p in loc["placements"]:
            assert p["entry_kind"] == "content"
            if p["surface_id"] == "shell":
                # Drawn by the app shell: on every page, no route, no path. A
                # control a page draws too is a shared one, the shell listed last.
                assert p["route"] == "" and p["parent_ids"] == [] and p["requires"] == []
                assert loc["id"].startswith(("auto:shell:", "auto:shared:")), loc["id"]
                continue
            parent = by_id[p["parent_ids"][-1]]
            assert parent["kind"] in ("page", "tab") and parent.get("tier") != "auto"
    # Controls only the shell draws (the rail, the top bar) are indexed, not skipped.
    assert sum(loc["placements"][0]["surface_id"] == "shell" for loc in auto["locations"]) >= 20


def test_a_generated_shell_only_control_is_on_every_page(built: tuple[Path, Path]) -> None:
    d = ui_index.find_ui("new terminal", "en", path=built[0], auto_path=built[1])
    assert d["status"] == "ok", d
    top = d["results"][0]
    assert top["tier"] == "auto" and top["label"] == "New terminal"
    assert top["placements"][0] == {**top["placements"][0], "on_every_page": True}
    assert "route" not in top["placements"][0]
    assert [s["label"] for s in top["placements"][0]["path"]] == ["New terminal"]
    # Kept under any surface filter, like a registered shell control.
    assert (
        ui_index.find_ui("new terminal", "en", "chat", path=built[0], auto_path=built[1])[
            "results"
        ][0]["id"]
        == top["id"]
    )


@pytest.mark.parametrize(
    ("query", "lang", "label", "path"),
    [
        (
            "continue with github",
            "en",
            "Continue with GitHub",
            ["Settings", "Agent Harness", "Continue with GitHub"],
        ),
        (
            "where is the download tailscale button",
            "en",
            "Download Tailscale",
            ["Settings", "Overview", "Download Tailscale"],
        ),
        ("download tailscale", "en", "Download Tailscale", None),
        ("下载 Tailscale", "zh-CN", "下载 Tailscale", ["设置", "概览", "下载 Tailscale"]),
        ("copy redirect URI", "en", "Copy redirect URI", None),
        ("show pairing code", "en", "Show pairing code", None),
    ],
)
def test_the_generated_auto_tier_answers_labels_only_it_carries(
    built: tuple[Path, Path], query: str, lang: str, label: str, path: list[str] | None
) -> None:
    d = ui_index.find_ui(query, lang, path=built[0], auto_path=built[1])
    assert d["status"] == "ok" and d["auto_tier"] == "available", d
    top = d["results"][0]
    assert top["tier"] == "auto" and top["conditions_unknown"] is True
    assert top["label"] == label
    if path is not None:
        assert [s["label"] for s in top["placements"][0]["path"]] == path
    # Without the build-time file the same question is curated-only: no answer.
    ui_index._cache.update(key=None, index=None)
    bare = ui_index.find_ui(query, lang, path=built[0])
    assert bare["status"] == "no_match" and bare["auto_tier"] == "unavailable"


@pytest.mark.parametrize("query", ["back", "name", "Back", "where is the name field"])
def test_the_generated_auto_tier_never_answers_one_word(
    built: tuple[Path, Path], query: str
) -> None:
    auto = json.loads(built[1].read_text(encoding="utf-8"))
    english = {auto["labels"]["en"][loc["label_key"]] for loc in auto["locations"]}
    # The tier really carries these labels; the one-word rule is what refuses them.
    assert {"Back", "Name"} <= english
    d = ui_index.find_ui(query, None, path=built[0], auto_path=built[1])
    assert d["status"] == "no_match", d


def test_the_tool_description_says_how_to_relay_an_auto_result() -> None:
    tool = next(t for t in mcp_guide._list_tools() if t["name"] == "find_ui")
    assert "tier: auto" in tool["description"]
    assert "hedged" in tool["description"]
    assert "auto_tier: unavailable" in tool["description"]
