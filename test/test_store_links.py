"""The optional app.json ``links`` field: validated, then carried to the store row."""

from __future__ import annotations

import logging

import pytest

from kiro_crew.apps import official_catalog as oc
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.registry import _merge_manifest
from kiro_crew.apps.store_links import MAX_LINKS, sanitize_links

GOOD = {"label": "Wiki", "url": "https://wiki.example.com/my-app"}
GUIDE = {"label": "User guide", "url": "https://example.com/guide?x=1#top"}


def test_good_links_are_kept_in_order():
    assert sanitize_links([GOOD, GUIDE]) == [GOOD, GUIDE]


def test_explicit_default_port_is_kept():
    link = {"label": "Docs", "url": "https://example.com:443/docs"}
    assert sanitize_links([link]) == [link]


def test_label_is_trimmed_and_extra_keys_dropped():
    assert sanitize_links([{"label": "  Wiki ", "url": GOOD["url"], "icon": "x"}]) == [GOOD]


@pytest.mark.parametrize(
    "entry",
    [
        {"label": "x", "url": "javascript:alert(1)"},
        {"label": "x", "url": "data:text/html,hi"},
        {"label": "x", "url": "http://example.com/"},
        {"label": "x", "url": "https://user@example.com/"},
        {"label": "x", "url": "https://user:pw@example.com/"},
        {"label": "x", "url": "https://example.com:8443/"},
        {"label": "x", "url": "https://example.com:notaport/"},
        {"label": "x", "url": "https:///no-host"},
        {"label": "x", "url": "https://example.com/a b"},
        {"label": "x", "url": "https://example.com\\@evil.example/"},
        {"label": "x", "url": "https://example.com/" + "a" * 2048},
        {"label": "x", "url": 5},
        {"label": "", "url": GOOD["url"]},
        {"label": "   ", "url": GOOD["url"]},
        {"label": "a" * 41, "url": GOOD["url"]},
        {"label": "a\nb", "url": GOOD["url"]},
        {"label": 3, "url": GOOD["url"]},
        {"url": GOOD["url"]},
        "https://example.com/",
        None,
    ],
)
def test_each_bad_kind_is_dropped_and_logged(entry, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.store_links"):
        assert sanitize_links([entry, GOOD], app_name="demo") == [GOOD]
    assert sum("dropped 1 invalid" in r.getMessage() for r in caplog.records) == 1


def test_entries_past_the_cap_are_dropped_and_logged_as_over_the_cap(caplog):
    links = [{"label": f"L{i}", "url": f"https://example.com/{i}"} for i in range(MAX_LINKS + 2)]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.store_links"):
        assert sanitize_links(links) == links[:MAX_LINKS]
    (record,) = caplog.records
    assert "dropped 0 invalid" in record.getMessage()
    assert f"2 past the cap of {MAX_LINKS}" in record.getMessage()


@pytest.mark.parametrize("raw", [{"label": "x"}, "https://example.com", 7, None])
def test_a_non_list_value_yields_nothing(raw):
    assert sanitize_links(raw) == []


def test_a_bad_links_value_never_fails_the_manifest():
    data = {"name": "demo-app", "version": "1.0.0", "displayName": "Demo", "description": "d"}
    bad = AppManifest.from_dict({**data, "links": "nope"})
    assert "links" not in bad.to_dict()
    mixed = AppManifest.from_dict({**data, "links": [GOOD, {"label": "x", "url": "http://x"}]})
    assert mixed.to_dict()["links"] == [GOOD]


def test_installed_manifest_without_links_is_unchanged():
    data = {"name": "demo-app", "version": "1.0.0", "displayName": "Demo", "description": "d"}
    assert "links" not in AppManifest.from_dict(data).to_dict()


def test_registry_row_carries_only_valid_links():
    entry = {"name": "demo-app", "repo": "https://github.com/org/demo-app"}
    manifest = {"links": [GOOD, {"label": "Bad", "url": "javascript:alert(1)"}]}
    assert _merge_manifest(entry, manifest)["links"] == [GOOD]
    assert "links" not in _merge_manifest(entry, {"displayName": "Demo"})
    assert "links" not in _merge_manifest(entry, {"links": [{"label": "x", "url": "http://x"}]})


def _catalog_entry(**over):
    entry = {
        "name": "demo-app",
        "source": {"type": "git", "url": "https://github.com/org/demo-app", "ref": "a" * 40},
        "version": "1.0.0",
        "displayName": "Demo",
    }
    entry.update(over)
    return entry


def test_catalog_rows_carry_valid_links(monkeypatch):
    entry = _catalog_entry(links=[GOOD, {"label": "x", "url": "data:,x"}])
    (row,) = oc.inventory([entry])
    assert row["links"] == [GOOD]
    monkeypatch.setattr(oc, "load_official_catalog", lambda: [entry])
    (display_row,) = oc.list_catalog_rows()
    assert display_row["links"] == [GOOD]
    overlaid = [{"name": "demo-app"}]
    oc.annotate(overlaid, [entry])
    assert overlaid[0]["links"] == [GOOD]


def test_catalog_rows_without_links_are_unchanged():
    (row,) = oc.inventory([_catalog_entry()])
    assert "links" not in row
