"""Policy distribution: publishing a fetched document, then installing it.

The cache is published, and the publish confirmed, before the ceiling is installed; a
publish or a boot never rolls the ceiling backward, and the cache swap compares the
source as well as the bytes; a rollback never destroys a newer ceiling and leaves no
rejected copy behind, nor does a boot whose publish fails leave a looser one; a refresh
cannot migrate the source, boot applies the same gates as a refresh, and the controls
materialised from a ceiling are re-derived once it is installed.

One topic of the ``test_governance_distribution*.py`` family; the constants, document
builders, fake fetchers and fixtures the family shares come from
``governance_distribution_helpers.py``. Nothing here touches the network either: see
``test_governance_distribution.py``'s docstring for how the transport is faked.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import profiles_dir_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import _TEST_SOURCE, _body, _cache, _doc, _static_fetcher

from kiro_crew.platform import governance
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError, current_context


class TestTheCacheIsPublishedBeforeTheCeilingIsInstalled:
    def test_a_concurrent_child_cannot_read_the_retired_ceiling(self, transport, install_ceiling):
        """A cache-only child adopts whatever the cache holds.

        Installing first leaves a window where the gateway enforces the new ceiling and a
        fresh app backend adopts the retired one.
        """
        pushed = _body("pushed")
        source = transport(_static_fetcher(pd.FetchedPolicy(body=pushed, etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        seen_at_install: dict = {}

        import kiro_crew.platform.context as ctx_mod

        original = ctx_mod.set_context

        def spy(ctx):
            # What the cache holds AT THE MOMENT the new ceiling goes live.
            cached = pd.read_cache()
            seen_at_install["body"] = cached.body if cached else None
            return original(ctx)

        try:
            ctx_mod.set_context = spy
            assert pd.refresh_now().status == pd.REFRESH_APPLIED
        finally:
            ctx_mod.set_context = original

        assert (
            seen_at_install["body"] == pushed
        ), "the cache still held the retired ceiling when the new one was installed"

    def test_a_document_that_does_not_compose_is_never_published(
        self, transport, install_ceiling, profiles_dir
    ):
        """Publishing before validating would hand a child a ceiling the host refused."""
        _cache("last-good", _TEST_SOURCE)
        (profiles_dir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "sandbox": {"min_level": "off"},
                }
            ),
            encoding="utf-8",
        )
        gp.reset_store()
        pushed = _body("pushed", sandbox={"min_level": "strict"})
        source = transport(_static_fetcher(pd.FetchedPolicy(body=pushed)))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": _TEST_SOURCE}))
        )
        del source

        assert pd.refresh_now().status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert (
            cached is not None and cached.body != pushed
        ), "a refused document reached the cache, where a cache-only child would adopt it"


class TestThePublishIsConfirmedBeforeInstalling:
    def test_a_failed_cache_write_keeps_the_running_ceiling(
        self, transport, install_ceiling, monkeypatch
    ):
        """``write_cache`` is best-effort, but a cache-only child inherits FROM it.

        A swallowed write failure would leave the gateway enforcing the new, possibly
        tighter ceiling while every app backend spawned afterwards adopted the older,
        looser one.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: None)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running

    def test_the_confirmation_checks_the_recorded_source_not_only_the_bytes(
        self, transport, install_ceiling, monkeypatch
    ):
        """A repoint that publishes IDENTICAL bytes cannot be confirmed by body alone.

        It is the recorded source the next boot's repoint rule judges, so a metadata write
        that failed while the document write succeeded leaves the old source's cache
        passing a body-only check — a refresh reporting success that a restart then
        discards, failing closed on a host that had just been told it was fine.
        """
        body = _body("pushed")
        new_source = transport(_static_fetcher(pd.FetchedPolicy(body=body, etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": new_source}))
        )
        # The same bytes, still recorded against the endpoint this host has moved OFF.
        pd.write_cache(body, source="https://retired.corp.example/p.json", etag="v1")
        # The document write lands; the metadata write does not, so the recorded source
        # stays the retired one.
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: None)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running


class TestAPublishCannotRollTheCeilingBackward:
    """A fetch is slow, and a refresh is not the only writer. A refresher that fetched v2
    over a slow link while ``policy fetch --force`` published v3 would write v2 over v3 and
    then INSTALL v2 — the ceiling moving backward to a looser document, which is the one
    direction this tier must never move on its own.
    """

    def test_a_lost_swap_neither_overwrites_the_cache_nor_installs(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        # A concurrent writer publishes v3 while this refresh is fetching. Hooked on the
        # fetch itself so the ordering is the real one: observed-before, published-during.
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            pd.write_cache(_body("v3"), source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        outcome = pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body("v3"), "the newer document must survive the race"
        assert current_context().governance is running, "v2 must not have been installed"
        assert outcome.status == pd.REFRESH_UNCHANGED

    def test_the_snapshot_is_taken_before_the_fetch_not_after(
        self, transport, install_ceiling, monkeypatch
    ):
        """Reading it afterwards would already include the concurrent write, so the swap
        would pass and overwrite the newer document — the ordering IS the control."""
        seen: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")

        real_write = pd.write_cache

        def record(body, **kw):
            if "expect_pair" in kw:
                seen.append(kw["expect_pair"])
            return real_write(body, **kw)

        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            real_write(_body("v3"), source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        monkeypatch.setattr(pd, "write_cache", record)
        pd.refresh_now()

        assert seen, "the publish must carry an expectation at all"
        assert seen[0] == (
            pd._body_digest(_body("v1")),
            pd._source_digest(source),
        ), "the expectation must be the PRE-fetch pair, not the racer's"

    def test_an_uncontended_refresh_still_publishes_and_installs(self, transport, install_ceiling):
        """The control: with no racer the swap passes and the refresh applies as before."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v2")

    def test_a_first_publish_expects_no_cache(self, transport, install_ceiling, monkeypatch):
        """With nothing cached the expectation is the empty digest, so a writer that
        appears during the fetch still wins rather than being overwritten."""
        seen: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        assert pd.read_cache() is None

        real_write = pd.write_cache

        def record(body, **kw):
            seen.append(kw.get("expect_pair", "<absent>"))
            return real_write(body, **kw)

        monkeypatch.setattr(pd, "write_cache", record)
        pd.refresh_now()
        assert seen and seen[0] == ("", ""), "an absent cache must be expected as absent"

    def test_a_failed_write_is_still_reported_as_a_cache_problem_not_a_race(
        self, transport, install_ceiling, monkeypatch
    ):
        """The two reasons a publish did not happen want different answers, and re-reading
        is how they are told apart."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert "policy cache" in outcome.detail
        assert current_context().governance is running


class TestARollbackCannotDestroyANewerCeiling:
    """The publish and the failure are separated by ``apply_ceiling``, so another process
    can publish in between — and rolling back over it would destroy a valid, newer ceiling
    nobody asked us to touch. The restore is a compare-and-swap on the document THIS
    refresh published, mirroring the publish it undoes.
    """

    def test_a_newer_document_published_after_our_publish_is_left_alone(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        # Publish v3 from "another process" at the moment the install refuses — that is
        # exactly the window between this refresh's publish and its rollback.
        def refuse_and_race(_ceiling):
            pd.write_cache(_body("v3"), source=source, etag="v3")
            raise RuntimeError("profile floor")

        monkeypatch.setattr(pd, "apply_ceiling", refuse_and_race)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body("v3"), "a rollback must not destroy a newer ceiling"

    def test_an_uncontended_rollback_still_restores(self, transport, install_ceiling, monkeypatch):
        """The control: with no racer the swap passes and the prior copy comes back."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )

        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v1")


class TestABootPublishFailureDoesNotLeaveALooserCache:
    """``write_cache`` is best-effort for the GATEWAY — it is governed by what it fetched
    either way — but a cache-only app backend resolves its ceiling FROM that file. An
    unpublished tightening therefore leaves a stale, looser document as what every child
    spawned afterwards adopts: the failure the cache-only handoff exists to prevent,
    reached through a swallowed write error.
    """

    def test_a_disagreeing_stale_cache_is_removed(self, transport, monkeypatch):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tightened"))))
        pd.write_cache(_body("loose"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert ceiling is not None and ceiling.identity_issuer == "tightened"
        assert (
            pd.read_cache() is None
        ), "a child must find no cache and fail closed, not adopt the superseded ceiling"

    def test_an_agreeing_cache_is_left_alone(self, transport, monkeypatch):
        """An equal document is not stale, and the write may simply have been redundant."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("same"))))
        pd.write_cache(_body("same"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("same")

    def test_a_stale_cache_that_cannot_be_removed_fails_boot(self, transport, monkeypatch):
        """Nothing can then make the cache agree with the ceiling being adopted, so this is
        a boot refusal rather than something to log and continue past."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tightened"))))
        pd.write_cache(_body("loose"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)
        monkeypatch.setattr(
            pd.Path,
            "unlink",
            lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("read-only filesystem")),
        )

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "cache" in str(caught.value)

    def test_no_prior_cache_is_not_a_failure(self, transport, monkeypatch):
        """With nothing cached there is nothing a child could adopt instead, so a failed
        write costs the outage fallback and nothing else."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        monkeypatch.setattr(pd, "write_cache", lambda *a, **k: False)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"


class TestALiveRefreshCannotMigrateTheSource:
    """A candidate that MOVES ``distribution.source`` is refused, not installed.

    Nothing else would notice: the refresher re-reads the installed ceiling each cycle, so
    it would start polling the new address and the migration would look like it worked —
    until a restart, where the bootstrap declaration still names the OLD source and the
    cache, recorded against the new one, is discarded by the repoint rule. A fleet that had
    retired the old address would have hosts that run fine and cannot reboot.
    """

    def test_a_candidate_that_moves_the_source_is_refused(self, transport, install_ceiling):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert pd.POLICY_URL_ENV in outcome.detail, "the operator needs the durable channel"
        assert "elsewhere.corp.example" not in outcome.detail, "and not the new address"

    def test_the_refused_document_is_not_cached(self, transport, install_ceiling):
        """It must not become the last-known-good either, or the next boot adopts a
        document naming a source this host will not resolve."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        moved = _body("v2", distribution={"source": "https://elsewhere.corp.example/p.json"})
        transport(_static_fetcher(pd.FetchedPolicy(body=moved, etag="v2")))

        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is None or cached.body != moved

    def test_the_same_source_carried_forward_is_fine(self, transport, install_ceiling):
        """The ordinary case: a published document repeats its own source, and that is not
        a migration. Refusing it would break every fleet using the self-refresh channel."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        carried = _body("v2", distribution={"source": source})
        transport(_static_fetcher(pd.FetchedPolicy(body=carried, etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    def test_a_document_declaring_no_source_is_fine(self, transport, install_ceiling):
        """A published document need not repeat the block at all."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v1"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED

    @pytest.mark.parametrize(
        "declared",
        [None, 123, [], {}, "", "   "],
        ids=["null", "int", "list", "dict", "empty", "blank"],
    )
    def test_an_unusable_declaration_reads_as_no_source(self, declared):
        body = json.dumps({"version": 1, "distribution": {"source": declared}}).encode("utf-8")
        assert pd._declared_source(body) == ""

    def test_a_document_that_is_not_json_yields_no_source(self):
        assert pd._declared_source(b"{ not json") == ""


class TestBootAppliesTheSameGatesAsARefresh:
    """Without this, boot would cache and install a document ``refresh_now`` refuses — and
    the refresher would then reject it on every cycle for the lifetime of the process,
    logging a rejection forever while the host ran on it.
    """

    def test_a_document_that_moves_the_source_is_refused_at_boot(self, transport):
        moved = _body("v1", distribution={"source": "https://elsewhere.corp.example/p.json"})
        source = transport(_static_fetcher(pd.FetchedPolicy(body=moved)))

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert pd.POLICY_URL_ENV in str(caught.value)
        assert pd.read_cache() is None, "a refused document must not become last-known-good"

    def test_a_document_refused_by_the_profile_floor_is_not_cached_at_boot(
        self, transport, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tight"))))
        monkeypatch.setattr(
            pd,
            "validate_ceiling",
            lambda c: (_ for _ in ()).throw(PlatformCompositionError("a profile is looser")),
        )

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert "does not compose" in str(caught.value)
        assert pd.read_cache() is None, (
            "caching it would make the next boot fail on the same document instead of "
            "re-fetching a corrected one"
        )

    def test_a_usable_cache_still_salvages_the_boot(self, transport, monkeypatch):
        """A read-and-refused document is not an availability failure, but the cache is
        still a ceiling this fleet published and this host verified."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("tight"))))
        pd.write_cache(_body("previous"), source=source, etag="v1", now=time.time() - 100_000)
        monkeypatch.setattr(
            pd,
            "validate_ceiling",
            lambda c: (_ for _ in ()).throw(PlatformCompositionError("a profile is looser")),
        )

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "previous"

    def test_an_ordinary_document_still_boots_and_caches(self, transport):
        """The control: the gates must not refuse the normal case."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("central"))))
        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "central"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("central")


class TestAFailedRollbackLeavesNoCacheRatherThanARejectedOne:
    """The bytes on disk at that moment are the ones this host just REFUSED, so the
    invalidate-then-write ordering is what makes a partial failure safe: a failed restoring
    write leaves the cache ABSENT, and a cache-only child fails closed instead of adopting
    a rejected ceiling.
    """

    def test_a_restore_that_cannot_write_leaves_no_cache(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("v1"), source=source, etag="v1")

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        # The restoring write fails AFTER the invalidation. Only the restore path is broken,
        # so the publish earlier in the refresh still succeeds.
        calls: list[int] = []

        real_write_file = pd._write_cache_file

        def fail_on_restore(path, data):
            calls.append(1)
            if len(calls) > 2:  # the publish writes two files first
                raise OSError("read-only filesystem")
            return real_write_file(path, data)

        monkeypatch.setattr(pd, "_write_cache_file", fail_on_restore)
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert (
            pd.read_cache() is None
        ), "the refused document must not survive as the last-known-good"

    def test_the_lost_fallback_is_reported_as_an_incident(
        self, transport, install_ceiling, monkeypatch
    ):
        """``refresh_now`` cannot raise, so this log and mark are the only signal."""
        marks: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("v1"), source=source, etag="v1")
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        monkeypatch.setattr(
            pd,
            "mark_governance_incident",
            lambda kind, detail="": marks.append((kind, detail)),
        )

        real_write_file = pd._write_cache_file
        calls: list[int] = []

        def fail_on_restore(path, data):
            calls.append(1)
            if len(calls) > 2:
                raise OSError("read-only filesystem")
            return real_write_file(path, data)

        monkeypatch.setattr(pd, "_write_cache_file", fail_on_restore)
        pd.refresh_now()

        assert any(kind == "degraded" and "restore" in detail for kind, detail in marks)


class TestAMaterialisedControlIsReDerivedAfterAnInstall:
    """Most governed controls are LIVE evaluations — the sandbox floor is re-read per spawn,
    the governance gate per tool call — so a ceiling swap binds them with nothing further.
    A few are materialised once, when an action is taken, and outlive the decision. A
    published tailnet origin is the case that matters: its gate fires when ``publish`` is
    CALLED, so a ceiling that later pins the capability off does not retract what is already
    serving. Before live refresh existed the ceiling only changed at boot and that could not
    arise.
    """

    def test_a_hook_runs_after_the_install(self, transport, install_ceiling):
        ran: list[str] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(
            lambda: ran.append(current_context().governance.identity_issuer)
        )

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert ran == ["v2"], "the hook must see the ceiling it is meant to enforce"

    def test_hooks_run_on_an_UNCHANGED_poll_so_a_failure_is_retried(
        self, transport, install_ceiling
    ):
        """They are best-effort, so a transient failure — the tailnet daemon busy for one
        cycle — would otherwise never be retried: the document does not change, every later
        poll returns UNCHANGED, and a control the ceiling forbids stays materialised until
        someone restarts the host."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        pd.register_post_install_hook(lambda: ran.append(1))

        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED
        assert ran == [1], "a confirming poll must re-derive materialised controls"

        pd.reset_fetch_window()
        assert pd.refresh_now().status == pd.REFRESH_UNCHANGED
        assert ran == [1, 1], "and again on the next one, which is what makes it a retry"

    def test_no_hook_runs_when_the_install_is_refused(
        self, transport, install_ceiling, monkeypatch
    ):
        """A rejected candidate materialises nothing, so there is nothing to re-derive."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(lambda: ran.append(1))
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )

        assert pd.refresh_now().status == pd.REFRESH_REJECTED
        assert ran == []

    def test_a_failing_hook_is_reported_as_a_governance_incident(
        self, transport, install_ceiling, monkeypatch, caplog
    ):
        """A swallowed failure is a control the ceiling calls for and the host is not applying,
        with nothing on any surface saying so. The mark is what puts it on ``security_posture``
        and the dashboard rather than leaving it in a log nobody reads."""
        marks: list = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        monkeypatch.setattr(
            pd, "mark_governance_incident", lambda kind, detail="": marks.append((kind, detail))
        )
        pd.register_post_install_hook(lambda: (_ for _ in ()).throw(OSError("disk full")))

        with caplog.at_level(logging.ERROR, logger="kiro_crew.platform.policy_distribution"):
            assert pd.refresh_now().status == pd.REFRESH_APPLIED

        assert any(k == "degraded" and "post_install_hook" in d for k, d in marks)
        assert any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), "a control not being applied is an error, not a warning"

    def test_a_failing_hook_does_not_unwind_the_install(self, transport, install_ceiling):
        """A control that cannot be re-derived must not stop the ceiling that WAS installed
        from being reported as installed — the refresh already succeeded. Rolling back would
        restore the OLD, looser ceiling: strictly less protection than the new one with a single
        materialised control stale, since the tightened ceiling still binds every call that
        control does not pre-approve."""
        ran: list[int] = []
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.register_post_install_hook(lambda: (_ for _ in ()).throw(RuntimeError("no daemon")))
        pd.register_post_install_hook(lambda: ran.append(1))

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        assert ran == [1], "a later hook must still run after an earlier one raised"


class TestAllowedToolsIsReDerivedWhenTheCeilingTightens:
    """``allowedTools`` is kiro-cli's blanket auto-approve list, and it is materialised: the
    writers consult the ceiling when they WRITE, and kiro-cli then reads the FILE. So a ceiling
    that comes to deny a tool mid-flight does not narrow a list already on disk, and every
    session started afterwards keeps auto-approving what the fleet now forbids — the tool
    short-circuits inside the harness and never reaches Kiro Crew's own PreToolUse gate.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's empty rebuild hold, moved verbatim; tracked for a de-flake pass
    def _no_hold_carried_in(self, monkeypatch):
        """``prime_ceiling_projection`` seeds nothing while the last real rebuild left a
        conductor spec unwritten; these tests pin the seeded baseline, so a hold a test
        elsewhere in this process left behind must not reach them."""
        from kiro_crew import agent as agent_mod

        monkeypatch.setattr(agent_mod, "_conductor_spec_held", False, raising=False)
        # Nor may an earlier test's rebuild answer seed the baseline these tests pin.
        monkeypatch.setattr(agent_mod, "_rebuild_answer_generation", None, raising=False)
        monkeypatch.setattr(agent_mod, "_rebuild_incomplete", False, raising=False)

    @staticmethod
    def _hook(monkeypatch):
        from kiro_crew import agent as agent_mod

        rebuilds: list[int] = []

        def fake_reporting(**kw):
            rebuilds.append(1)
            return Path("/shared/agents/kirocrew.json"), True

        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", fake_reporting)
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        monkeypatch.setattr(agent_mod, "_pending_projection_warned_generation", None, raising=False)
        return agent_mod, rebuilds

    def test_a_seeded_baseline_rebuilds_nothing_until_the_ceiling_moves(self, monkeypatch):
        """Boot has already projected under the current ceiling, and the baseline records
        that — before the poller starts, not on the hook's first call."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()
        for _ in range(3):
            agent_mod.reproject_for_ceiling_change()
        assert rebuilds == []

    def test_a_moved_ceiling_re_derives_the_config(self, monkeypatch, install_ceiling):
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()

        install_ceiling(governance.parse_policy(_doc("tightened")))
        agent_mod.reproject_for_ceiling_change()

        assert rebuilds == [1], "a ceiling swap must re-derive the on-disk auto-approvals"

    def test_a_ceiling_that_moved_before_the_first_call_is_not_missed(
        self, monkeypatch, install_ceiling
    ):
        """The reason the baseline is seeded at registration. The FIRST poll can itself
        install a new ceiling, and a first-call baseline would record that generation and skip
        the very rebuild it needed."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.prime_ceiling_projection()

        # The first poll installs a tightened ceiling, then the hook runs for the first time.
        install_ceiling(governance.parse_policy(_doc("tightened")))
        agent_mod.reproject_for_ceiling_change()

        assert rebuilds == [1]

    def test_a_failed_rebuild_is_retried_on_the_next_poll(self, monkeypatch, install_ceiling):
        """The memo advances only after a SUCCESSFUL rebuild. Marking the generation
        synchronised on failure loses the retry, leaving forbidden auto-approvals on disk for
        the process lifetime."""
        from kiro_crew import agent as agent_mod

        attempts: list[int] = []

        def flaky(**kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("could not write the agent config")
            return Path("/shared/agents/kirocrew.json"), True

        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", flaky)
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        agent_mod.prime_ceiling_projection()
        install_ceiling(governance.parse_policy(_doc("tightened")))

        with pytest.raises(OSError):
            agent_mod.reproject_for_ceiling_change()
        # The next confirming poll tries again, because the generation was never marked.
        agent_mod.reproject_for_ceiling_change()
        assert len(attempts) == 2

        # And once it succeeds, it stops.
        agent_mod.reproject_for_ceiling_change()
        assert len(attempts) == 2

    def test_a_declined_shared_home_holds_the_memo(self, monkeypatch, install_ceiling, caplog):
        """A REFUSED rebuild is not a successful one, and the memo must not say it was.

        ``rebuild_agent_config_reporting`` reports the shared-home refusal as
        ``wrote=False`` from the SAME evaluation that gated the write, so
        without this arm the hook would mark the moved generation synchronised
        while the on-disk ``allowedTools`` were never narrowed — stale entries
        then auto-approve tools the ceiling forbids, short-circuiting inside
        the harness before PreToolUse. The projection must instead stay
        pending and apply the moment the refusal clears. The pending state
        warns ONCE per generation, not on every confirming poll.
        """
        import logging

        from kiro_crew import agent as agent_mod

        rebuilds: list[int] = []
        declined = {"value": True}

        def fake_reporting(**kw):
            if declined["value"]:
                return Path("/shared/agents/kirocrew.json"), False
            rebuilds.append(1)
            return Path("/shared/agents/kirocrew.json"), True

        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", fake_reporting)
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        monkeypatch.setattr(agent_mod, "_pending_projection_warned_generation", None, raising=False)
        agent_mod.prime_ceiling_projection()
        install_ceiling(governance.parse_policy(_doc("tightened")))

        # While declined: the rebuild refuses and the memo stays behind, and
        # the pending state warns once for the generation, not per poll.
        with caplog.at_level(logging.WARNING, logger=agent_mod.logger.name):
            agent_mod.reproject_for_ceiling_change()
            agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [], "a declined instance must not commit the shared rewrite"
        pending_warnings = [r for r in caplog.records if "is pending" in r.getMessage()]
        assert len(pending_warnings) == 1, "the pending warning must fire once per generation"

        # The refusal clears (e.g. the stale shared spec was removed): the
        # pending generation is projected rather than lost.
        declined["value"] = False
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [1], "the pending generation must be projected once writable"

        # And once projected, it stops.
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [1]

    def test_the_hook_never_probes_the_guard_before_the_rebuild(self, monkeypatch, install_ceiling):
        """The refusal verdict comes from inside the rebuild, never a pre-probe.

        A guard probe followed by the rebuild leaves a window: a concurrent
        default-home boot rewrites the specs between the two reads, the
        rebuild's own check refuses, and a memo advanced on the stale probe
        records a projection that never landed — permanently retaining stale
        forbidden auto-approvals. The hook must consult only the rebuild's own
        verdict.
        """
        from kiro_crew import agent as agent_mod

        def no_probe(**kw):
            raise AssertionError("reproject pre-probed the guard: probe/rebuild race reopened")

        monkeypatch.setattr(agent_mod, "_decline_shared_agent_home", no_probe)
        monkeypatch.setattr(
            agent_mod,
            "rebuild_agent_config_reporting",
            lambda **kw: (Path("/shared/agents/kirocrew.json"), False),
        )
        monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        install_ceiling(governance.parse_policy(_doc("tightened")))

        # An unseeded baseline always attempts the rebuild; the refused verdict
        # must come back from the rebuild itself (no_probe proves no pre-probe
        # ran) and the memo must stay behind.
        agent_mod.reproject_for_ceiling_change()
        assert agent_mod._projected_ceiling_generation is None, "memo advanced on a refusal"

    def test_an_unseeded_baseline_rebuilds_rather_than_skipping(self, monkeypatch):
        """The safe direction if some other entry point starts the poller: a redundant
        rewrite costs a file write, a skipped one costs the tighten."""
        agent_mod, rebuilds = self._hook(monkeypatch)
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [1]

    def test_it_is_registered_as_a_post_install_hook(self):
        """The wiring, not just the function: the gateway registers it beside the tailnet
        revocation, before the poller starts."""
        from pathlib import Path as _Path

        from kiro_crew.slack.gateway_runtime import service_boot

        src = _Path(service_boot.__file__).read_text(encoding="utf-8")
        assert "register_post_install_hook(reproject_for_ceiling_change)" in src
        assert src.index("register_post_install_hook(reproject_for_ceiling_change)") < src.index(
            "await asyncio.to_thread(start_refresher)"
        ), "registered before the poller, so the first install already re-derives"


class TestTheCacheSwapComparesTheSourceToo:
    """Identical bytes can be published for a DIFFERENT source — a repoint whose document
    did not change — and a body-only swap would let an in-flight refresh overwrite that
    provenance with its own. The next boot's repoint rule then discards a perfectly good
    cache as not belonging to the source it resolves.
    """

    def test_the_pair_distinguishes_a_repoint_that_did_not_change_the_bytes(self):
        body = _body("central")
        pd.write_cache(body, source="https://old.corp.example/p.json", etag="v1")
        old_pair = pd._cache_pair(pd.read_cache_meta())

        pd.write_cache(body, source="https://new.corp.example/p.json", etag="v1")
        new_pair = pd._cache_pair(pd.read_cache_meta())

        assert old_pair[0] == new_pair[0], "the bytes are identical, by construction"
        assert old_pair != new_pair, "so only the source half can tell them apart"

    def test_a_swap_expecting_the_old_source_does_not_overwrite_the_repointed_cache(self):
        body = _body("central")
        pd.write_cache(body, source="https://old.corp.example/p.json", etag="v1")
        stale_expectation = pd._cache_pair(pd.read_cache_meta())

        # The repoint lands, with the same bytes under the new source.
        pd.write_cache(body, source="https://new.corp.example/p.json", etag="v2")

        published = pd.write_cache(
            body,
            source="https://old.corp.example/p.json",
            etag="v1",
            expect_pair=stale_expectation,
        )

        assert published is False, "the swap must lose"
        meta = pd.read_cache_meta()
        assert meta is not None
        assert meta.source_digest == pd._source_digest("https://new.corp.example/p.json")
        assert meta.etag == "v2", "the repointed provenance must survive intact"

    def test_no_cache_is_the_empty_pair(self):
        assert pd._cache_pair(None) == ("", "")


class TestBootCannotRollTheCeilingBackwardEither:
    """The live refresh has this guard; boot is not exempt. A slow boot fetch of v2
    racing a ``policy fetch --force`` that publishes v3 would overwrite the cache with v2 and
    install it — rolling the ceiling backward to a possibly looser document on the one path
    where nothing is running yet to notice.
    """

    @staticmethod
    def _race(transport, monkeypatch, *, racer: bytes):
        """A boot fetch of v2 with *racer* published during the fetch."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            pd.write_cache(racer, source=source, etag="v3")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)
        return source

    def test_the_winner_is_adopted_not_overwritten(self, transport, monkeypatch):
        source = self._race(transport, monkeypatch, racer=_body("v3"))

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert (
            ceiling is not None and ceiling.identity_issuer == "v3"
        ), "boot must install the newer document, not the one it fetched"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body(
            "v3"
        ), "and must not overwrite it with the older bytes"

    def test_an_uncontended_boot_still_installs_what_it_fetched(self, transport):
        """The control: with no racer the swap passes and boot behaves as before."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)

        ceiling = pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert ceiling is not None and ceiling.identity_issuer == "v2"
        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("v2")

    def test_a_winner_this_host_cannot_run_is_refused_not_installed(self, transport, monkeypatch):
        """ "Someone else published it" is not evidence this host can run under it, so the
        winner goes through the same gates the fetched document does."""
        source = self._race(transport, monkeypatch, racer=_body("v3"))
        # Selective: v2 must still clear the gate, or the boot short-circuits into the
        # older cache before the race is ever reached and the test proves nothing.
        real_validate = pd.validate_ceiling

        def refuse_only_the_winner(ceiling):
            if ceiling.identity_issuer == "v3":
                raise PlatformCompositionError("a profile is looser")
            return real_validate(ceiling)

        monkeypatch.setattr(pd, "validate_ceiling", refuse_only_the_winner)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "another process cached" in str(caught.value)

    def test_a_winner_cached_against_a_DIFFERENT_source_is_refused(self, transport, monkeypatch):
        """The CAS loss is exactly what makes this reachable: the winner was published by
        another process, which may have been configured for a different source. Re-gating parse,
        migration and composition but not provenance would let source B's ceiling govern a host
        pointed at source A."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("v2"), etag="v2")))
        pd.write_cache(_body("v1"), source=source, etag="v1", now=time.time() - 100_000)
        real_fetch = pd.fetch_once

        def fetch_then_race(dist, cached=None):
            result = real_fetch(dist, cached)
            # Another process, pointed elsewhere, publishes its own ceiling.
            pd.write_cache(_body("elsewhere"), source="https://other.corp.example/p.json")
            return result

        monkeypatch.setattr(pd, "fetch_once", fetch_then_race)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert "different source" in str(caught.value)

    def test_a_rejected_winner_leaves_the_host_UNgoverned_not_flagged(self, transport, monkeypatch):
        """``central_ceiling_installed`` is what the gateway flags an app backend cache-only on.
        Recording the fetched bytes before the publish resolved meant a lost swap whose winner
        was then REJECTED left the host degraded while every child was told to resolve its
        ceiling from a cache holding the document this host had just refused."""
        source = self._race(transport, monkeypatch, racer=_body("v3"))
        real_validate = pd.validate_ceiling

        def refuse_only_the_winner(ceiling):
            if ceiling.identity_issuer == "v3":
                raise PlatformCompositionError("a profile is looser")
            return real_validate(ceiling)

        monkeypatch.setattr(pd, "validate_ceiling", refuse_only_the_winner)

        with pytest.raises(PlatformCompositionError):
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))

        assert (
            not pd.central_ceiling_installed()
        ), "a refused winner must not leave children resolving from the cache that holds it"

    def test_a_winner_that_moves_the_source_is_refused_too(self, transport, monkeypatch):
        moved = _body("v3", distribution={"source": "https://elsewhere.corp.example/p.json"})
        source = self._race(transport, monkeypatch, racer=moved)

        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert pd.POLICY_URL_ENV in str(caught.value)


class TestARejectedPushIsNotLeftAsTheLastKnownGood:
    """The publish is confirmed BEFORE the install, so the bytes are on disk before
    ``apply_ceiling`` has had its say — and that step refuses for reasons the earlier
    validation cannot see (a profile, the trust root, or a tier-1 pin that moved between
    the two). This module promises a document failing a mandated check never becomes what
    the next boot adopts, and every cache-only child adopts it too.
    """

    def test_the_previous_document_is_restored_when_the_install_refuses(
        self, transport, install_ceiling, monkeypatch
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        pd.write_cache(_body("good"), source=source, etag="v1")

        # Refuse at the INSTALL, after the publish has already happened.
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        cached = pd.read_cache()
        assert cached is not None
        assert cached.body == _body(
            "good"
        ), "a rejected push must not survive as the document the next boot adopts"

    def test_the_restored_age_is_the_last_successful_fetch_not_this_failure(
        self, transport, install_ceiling, monkeypatch
    ):
        """Otherwise a failing refresh would keep resetting the staleness clock, and a
        fleet's ``max_cache_age_secs`` would never notice a source that stopped working."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("good"), source=source, etag="v1", now=time.time() - 5_000)

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        pd.refresh_now()

        cached = pd.read_cache()
        assert cached is not None and cached.body == _body("good")
        assert cached.age_secs() > 4_000, "the restore must not restart the staleness clock"

    def test_with_nothing_to_restore_the_rejected_bytes_are_REMOVED(
        self, transport, install_ceiling, monkeypatch
    ):
        """The first-refresh case, and the answer is deletion rather than "leave it".

        Leaving them is not the same trade-off as keeping a stale-but-composable copy:
        these are the bytes this host just REFUSED. Kept, the next boot serves them from
        cache instead of re-fetching, so a source the administrator has since corrected
        does not reach this host until the window expires — and a cache-only child adopts
        the refused ceiling meanwhile. With no cache, boot fetches: it either gets the fix
        or fails exactly as it would have anyway.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        running = install_ceiling(
            governance.parse_policy(_doc("running", distribution={"source": source}))
        )
        assert pd.read_cache() is None, "the premise: nothing cached for this source yet"
        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_REJECTED
        assert current_context().governance is running
        assert pd.read_cache() is None, "a refused document must not be the last-known-good"

    def test_a_cache_from_another_source_is_not_a_rollback_target(
        self, transport, install_ceiling, monkeypatch
    ):
        """Restoring it would re-cache a retired endpoint's document under the new
        endpoint's name, which is what the repoint rule exists to refuse."""
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"), etag="v2")))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd.write_cache(_body("retired"), source="https://retired.corp.example/p.json", etag="v0")

        monkeypatch.setattr(
            pd, "apply_ceiling", lambda c: (_ for _ in ()).throw(RuntimeError("profile floor"))
        )
        pd.refresh_now()

        # Not restored, and not left in place either: a retired endpoint's document is not
        # this source's last-known-good, so there is nothing to roll back TO and the
        # refused bytes go the same way as in the no-prior-copy case above.
        cached = pd.read_cache()
        assert cached is None or cached.body != _body(
            "retired"
        ), "a retired source's copy must never become this source's last-known-good"
