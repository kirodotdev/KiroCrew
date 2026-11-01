"""A link-preview provider's authority is granted by what it can see, not by its id.

* Hosts are normalized (case, trailing dot, IDNA) and may not claim a public
  suffix or a bare TLD.
* The approved set stores the normalized hosts and each pattern; an update that
  adds one holds it back until the owner approves, one that drops one needs
  nothing, and a stored set from before these families approves what is declared.
* The dashboard mirrors the normalization and the suffix list.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import app_frame as lpf
from kiro_crew.apps import link_preview_grants as g
from kiro_crew.apps import point_grants as pg
from kiro_crew.apps.manager_records import (
    STAGED_GRANT_FAMILIES,
    _approved_grants_after_manifest_change,
    _declared_grant_entries,
    _held_back_entries,
    _parse_approved_grants,
)
from kiro_crew.apps.manifest_contributes import LinkPreviewConfig

ROOT = Path(__file__).resolve().parents[1]


def _manifest(hosts: list[str], patterns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    preview: dict[str, Any] = {"id": "cards", "entry": "dist/lp.mjs", "hosts": hosts}
    if patterns is not None:
        preview["textPatterns"] = patterns
    return {
        "name": "cards-app",
        "permissions": {"api": ["/api/x"]},
        "contributes": {"linkPreviews": [preview]},
    }


@pytest.mark.parametrize(
    ("raw", "normal"),
    [
        ("Tickets.Example.COM.", "tickets.example.com"),
        (".Example.dev", ".example.dev"),
        ("bücher.example", "xn--bcher-kva.example"),
        # Transitional IDNA would turn this into fass.de, a different domain from
        # the browser's xn--fa-hia.de: refused, never claimed as the wrong one.
        ("faß.de", None),
        ("σίσυφος\u03c2.example", None),
        ("", None),
        (".", None),
    ],
)
def test_hosts_are_normalized(raw: str, normal: str | None) -> None:
    assert g.normalize_host(raw) == normal


@pytest.mark.parametrize(
    "host", [".co.uk", "co.uk", "com.au", ".github.io", "vercel.app", "com", ".dev"]
)
def test_a_public_suffix_or_bare_tld_is_refused(host: str) -> None:
    normal = g.normalize_host(host)
    assert normal is not None
    assert g.host_refusal(normal) is not None
    errors = LinkPreviewConfig.from_dict({"id": "c", "entry": "x.mjs", "hosts": [host]}).validate()
    assert any("hosts entry" in e for e in errors), errors


@pytest.mark.parametrize(
    "host", ["example.co.uk", ".example.com", "me.github.io", "xn--bcher-kva.example"]
)
def test_an_ordinary_host_is_accepted(host: str) -> None:
    assert g.host_refusal(host) is None


def test_the_manifest_keeps_the_normalized_form() -> None:
    cfg = LinkPreviewConfig.from_dict(
        {"id": "c", "entry": "x.mjs", "hosts": ["Tickets.Example.COM."]}
    )
    assert cfg.hosts == ["tickets.example.com"]
    assert cfg.validate() == []


def test_full_url_is_an_explicit_boolean_opt_in() -> None:
    on = LinkPreviewConfig.from_dict(
        {"id": "c", "entry": "x.mjs", "hosts": ["a.example.com"], "fullUrl": True}
    )
    assert on.fullUrl and on.to_dict()["fullUrl"] is True
    off = LinkPreviewConfig.from_dict({"id": "c", "entry": "x.mjs", "hosts": ["a.example.com"]})
    assert "fullUrl" not in off.to_dict()
    bad = LinkPreviewConfig.from_dict(
        {"id": "c", "entry": "x.mjs", "hosts": ["a.example.com"], "fullUrl": "yes"}
    )
    assert any("fullUrl" in e for e in bad.validate())


def test_the_descriptor_is_hosts_and_patterns() -> None:
    m = _manifest(
        ["A.Example.com.", ".b.example.com"], [{"id": "t", "pattern": "T-\\d+", "flags": "gi"}]
    )
    src = pg.grant_source(m)
    assert src is not None
    assert src["api"] == ["/api/x"]
    assert src["linkPreviewHosts"] == ["a.example.com", ".b.example.com"]
    assert src["linkPreviewTextPatterns"] == ["t:i:T-\\d+"]
    assert set(STAGED_GRANT_FAMILIES) >= set(g.LINK_PREVIEW_GRANT_FAMILIES)
    assert _declared_grant_entries(src)["linkPreviewHosts"] == ["a.example.com", ".b.example.com"]


def test_a_widening_update_holds_the_new_host_and_pattern_back() -> None:
    old = pg.grant_source(_manifest(["a.example.com"], [{"id": "t", "pattern": "T-\\d+"}]))
    new = pg.grant_source(
        _manifest(["a.example.com", "b.example.com"], [{"id": "t", "pattern": "T-\\d+|.*"}])
    )
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(old), old_permissions=old, new_permissions=new
    )
    assert approved["linkPreviewHosts"] == ["a.example.com"]
    # Same id, another regex: a new entry, not approved.
    assert approved["linkPreviewTextPatterns"] == []
    held = _held_back_entries(approved, new)
    assert held["linkPreviewHosts"] == ["b.example.com"]
    assert held["linkPreviewTextPatterns"] == ["t::T-\\d+|.*"]


def _with_full_url(m: dict[str, Any], on: bool) -> dict[str, Any]:
    m["contributes"]["linkPreviews"][0]["fullUrl"] = on
    return m


def test_turning_full_urls_on_is_held_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Query and fragment can carry a signed link's credential: asking for them widens."""
    old = pg.grant_source(_with_full_url(_manifest(["a.example.com"]), False))
    new = pg.grant_source(_with_full_url(_manifest(["a.example.com"]), True))
    assert old is not None and new is not None
    assert old["linkPreviewFullUrl"] == []
    assert new["linkPreviewFullUrl"] == [g.FULL_URL_ENTRY]
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(old), old_permissions=old, new_permissions=new
    )
    assert approved["linkPreviewFullUrl"] == []
    assert _held_back_entries(approved, new)["linkPreviewFullUrl"] == [g.FULL_URL_ENTRY]

    def staged(name: str, family: str, read: Any) -> list[str]:
        return list(approved[family])

    monkeypatch.setattr("kiro_crew.apps.manager_records.staged_app_grants", staged)
    row = g.link_preview_grant_row("cards-app", _with_full_url(_manifest(["a.example.com"]), True))
    assert row["linkPreviewGrant"]["fullUrl"] is False
    assert row["linkPreviewGrant"]["stagedFullUrl"] is True
    # Turning it off again needs nothing.
    back = _approved_grants_after_manifest_change(
        existing_approved={**approved, "linkPreviewFullUrl": [g.FULL_URL_ENTRY]},
        old_permissions=new,
        new_permissions=old,
    )
    assert not any(_held_back_entries(back, old).values())


