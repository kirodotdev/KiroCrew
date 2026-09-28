"""Tests for the model picker's entitlement narrowing (/api/models, kiro path).

``kiro chat --list-models`` is a CATALOG, not an entitlement: it returns the same
rows whatever the account's plan. So after a downgrade the picker kept offering —
and the composer kept displaying as selected — a premium model no turn could run,
while the session itself quietly ran on the backend default.

The tier-aware signal is the live session's ``session/new`` ``availableModels``
list, the same one ``model_is_unusable`` pre-flights against. These tests pin that
it narrows the catalog when known, and that every unknowable case FAILS OPEN
(returns the full catalog) rather than emptying the picker.

The read path additionally revalidates that snapshot before it narrows: an
unconfirmed startup-race snapshot would silently hide entitled models, and the
picker has no explicit-pick refusal to trigger the refresh-before-refuse heal.
The narrowing assertions below give each fake provider a probe that AGREES with
its snapshot (the common case), so they pin the narrowing itself; the read-path
revalidation is exercised by its own section at the end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.client import (
    catalog_row_would_drop,
    model_is_unusable,
    resolve_pin_spelling,
)
from kiro_crew.acp.session_handle import EntitlementRevalidating
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider

CATALOG = [
    {"model_name": "auto", "description": "Models chosen by task"},
    {"model_name": "claude-opus-5", "description": "Opus 5"},
    {"model_name": "claude-sonnet-5", "description": "Sonnet 5"},
    {"model_name": "claude-opus-4.8", "description": "Opus 4.8"},
]


def _stub_wrap_argv(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
    """Pass-through stand-in for ``sandbox.wrap_argv`` (twin of the one in
    ``test_api_models_retry.py``): absorbs the real signature's keyword arguments
    so an added one cannot masquerade as a degraded response."""
    del kwargs
    return argv, None


def _provider(
    models: object,
    *,
    getter: bool = True,
    raises: bool = False,
    probe: object = None,
    probe_raises: bool = False,
    probe_revalidating: bool = False,
) -> MagicMock:
    """A fake kiro session provider.

    ``models`` is the current session-init snapshot the picker reads. ``probe``
    is what the read-path revalidation's probe would return: by default the
    provider's revalidation is a NO-OP that returns the snapshot unchanged (probe
    agrees), so the narrowing tests are unaffected by the read-path plumbing.
    Pass ``probe`` to simulate a probe that disagrees (a fuller entitlement),
    ``probe_raises`` to simulate a probe failure (fail open), or
    ``probe_revalidating`` to simulate the deadline-timeout signal.
    """
    provider = MagicMock()
    provider._refresher_calls = 0
    if not getter:
        # A provider type with no available_models attribute at all (the
        # claude-code placeholder before session init).
        del provider.available_models
        del provider.maybe_refresh_available_models
        return provider
    if raises:
        provider.available_models = MagicMock(side_effect=RuntimeError("boom"))
    else:
        provider.available_models = MagicMock(return_value=models)

    async def _maybe_refresh(catalog_ids: list[str]) -> object:
        provider._refresher_calls += 1
        if probe_revalidating:
            raise EntitlementRevalidating
        if probe_raises:
            raise RuntimeError("probe boom")
        if probe is not None:
            # A disagreeing probe replaces the snapshot the getter now returns,
            # exactly as the real handle mutates ``_available_models`` in place.
            provider.available_models = MagicMock(return_value=probe)
            return probe
        return models

    provider.maybe_refresh_available_models = _maybe_refresh
    return provider


def _claude_provider(models: object) -> MagicMock:
    """A live claude session: advertises prefixed ids in the claude_code namespace."""
    provider = _provider(models)
    provider.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
    return provider


def _request(*providers: MagicMock) -> MagicMock:
    state = MagicMock()
    state.sessions.active_providers = MagicMock(return_value=list(providers))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _names(rows: list[dict]) -> list[str]:
    return [r["model_name"] for r in rows]


@pytest.mark.asyncio
async def test_advertised_narrows_the_catalog():
    # The free tier advertises auto + sonnet only: opus rows must not survive.
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]))
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_a_spelling_the_wire_rejects_is_not_offered():
    # The catalog spells versions dotted (`claude-opus-4.8`); a backend may
    # advertise them dashed. `set_model` pre-flights the id exactly as given, and
    # the pin validator in handlers/core applies the same raw predicate, so the
    # dotted row must not be offered as-is: the picker would offer it and the
    # spawn would withhold it. The keep/drop applies the wire's own fold
    # (`resolve_pin_spelling`), and that fold refuses these two in particular:
    # the registry lists dashed `claude-opus-4-8` (200K) and dotted
    # `claude-opus-4.8` (1M) as different models, so the row is not rewritten
    # onto its neighbour -- it drops, like sonnet, which is advertised under no
    # spelling at all.
    request = _request(
        _provider(
            [{"modelId": "auto"}, {"modelId": "claude-opus-4-8"}, {"modelId": "claude-opus-5"}]
        )
    )
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == ["auto", "claude-opus-5"]


@pytest.mark.asyncio
async def test_a_spelling_variant_of_the_same_model_is_offered_as_advertised():
    # The fold's positive case: the registry does not know `claude-zeta-9`, so
    # a prefixed catalog spelling and the bare advertised id are one model on
    # spelling alone, and the row is offered under the id the wire accepts.
    catalog = CATALOG + [{"model_name": "us.anthropic.claude-zeta-9", "description": "Zeta"}]
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "claude-zeta-9"}]))
    kept = await agents._entitled_kiro_models(request, catalog)
    assert _names(kept) == ["auto", "claude-zeta-9"]
    assert kept[1]["description"] == "Zeta"


@pytest.mark.asyncio
async def test_picker_and_wire_never_disagree_row_by_row():
    # The invariant behind the above, asserted directly against the shared
    # predicates rather than a hardcoded expectation, in both directions:
    # every non-auto row the picker offers must pass the RAW predicate (the
    # agent/crew pin validator and set_model's pre-flight compare the picked
    # value literally, so a kept row's spelling must survive them verbatim),
    # and every dropped catalog row must be one the wire withholds under BOTH
    # spellings (raw miss AND fold miss).
    advertised = ["auto", "claude-opus-4-8", "claude-sonnet-5", "z-ai/glm-5.3-flash"]
    catalog = CATALOG + [{"model_name": "openrouter::z-ai/glm-5.3-flash", "description": "GLM"}]
    request = _request(_provider([{"modelId": m} for m in advertised]))
    kept = set(_names(await agents._entitled_kiro_models(request, catalog)))
    for name in kept:
        if name == "auto":
            continue
        assert not model_is_unusable(
            name, advertised
        ), f"{name}: offered by the picker but rejected by the raw wire predicate"
    for row in catalog:
        name = row["model_name"]
        if name == "auto" or name in kept:
            continue
        resolved = resolve_pin_spelling(name, advertised)
        if resolved:
            # Dropped as spelled, but the MODEL is offered — under the
            # advertised spelling the fold answers with.
            assert resolved in kept, f"{name}: resolvable but its advertised spelling not offered"
        else:
            assert model_is_unusable(
                name, advertised
            ), f"{name}: dropped by the picker but usable on the wire"
    # The fold-recovered model is offered — under its advertised spelling.
    assert "z-ai/glm-5.3-flash" in kept
    assert "openrouter::z-ai/glm-5.3-flash" not in kept


@pytest.mark.asyncio
async def test_auto_survives_a_backend_that_does_not_advertise_it():
    # `auto` means "inherit whatever the session resolved", so it stays selectable
    # even when the backend never names it — dropping it would remove the only
    # always-valid choice.
    request = _request(_provider([{"modelId": "claude-sonnet-5"}]))
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_auto_only_entitlement_narrows_to_auto():
    # The reported bug's own end state: a tier that can run nothing but Auto. The
    # advertised set naming `auto` proves it is comparable with the catalog, so
    # this is a real (maximally restrictive) entitlement — treating it as a
    # namespace mismatch would hand back the whole premium catalog, which is the
    # exact symptom this narrowing exists to remove. Here the probe AGREES with
    # the auto-only snapshot (a genuine downgrade), so it must stand.
    request = _request(_provider([{"modelId": "auto"}]))
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == ["auto"]


@pytest.mark.asyncio
async def test_sentinel_only_survivor_fails_open():
    # A catalog sharing no namespace with the advertised ids is a mismatch, not an
    # account entitled to nothing: nothing lines up, `auto` included. Showing the
    # whole catalog beats emptying the picker.
    request = _request(_provider([{"modelId": "openai.gpt-9-nova[high]"}]))
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == _names(CATALOG)


@pytest.mark.asyncio
async def test_a_claude_session_never_narrows_the_kiro_picker():
    # claude's ids name the same models under a prefixed spelling, and the wire
    # fold (`resolve_pin_spelling`) folds them onto the catalog's bare ids. That
    # fold is right on a kiro session; on a claude session it would rewrite the
    # kiro picker into claude's spelling and narrow it to claude's entitlements.
    # The namespace gate keeps a claude list out of this narrowing entirely.
    request = _request(
        _claude_provider(
            [
                {"modelId": "global.anthropic.claude-opus-4-8[1m]"},
                {"modelId": "global.anthropic.claude-sonnet-4-6[1m]"},
            ]
        )
    )
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_a_claude_session_is_skipped_in_favour_of_an_older_kiro_one():
    # Newest-first is newest-in-namespace-first: the claude session started last,
    # but the older kiro session is the one whose list narrows kiro's catalog.
    kiro = _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    claude = _claude_provider([{"modelId": "global.anthropic.claude-opus-4-8[1m]"}])
    request = _request(kiro, claude)  # oldest first, as the dict yields them
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_newest_session_wins_over_a_stale_one():
    # The reported failure mode after a plan change: a session started BEFORE the
    # downgrade is still live and still holds its pre-downgrade advertised list,
    # while a newer session carries the narrowed one. active_providers() is
    # creation-ordered, so reading the FIRST match would narrow the catalog to the
    # stale entitlements and keep offering the premium model.
    stale = _provider([{"modelId": "auto"}, {"modelId": "claude-opus-5"}])
    fresh = _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    request = _request(stale, fresh)  # oldest first, as the dict yields them
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_newest_session_with_no_list_falls_back_to_an_older_one():
    # Newest-first must not mean newest-only: a just-spawned session that has not
    # captured a list yet knows nothing, and an older session's list is still
    # better evidence than giving up and showing the whole catalog.
    fresh_but_silent = _provider([])
    older = _provider([{"modelId": "claude-sonnet-5"}])
    request = _request(older, fresh_but_silent)
    # `auto` is always kept — it means "inherit", so entitlement never removes it.
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_no_live_session_leaves_the_catalog_alone():
    # Nothing has initialized yet: entitlement is unknown, not "nothing".
    request = _request()
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_missing_state_leaves_the_catalog_alone():
    request = MagicMock()
    request.app = {}
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_backend_that_advertises_nothing_leaves_the_catalog_alone():
    request = _request(_provider([]))
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_namespaced_row_is_rewritten_to_the_advertised_spelling():
    # A catalog row can carry a `<namespace>::<bare-id>` qualifier from the
    # catalog that named it, while the live session advertises the BARE id. The
    # picker must offer the model — but under the ADVERTISED spelling, because
    # the selection sinks (the agent/crew pin validator and set_model's
    # pre-flight) compare the picked value literally and would refuse the
    # qualified one. The rest of the row (description) is preserved.
    catalog = CATALOG + [{"model_name": "openrouter::z-ai/glm-5.3-flash", "description": "GLM"}]
    request = _request(
        _provider(
            [
                {"modelId": "auto"},
                {"modelId": "claude-sonnet-5"},
                {"modelId": "z-ai/glm-5.3-flash"},
            ]
        )
    )
    rows = await agents._entitled_kiro_models(request, catalog)
    assert _names(rows) == ["auto", "claude-sonnet-5", "z-ai/glm-5.3-flash"]
    assert rows[-1]["description"] == "GLM"


@pytest.mark.asyncio
async def test_namespaced_row_absent_under_both_spellings_still_drops():
    # The fold clears false drops only: a model served under neither spelling
    # stays hidden, exactly as the wire would withhold it.
    catalog = CATALOG + [{"model_name": "openrouter::not-served", "description": "x"}]
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}]))
    assert _names(await agents._entitled_kiro_models(request, catalog)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_namespaced_row_duplicating_a_literal_row_is_dropped():
    # When the catalog lists BOTH the bare id and a qualified variant of the
    # same model, rewriting the qualified row would duplicate the bare one —
    # so it drops instead.
    catalog = CATALOG + [
        {"model_name": "z-ai/glm-5.3-flash", "description": "bare"},
        {"model_name": "openrouter::z-ai/glm-5.3-flash", "description": "qualified"},
    ]
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "z-ai/glm-5.3-flash"}]))
    rows = await agents._entitled_kiro_models(request, catalog)
    assert _names(rows) == ["auto", "z-ai/glm-5.3-flash"]
    assert rows[-1]["description"] == "bare"


@pytest.mark.asyncio
async def test_two_qualifiers_over_one_bare_id_produce_one_row():
    # Two catalog rows whose qualifiers peel to the same advertised id must not
    # become two visually distinct rows for one model.
    catalog = CATALOG + [
        {"model_name": "openrouter::z-ai/glm-5.3-flash", "description": "first"},
        {"model_name": "azure::z-ai/glm-5.3-flash", "description": "second"},
    ]
    request = _request(_provider([{"modelId": "auto"}, {"modelId": "z-ai/glm-5.3-flash"}]))
    rows = await agents._entitled_kiro_models(request, catalog)
    assert _names(rows) == ["auto", "z-ai/glm-5.3-flash"]
    assert rows[-1]["description"] == "first"


@pytest.mark.asyncio
async def test_fold_kept_row_counts_as_comparability_evidence():
    # A namespaced row resolving against a bare advertised set proves the two
    # vocabularies line up once the qualifier is peeled — a real narrowing, not
    # the sentinel-only namespace mismatch that fails open to the full catalog.
    catalog = CATALOG + [{"model_name": "openrouter::z-ai/glm-5.3-flash", "description": "GLM"}]
    request = _request(_provider([{"modelId": "z-ai/glm-5.3-flash"}]))
    assert _names(await agents._entitled_kiro_models(request, catalog)) == [
        "auto",
        "z-ai/glm-5.3-flash",
    ]


@pytest.mark.asyncio
async def test_provider_without_getter_is_skipped_not_fatal():
    # First provider has no getter; the second one's list still applies.
    request = _request(
        _provider(None, getter=False),
        _provider([{"modelId": "claude-sonnet-5"}]),
    )
    # `auto` is always kept — it means "inherit", so entitlement never removes it.
    assert _names(await agents._entitled_kiro_models(request, CATALOG)) == [
        "auto",
        "claude-sonnet-5",
    ]


@pytest.mark.asyncio
async def test_getter_raising_is_skipped_not_fatal():
    request = _request(_provider(None, raises=True))
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_disjoint_advertised_set_fails_open():
    # An advertised set that intersects the catalog under no spelling is a
    # mismatch, not an entitlement. Filtering there would empty the picker, so
    # the catalog is returned untouched.
    request = _request(_provider([{"modelId": "openrouter::z-ai/glm-5.3-flash"}]))
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


@pytest.mark.asyncio
async def test_malformed_advertised_entries_are_ignored():
    # advertised_model_ids tolerates junk; a list that yields no usable id is
    # the same as "advertised nothing".
    request = _request(_provider(["not-a-dict", {"no_model_id": 1}, {"modelId": ""}]))
    assert await agents._entitled_kiro_models(request, CATALOG) == CATALOG


# ── Read-path revalidation (item 1 of the follow-ups) ──
#
# The picker filter has no explicit-pick refusal to trigger the
# refresh-before-refuse heal, so a snapshot that would NARROW the catalog is
# revalidated here before it is trusted. The verdict of what to keep stays with
# ``model_is_unusable``; these pin only the WHEN and the fail-open contract.


@pytest.mark.asyncio
async def test_auto_only_snapshot_revalidates_and_serves_the_probed_list():
    # The strongest staleness signal: an auto-only snapshot against a richer
    # catalog. The probe disagrees (the account really has sonnet + opus-5), and
    # the picker must serve the PROBED list, not the degraded auto-only one.
    provider = _provider(
        [{"modelId": "auto"}],
        probe=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}, {"modelId": "claude-opus-5"}],
    )
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == ["auto", "claude-opus-5", "claude-sonnet-5"]
    assert provider._refresher_calls == 1


@pytest.mark.asyncio
async def test_confirmed_narrow_snapshot_still_revalidates_but_probe_agrees():
    # A legitimately narrow snapshot (auto + sonnet). The read path still asks the
    # provider to revalidate — the provider owns the staleness heuristic and here
    # returns the same list (probe agrees) — and the narrowing stands unchanged.
    provider = _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == ["auto", "claude-sonnet-5"]
    assert provider._refresher_calls == 1


@pytest.mark.asyncio
async def test_probe_failure_is_identical_to_today_fail_open():
    # A probe that raises must never make the picker worse: the narrowing falls
    # back to the current snapshot exactly as before the read-path revalidation
    # existed (auto + sonnet), not the empty picker a propagated error would give.
    provider = _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}], probe_raises=True)
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == ["auto", "claude-sonnet-5"]


@pytest.mark.asyncio
async def test_revalidation_in_flight_propagates_not_swallowed():
    # O1: a deadline-timeout signal (EntitlementRevalidating) must propagate out
    # of _entitled_kiro_models — NOT be swallowed by the fail-open except — so
    # the endpoint can return its degraded response instead of serving the
    # un-revalidated snapshot as a live 200.
    provider = _provider([{"modelId": "auto"}], probe_revalidating=True)
    request = _request(provider)
    with pytest.raises(EntitlementRevalidating):
        await agents._entitled_kiro_models(request, CATALOG)


@pytest.mark.asyncio
async def test_full_snapshot_still_yields_the_whole_catalog():
    # A snapshot that already covers the whole catalog narrows nothing. The
    # read path consults the selected provider's revalidation (the would-narrow
    # cheap-path gate that decides whether to actually probe lives inside the
    # handle, pinned in test_acp_runtime.py), and the catalog is returned intact.
    provider = _provider([{"modelId": m["model_name"]} for m in CATALOG])
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == _names(CATALOG)


# ── The provider the picker actually reads is an AcpProvider (F1) ──
#
# active_providers() returns session.provider, which on the get_or_create/start
# path is an AcpProvider wrapping an inner client — NOT an AcpSessionProvider.
# The read path forwards through the wrapper, so AcpProvider must carry the
# revalidation and hand it to its inner client. These drive the REAL AcpProvider
# through the REAL active_providers() shape — the shape a handle-only test misses.


class _InnerSessionProviderDouble:
    """An AcpSessionProvider-shaped inner client: an LLMProvider (registered
    below, matching the isinstance gate) that advertises a kiro list and can
    revalidate — the shape AcpProvider._client becomes after kiro startup."""

    backend = ACP_BACKEND_KIRO

    def __init__(self, snapshot: list[dict], probe: list[dict] | None) -> None:
        self._snapshot = snapshot
        self._probe = probe
        self.refresh_calls = 0

    def available_models(self) -> list[dict]:
        return self._snapshot

    async def maybe_refresh_available_models(self, catalog_ids: list[str]) -> list[dict]:
        self.refresh_calls += 1
        if self._probe is not None:
            self._snapshot = self._probe
            return self._probe
        return self._snapshot


# Virtual-subclass registration: the started kiro _client is a real
# AcpSessionProvider (an LLMProvider), and AcpProvider gates forwarding on
# isinstance(LLMProvider). Registering the double reproduces that gate without
# implementing the ABC's abstract methods it never exercises.
LLMProvider.register(_InnerSessionProviderDouble)


class _InnerPlaceholderClient:
    """A pre-startup placeholder AcpClient: NOT an LLMProvider (nor is a non-kiro
    direct client). It advertises a list but the wrapper must not forward to it."""

    backend = ACP_BACKEND_KIRO

    def __init__(self, snapshot: list[dict]) -> None:
        self._snapshot = snapshot

    def available_models(self) -> list[dict]:
        return self._snapshot


def _acp_provider_with(client: object) -> AcpProvider:
    # Build the real AcpProvider WITHOUT __init__ (which would construct a real
    # AcpClient) and give it the inner client under test. capabilities is a real
    # property reading client.backend, so the kiro namespace gate resolves.
    provider = object.__new__(AcpProvider)
    provider._client = client  # type: ignore[attr-defined]
    return provider


@pytest.mark.asyncio
async def test_acp_provider_forwards_revalidation_to_its_session_client():
    # The bug F1 caught: the picker reads an AcpProvider, and if it does not
    # forward the refresh, the snapshot is never revalidated. A disagreeing probe
    # on the inner AcpSessionProvider must reach the picker THROUGH the wrapper.
    inner = _InnerSessionProviderDouble(
        [{"modelId": "auto"}],
        probe=[{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}, {"modelId": "claude-opus-5"}],
    )
    provider = _acp_provider_with(inner)
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == ["auto", "claude-opus-5", "claude-sonnet-5"]
    assert inner.refresh_calls == 1


@pytest.mark.asyncio
async def test_acp_provider_without_a_revalidating_client_leaves_ids_untouched():
    # The negative: a pre-startup placeholder AcpClient is NOT an LLMProvider,
    # so the wrapper must not forward to it. AcpProvider fails open — returns the
    # current snapshot, raises nothing — and the picker narrows on it as before.
    inner = _InnerPlaceholderClient([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    provider = _acp_provider_with(inner)
    # Forwarding itself returns the snapshot unchanged and does not raise.
    assert await provider.maybe_refresh_available_models(["auto", "claude-opus-5"]) == [
        {"modelId": "auto"},
        {"modelId": "claude-sonnet-5"},
    ]
    request = _request(provider)
    rows = await agents._entitled_kiro_models(request, CATALOG)
    assert _names(rows) == ["auto", "claude-sonnet-5"]


# ── End-to-end through the handler ──


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    def kill(self):  # noqa: D401 - matches Process API
        pass

    async def communicate(self):
        return self._stdout, b""


def _kiro_request(tmp_path: Path, *providers: MagicMock) -> MagicMock:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    state = MagicMock()
    state.sessions.active_providers = MagicMock(return_value=list(providers))
    request = MagicMock()
    request.app = {"kiro_prerequisite_service": service, "state": state}
    return request


def _run_api_models(request, payload: bytes):
    with (
        patch.object(
            agents.KiroCrewConfig,
            "load",
            return_value=SimpleNamespace(agent=SimpleNamespace(provider="kiro")),
        ),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch("kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv),
        patch("kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
        patch.object(
            agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(stdout=payload)
        ),
    ):
        return asyncio.get_event_loop().run_until_complete(agents.api_models(request))


def test_api_models_returns_only_entitled_rows(tmp_path):
    payload = json.dumps({"models": CATALOG}).encode()
    request = _kiro_request(
        tmp_path, _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}])
    )
    resp = _run_api_models(request, payload)
    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", "claude-sonnet-5"]


def test_api_models_503_while_revalidation_is_in_flight(tmp_path):
    # O1: the read-path probe timed out (still in flight). The endpoint must
    # return the degraded 503 contract, NOT serve the un-revalidated snapshot as
    # a live 200 the frontend would cache with no refetch.
    payload = json.dumps({"models": CATALOG}).encode()
    request = _kiro_request(tmp_path, _provider([{"modelId": "auto"}], probe_revalidating=True))
    resp = _run_api_models(request, payload)
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "model_list_revalidating"


def test_api_models_200_with_corrected_list_once_the_probe_lands(tmp_path):
    # O1: the probe has landed (disagreeing — the account really has opus-5), so
    # the next read is a normal 200 serving the corrected, narrowed list.
    payload = json.dumps({"models": CATALOG}).encode()
    request = _kiro_request(
        tmp_path,
        _provider(
            [{"modelId": "auto"}],
            probe=[{"modelId": "auto"}, {"modelId": "claude-opus-5"}],
        ),
    )
    resp = _run_api_models(request, payload)
    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", "claude-opus-5"]


def test_api_models_200_with_current_snapshot_when_the_probe_fails(tmp_path):
    # O1: a probe that FAILS (not a timeout) still fails open to 200 with the
    # current snapshot — never worse than before the revalidation existed.
    payload = json.dumps({"models": CATALOG}).encode()
    request = _kiro_request(
        tmp_path,
        _provider([{"modelId": "auto"}, {"modelId": "claude-sonnet-5"}], probe_raises=True),
    )
    resp = _run_api_models(request, payload)
    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", "claude-sonnet-5"]


@pytest.mark.parametrize(
    ("model_id", "advertised", "drops"),
    [
        ("auto", ["m1"], False),
        ("default", ["m1"], False),
        ("m1", ["m1"], False),
        ("ns::m1", ["m1"], False),
        ("m2", ["m1"], True),
        ("", ["m1"], True),
        ("m2", [], False),
    ],
)
def test_catalog_row_would_drop_is_the_per_row_picker_verdict(
    model_id: str, advertised: list[str], drops: bool
) -> None:
    # The read-path revalidation spends a probe only on rows this helper drops,
    # so it must match exactly what the endpoint keeps: auto, advertised ids,
    # and ``ns::`` rows that fold onto an advertised bare id survive; an empty
    # advertised set withholds nothing.
    assert catalog_row_would_drop(model_id, advertised) is drops


@pytest.mark.asyncio
async def test_endpoint_keeps_exactly_the_rows_the_shared_verdict_keeps():
    catalog = [
        {"model_name": "auto", "description": ""},
        {"model_name": "claude-sonnet-5", "description": ""},
        {"model_name": "claude-opus-5", "description": ""},
        {"model_name": "ns::claude-opus-4.8", "description": ""},
    ]
    advertised = ["auto", "claude-sonnet-5", "claude-opus-4.8"]
    provider = _provider([{"modelId": mid} for mid in advertised])
    rows = await agents._entitled_kiro_models(_request(provider), catalog)
    expected = [
        m["model_name"] for m in catalog if not catalog_row_would_drop(m["model_name"], advertised)
    ]
    assert len(rows) == len(expected)
    assert _names(rows) == ["auto", "claude-sonnet-5", "claude-opus-4.8"]
