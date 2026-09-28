"""The ``mixed_internal_api_paths`` contributor seam.

An edition mounts its routes through ``DashboardContributor.contribute_routes``,
so the core cannot name them in a module-level frozenset. Without this seam an
edition's own MCP tool authenticating with the loopback ``X-Internal-Secret``
handshake is not recognized as internal at all: token_auth ignores the secret,
falls through to cookie auth, and the tool answers ``Token required``.

What is pinned here is the composition, not the transport: that the contribution
is UNIONED (never replaces), that a contribution which would soften a core STRICT
path to mixed is DROPPED, and that every degraded contributor shape yields the
core set unchanged rather than a widened one.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    _STRICT_INTERNAL_API_PATHS,
    _mixed_internal_api_paths,
)
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.platform import (
    PlatformCompositionError,
    build_default_context,
    set_context,
)
from kiro_crew.platform.defaults import DefaultDashboardContributor

_EDITION_PATH = "/api/some-edition-app/agent-surface"


class _Contributor:
    """A dashboard contributor returning a fixed set."""

    def __init__(self, paths) -> None:
        self._paths = paths

    def mixed_internal_api_paths(self):
        return self._paths


class _Raising:
    def mixed_internal_api_paths(self):
        raise RuntimeError("boom")


class _RaisingAttributeError:
    """Implemented, but its body raises AttributeError — an ordinary bug shape.

    Must NOT be mistaken for a contributor that predates the seam.
    """

    def mixed_internal_api_paths(self):
        missing = None
        return missing.paths  # type: ignore[union-attr]


class _Legacy:
    """A contributor predating the seam — it simply has no such method."""


@pytest.fixture()
def cfg(tmp_path) -> KiroCrewConfig:
    return KiroCrewConfig()


def _with(contributor, cfg: KiroCrewConfig) -> None:
    base = build_default_context(cfg)
    set_context(dataclasses.replace(base, dashboard=contributor))


def test_the_public_default_changes_nothing(cfg) -> None:
    # The whole point of shipping this seam with an empty Default: a public build
    # must admit exactly the set it admitted before.
    _with(DefaultDashboardContributor(), cfg)
    assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS


def test_a_contributed_path_is_admitted(cfg) -> None:
    # The positive case. Without it, a regression that always returns the core set
    # would satisfy every fail-closed assertion below while leaving the seam inert
    # and the edition's tool answering Token required.
    _with(_Contributor(frozenset({_EDITION_PATH})), cfg)
    composed = _mixed_internal_api_paths()
    assert internal_path_matches(_EDITION_PATH, composed)


def test_the_core_set_is_never_dropped(cfg) -> None:
    _with(_Contributor(frozenset({_EDITION_PATH})), cfg)
    assert _MIXED_INTERNAL_API_PATHS <= _mixed_internal_api_paths()


def test_an_admitted_contribution_is_recorded(cfg, caplog) -> None:
    # The symmetric half of the drop audit. A dropped contribution is invisible to
    # the EDITION; an honoured one is invisible to the OPERATOR. Without this,
    # nothing in the log or in SEL distinguishes a deployment whose auth surface
    # an edition widened from a stock one.
    _with(_Contributor(frozenset({_EDITION_PATH})), cfg)
    with caplog.at_level("INFO", logger="kiro_crew.dashboard.server"):
        composed = _mixed_internal_api_paths()
    assert internal_path_matches(_EDITION_PATH, composed)
    recorded = [r for r in caplog.records if _EDITION_PATH in r.getMessage()]
    assert recorded, "an admitted contribution must leave a trace naming the path"


def test_a_stock_build_records_nothing(cfg, caplog) -> None:
    # A public build contributes an empty set, so a line there would say nothing
    # and would appear on every gateway start.
    _with(DefaultDashboardContributor(), cfg)
    with caplog.at_level("INFO", logger="kiro_crew.dashboard.server"):
        assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS
    assert not [
        r for r in caplog.records if "internal-reachable" in r.getMessage()
    ], "a stock start must stay silent"


def test_a_contribution_cannot_soften_a_core_strict_path(cfg) -> None:
    # Strict and mixed differ OFF-loopback: strict hard-denies, mixed accepts a
    # validated cookie. So admitting a strict path into the mixed set would make a
    # route the core keeps loopback-only reachable from a forwarded browser.
    strict = sorted(_STRICT_INTERNAL_API_PATHS)[0]
    _with(_Contributor(frozenset({strict, _EDITION_PATH})), cfg)
    composed = _mixed_internal_api_paths()
    assert strict not in composed
    # ... and the legitimate sibling in the same contribution still lands, so the
    # refusal drops the offending entry rather than the whole contribution.
    assert internal_path_matches(_EDITION_PATH, composed)


def test_a_child_of_a_core_strict_path_is_also_dropped(cfg) -> None:
    # ``internal_path_matches`` is prefix-matching, so a child entry reaches the
    # same handler tree. Comparing by equality alone would let it through.
    strict = sorted(_STRICT_INTERNAL_API_PATHS)[0]
    _with(_Contributor(frozenset({strict + "/child"})), cfg)
    assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS


def test_an_ancestor_of_a_core_strict_path_is_also_dropped(cfg) -> None:
    # The direction a one-directional check misses, and the one that actually
    # reclassifies the route. Contributing ``/api/browser`` against the strict
    # ``/api/browser/command`` makes a request for the strict path match BOTH
    # sets, and token_auth's off-loopback arm tests ``_matches_mixed`` FIRST
    # (``elif _matches_internal: if _matches_mixed:``) — so the hard-deny becomes
    # cookie acceptance. The entry itself is not a strict path, which is exactly
    # why equality and child-matching both pass it.
    strict = sorted(_STRICT_INTERNAL_API_PATHS)[0]
    ancestor = strict.rsplit("/", 1)[0]
    assert ancestor and ancestor != strict, "need a strict entry with a parent segment"
    assert ancestor not in _STRICT_INTERNAL_API_PATHS, "the ancestor must not itself be strict"
    _with(_Contributor(frozenset({ancestor})), cfg)
    composed = _mixed_internal_api_paths()
    assert ancestor not in composed
    # And the strict route is still NOT reachable through the composed mixed set,
    # which is the property the entry would have broken.
    assert not internal_path_matches(strict, composed)


def test_an_unrelated_sibling_prefix_is_still_admitted(cfg) -> None:
    # The guard must not degrade into "drop anything sharing a prefix string".
    # A path that neither contains nor is contained by a strict entry is fine.
    _with(_Contributor(frozenset({_EDITION_PATH})), cfg)
    assert internal_path_matches(_EDITION_PATH, _mixed_internal_api_paths())


def test_a_raising_contributor_contributes_nothing(cfg) -> None:
    _with(_Raising(), cfg)
    assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS


def test_a_contributor_without_the_method_contributes_nothing(cfg, caplog) -> None:
    # The set is the easy half, and the generic handler below would produce it
    # anyway. What the dedicated ``AttributeError`` branch is FOR is the second
    # assertion: a contributor predating the seam is not a fault, so it must not
    # log a warning on every gateway start. Without that distinction the branch is
    # dead code.
    _with(_Legacy(), cfg)
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.server"):
        assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_a_raising_contributor_does_warn(cfg, caplog) -> None:
    # The other side of the same distinction: a contributor that BREAKS is a fault
    # and has to be visible, or the edition's tool fails with no trace of why.
    _with(_Raising(), cfg)
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.server"):
        assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS
    assert [r for r in caplog.records if r.levelname == "WARNING"]


def test_an_implemented_contributor_raising_attributeerror_still_warns(cfg, caplog) -> None:
    # The reason lookup is separated from invocation. Guarding the CALL with
    # ``except AttributeError`` also swallows an AttributeError raised inside an
    # implemented contributor — an ordinary bug shape (``self._cfg.x`` where
    # ``_cfg`` is None) — so a genuinely broken edition would take the silent
    # "predates the seam" path and contribute nothing with no warning at all.
    _with(_RaisingAttributeError(), cfg)
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.server"):
        assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS
    assert [
        r for r in caplog.records if r.levelname == "WARNING"
    ], "an implemented-but-broken contributor must not read as a legacy one"


def test_a_non_iterable_contribution_contributes_nothing(cfg) -> None:
    _with(_Contributor(object()), cfg)
    assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS


def test_a_generator_raising_mid_iteration_does_not_abort_the_gateway(cfg, caplog) -> None:
    # The comprehension is materialized inside the fail-closed thunk. Left outside,
    # a generator that raises part-way through iteration escapes middleware
    # construction — and this helper runs while the middleware is being built, so
    # the gateway would never bind at all rather than degrade.
    def _half_broken():
        yield _EDITION_PATH
        raise RuntimeError("iterator died")

    _with(_Contributor(_half_broken()), cfg)
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.server"):
        assert _mixed_internal_api_paths() == _MIXED_INTERNAL_API_PATHS
    assert [r for r in caplog.records if r.levelname == "WARNING"]


def test_a_composition_error_is_re_raised_not_degraded(cfg) -> None:
    # The CPP fail-closed invariant, and the reason this goes through
    # ``safe_context_call`` rather than a hand-written ``except Exception``: a host
    # that could not compose its companion MUST abort. Degrading it to the core set
    # would answer a mis-composed edition with a quietly narrower auth surface
    # instead of a refusal — the one failure the centralized helper exists to stop a
    # call site from swallowing.
    class _MisComposed:
        def mixed_internal_api_paths(self):
            raise PlatformCompositionError("companion did not compose")

    _with(_MisComposed(), cfg)
    with pytest.raises(PlatformCompositionError):
        _mixed_internal_api_paths()


@pytest.mark.parametrize("junk", ["relative/path", "", 7, None])
def test_entries_the_core_cannot_check_are_dropped(cfg, junk) -> None:
    # A non-string, or a string that is not a path, cannot be matched against
    # anything the core knows. Admitting it would widen the set on a value the
    # core never validated.
    _with(_Contributor({junk, _EDITION_PATH}), cfg)
    composed = _mixed_internal_api_paths()
    assert junk not in composed
    assert internal_path_matches(_EDITION_PATH, composed)


def test_both_middleware_sites_compose_through_the_helper() -> None:
    """The dashboard chain and the headless ``--slack-only`` one cannot drift.

    Drift is an auth bug: the headless server would gate a different set of routes
    than the dashboard does. Asserted over the source because the two call sites
    live in separate start functions that a test cannot run end to end cheaply.
    """
    import inspect

    from kiro_crew.dashboard import server

    src = inspect.getsource(server)
    assert src.count("mixed_internal_paths=_mixed_internal_api_paths()") == 2
    assert "mixed_internal_paths=_MIXED_INTERNAL_API_PATHS" not in src
