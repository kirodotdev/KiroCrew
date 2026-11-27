"""Every reader of a link-preview host list stays linear in its length.

``contributes.linkPreviews[].hosts`` has no cap, so a membership or de-duplication
check against a list, or a set rebuilt for every entry, costs about n²/2 operations
per call. These tests count operations instead of timing them: each host is a
``_Counted`` string whose ``__eq__`` and ``__hash__`` bump a counter. A set lookup
hashes and compares a host about once; a list scan or a per-entry set rebuild touches
every other entry, which at N hosts is about N²/2 calls and fails the bound. Equal
hosts are always distinct objects, so CPython's identity shortcut cannot hide a list
scan.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps import app_hosts, link_preview_grants, manager_records

N = 2_000
#: A linear reader compares each host a small constant number of times; a quadratic
#: one compares about N²/2 = 2,000,000 times at N = 2,000.
LINEAR_BOUND = 10 * N


class _Counted(str):
    """A ``str`` that counts every ``==`` it takes part in."""

    calls = 0

    def __eq__(self, other: object) -> bool:
        _Counted.calls += 1
        return str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        _Counted.calls += 1
        return str.__ne__(self, other)

    def __hash__(self) -> int:
        # Counted too: rebuilding a set once per entry hashes every host again.
        _Counted.calls += 1
        return str.__hash__(self)


def _hosts(n: int = N) -> list[str]:
    """*n* distinct hosts, each a fresh ``_Counted`` object."""
    return [_Counted(f"h{i}.example.com") for i in range(n)]


@pytest.fixture(autouse=True)
def _reset_count() -> None:
    _Counted.calls = 0


@pytest.fixture
def counted_normalize(monkeypatch: pytest.MonkeyPatch) -> None:
    """``normalize_host`` hands back a fresh ``_Counted`` copy of the real result."""
    real = app_hosts.normalize_host

    def normalize(raw: str) -> str | None:
        n = real(raw)
        return None if n is None else _Counted(n)

    monkeypatch.setattr(app_hosts, "normalize_host", normalize)


@pytest.fixture
def every_other_host_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The approved set is every other declared host, as fresh objects."""

    def staged(name: str, family: str, read: Any) -> list[str]:
        return [_Counted(str(h)) for h in read()[::2]]

    monkeypatch.setattr(manager_records, "staged_app_grants", staged)
    monkeypatch.setattr("kiro_crew.apps.manager.staged_app_grants", staged)


def test_normalized_hosts_dedupes_linearly(counted_normalize: None) -> None:
    raw = [f"h{i}.example.com" for i in range(N)] * 2
    out = app_hosts.normalized_hosts(raw)
    assert out == [f"h{i}.example.com" for i in range(N)]
    assert _Counted.calls < LINEAR_BOUND, _Counted.calls


def test_link_preview_staged_hosts_split_linearly(
    counted_normalize: None, every_other_host_approved: None
) -> None:
    manifest = {
        "name": "cards-app",
        "contributes": {
            "linkPreviews": [
                {"id": "cards", "entry": "dist/lp.mjs", "hosts": [str(h) for h in _hosts()]}
            ]
        },
    }
    _Counted.calls = 0
    row = link_preview_grants.link_preview_grant_row("cards-app", manifest)["linkPreviewGrant"]
    assert row["stagedHosts"] == [f"h{i}.example.com" for i in range(1, N, 2)]
    assert _Counted.calls < LINEAR_BOUND, _Counted.calls


def test_held_back_entries_are_found_linearly() -> None:
    declared = _hosts()
    approved = {"linkPreviewHosts": [_Counted(str(h)) for h in declared[::2]]}
    held = manager_records._held_back_entries(approved, {"linkPreviewHosts": declared})
    assert held["linkPreviewHosts"] == [f"h{i}.example.com" for i in range(1, N, 2)]
    assert _Counted.calls < LINEAR_BOUND, _Counted.calls


def test_an_update_keeps_its_approved_hosts_linearly() -> None:
    old = _hosts()
    new = _hosts(N + N // 2)
    kept = manager_records._approved_grants_after_manifest_change(
        existing_approved={"linkPreviewHosts": [_Counted(str(h)) for h in old]},
        old_permissions={"linkPreviewHosts": old},
        new_permissions={"linkPreviewHosts": new},
    )
    assert kept["linkPreviewHosts"] == [f"h{i}.example.com" for i in range(N)]
    assert _Counted.calls < LINEAR_BOUND, _Counted.calls
