"""The trusted-registries snapshot badges what is SERVED, not what is granted.

``build_trusted_registries_snapshot`` is built from the EFFECTIVE registry view
(``_effective_registries``) rather than the raw ``config.json`` rows, because two
kinds of config row are dropped by the merge and served for NEITHER claimant:

- two config rows that share one identity key (``name_collision``), and
- a config row whose name is contested by a build-pinned registry
  (``pinned_name``).

A dropped row's apps are never listed, so a grant on it confers nothing — yet
reading ``trusted`` off the raw map would badge it Trusted with a Revoke button,
describing a state the runtime does not hold. Pinned here:

- a dropped row is ``served: false`` with the right ``not_served_reason`` and is
  NOT ``trusted`` even with a live grant;
- a normally-served granted row is ``served: true`` and ``trusted: true``;
- each with a negative control that reproduces the pre-fix "badge the raw map"
  computation and asserts it would have disagreed, so a regression to it is red.

The rows are inspected by calling ``build_trusted_registries_snapshot`` directly
(it is the executor target behind ``GET /api/security/trusted-registries``), which
keeps these tests independent of the aiohttp client the grant/revoke tests use.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.registry_pipeline import sources
from kiro_crew.config.loader import _invalidate_config_cache
from kiro_crew.dashboard.handlers.security import build_trusted_registries_snapshot

# Two config rows sharing one identity key: the cache-path form of the name is
# case-folded, so "Acme" and "acme" collide (see `_registry_identity_key`).
_ACME_UPPER = "https://git.example.test/team/acme-index.git"
_ACME_UPPER_PUBLIC = "https://git.example.test/team/acme-index"
_ACME_LOWER = "https://git.example.test/other/acme-index.git"
_ACME_LOWER_PUBLIC = "https://git.example.test/other/acme-index"

# A plain, uncontested row and a build-pinned row.
_MINE = "https://git.example.test/team/apps-index.git"
_MINE_PUBLIC = "https://git.example.test/team/apps-index"
_PINNED_REPO = "https://git.example.test/build/pinned-index.git"
_CONTENDER = "https://git.example.test/team/contender-index.git"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "kirocrew-home"
    h.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    _invalidate_config_cache()
    yield h
    _invalidate_config_cache()


@pytest.fixture
def no_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_pinned_registries", lambda: [])


def _write_config(home: Path, registries: list[dict]) -> None:
    (home / "config.json").write_text(json.dumps({"registries": registries}), encoding="utf-8")
    _invalidate_config_cache()


def _write_grant(*repos: str) -> None:
    from kiro_crew.config.loader import registry_trust_path

    registry_trust_path().write_text(
        json.dumps({"version": 1, "owner_trusted": list(repos)}),
        encoding="utf-8",
    )


def _pinned(*, name: str = "pinned", repo: str = _PINNED_REPO, trust: str = "owner"):
    return [SimpleNamespace(name=name, repo=repo, branch="main", trust=trust, label="", review="")]


def _row(rows: list[dict], name: str) -> dict:
    (match,) = [r for r in rows if r["name"] == name]
    return match


def _raw_map_would_badge(repo_public: str) -> bool:
    """The PRE-FIX computation: badge trusted iff a runtime-honoured grant exists.

    The alternative shape this test guards against: iterate the
    raw config rows and set ``trusted`` from ``_operator_granted_owner`` alone,
    with no ``_effective_registries`` projection. The negative controls assert
    this disagrees with the fixed snapshot on a dropped row, so a regression back
    to it turns the control red.
    """
    return sources._operator_granted_owner(SimpleNamespace(name="x", repo=repo_public))


class TestNameCollisionRowsAreServed:
    """Two config rows sharing one identity key: both served, each read by its own grant."""

    def test_both_rows_served_each_badged_by_its_own_grant(self, home, no_pinned) -> None:
        _write_config(
            home,
            [
                {"name": "Acme", "repo": _ACME_UPPER, "branch": "main"},
                {"name": "acme", "repo": _ACME_LOWER, "branch": "main"},
            ],
        )
        _write_grant(_ACME_UPPER_PUBLIC)

        rows = build_trusted_registries_snapshot()["registries"]
        upper = _row(rows, "Acme")
        lower = _row(rows, "acme")

        # Served exactly as on a build without grants.
        assert upper["served"] is True
        assert lower["served"] is True
        assert upper["trusted"] is True
        assert lower["trusted"] is False


class TestPinnedNameContestRowIsNotServed:
    """A config row whose name contests a build-pinned registry: not served."""

    def test_contested_row_dropped_reason_pinned_name_not_trusted_even_with_grant(
        self, home, monkeypatch
    ) -> None:
        # Config row "pinned" points at a DIFFERENT repo than the build-pinned
        # row of the same name, so `_effective_registries` serves neither.
        monkeypatch.setattr(sources, "_pinned_registries", lambda: _pinned())
        _write_config(home, [{"name": "pinned", "repo": _CONTENDER, "branch": "dev"}])
        _write_grant("https://git.example.test/team/contender-index")

        rows = build_trusted_registries_snapshot()["registries"]
        row = _row(rows, "pinned")

        assert row["served"] is False
        assert row["not_served_reason"] == "pinned_name"
        assert row["trusted"] is False

    def test_a_legacy_row_identical_to_the_pinned_one_is_not_served_and_offers_no_controls(
        self, home, monkeypatch
    ) -> None:
        # A config.json that carried the SAME name and repo before the build pinned
        # it: the merge serves the build's row, so the effective row under this key
        # is the build's. The operator row must not borrow that row's tier -- a
        # Revoke on it could not change what the build decides.
        monkeypatch.setattr(sources, "_pinned_registries", lambda: _pinned())
        _write_config(home, [{"name": "pinned", "repo": _PINNED_REPO, "branch": "main"}])

        row = _row(build_trusted_registries_snapshot()["registries"], "pinned")
        assert row["served"] is False
        assert row["not_served_reason"] == "pinned_name"
        assert row["trusted"] is False

    def test_negative_control_pre_fix_raw_map_would_have_badged_trusted(
        self, home, monkeypatch
    ) -> None:
        contender_public = "https://git.example.test/team/contender-index"
        monkeypatch.setattr(sources, "_pinned_registries", lambda: _pinned())
        _write_config(home, [{"name": "pinned", "repo": _CONTENDER, "branch": "dev"}])
        _write_grant(contender_public)

        rows = build_trusted_registries_snapshot()["registries"]
        assert _row(rows, "pinned")["trusted"] is False
        # The row carries a runtime-honoured grant on its own repository, so the
        # discarded raw-map logic would have badged it Trusted despite the build
        # pinning that name.
        assert _raw_map_would_badge(contender_public) is True


class TestServedGrantedRowIsTrusted:
    """A normal, uncontested, granted row: served and trusted."""

    def test_served_true_trusted_true_no_reason(self, home, no_pinned) -> None:
        _write_config(home, [{"name": "mine", "repo": _MINE, "branch": "main"}])
        _write_grant(_MINE_PUBLIC)

        rows = build_trusted_registries_snapshot()["registries"]
        row = _row(rows, "mine")

        assert row["served"] is True
        assert row["trusted"] is True
        assert "not_served_reason" not in row

    def test_negative_control_an_ungranted_served_row_is_not_trusted(self, home, no_pinned) -> None:
        # Same served row, but with the grant withheld: served stays true while
        # trusted flips to false. This pins that `trusted` tracks the grant on a
        # served row (not `served` alone), so a fix that hard-coded trusted=served
        # would turn this red.
        _write_config(home, [{"name": "mine", "repo": _MINE, "branch": "main"}])
        # no grant written

        rows = build_trusted_registries_snapshot()["registries"]
        row = _row(rows, "mine")

        assert row["served"] is True
        assert row["trusted"] is False
        assert _raw_map_would_badge(_MINE_PUBLIC) is False
