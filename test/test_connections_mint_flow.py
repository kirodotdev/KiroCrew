"""On-demand minting of a Connections provider's approval URL: the mint flow.

The mint session's shape, the result matrix and grant revalidation, what the
watcher may report and how it disposes its own row, the PID shielded from the
orphan sweep, concurrent mints for one slug, and the URL screen: a
credential-bearing approval URL is disposed and retried once, and the card names
the endpoint an operator extension can allow.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import connections_mint_helpers
import pytest
from connections_mint_helpers import (
    _AUTHORIZE,
    _URL,
    _FakeClient,
    _state_only,
    _write_paired_grant_artifacts,
)
from oauth_url_corpus import OPERATOR_EXTENSION_OAUTH_URLS

from kiro_crew import mcp_grant
from kiro_crew.connections import mint
from kiro_crew.testing.wait import await_until, finished

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_mint = connections_mint_helpers._isolated_mint
protected_pids = connections_mint_helpers.protected_pids


# ── the mint session's shape ──


@pytest.mark.asyncio
async def test_a_successful_mint_holds_the_process_and_serves_the_url():
    await mint.start_oauth_mint("notion", _URL)

    view = mint.pending_mint_for("notion")
    assert _state_only(view) == {"state": "waiting", "oauth_url": _AUTHORIZE}
    client = _FakeClient.instances[-1]
    # Held, not disposed: the PKCE verifier and the loopback listener live here.
    assert client.shutdowns == 0
    await mint._dispose_mint(mint._mints["notion"])


def test_the_session_is_promptless_model_free_and_mounts_one_server():
    body = mint._mint_spec_body("kirocrew-mint-notion", {"notion": {"url": _URL}}, "desc")

    assert body["prompt"] == ""
    assert body["model"] == "auto"
    assert body["allowedTools"] == []
    assert body["includeMcpJson"] is False
    assert list(body["mcpServers"]) == ["notion"]
    assert body["tools"] == ["@notion"]


@pytest.mark.asyncio
async def test_the_mint_runs_on_a_dedicated_single_server_spec():
    await mint.start_oauth_mint("notion", _URL)

    agent = _FakeClient.instances[-1].kwargs["agent"]
    assert agent.startswith(f"kirocrew-mint-notion-{os.getpid()}-")
    await mint._dispose_mint(mint._mints["notion"])


# ── result matrix ──


@pytest.mark.asyncio
async def test_an_existing_grant_short_circuits_only_after_validating_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    # The validation spawn happened -- it is the process that got shut down --
    # and it asked the same two commands the Test button asks, promptless.
    assert len(_FakeClient.instances) == 1
    validator = _FakeClient.instances[-1]
    assert validator.commands == ["/mcp", "/tools"]
    assert validator.shutdowns == 1


@pytest.mark.asyncio
async def test_a_stale_grant_on_disk_falls_through_to_a_fresh_mint_instead_of_lying(
    monkeypatch: pytest.MonkeyPatch,
):
    # The artifact pair exists (a provider-side revoke never touches it), but the
    # authenticated check proves the server is not actually usable. Reporting
    # `granted` here is exactly the defect: Connect flips to Connected, then Test
    # later reveals the pair was dead all along.
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    _FakeClient.command_results["/mcp"] = {
        "data": {
            "servers": [
                {"name": "notion", "status": "failed", "toolCount": 0, "authenticating": False}
            ]
        }
    }

    await mint.start_oauth_mint("notion", _URL)

    # Fell through to a REAL fresh mint: a second process was spawned (the
    # validator, then the cold mint) and the card gets an approval URL, not a
    # coarse failure -- exactly what a user clicking Connect on a dead grant
    # expects the button to do.
    assert len(_FakeClient.instances) == 2
    validator, fresh = _FakeClient.instances
    assert validator.commands == ["/mcp", "/tools"]
    assert validator.shutdowns == 1
    assert _state_only(mint.pending_mint_for("notion")) == {
        "state": "waiting",
        "oauth_url": _AUTHORIZE,
    }
    assert fresh.shutdowns == 0
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_a_grant_with_zero_exposed_tools_is_proven_working_not_reconsented(
    monkeypatch: pytest.MonkeyPatch,
):
    # `no_tools` is reported only once the MCP server reached status `running`,
    # which for an OAuth provider means kiro-cli authenticated and completed
    # tools/list with the stored bearer. The credential WORKS; a zero-tools server
    # is a provider-prerequisite question. Sending this user to a consent page
    # would ask them to re-authorize something already authorized, and could not
    # fix what they actually have.
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    _FakeClient.command_results["/tools"] = {"data": {"tools": []}}

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    # One process -- the validator -- and no fresh consent spawn behind it.
    assert len(_FakeClient.instances) == 1


@pytest.mark.asyncio
async def test_a_spec_write_failure_during_validation_never_strands_the_row(
    monkeypatch: pytest.MonkeyPatch,
):
    # `_write_mint_agent_spec` raises OSError on an unusable main spec or an
    # unrecordable manifest row, and the validation runs on a fire-and-forget task
    # BEFORE the caller's own try. An escape would kill the flow and leave the row
    # at `minting` forever -- a card spinning with no terminal state to read.
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    calls: list[str] = []
    real = mint._write_mint_agent_spec

    def _fail_first(slug: str):
        calls.append(slug)
        if len(calls) == 1:
            raise OSError("main agent spec unusable")
        return real(slug)

    monkeypatch.setattr(mint, "_write_mint_agent_spec", _fail_first)

    await mint.start_oauth_mint("notion", _URL)

    # The flow survived the raise and reached a terminal state the card can act on.
    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["state"] != "minting"
    assert _state_only(view) == {"state": "waiting", "oauth_url": _AUTHORIZE}
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_validation_bounds_readiness_and_both_commands_with_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    # Each command carries its own transport timeout, so bounding only readiness
    # would let a provider that answers slowly three times over hold Connect in
    # `minting` for the sum of all three waits.
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    monkeypatch.setattr(mint, "_MINT_READY_TIMEOUT_SECONDS", 0.05)

    class _SlowCommand(_FakeClient):
        async def command_result(self, command: str, args: dict | None = None):
            # flake-ok: pre-existing 10 s sleep standing in for a command that never answers; the 50 ms deadline under test ends it first; moved verbatim, tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 s stand-in for a command that never answers and the deadline under test ends it; moved verbatim and tracked for a de-flake pass
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    attempts = iter([_SlowCommand, _FakeClient])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))

    await finished(mint.start_oauth_mint("notion", _URL), "start_oauth_mint", timeout=5)

    # The deadline fired inside the commands, the verdict read as unproven, and the
    # flow fell through to a real consent mint instead of hanging.
    assert _state_only(mint.pending_mint_for("notion")) == {
        "state": "waiting",
        "oauth_url": _AUTHORIZE,
    }
    await mint._dispose_mint(mint._mints["notion"])


# ── the watcher must not resurrect a disproven pair ──


@pytest.mark.asyncio
async def test_the_watcher_never_reports_granted_from_the_pair_it_disproved(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # THE regression these fixes exist to prevent. Nothing deletes a credential on
    # one failed check, so the disproven pair is still on disk. A watcher polling
    # bare presence sees it on its FIRST tick, five seconds in, flips the row to
    # `granted` and disposes the process holding the PKCE verifier -- reproducing
    # the exact lie AND destroying the consent URL.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"
    fresh = _FakeClient.instances[-1]

    # Several poll intervals with the disproven pair sitting on disk, untouched.
    # flake-ok: pre-existing 50 ms window of watcher polls that must not publish the disproven pair; moved verbatim, tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window of watcher polls that must not publish the disproven pair; moved verbatim and tracked for a de-flake pass
    await asyncio.sleep(0.05)

    assert mint._mints["notion"]["state"] == "waiting"
    assert mint._mints["notion"]["oauth_url"] == _AUTHORIZE
    # The process holding the verifier and the loopback listener is still alive,
    # so the URL the card is showing can still actually be redeemed.
    assert fresh.shutdowns == 0
    await mint._dispose_mint(entry)


@pytest.mark.asyncio
async def test_the_watcher_reports_granted_once_the_disproven_artifact_changes(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # Completing the exchange makes kiro-cli REWRITE the token artifact, so a
    # changed fingerprint is the honest completion signal -- the flow must still
    # resolve, or a real consent would hang to the TTL.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"

    # The user completes consent: the token artifact is rewritten.
    key = mcp_grant.grant_key(_URL)
    token_path = mcp_grant.kiro_oauth_cache_dir() / f"{key}.token.json"
    token_path.write_text('{"fresh": true, "padding": "xxxxxxxx"}', encoding="utf-8")
    later = time.time() + 5
    os.utime(token_path, (later, later))

    await await_until(entry["watcher"])

    assert mint._mints["notion"]["state"] == "granted"


@pytest.mark.asyncio
async def test_an_unreadable_fingerprint_still_fails_closed_after_a_disproof(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # The disproof and the fingerprint are SEPARATE facts. `grant_fingerprint`
    # answers None on any failed stat, so keying the watcher's proof requirement on
    # "fingerprint is not None" let an unreadable capture read as "nothing was
    # disproved" -- reopening the resurrection path the fingerprint exists to close.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    # The capture stat fails, so no baseline can be recorded.
    monkeypatch.setattr(mint, "grant_fingerprint", lambda url, **kw: None)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"
    fresh = _FakeClient.instances[-1]

    # flake-ok: pre-existing 50 ms window of watcher polls that must not publish the stale pair; moved verbatim, tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window of watcher polls that must not publish the stale pair; moved verbatim and tracked for a de-flake pass
    await asyncio.sleep(0.05)

    # No baseline and no proven revalidation: the stale pair must NOT be published,
    # and the process holding the redeemable URL must survive.
    assert mint._mints["notion"]["state"] == "waiting"
    assert mint._mints["notion"]["oauth_url"] == _AUTHORIZE
    assert fresh.shutdowns == 0
    await mint._dispose_mint(entry)


@pytest.mark.asyncio
async def test_no_baseline_grants_only_on_a_positive_revalidation(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # With no baseline, change cannot be observed, so the ONLY admissible proof is
    # a fresh authenticated validation -- and it must actually be consulted, or a
    # real consent completed in this window could never resolve.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    monkeypatch.setattr(mint, "grant_fingerprint", lambda url, **kw: None)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"

    # The user completes consent; a fresh validation now proves it works. The
    # fallback is rate-limited, not once-only, so a consent that lands after the
    # first probe still resolves.
    monkeypatch.setattr(mint, "_GRANT_REVALIDATION_INTERVAL_SECONDS", 0.0)
    verdicts = iter([False, True, True, True])
    monkeypatch.setattr(
        mint, "_validate_existing_grant", lambda slug, url: _async_value(next(verdicts))
    )

    await await_until(entry["watcher"])

    assert mint._mints["notion"]["state"] == "granted"


@pytest.mark.asyncio
async def test_a_pinned_mint_revalidates_against_the_pinned_entry(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # The no-baseline revalidation of a pinned mint must check the entry the
    # owner record validated, never a fresh read of the main spec.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    monkeypatch.setattr(mint, "grant_fingerprint", lambda url, **kw: None)
    pinned = {"url": _URL}

    await mint.start_oauth_mint("notion", _URL, pinned=pinned)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"

    monkeypatch.setattr(mint, "_GRANT_REVALIDATION_INTERVAL_SECONDS", 0.0)
    seen: list[object] = []

    async def _validate(slug: str, url: str, pinned: object = None) -> bool:
        seen.append(pinned)
        return True

    monkeypatch.setattr(mint, "_validate_existing_grant", _validate)

    await await_until(entry["watcher"])

    assert mint._mints["notion"]["state"] == "granted"
    assert seen and all(p is pinned for p in seen)


@pytest.mark.asyncio
async def test_the_revalidation_fallback_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # Each fallback spawns a process, so it must not fire on every grant poll for
    # the whole TTL. With the interval left at its real value, many ticks yield
    # exactly one probe. Counted in watcher ticks, not wall-clock time: a
    # contended runner can take longer than any fixed sleep to finish the first
    # tick.
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    _write_paired_grant_artifacts(_URL)
    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    monkeypatch.setattr(mint, "grant_fingerprint", lambda url, **kw: None)

    # Patched before the mint starts: the watcher is live before
    # `start_oauth_mint` returns, so a validator swapped in afterwards can miss
    # the first fallback. The first call is the start-up validation, made before
    # the watcher exists, so it goes to the real validator. Every later call is
    # a watcher fallback.
    calls: list[str] = []
    first_fallback = asyncio.Event()
    real_validate = mint._validate_existing_grant
    startup_validated = False

    async def _never_proves(slug: str, url: str) -> bool:
        nonlocal startup_validated
        if not startup_validated:
            startup_validated = True
            return await real_validate(slug, url)
        calls.append(slug)
        first_fallback.set()
        return False

    monkeypatch.setattr(mint, "_validate_existing_grant", _never_proves)

    # Every watcher tick calls `grant_observed` first, so counting its calls after
    # the first fallback counts the polls that had a chance to fire a second one.
    ticks_after_first = 0
    enough_ticks = asyncio.Event()
    real_grant_observed = mint.grant_observed

    async def _counting_grant_observed(url: str, **kw: Any) -> bool | None:
        nonlocal ticks_after_first
        if first_fallback.is_set():
            ticks_after_first += 1
            if ticks_after_first >= 20:
                enough_ticks.set()
        return await real_grant_observed(url, **kw)

    monkeypatch.setattr(mint, "grant_observed", _counting_grant_observed)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert startup_validated

    await asyncio.wait_for(first_fallback.wait(), timeout=30)
    # The window opens at the first fallback, and this bound keeps every counted
    # tick inside the shipped 60 s interval.
    await asyncio.wait_for(enough_ticks.wait(), timeout=30)

    assert len(calls) == 1
    assert mint._mints["notion"]["state"] == "waiting"
    await mint._dispose_mint(entry)


@pytest.mark.asyncio
async def test_a_refuted_grant_with_no_challenge_is_not_republished_as_granted(
    monkeypatch: pytest.MonkeyPatch,
):
    # The fresh spawn producing no challenge is not proof either: publishing
    # `granted` from it republishes the exact authorization this attempt just
    # refuted. It has to be the retryable failure instead.
    _write_paired_grant_artifacts(_URL)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )

    class _RefutedThenSilent(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = []  # no challenge on the fresh spawn

    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    attempts = iter([_FakeClient, _RefutedThenSilent])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))

    await mint.start_oauth_mint("notion", _URL)

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert _state_only(view) == {"state": "failed", "reason": "mint_grant_unproven"}
    assert logged == ["error reason=mint_grant_unproven"]


def _async_value(value: bool):
    """A ready coroutine returning ``value``, for patching an async predicate."""

    async def _coro() -> bool:
        return value

    return _coro()


def test_the_grant_fingerprint_changes_when_the_token_artifact_is_rewritten(tmp_path: Path):
    key = mcp_grant.grant_key(_URL)
    token = tmp_path / f"{key}.token.json"
    (tmp_path / f"{key}.registration.json").write_text("{}", encoding="utf-8")

    # Absent reads as None -- "no evidence of change", never a sentinel a caller
    # could mistake for a real reading.
    assert mcp_grant.grant_fingerprint(_URL, cache_dir=tmp_path) is None

    token.write_text("{}", encoding="utf-8")
    first = mcp_grant.grant_fingerprint(_URL, cache_dir=tmp_path)
    assert first is not None

    token.write_text('{"rotated": true}', encoding="utf-8")
    later = time.time() + 5
    os.utime(token, (later, later))

    assert mcp_grant.grant_fingerprint(_URL, cache_dir=tmp_path) != first


@pytest.mark.asyncio
async def test_a_validation_spawn_that_never_completes_falls_through_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)

    class _ValidatorBoom(_FakeClient):
        async def ensure_ready(self) -> None:
            raise RuntimeError("provider unreachable")

    attempts = iter([_ValidatorBoom, _FakeClient])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))

    await mint.start_oauth_mint("notion", _URL)

    # The validator's own failure never raises out of the mint flow, and the
    # fresh cold mint that follows still succeeds normally.
    assert _state_only(mint.pending_mint_for("notion")) == {
        "state": "waiting",
        "oauth_url": _AUTHORIZE,
    }
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_validation_never_holds_its_own_process_win_or_lose(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # Unlike a cold mint's URL-holding process, the validation session has no
    # consent to protect once it answers -- it must always dispose itself, on
    # both a usable and an unusable verdict, and never leave a PID shielded.
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)

    await mint.start_oauth_mint("notion", _URL)  # usable verdict

    assert protected_pids == set()
    await mint._dispose_mint(mint._mints["notion"])
    protected_pids.clear()

    _FakeClient.command_results["/mcp"] = {
        "data": {"servers": [{"name": "notion", "status": "failed", "toolCount": 0}]}
    }
    await mint.start_oauth_mint("notion", _URL)  # unusable verdict, falls through

    validator = _FakeClient.instances[0]
    assert validator._pid not in protected_pids
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_a_server_that_never_challenges_is_granted_and_disposed(
    monkeypatch: pytest.MonkeyPatch,
):
    class _NoChallenge(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    # Nothing to hold, so the process goes immediately.
    assert _FakeClient.instances[-1].shutdowns == 1


@pytest.mark.asyncio
async def test_a_challenge_for_another_server_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
):
    class _WrongServer(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "linear", "oauthUrl": _AUTHORIZE}]

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _WrongServer)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}


@pytest.mark.asyncio
async def test_a_failed_mint_records_a_coarse_reason_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Boom(_FakeClient):
        async def ensure_ready(self) -> None:
            raise TimeoutError("provider unreachable at 10.0.0.5")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _Boom)

    await mint.start_oauth_mint("notion", _URL)

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["state"] == "failed"
    # Coarse code only: no provider- or exception-supplied text.
    assert view["reason"] == "mint_timeouterror"
    assert "10.0.0.5" not in json.dumps(view)
    assert _FakeClient.instances[-1].shutdowns == 1


@pytest.mark.asyncio
async def test_a_dead_holder_reports_expired_rather_than_serving_a_dead_url():
    await mint.start_oauth_mint("notion", _URL)
    _FakeClient.instances[-1].alive = False

    # The GET path commits the verdict first, so the view reports a state the
    # stored row actually has -- a URL whose verifier and listener are gone can
    # never be redeemed, and the card must fall back to idle Connect.
    await mint.expire_dead_holder("notion")
    view = mint.pending_mint_for("notion")

    assert _state_only(view) == {"state": "expired", "reason": "mint_process_gone"}
    assert view.get("oauth_url") is None


@pytest.mark.asyncio
async def test_the_view_never_reports_a_state_the_stored_row_does_not_have():
    # The holder is alive when expire_dead_holder probes, and dies before the view
    # is read. Probing a second time here would report expired while the row still
    # said waiting -- and the abandon fence, which only acts on a stored 'expired',
    # would then refuse every attempt to clean the entry up.
    await mint.start_oauth_mint("notion", _URL)
    holder = _FakeClient.instances[-1]

    await mint.expire_dead_holder("notion")  # alive: nothing to commit
    holder.alive = False

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["state"] == mint._mints["notion"]["state"] == "waiting"

    # The next poll commits it, and only then does the fence accept the claim.
    await mint.expire_dead_holder("notion")
    assert mint.pending_mint_for("notion")["state"] == "expired"
    # The row is committed, not merely reported -- nothing deletes the entry.
    assert mint._mints["notion"]["state"] == "expired"


def test_no_mint_reads_as_absent():
    assert mint.pending_mint_for("notion") is None


@pytest.mark.asyncio
async def test_a_second_mint_supersedes_and_disposes_the_first():
    await mint.start_oauth_mint("notion", _URL)
    first = _FakeClient.instances[-1]

    await mint.start_oauth_mint("notion", _URL)
    second = _FakeClient.instances[-1]

    assert first is not second
    assert first.shutdowns == 1
    assert second.shutdowns == 0
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_dispose_releases_the_process_the_watcher_and_the_spec(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    removed: list[str] = []
    monkeypatch.setattr(mint, "_remove_mint_agent_spec", removed.append)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    watcher = entry["watcher"]
    spec_path = entry["spec_path"]
    assert protected_pids == {_FakeClient.instances[-1]._pid}

    await mint._dispose_mint(entry)

    assert _FakeClient.instances[-1].shutdowns == 1
    assert watcher.cancelled() or watcher.cancelling() > 0
    assert removed == [spec_path]
    assert protected_pids == set()
    assert "client" not in entry and "agent" not in entry


# ── the watcher disposing its own row ──


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["grant", "expiry"])
async def test_the_watcher_completes_teardown_when_it_disposes_its_own_row(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], terminal: str
):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    if terminal == "expiry":
        # Expiry is the one terminal this test does not trigger itself, so order it
        # structurally rather than by margin. The watcher always awaits a full poll
        # interval BEFORE it re-checks its deadline, so a poll longer than the TTL
        # parks it in that sleep for the whole of the synchronous row read below and
        # lets it take the expiry branch on its first wake. Reading ``spec_path`` off
        # a row that a concurrent teardown is free to strip is what made this flaky.
        monkeypatch.setattr(mint, "_MINT_TTL_SECONDS", 0.05)
        monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.25)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    client = _FakeClient.instances[-1]
    spec_path = Path(entry["spec_path"])
    assert spec_path.is_file()

    if terminal == "grant":
        monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    await await_until(entry["watcher"])

    # Cancelling the calling task would land the cancellation inside the client
    # teardown and abandon it, leaking the process tree and the listener.
    assert client.shutdowns == 1
    assert not spec_path.exists()
    assert protected_pids == set()
    assert mint._mints["notion"]["state"] == ("granted" if terminal == "grant" else "expired")
    assert "client" not in mint._mints["notion"]


@pytest.mark.asyncio
async def test_a_teardown_cancelled_from_outside_still_releases_the_spec_and_pid(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    class _HangingShutdown(_FakeClient):
        async def shutdown(self) -> None:
            self.shutdowns += 1
            # flake-ok: pre-existing unbounded wait in the stubbed shutdown; the test cancels the teardown that runs it; moved verbatim, tracked for a de-flake pass
            await asyncio.Event().wait()

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _HangingShutdown)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    assert protected_pids == {_FakeClient.instances[-1]._pid}

    task = asyncio.create_task(mint._dispose_mint(entry))
    # flake-ok: pre-existing 50 ms barrier that lets the teardown reach the hanging shutdown before the cancel; moved verbatim, tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms barrier that lets the teardown reach the hanging shutdown before the cancel; moved verbatim and tracked for a de-flake pass
    await asyncio.sleep(0.05)
    task.cancel()
    # flake-ok: pre-existing unbounded await of the teardown task the test has just cancelled; moved verbatim, tracked for a de-flake pass
    await asyncio.gather(task, return_exceptions=True)

    # A wedged teardown cancelled from outside must not leave the spec on disk
    # or the PID shielded from the sweep for the rest of the process's life.
    assert not spec_path.exists()
    assert protected_pids == set()


@pytest.mark.asyncio
async def test_a_watcher_never_writes_to_a_row_it_does_not_own(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url, **kw: True)
    live: mint.MintState = {
        "state": "waiting",
        "started": 2.0,
        "oauth_url": _AUTHORIZE,
        "token": "b" * 32,
    }
    mint._mints["notion"] = live

    # A watcher left over from a superseded flow: its token does not name the
    # row the slug now points at, so it must leave that row alone.
    stale_token = "f" * 32
    assert live["token"] != stale_token
    await finished(mint._mint_watcher("notion", _URL, stale_token), "mint watcher")

    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"


# ── the sweep-protected PID ──


@pytest.mark.asyncio
async def test_the_held_process_is_shielded_from_the_orphan_sweep(protected_pids: set[int]):
    await mint.start_oauth_mint("notion", _URL)

    # Nothing claims a mint as a session, so without this the periodic sweep
    # reaps it once it ages past the spawn grace -- inside the mint's own TTL.
    assert protected_pids == {_FakeClient.instances[-1]._pid}
    await mint._dispose_mint(mint._mints["notion"])
    assert protected_pids == set()


@pytest.mark.asyncio
async def test_a_failed_mint_releases_its_pid_protection(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    class _FailsAfterSpawn(_FakeClient):
        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            raise RuntimeError("drain exploded")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FailsAfterSpawn)

    await mint.start_oauth_mint("notion", _URL)

    assert protected_pids == set()
    assert _FakeClient.instances[-1].shutdowns == 1


# ── concurrent mints for one slug ──


@pytest.mark.asyncio
async def test_row_identity_survives_a_coarse_clock(monkeypatch: pytest.MonkeyPatch):
    # Windows' time.monotonic() has ~15.6ms granularity, so two Connects inside one
    # tick read the same clock value. Identity must not be a clock reading, or
    # every token guard fails open and the late-failure clobber returns.
    # flake-ok: pre-existing patch of the stdlib time.monotonic the mint module reads, the coarse clock under test, undone by monkeypatch at teardown; moved verbatim, tracked for a de-flake pass
    # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.monotonic the mint module reads as the coarse clock under test; moved verbatim and tracked for a de-flake pass
    monkeypatch.setattr(mint.time, "monotonic", lambda: 1234.5)
    gate = asyncio.Event()

    class _SlowFailure(_FakeClient):
        async def ensure_ready(self) -> None:
            # flake-ok: pre-existing unbounded wait in the stubbed readiness; the test sets gate itself; moved verbatim, tracked for a de-flake pass
            await gate.wait()
            raise RuntimeError("late boom")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowFailure)
    first = asyncio.create_task(mint.start_oauth_mint("notion", _URL))
    # flake-ok: pre-existing zero yield that lets the first mint reach the stubbed readiness; moved verbatim, tracked for a de-flake pass
    await asyncio.sleep(0)

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    await mint.start_oauth_mint("notion", _URL)
    live = mint._mints["notion"]

    gate.set()
    await await_until(first)

    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"
    await mint._dispose_mint(live)


@pytest.mark.asyncio
async def test_a_late_failure_does_not_clobber_the_live_row(monkeypatch: pytest.MonkeyPatch):
    gate = asyncio.Event()

    class _SlowFailure(_FakeClient):
        async def ensure_ready(self) -> None:
            # flake-ok: pre-existing unbounded wait in the stubbed readiness; the test sets gate itself; moved verbatim, tracked for a de-flake pass
            await gate.wait()
            raise RuntimeError("late boom")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowFailure)
    first = asyncio.create_task(mint.start_oauth_mint("notion", _URL))
    # flake-ok: pre-existing zero yield that lets the first mint reach the stubbed readiness; moved verbatim, tracked for a de-flake pass
    await asyncio.sleep(0)

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _FakeClient)
    await mint.start_oauth_mint("notion", _URL)
    live = mint._mints["notion"]
    assert live["state"] == "waiting"

    gate.set()
    await await_until(first)

    # The superseded flow's failure must not replace the row that holds a live
    # client, watcher and spec -- that would strand all three.
    assert mint._mints["notion"] is live
    assert live["state"] == "waiting"
    assert live["oauth_url"] == _AUTHORIZE
    await mint._dispose_mint(live)


# ── the URL predicate ──


@pytest.mark.asyncio
async def test_a_credential_bearing_url_is_disposed_and_retried_once(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog
):
    tainted = "https://auth.example.com/authorize?client_id=abc&access_token=AKIAIOSFODNN7EXAMPLE"

    class _Tainted(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "notion", "oauthUrl": tainted}]

    attempts = iter([_Tainted, _FakeClient])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )
    token, prior = await mint.reserve_mint_row("notion")

    with caplog.at_level("WARNING"):
        await mint.start_oauth_mint("notion", _URL, token, prior)

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["token"] == token
    assert _state_only(view) == {"state": "waiting", "oauth_url": _AUTHORIZE}
    assert len(_FakeClient.instances) == 2
    rejected, fresh = _FakeClient.instances
    assert rejected.shutdowns == 1
    assert fresh.shutdowns == 0
    assert protected_pids == {fresh._pid}
    assert logged == ["ok url_minted=True"]
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(logged)

    await mint._dispose_mint(mint._mints["notion"])
    assert fresh.shutdowns == 1
    assert protected_pids == set()


@pytest.mark.asyncio
async def test_a_rejected_url_and_its_retry_are_screened_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[int] = []
    verdicts = iter([True, False])

    def _screen(_url: str) -> bool:
        seen.append(threading.get_ident())
        return next(verdicts)

    monkeypatch.setattr(mint, "oauth_url_contains_credential", _screen)

    await mint.start_oauth_mint("notion", _URL)
    clients = list(_FakeClient.instances)
    await mint._dispose_mint(mint._mints["notion"])

    assert len(seen) == 2
    assert threading.get_ident() not in seen
    assert len(clients) == 2


@pytest.mark.asyncio
async def test_a_second_credential_bearing_url_surfaces_failure_without_a_third_attempt(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog
):
    tainted = "https://auth.example.com/authorize?client_id=abc&access_token=AKIAIOSFODNN7EXAMPLE"

    class _Tainted(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "notion", "oauthUrl": tainted}]

    attempts = iter([_Tainted, _Tainted])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )
    token, prior = await mint.reserve_mint_row("notion")

    with caplog.at_level("WARNING"):
        await mint.start_oauth_mint("notion", _URL, token, prior)

    view = mint.pending_mint_for("notion")
    assert view is not None
    assert view["token"] == token
    # A fixed credential in the query is a rejection the allowlist cannot
    # clear, so the card is NOT told which endpoint to add: naming it here would
    # advertise a remedy that leaves the URL rejected. The naming case is the
    # allowlist-clearable rejection below.
    assert _state_only(view) == {"state": "failed", "reason": "mint_url_rejected"}
    assert len(_FakeClient.instances) == 2
    assert [client.shutdowns for client in _FakeClient.instances] == [1, 1]
    assert protected_pids == set()
    assert logged == ["error reason=mint_url_rejected"]
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(logged)
    assert "auth.example.com" not in caplog.text
    assert tainted not in json.dumps(view)


# The URL the operator extension exists for: standard front-channel parameters
# whose opaque state trips the long-query heuristic at any endpoint outside the
# allowlist, and passes once the endpoint is added. This is the shape the card
# must NAME, because adding the entry actually fixes it.
_EXEMPTIBLE_NAME, _EXEMPTIBLE_URL, (_EXEMPTIBLE_HOST, _EXEMPTIBLE_PATH) = (
    OPERATOR_EXTENSION_OAUTH_URLS[0]
)
_EXEMPTIBLE_ENDPOINT = f"{_EXEMPTIBLE_HOST}{_EXEMPTIBLE_PATH}"


async def _reject_twice(
    monkeypatch: pytest.MonkeyPatch, caplog, tainted: str
) -> tuple[dict | None, list[str]]:
    """Drive a mint whose every attempt yields *tainted* to its terminal rejection."""

    class _Tainted(_FakeClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.requests = [{"serverName": "notion", "oauthUrl": tainted}]

    attempts = iter([_Tainted, _Tainted])
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: next(attempts))
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )
    token, prior = await mint.reserve_mint_row("notion")
    with caplog.at_level("WARNING"):
        await mint.start_oauth_mint("notion", _URL, token, prior)
    return mint.pending_mint_for("notion"), logged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tainted",
    [
        # The credential IS the host label: a redacted host would name nothing.
        "https://AKIAIOSFODNN7EXAMPLE.example.com/authorize?client_id=abc",
        # The credential is a PATH segment: the diagnostic pair carries the
        # redaction tag, which is not a path a user could type.
        "https://idp.example.com/AKIAIOSFODNN7EXAMPLE/authorize",
        # Nameable host+path, but the extension loader would refuse the host,
        # so naming it would advertise an entry that gets skipped on load.
        _EXEMPTIBLE_URL.replace(_EXEMPTIBLE_HOST, "localhost", 1),
        # Nameable host+path, but the gate never grants the carve-out to an
        # explicit port, so the entry would be refused again after adding it.
        _EXEMPTIBLE_URL.replace(_EXEMPTIBLE_HOST, _EXEMPTIBLE_HOST + ":8443", 1),
        # Nameable, loader-valid host+path, but a fixed credential in the query
        # is refused unconditionally -- the allowlist would not clear it.
        f"https://{_EXEMPTIBLE_HOST}{_EXEMPTIBLE_PATH}?access_token=AKIAIOSFODNN7EXAMPLE",
        # Same, for a fragment: providers never emit one, the gate never allows one.
        _EXEMPTIBLE_URL + "#AKIAIOSFODNN7EXAMPLE",
    ],
    ids=[
        "credential-in-host",
        "credential-in-path",
        "loader-refuses-host",
        "explicit-port",
        "fixed-credential-in-query",
        "fragment",
    ],
)
async def test_a_rejection_the_helper_cannot_name_leaves_the_card_unnamed(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog, tainted: str
):
    """Control for the naming case: when no copy-ready endpoint exists the row
    carries NO rejected_endpoint, so the card falls back to its unnamed message
    rather than showing a partial host, a redaction tag, or a remedy that
    cannot work."""
    view, logged = await _reject_twice(monkeypatch, caplog, tainted)

    assert view is not None
    assert _state_only(view) == {"state": "failed", "reason": "mint_url_rejected"}
    assert logged == ["error reason=mint_url_rejected"]
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(logged)
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(view)


@pytest.mark.asyncio
async def test_the_named_endpoint_is_host_and_path_only(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog
):
    """The card gets the endpoint identity and nothing else from the URL: no
    scheme, query, fragment, or the state/PKCE material that lives in them."""
    # Mixed-case host, and every query value is opaque material the card must
    # never echo (the 128-char state is what trips the gate).
    tainted = _EXEMPTIBLE_URL.replace(_EXEMPTIBLE_HOST, _EXEMPTIBLE_HOST.title(), 1)
    view, logged = await _reject_twice(monkeypatch, caplog, tainted)

    assert view is not None
    assert view["rejected_endpoint"] == _EXEMPTIBLE_ENDPOINT
    serialized = json.dumps(view)
    for never in (
        "https://",
        "a1B2c3D4",
        "E9Melhoa2Owv",
        "client_id",
        "?",
    ):
        assert never not in serialized
        assert never not in caplog.text
        assert never not in json.dumps(logged)


@pytest.mark.asyncio
async def test_a_named_endpoint_never_reaches_the_log_or_audit_line(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int], caplog
):
    """The scanner decision: the endpoint is a CARD field, not log text. If a
    future edit routes it through the logger, this fails before Semgrep or
    CodeQL do."""
    view, logged = await _reject_twice(monkeypatch, caplog, _EXEMPTIBLE_URL)

    assert view is not None
    assert view["rejected_endpoint"] == _EXEMPTIBLE_ENDPOINT
    assert caplog.text  # the slug-only warning still fires
    assert "notion" in caplog.text
    assert _EXEMPTIBLE_HOST not in caplog.text
    assert _EXEMPTIBLE_HOST not in json.dumps(logged)
    assert logged == ["error reason=mint_url_rejected"]


def test_a_rejected_endpoint_rides_the_card_view_only_beside_its_reason():
    """``pending_mint_for`` projects the field through the same allowlist as
    ``reason``: present when stored, absent when not, never invented."""
    mint._mints["notion"] = {"state": "failed", "reason": "mint_url_rejected", "token": "t1", "started": 0.0, "rejected_endpoint": "idp.example.com/authorize"}  # type: ignore[typeddict-item]
    assert _state_only(mint.pending_mint_for("notion")) == {
        "state": "failed",
        "reason": "mint_url_rejected",
        "rejected_endpoint": "idp.example.com/authorize",
    }
    mint._mints["notion"] = {
        "state": "failed",
        "reason": "mint_url_rejected",
        "token": "t2",
        "started": 0.0,
    }
    assert _state_only(mint.pending_mint_for("notion")) == {
        "state": "failed",
        "reason": "mint_url_rejected",
    }


@pytest.mark.asyncio
async def test_a_failure_records_an_audit_event_with_no_exception_text(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Boom(_FakeClient):
        async def ensure_ready(self) -> None:
            raise TimeoutError("provider unreachable at 10.0.0.5")

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _Boom)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )

    await mint.start_oauth_mint("notion", _URL)

    assert logged == ["error reason=mint_timeouterror"]
    assert "10.0.0.5" not in json.dumps(logged)