def test_a_narrowing_update_needs_nothing() -> None:
    old = pg.grant_source(_manifest(["a.example.com", "b.example.com"]))
    new = pg.grant_source(_manifest(["a.example.com"]))
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(old), old_permissions=old, new_permissions=new
    )
    assert approved["linkPreviewHosts"] == ["a.example.com"]
    assert not any(_held_back_entries(approved, new).values())


def test_a_stored_set_from_before_the_families_approves_what_the_replaced_manifest_declared() -> (
    None
):
    legacy = _parse_approved_grants({"api": ["/api/x"], "events": []})
    assert "linkPreviewHosts" not in legacy
    old = pg.grant_source(_manifest(["a.example.com"]))
    new = pg.grant_source(_manifest(["a.example.com", "c.example.com"]))
    approved = _approved_grants_after_manifest_change(
        existing_approved=legacy, old_permissions=old, new_permissions=new
    )
    assert approved["linkPreviewHosts"] == ["a.example.com"]
    # And nothing is held back for a family the stored set never recorded.
    assert not _held_back_entries(legacy, new)["linkPreviewHosts"]


def test_a_damaged_set_approves_nothing() -> None:
    assert _parse_approved_grants("not a mapping")["linkPreviewHosts"] == []


def test_the_grant_row_reports_approved_and_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    def staged(name: str, family: str, read: Any) -> list[str]:
        declared = read()
        return declared[:1]

    monkeypatch.setattr("kiro_crew.apps.manager_records.staged_app_grants", staged)
    row = g.link_preview_grant_row("cards-app", _manifest(["a.example.com", "b.example.com"]))
    assert row["linkPreviewGrant"]["hosts"] == ["a.example.com"]
    assert row["linkPreviewGrant"]["stagedHosts"] == ["b.example.com"]
    assert g.link_preview_grant_row("x", {"contributes": {}}) == {}


def test_revoking_the_grant_stops_the_frame_and_its_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing approved the provider is gone: no mint, no frame, no fetch."""
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        name="cards-app",
        contributes=SimpleNamespace(linkPreviews=[SimpleNamespace(entry="dist/lp.mjs")]),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )
    monkeypatch.setattr(lpf, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(lpf, "get_app_manifest", lambda name: manifest)
    grant: dict[str, Any] = {"linkPreviewGrant": {"hosts": ["a.example.com"], "textPatterns": []}}
    monkeypatch.setattr(lpf, "link_preview_grant_row", lambda name, m: grant)
    assert lpf._provider_entry("cards-app") is not None
    grant["linkPreviewGrant"] = {"hosts": [], "textPatterns": []}
    assert lpf._provider_entry("cards-app") is None


def test_the_dashboard_mirrors_the_suffix_list() -> None:
    ts = (ROOT / "website/src/linkPreview/linkPreviewRegistry.ts").read_text(encoding="utf-8")
    block = ts[ts.index("export const PUBLIC_SUFFIXES") : ts.index("export const CC_SECOND_LEVELS")]
    assert set(re.findall(r"'([a-z0-9.-]+)'", block)) == set(g.PUBLIC_SUFFIXES)
    cc = ts[ts.index("export const CC_SECOND_LEVELS") : ts.index("/** Lowercase, no trailing dot")]
    assert set(re.findall(r"'([a-z]+)'", cc)) == set(g._CC_SECOND_LEVELS)
