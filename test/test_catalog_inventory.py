"""The catalog as the shelf: inventory rows, and installs that honour the pin.

These cover the seam this change opens. The one that matters most is
``test_a_pinned_entry_never_falls_back_to_a_branch``: every other failure here is
loud, and that one is the only way this feature can fail while reporting success.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from kiro_crew.apps import official_catalog as oc
from kiro_crew.apps import registry as reg

SHA = "a" * 40
OTHER_SHA = "b" * 40
URL = "https://github.com/org/demo-app"


def catalog_git(name: str = "demo-app", **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "source": {"type": "git", "url": URL, "ref": SHA},
        "version": "1.2.3",
        "displayName": "Demo App",
        "summary": "Does the demo thing.",
        "tags": ["dev"],
        "author": {"name": "Demo Labs"},
    }
    entry.update(over)
    return entry


class TestInventory:
    def test_a_git_entry_becomes_an_installable_row(self):
        (row,) = oc.inventory([catalog_git()])
        assert row["name"] == "demo-app"
        assert row["gitUrl"] == URL
        assert row["repo"] == URL, "the legacy alias carries the same value"
        assert row["commit"] == SHA
        assert row["_catalog"] is True

    def test_the_row_carries_the_catalogs_version(self):
        """Update availability belongs to the store, not to the app's branch tip.

        `_enrich_with_install_status` compares this against the installed version,
        so a row without it reads as "never any update".
        """
        (row,) = oc.inventory([catalog_git()])
        assert row["version"] == "1.2.3"

    def test_the_row_carries_display_fields_so_no_manifest_fetch_is_needed(self):
        (row,) = oc.inventory([catalog_git()])
        assert row["displayName"] == "Demo App"
        assert row["description"] == "Does the demo thing.", "summary lands on description"
        assert row["tags"] == ["dev"]

    def test_no_author_is_emitted(self):
        """`list_registry` snapshots `_index_author = entry["author"]`
        unconditionally, and `_apply_trust_fields` derives the first-party badge
        from that snapshot -- so an author here is a path from an unsigned document
        to the badge. `annotate` sets it for display AFTER the snapshot."""
        (row,) = oc.inventory([catalog_git()])
        assert "author" not in row

    @pytest.mark.parametrize(
        "name",
        [
            "../../../etc/passwd",  # traversal into paths this client owns
            "..",
            "/abs",
            "a/b",  # a name becomes ONE directory segment
            "a\\b",
            "Upper",  # the contract is a lowercase slug
            "-leading",
            "trailing-",
            "with space",
            "demo-app\n",  # trailing newline, the \Z-vs-$ class
            "",
            "x" * 65,
        ],
    )
    def test_a_name_that_is_not_a_slug_is_dropped(self, name):
        """The name reaches `app_source_dir(name)` and the persistent app data
        root, so it is a filesystem path before it is a label."""
        assert oc.inventory([catalog_git(name=name)]) == []

    def test_a_builtin_entry_produces_no_row(self):
        """Its code ships in the wheel and is discovered from disk. A row here
        would render an install control for something registry install cannot do."""
        assert oc.inventory([{"name": "mochi", "source": {"type": "builtin"}}]) == []

    def test_no_branch_is_emitted(self):
        """The absence is the guard. `install_from_registry` defaults `branch` to
        "main", so a row carrying one would let a pin-ignoring path succeed
        against the wrong bytes."""
        (row,) = oc.inventory([catalog_git()])
        assert "branch" not in row

    def test_no_registry_marker_is_emitted(self):
        """`_registry` means "external": it flips provenance, drops `verified`,
        strips `featured`, and makes `annotate` skip the row entirely."""
        (row,) = oc.inventory([catalog_git()])
        assert "_registry" not in row

    def test_no_index_author_is_emitted(self):
        """The catalog's signature is not verified yet, so its curated author must
        not mint the verified badge -- the same restraint `annotate` already has."""
        (row,) = oc.inventory([catalog_git()])
        assert "_index_author" not in row

    @pytest.mark.parametrize(
        "source",
        [
            {"type": "git", "url": URL},  # no pin at all
            {"type": "git", "url": URL, "ref": "main"},  # a branch where a pin belongs
            {"type": "git", "url": URL, "ref": SHA + "\n"},  # trailing newline
            {"type": "git", "url": URL, "ref": "A" * 40},  # uppercase hex
            {"type": "git", "url": "http://x/a", "ref": SHA},  # not https
            {"type": "git", "url": "ext::sh -c id", "ref": SHA},  # executes
            {"type": "git", "url": "file:///etc", "ref": SHA},  # reads local paths
            {"type": "git", "url": URL + "\n", "ref": SHA},  # trailing newline in url
            {"type": "git", "url": URL, "ref": SHA, "subdir": "../etc"},  # traversal
            {"type": "git", "url": URL, "ref": SHA, "subdir": "apps/..\n"},  # hidden by \n
            {"type": "git", "url": URL, "ref": SHA, "subdir": "/abs"},  # absolute
        ],
    )
    def test_a_row_with_unusable_coordinates_is_dropped_not_repaired(self, source):
        """Dropped, never repaired. Guessing a branch when the pin is malformed
        installs bytes nobody attested, which is what the pin prevents."""
        assert oc.inventory([{"name": "demo-app", "source": source}]) == []

    def test_hostile_field_types_drop_the_field_not_the_row(self):
        entry = catalog_git(version=5, displayName=[], tags="devops", author="Labs")
        (row,) = oc.inventory([entry])
        assert "version" not in row
        assert "displayName" not in row
        assert "tags" not in row, "a bare string must not become one tag per character"
        assert row["commit"] == SHA, "the row itself survives"


class TestInstallCoordinatesDoNotTrustTheCache:
    """The cache under the data home is NOT a sensitive path, so the agent's own
    file tools can write it. That is harmless for display copy and unacceptable for
    install coordinates: app trust is keyed by NAME, so whoever controls the
    name-to-URL binding controls what a trusted name installs.
    """

    def test_the_cache_is_writable_by_the_agent(self):
        """The premise, asserted rather than assumed -- if this ever becomes False
        the reasoning below can be revisited, and the test says so out loud."""
        from kiro_crew import security
        from kiro_crew.config.loader import config_dir

        cache = config_dir() / "cache" / "official-catalog.json"
        assert security.is_sensitive_write_path(str(cache)) is False
        assert security.is_sensitive_write_path(str(config_dir() / "security_policy.json")) is True

    def test_install_lookup_refetches_and_ignores_a_poisoned_cache(self, monkeypatch):
        poisoned = {
            "schemaVersion": 1,
            "apps": [{
                "name": "demo-app",
                "source": {"type": "git", "url": "https://evil.example/x", "ref": OTHER_SHA},
            }],
        }
        honest = {
            "schemaVersion": 1,
            "apps": [catalog_git()],
        }
        monkeypatch.setattr(oc, "_read_cache", lambda: poisoned)
        monkeypatch.setattr(oc, "fetch_document", lambda url: honest)

        row = oc.inventory_for_install("demo-app")
        assert row is not None
        assert row["gitUrl"] == URL, "the freshly fetched document decides the URL"
        assert row["commit"] == SHA

    def test_a_remembered_failure_skips_the_fetch(self, monkeypatch):
        """The module remembers a failed fetch so the next caller does not wait again;
        the fresh path bypassed that, so every listing during an outage paid a full
        HTTPS timeout."""
        calls: list[str] = []

        def counting_fetch(url, *a, **k):
            calls.append(url)
            return None

        monkeypatch.setattr(oc, "fetch_document", counting_fetch)
        monkeypatch.setattr(oc, "_read_cache", lambda: {oc._FAILED_KEY: 1.0})
        with pytest.raises(oc.CatalogUnavailable):
            oc.fetch_inventory_entries()
        assert calls == [], "a remembered failure must not be retried"

    def test_the_memory_can_only_refuse_earlier_never_answer(self, monkeypatch):
        """Reading the agent-writable memory is safe only in this direction: clearing it
        buys one real fetch attempt, which is the same fail-closed answer."""
        monkeypatch.setattr(oc, "fetch_document", lambda url, *a, **k: None)
        monkeypatch.setattr(oc, "_read_cache", lambda: None)  # memory cleared
        monkeypatch.setattr(oc, "_write_failure", lambda: None)
        with pytest.raises(oc.CatalogUnavailable):
            oc.fetch_inventory_entries()

    def test_install_lookup_refuses_when_the_document_cannot_be_fetched(self, monkeypatch):
        """Refusing beats falling back to a local copy that may have been edited.

        An app whose coordinates cannot be confirmed right now must not be installed
        from bytes an attacker could have chosen.

        It RAISES rather than returning None: the caller reads None as "not in the
        catalog", which is its licence to use the unpinned bundled coordinates. A
        refusal that looks like an absence is not a refusal.
        """
        monkeypatch.setattr(oc, "_read_cache", lambda: {"schemaVersion": 1, "apps": [catalog_git()]})
        monkeypatch.setattr(oc, "fetch_document", lambda url: None)
        with pytest.raises(oc.CatalogUnavailable):
            oc.inventory_for_install("demo-app")

    @pytest.mark.parametrize(
        "doc",
        [
            {"schemaVersion": 2, "apps": [catalog_git()]},  # unknown schema
            {"schemaVersion": 1, "apps": [catalog_git()], "removed": [{"name": "x"}]},
            {"schemaVersion": 1, "apps": "not-a-list"},
            {"schemaVersion": 1},
        ],
    )
    def test_install_lookup_applies_the_same_envelope_gates(self, monkeypatch, doc):
        """A fresh fetch is not a licence to skip the checks the cached path makes.

        Each of these is "I could not interpret the catalog", never "the app is
        absent from it", so each raises.
        """
        monkeypatch.setattr(oc, "fetch_document", lambda url: doc)
        with pytest.raises(oc.CatalogUnavailable):
            oc.inventory_for_install("demo-app")


class TestCredentialPosture:
    def test_a_catalog_row_takes_the_credential_free_posture(self):
        """A catalog URL is remote-controlled content, not a repo the owner typed.

        `index_originated` drives the clone's credential posture. Treating "no
        `_registry`" as "owner-designated" was true while the only marker-less rows
        came from the wheel's bundled seed; a catalog row is not that, and a
        repointed row on a trusted forge would otherwise be cloned with the
        gateway's ambient git/ssh identity.
        """
        assert reg._remote_controlled_url({"_catalog": True}) is True
        assert reg._remote_controlled_url({"_registry": "acme"}) is True
        assert reg._remote_controlled_url({}) is False, "the bundled seed stays owner-designated"

    def test_a_catalog_row_is_still_an_official_entry(self):
        """Credential posture and officialness are different questions, and the
        catalog row is the case that separates them: credential-free, yet an app we
        list -- so its install receipt must still fire."""
        assert reg._official_entry({"_catalog": True}) is True
        assert reg._official_entry({"_registry": "acme"}) is False
        assert reg._official_entry({}) is True


class TestPinnedFetchNeverEatsUserData:
    @pytest.mark.asyncio
    async def test_a_destination_that_is_not_a_checkout_is_refused_not_deleted(self, tmp_path):
        """The first draft read "no .git directory" as "I am creating this", so a
        plain directory of the user's files was deleted when a fetch failed."""
        dest = tmp_path / "app-source"
        dest.mkdir()
        (dest / "important.txt").write_text("user data", encoding="utf-8")
        log: list[str] = []

        result = await reg._git_fetch_commit(
            "https://example.com/a.git", SHA, dest, log,
            clone_env={}, sandbox_mode="standard",
        )
        assert result is not None and result["error"] == "destination_not_a_checkout"
        assert (dest / "important.txt").read_text(encoding="utf-8") == "user data"

    @pytest.mark.asyncio
    async def test_a_git_file_rather_than_directory_is_also_refused(self, tmp_path):
        """A worktree link stores `.git` as a FILE, so an `is_dir()` test alone
        classifies a real checkout as fresh."""
        dest = tmp_path / "linked"
        dest.mkdir()
        (dest / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        log: list[str] = []

        result = await reg._git_fetch_commit(
            "https://example.com/a.git", SHA, dest, log,
            clone_env={}, sandbox_mode="standard",
        )
        assert result is not None and result["error"] == "destination_not_a_checkout"
        assert (dest / ".git").is_file(), "the link was left alone"

    @pytest.mark.asyncio
    async def test_a_spawn_failure_after_init_leaves_nothing_behind(self, tmp_path, monkeypatch):
        """The exit three rounds of patches all missed.

        Cleanup used to sit on each failure BRANCH, so an exception between
        `git init` and `git remote add` bypassed every one of them and left a `.git`
        directory with no origin -- which then wedges each later attempt on
        `unreadable_clone_origin`, a fail-closed path that deliberately does not
        clean up after itself. Cleanup now belongs to the destination's lifetime.
        """
        dest = tmp_path / "app-source"
        calls: list[list[str]] = []

        async def flaky(*argv, **kwargs):
            calls.append(list(argv))
            if any("remote" in str(a) for a in argv):
                raise OSError("simulated spawn failure")
            (dest / ".git").mkdir(parents=True, exist_ok=True)

            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return _P()

        monkeypatch.setattr(reg, "create_subprocess_limited", flaky)
        monkeypatch.setattr(reg, "wrap_argv", lambda argv, mode=None: (argv, None))
        monkeypatch.setattr(reg, "cgroup_scope_argv", lambda argv: argv)

        with pytest.raises(OSError):
            await reg._git_fetch_commit(
                "https://example.com/a.git", SHA, dest, [],
                clone_env={}, sandbox_mode="standard",
            )
        assert not dest.exists(), "a directory this call created must not survive a raise"

    @pytest.mark.asyncio
    async def test_cancellation_after_init_leaves_nothing_behind(self, tmp_path, monkeypatch):
        """Same invariant, different exit: `finally` covers cancellation too, which a
        per-branch `if` never can."""
        dest = tmp_path / "app-source"

        async def cancelling(*argv, **kwargs):
            if any("remote" in str(a) for a in argv):
                raise asyncio.CancelledError()
            (dest / ".git").mkdir(parents=True, exist_ok=True)

            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return _P()

        monkeypatch.setattr(reg, "create_subprocess_limited", cancelling)
        monkeypatch.setattr(reg, "wrap_argv", lambda argv, mode=None: (argv, None))
        monkeypatch.setattr(reg, "cgroup_scope_argv", lambda argv: argv)

        with pytest.raises(asyncio.CancelledError):
            await reg._git_fetch_commit(
                "https://example.com/a.git", SHA, dest, [],
                clone_env={}, sandbox_mode="standard",
            )
        assert not dest.exists()


class TestPinIsACatalogMechanism:
    """An external index may not reach a commit its configured branch cannot.

    `commit` is on the row-projection allowlist, so an external registry's index --
    untrusted JSON -- can set it. A fetch BY SHA reaches objects no branch contains,
    so honouring an index-supplied pin would let the index escape the owner-
    configured `branch` and get arbitrary code built and `onInstall`-executed.
    """

    @pytest.mark.asyncio
    async def test_external_registry_pin_is_not_honoured(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def capture(git_url, branch, dest, log_lines, *, commit="", **kwargs):
            seen["branch"] = branch
            seen["commit"] = commit
            return {"ok": False, "name": "x", "error": "stopped after coordinates"}

        monkeypatch.setattr(reg, "_git_clone_or_pull", capture)
        monkeypatch.setattr(reg, "app_execution_denied", lambda *a, **k: None)
        monkeypatch.setattr(
            reg,
            "get_registry_app",
            lambda name: {
                "name": "evil",
                "gitUrl": "https://example.com/e.git",
                "repo": "o/e",
                "branch": "main",
                # An index naming a commit that no branch contains.
                "commit": SHA,
                "_registry": "some-external-registry",
                "_catalog": True,  # index-settable, so it must not be enough
            },
        )

        await reg.install_from_registry("evil")
        assert seen["commit"] == "", "an index-supplied pin must not reach the fetch"
        assert seen["branch"] == "main", "the configured branch still bounds the install"

    @pytest.mark.asyncio
    async def test_catalog_pin_is_still_honoured(self, monkeypatch):
        """The mirror case: gating the read must not disable the feature."""
        seen: dict[str, Any] = {}

        async def capture(git_url, branch, dest, log_lines, *, commit="", **kwargs):
            seen["commit"] = commit
            return {"ok": False, "name": "x", "error": "stopped after coordinates"}

        monkeypatch.setattr(reg, "_git_clone_or_pull", capture)
        monkeypatch.setattr(reg, "app_execution_denied", lambda *a, **k: None)
        monkeypatch.setattr(
            reg,
            "get_registry_app",
            lambda name: {
                "name": "good",
                "gitUrl": "https://example.com/g.git",
                "repo": "o/g",
                "commit": SHA,
                "_catalog": True,  # and no `_registry`
            },
        )

        await reg.install_from_registry("good")
        assert seen["commit"] == SHA


async def _async_passthrough(entry: Any) -> Any:
    """Stand in for `_resolve_manifest`, returning the row unchanged."""
    return entry


def _async_return(value: Any):
    """A stand-in coroutine function returning *value*, ignoring its arguments."""

    async def _inner(*args: Any, **kwargs: Any) -> Any:
        return value

    return _inner


class TestSeedCollisionKeepsThePin:
    """Resolution must DELIVER the pin, not merely be capable of honouring it.

    Both git catalog entries are also seed entries, so a collision rule favouring
    the seed made the pin unreachable for 100% of the apps that have one -- while
    every component-level test of the fetch still passed.
    """

    def test_install_lookup_prefers_the_catalog_row_for_the_same_repo(self, monkeypatch):
        monkeypatch.setattr(
            reg,
            "_load_registry_file",
            lambda: [{"name": "dup", "gitUrl": URL, "repo": URL, "branch": "main"}],
        )
        monkeypatch.setattr(
            reg.official_catalog,
            "inventory_for_install",
            lambda name: {
                "name": "dup",
                # Cosmetically different, same repository.
                "gitUrl": URL.rstrip("/") + ".git",
                "repo": URL,
                "commit": SHA,
                "_catalog": True,
            },
        )
        row = reg.get_registry_app("dup")
        assert row is not None
        assert row.get("commit") == SHA, "the seed row must not shadow the pin"
        assert reg._is_catalog_row(row), "and the row must still be pin-honouring"

    def test_a_catalog_row_naming_another_repo_does_not_replace_the_seed(self, monkeypatch):
        """The security half: app trust is keyed by name, so a same-name row from a
        different repository must not stand in for the bundled one."""
        seed = {"name": "dup", "gitUrl": URL, "repo": URL, "branch": "main"}
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [seed])
        monkeypatch.setattr(
            reg.official_catalog,
            "inventory_for_install",
            lambda name: {
                "name": "dup",
                "gitUrl": "https://example.com/somewhere-else",
                "repo": "o/other",
                "commit": SHA,
                "_catalog": True,
            },
        )
        row = reg.get_registry_app("dup")
        assert row == seed, "a different repository must not supersede the seed"

    def test_candidates_put_the_pinned_row_first(self, monkeypatch):
        monkeypatch.setattr(
            reg,
            "_load_registry_file",
            lambda: [{"name": "dup", "gitUrl": URL, "repo": URL, "branch": "main"}],
        )
        monkeypatch.setattr(
            reg.official_catalog,
            "inventory_for_install",
            lambda name: {
                "name": "dup",
                "gitUrl": URL,
                "repo": URL,
                "commit": SHA,
                "_catalog": True,
            },
        )
        monkeypatch.setattr(reg, "_read_external_registry_cache", lambda *a, **k: None)
        candidates = reg._registry_app_candidates("dup")
        assert candidates[0].get("commit") == SHA, "the pinned row must be resolved first"


class TestPinnedInstallNeverReusesAnExistingTree:
    """The pin means "these bytes", so the tree the build sees must be one this
    call fetched -- not one it inspected.

    No cleanliness check can substitute: `git status` never reports `.git/`
    content, so an added `.git/hooks/post-checkout` is invisible to every variant
    of it, and an untracked `sitecustomize.py` executes on interpreter start.
    """

    @pytest.mark.asyncio
    async def test_an_existing_checkout_is_moved_aside_not_reused(self, tmp_path, monkeypatch):
        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)
        # Content no `git status` variant would report as a modification.
        hooks = dest / ".git" / "hooks"
        hooks.mkdir()
        (hooks / "post-checkout").write_text("#!/bin/sh\necho pwned\n")
        (dest / "sitecustomize.py").write_text("import os\n")

        fetched: dict[str, Any] = {}

        async def fake_fetch(git_url, commit, d, log_lines, **kwargs):
            # The fetch must see an ABSENT destination: that is what makes the
            # tree fresh, and what keeps `git init --template=` hook-free.
            fetched["dest_exists"] = d.exists()
            fetched["commit"] = commit
            return None

        monkeypatch.setattr(reg, "_clone_origin_url", _async_return(URL))
        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)

        result = await reg._git_clone_or_pull(URL, "main", dest, [], commit=SHA)
        assert result is None
        assert fetched["commit"] == SHA
        assert fetched["dest_exists"] is False, "the pinned fetch must get a fresh destination"
        # Nothing was destroyed: the old tree survives beside it for recovery.
        aside = [p for p in tmp_path.iterdir() if p.name.startswith("source.stale-")]
        assert len(aside) == 1, "the old checkout is moved aside, never deleted"
        assert (aside[0] / ".git" / "hooks" / "post-checkout").exists()

    @pytest.mark.asyncio
    async def test_a_checkout_already_at_the_pin_is_still_not_reused(self, tmp_path, monkeypatch):
        """HEAD equality is no longer consulted at all -- it described placement,
        never contents."""
        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)
        called: list[str] = []

        async def fake_fetch(git_url, commit, d, log_lines, **kwargs):
            called.append(commit)
            return None

        monkeypatch.setattr(reg, "_clone_origin_url", _async_return(URL))
        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)
        # Even reporting the exact pin must not short-circuit the fetch.
        monkeypatch.setattr(reg, "_resolved_clone_commit", lambda p: SHA)

        result = await reg._git_clone_or_pull(URL, "main", dest, [], commit=SHA)
        assert result is None
        assert called == [SHA], "a pinned install always fetches"

    @pytest.mark.asyncio
    async def test_a_failed_move_aside_refuses_instead_of_building(self, tmp_path, monkeypatch):
        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)

        async def _must_not_fetch(*a, **k):
            raise AssertionError("a refused move-aside must not reach the fetch")

        monkeypatch.setattr(reg, "_clone_origin_url", _async_return(URL))
        monkeypatch.setattr(reg, "_git_fetch_commit", _must_not_fetch)
        monkeypatch.setattr(reg, "_move_checkout_aside", _async_return(None))

        result = await reg._git_clone_or_pull(URL, "main", dest, [], commit=SHA)
        assert result is not None
        assert result["error"] == "existing_checkout_not_moved_aside"
        assert dest.exists(), "a checkout we could not move is left untouched"

    @pytest.mark.asyncio
    async def test_move_aside_never_deletes_on_rename_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "source"
        dest.mkdir()
        (dest / "user-file.txt").write_text("keep me")

        def boom(*a, **k):
            raise OSError("cross-device link")

        monkeypatch.setattr(pathlib.Path, "rename", boom)
        aside = await reg._move_checkout_aside(dest, [])
        assert aside is None
        assert (dest / "user-file.txt").read_text() == "keep me"


class TestCatalogFailureNeverDowngradesToAnUnpinnedSeed:
    """"The catalog says there is no pin" and "I could not ask the catalog" are
    different answers; collapsing them let a network failure install a branch tip
    for an app the store presents as pinned."""

    def _seed_only(self, monkeypatch, raising: bool):
        """Drive the REAL failure shape, not a hand-made one.

        The first version of this helper patched `inventory_for_install` to raise --
        a shape the real function never produced, because it returned None for every
        failure. The test passed while the defect it described stayed live. So the
        stub goes at the actual boundary: `fetch_document`, which is what a CDN
        outage breaks.
        """
        seed = {"name": "dup", "gitUrl": URL, "repo": URL, "branch": "main"}
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [seed])

        def fetch(url, *a, **k):
            if raising:
                return None  # what an unreachable catalog actually yields
            return {
                "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
                "apps": [],  # a valid catalog that simply does not name "dup"
            }

        monkeypatch.setattr(reg.official_catalog, "fetch_document", fetch)
        monkeypatch.setattr(reg, "_read_external_registry_cache", lambda *a, **k: None)
        return seed

    def test_an_unreachable_catalog_raises_rather_than_reporting_absence(self, monkeypatch):
        """The source-level half: `None` must mean "not in the catalog", nothing else.

        While every failure returned None, the caller's split below could not fire at
        all -- so this assertion is what makes the rest of the class meaningful.
        """
        self._seed_only(monkeypatch, raising=True)
        with pytest.raises(reg.official_catalog.CatalogUnavailable):
            reg.official_catalog.inventory_for_install("dup")

    def test_a_valid_catalog_without_the_name_returns_none(self, monkeypatch):
        self._seed_only(monkeypatch, raising=False)
        assert reg.official_catalog.inventory_for_install("dup") is None

    def test_a_failed_catalog_lookup_refuses_the_seed(self, monkeypatch):
        self._seed_only(monkeypatch, raising=True)
        row, reason = reg._resolve_registry_row("dup")
        assert row is None, "an unpinned seed must not stand in for an unknown pin"
        assert "could not be reached" in reason

    def test_an_authoritative_no_catalog_row_still_uses_the_seed(self, monkeypatch):
        """The other half: refusing on a successful "no row" would break every
        seed-only app for no security gain."""
        seed = self._seed_only(monkeypatch, raising=False)
        row, reason = reg._resolve_registry_row("dup")
        assert row == seed
        assert reason == ""

    def test_the_install_path_reports_why_it_refused(self, monkeypatch):
        self._seed_only(monkeypatch, raising=True)
        monkeypatch.setattr(reg, "get_app", lambda name: {})
        entry, error = reg._resolve_install_entry("dup")
        assert entry is None
        # Assert the substance, not a phrase: the user must learn the catalog could
        # not be consulted, rather than that the app does not exist.
        assert "could not be reached" in error
        assert "not found" not in error, (
            "a bare 'not found' would send the user looking for a missing app "
            "instead of a network failure"
        )

    def test_candidates_drop_unpinned_seed_rows_on_failure(self, monkeypatch):
        """A seed candidate names the SAME repo as the catalog row it shadows, so
        provenance-pinned resolution would accept it and deliver a branch tip."""
        self._seed_only(monkeypatch, raising=True)
        assert reg._registry_app_candidates("dup") == []


class TestPinnedManifestFetchReachesTheGates:
    """The admission and platform-compatibility gates read this manifest.

    No test covered this path, so a destination that `_git_fetch_commit` refuses --
    `tmp_root` itself, which already exists and is not a checkout -- made every
    pinned manifest fetch fail silently. `manifest = None` then let the platform and
    minClientVersion checks fall back to permissive defaults before the build runs.
    """

    @pytest.mark.asyncio
    async def test_the_pinned_fetch_gets_a_destination_it_can_create(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_fetch(git_url, commit, dest, log_lines, **kwargs):
            seen["dest"] = dest
            seen["existed"] = dest.exists()
            seen["parent_exists"] = dest.parent.exists()
            (dest).mkdir(parents=True, exist_ok=True)
            (dest / "app.json").write_text('{"name": "demo", "displayName": "Demo"}')
            return None

        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)
        manifest = await reg._fetch_app_manifest(
            "o/demo", "main", "", app_name="demo", git_url=URL, commit=SHA
        )
        assert seen["existed"] is False, (
            "_git_fetch_commit refuses a destination that exists but is not a checkout"
        )
        assert seen["parent_exists"] is True, "the temp root itself is still created for it"
        assert manifest is not None, "the gates must actually receive a manifest"
        assert manifest.get("displayName") == "Demo"

    @pytest.mark.asyncio
    async def test_a_pinned_subdirectory_is_read_from_the_fetched_root(self, monkeypatch):
        """Containment is measured from where the tree landed, not from the temp root."""

        async def fake_fetch(git_url, commit, dest, log_lines, **kwargs):
            (dest / "nested").mkdir(parents=True)
            (dest / "nested" / "app.json").write_text('{"name": "demo", "version": "9.9.9"}')
            return None

        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)
        manifest = await reg._fetch_app_manifest(
            "o/demo", "main", "nested", app_name="demo", git_url=URL, commit=SHA
        )
        assert manifest is not None and manifest.get("version") == "9.9.9"


class TestUrlEquivalenceDoesNotRebindNames:
    """`_same_git_target` decides whether a catalog row may stand in for a bundled
    app, and app trust is keyed by name -- so a false "same target" IS the rebinding
    that requiring URL equality exists to prevent."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("https://example.com/Owner/Repo", "https://example.com/owner/repo"),
            ("https://example.com/o/Repo", "https://example.com/o/repo"),
            ("https://example.com/O/r", "https://example.com/o/r"),
        ],
    )
    def test_path_case_is_not_folded(self, a, b):
        assert not reg._same_git_target(a, b), "repository paths are case-sensitive"

    @pytest.mark.parametrize(
        "a,b",
        [
            ("https://example.com/o/r", "https://example.com/o/r.git"),
            ("https://example.com/o/r/", "https://example.com/o/r"),
            ("HTTPS://EXAMPLE.COM/o/r", "https://example.com/o/r"),
            ("https://Example.Com/o/r.git/", "https://example.com/o/r"),
        ],
    )
    def test_genuinely_cosmetic_variance_still_matches(self, a, b):
        """The opposite failure mode: too strict here and the pin never applies,
        which is the round-6 defect all over again."""
        assert reg._same_git_target(a, b)

    def test_a_case_only_difference_does_not_let_the_catalog_replace_the_seed(self):
        seed = {"name": "x", "gitUrl": "https://example.com/Owner/Repo", "repo": "o/r"}
        catalog = {"name": "x", "gitUrl": "https://example.com/owner/repo", "commit": SHA}
        assert not reg._catalog_row_supersedes_seed(seed, catalog)


class TestCandidateSetReplacesEquivalentSeedRows:
    """Provenance matching compares the recorded URL EXACTLY, so a retained seed row
    is selected whenever its URL differs cosmetically from the catalog's."""

    def _rows(self, monkeypatch, seed_url: str, catalog_url: str):
        seed = {"name": "dup", "gitUrl": seed_url, "repo": seed_url, "branch": "main"}
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [seed])
        monkeypatch.setattr(
            reg.official_catalog,
            "inventory_for_install",
            lambda n: {"name": "dup", "gitUrl": catalog_url, "commit": SHA, "_catalog": True},
        )
        monkeypatch.setattr(reg, "_read_external_registry_cache", lambda *a, **k: None)

    def test_a_cosmetically_different_seed_row_is_removed_not_outranked(self, monkeypatch):
        self._rows(
            monkeypatch,
            seed_url="https://example.com/o/r",
            catalog_url="https://example.com/o/r.git",
        )
        candidates = reg._registry_app_candidates("dup")
        assert len(candidates) == 1, "the unpinned equivalent must not remain selectable"
        assert candidates[0].get("commit") == SHA

    def test_a_different_repository_is_still_kept(self, monkeypatch):
        """Scope: replacing must apply only to the SAME repository."""
        self._rows(
            monkeypatch,
            seed_url="https://example.com/o/r",
            catalog_url="https://example.com/other/repo",
        )
        candidates = reg._registry_app_candidates("dup")
        assert len(candidates) == 2, "a different repo's row is an addition, not a replacement"


class TestCatalogFailureBlocksExternalFallbackToo:
    """A catalog-only app has no seed row, so the old refusal (which required one)
    let a same-named external registry answer for it."""

    def _no_seed(self, monkeypatch):
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: None)
        monkeypatch.setattr(
            reg,
            "_read_external_registry_cache",
            lambda *a, **k: [{"name": "dup", "gitUrl": "https://evil.example/x", "repo": "e/x"}],
        )

    def test_an_external_row_may_not_answer_while_the_catalog_is_unreachable(self, monkeypatch):
        self._no_seed(monkeypatch)
        row, reason = reg._resolve_registry_row("dup")
        assert row is None, "a different source must not stand in for an unconfirmed name"
        assert "could not be reached" in reason

    def test_the_external_row_still_resolves_when_the_catalog_answers(self, monkeypatch):
        """Scope: only a FAILED lookup blocks the external path.

        Stubbed at `_external_registry_row` because what changed is whether
        resolution REACHES that fallback, not how it reads config.
        """
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(
            reg.official_catalog,
            "fetch_document",
            lambda url: {
                "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
                "apps": [],
            },
        )
        monkeypatch.setattr(
            reg,
            "_external_registry_row",
            lambda n: {"name": n, "gitUrl": "https://ext.example/x", "_registry": "ext"},
        )
        row, reason = reg._resolve_registry_row("dup")
        assert reason == ""
        assert row is not None and row.get("_registry") == "ext"


class TestTheCacheMayNotIntroduceInventory:
    """The listing is what a consent grant is made against, so a cache-planted name
    must not become a row.

    The cache is agent-writable. Supplying display copy for rows that exist anyway is
    safe (`annotate` skips `_registry` rows). MATERIALISING a row is not: a planted
    name renders with official provenance and deduplicates the real same-named
    external row out of the listing, so the prompt describes an official app while
    the name grant installs the external one.
    """

    @pytest.mark.asyncio
    async def test_a_poisoned_cache_cannot_relabel_a_fresh_row(self, monkeypatch):
        """Round 11 stopped the cache INTRODUCING a row; this stops it REWRITING one.

        `annotate` overlays `displayName` and `description` -- exactly what the consent
        modal renders -- and it used to read the cache, so a poisoned entry could
        re-label a freshly fetched first-party row while the name-scoped grant executed
        the real app.
        """
        real = {
            "name": "demo-app",
            "displayName": "Demo App",
            "source": {"type": "git", "url": URL, "ref": SHA},
        }
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [real],
        })
        # The cache claims a different identity for the same name.
        monkeypatch.setattr(reg.official_catalog, "_read_cache", lambda: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [{**real, "displayName": "SPOOFED", "summary": "spoofed copy"}],
        })
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg, "list_installed_apps", lambda: [])

        async def _no_external():
            return []

        monkeypatch.setattr(reg, "_load_external_registries", _no_external)
        monkeypatch.setattr(reg, "_resolve_manifest", _async_passthrough)

        rows = await reg.list_registry()
        row = next(r for r in rows if r.get("name") == "demo-app")
        assert row.get("displayName") == "Demo App", (
            "the identity the consent modal renders must come from the fetched document"
        )

    @pytest.mark.asyncio
    async def test_a_poisoned_cache_cannot_relabel_a_seed_row(self, monkeypatch):
        """Isolates the listing-SOURCE half of the fix.

        A seed row is not a `_catalog` row, so `annotate`'s skip does not protect it --
        only feeding the overlay from the fresh document does. Without this, the two
        layers cover each other and neither is independently tested.
        """
        seed = {"name": "seed-app", "gitUrl": URL, "repo": URL, "branch": "main"}
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [seed])
        # A valid fresh document that does not mention this app at all.
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [],
        })
        # The cache claims curated copy for it.
        monkeypatch.setattr(reg.official_catalog, "_read_cache", lambda: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [{"name": "seed-app", "displayName": "SPOOFED", "summary": "spoof"}],
        })
        monkeypatch.setattr(reg, "list_installed_apps", lambda: [])

        async def _no_external():
            return []

        monkeypatch.setattr(reg, "_load_external_registries", _no_external)

        async def _manifest_copy(entry):
            return {**entry, "displayName": "From Manifest"}

        monkeypatch.setattr(reg, "_resolve_manifest", _manifest_copy)

        rows = await reg.list_registry()
        row = next(r for r in rows if r.get("name") == "seed-app")
        assert row.get("displayName") == "From Manifest", (
            "a cached entry must not supply copy for any row the consent modal renders"
        )

    def test_annotate_applies_curated_copy(self):
        """Scope guard: skipping catalog rows must not turn the overlay into a no-op
        for the rows it exists to serve (seed rows and built-ins)."""
        row = {"name": "demo-app", "displayName": "From Manifest"}
        oc.annotate([row], [{"name": "demo-app", "displayName": "Curated Name"}])
        assert row["displayName"] == "Curated Name"

    def test_annotate_leaves_catalog_rows_alone(self):
        """Belt and braces at the overlay itself: a row materialised FROM the document
        needs no second pass over it."""
        row = {"name": "demo-app", "displayName": "Demo App", "_catalog": True}
        oc.annotate([row], [{"name": "demo-app", "displayName": "SPOOFED"}])
        assert row["displayName"] == "Demo App"

    @pytest.mark.asyncio
    async def test_a_planted_cache_row_does_not_reach_the_listing(self, monkeypatch):
        planted = {
            "name": "squatted",
            "displayName": "Totally Official",
            "source": {"type": "git", "url": URL, "ref": SHA},
        }
        # The CACHE says yes; the network says the catalog is unreachable. The listing
        # reads neither the cache's inventory nor its curated copy, so a poisoned cache
        # has no effect at all -- that is the property under test.
        monkeypatch.setattr(reg.official_catalog, "_read_cache", lambda: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [planted],
        })
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: None)
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg, "list_installed_apps", lambda: [])

        external = {
            "name": "squatted",
            "gitUrl": "https://ext.example/real",
            "repo": "e/real",
            "_registry": "ext",
        }

        async def _external():
            return [external]

        monkeypatch.setattr(reg, "_load_external_registries", _external)
        monkeypatch.setattr(reg, "_resolve_manifest", _async_passthrough)

        rows = await reg.list_registry()
        by_name = {r.get("name"): r for r in rows}
        assert "squatted" in by_name, "the real external row must survive"
        assert by_name["squatted"].get("_registry") == "ext", (
            "a planted cache row must not shadow the external row it squats"
        )

    def test_the_fresh_fetch_refuses_rather_than_reading_the_cache(self, monkeypatch):
        monkeypatch.setattr(reg.official_catalog, "_read_cache", lambda: {
            "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
            "apps": [{"name": "planted", "source": {"type": "git", "url": URL, "ref": SHA}}],
        })
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: None)
        with pytest.raises(reg.official_catalog.CatalogUnavailable):
            reg.official_catalog.fetch_inventory_entries()


class TestCatalogNamesUseTheCanonicalContract:
    """`app_name_error` documents itself as the SINGLE app-name contract, so a local
    regex here was the "other door" it exists to prevent."""

    @pytest.mark.parametrize("name", ["system", "nul", "con", "Not-Kebab", "has_underscore"])
    def test_names_the_canonical_contract_refuses_are_dropped(self, name):
        entry = {"name": name, "source": {"type": "git", "url": URL, "ref": SHA}}
        assert list(oc.inventory([entry])) == []

    def test_a_duplicate_name_is_dropped(self):
        """Two rows with one name make identity ambiguous: consent is shown for the
        row that rendered, while a name-only lookup can resolve the other."""
        a = {"name": "demo-app", "source": {"type": "git", "url": URL, "ref": SHA}}
        b = {
            "name": "demo-app",
            "source": {"type": "git", "url": "https://other.example/x", "ref": SHA},
        }
        rows = list(oc.inventory([a, b]))
        assert len(rows) == 1
        assert rows[0]["gitUrl"] == URL, "the first row wins; the second is not silently used"

    def test_a_normal_name_still_materialises(self):
        entry = {"name": "demo-app", "source": {"type": "git", "url": URL, "ref": SHA}}
        assert len(list(oc.inventory([entry]))) == 1


class TestPinnedManifestNeverReadsThePersistentCheckout:
    """The persistent checkout is agent-writable and `app.json` there can be edited
    without HEAD moving, so a commit comparison attests placement, not contents --
    and this manifest is what the admission and platform gates read."""

    @pytest.mark.asyncio
    async def test_a_pinned_entry_ignores_the_local_manifest(self, tmp_path, monkeypatch):
        planted = tmp_path / "demo"
        planted.mkdir()
        (planted / "app.json").write_text('{"name": "demo", "displayName": "PLANTED"}')
        monkeypatch.setattr(reg, "app_source_dir", lambda n: planted)
        monkeypatch.setattr(reg, "_resolved_clone_commit", lambda p: SHA)
        monkeypatch.setattr(reg, "_clone_origin_matches", _async_return(True))
        # Open EVERY route into the fast path, so the only thing keeping the planted
        # manifest out is the pinned-entry gate itself. Patching just one freshness
        # check would let a regression that restores the other route pass unnoticed.
        monkeypatch.setattr(reg, "_clone_branch_matches", _async_return(True))

        async def fake_fetch(git_url, commit, dest, log_lines, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "app.json").write_text('{"name": "demo", "displayName": "FETCHED"}')
            return None

        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)
        manifest = await reg._fetch_app_manifest(
            "o/demo", "main", "", app_name="demo", git_url=URL, commit=SHA
        )
        assert manifest is not None
        assert manifest["displayName"] == "FETCHED", (
            "a pinned manifest must come from a tree this call fetched"
        )

    @pytest.mark.asyncio
    async def test_an_unpinned_entry_still_uses_the_local_manifest(self, tmp_path, monkeypatch):
        """Scope: the fast path is removed for PINNED entries only -- taking it away
        from branch entries would make every listing a fresh network clone."""
        local = tmp_path / "demo"
        local.mkdir()
        (local / "app.json").write_text('{"name": "demo", "displayName": "LOCAL"}')
        monkeypatch.setattr(reg, "app_source_dir", lambda n: local)
        monkeypatch.setattr(reg, "_clone_origin_matches", _async_return(True))
        monkeypatch.setattr(reg, "_clone_branch_matches", _async_return(True))
        manifest = await reg._fetch_app_manifest(
            "o/demo", "main", "", app_name="demo", git_url=URL
        )
        assert manifest is not None and manifest["displayName"] == "LOCAL"


class TestAFailedInstallRestoresThePreviousCheckout:
    """A pinned install moves the previous checkout aside on EVERY reinstall, so an
    exit that forgets to restore leaves the user's only edited copy as a `.stale-*`
    sibling that the retention sweep later deletes."""

    @pytest.mark.asyncio
    async def test_the_moved_aside_checkout_is_put_back(self, tmp_path):
        pkg = tmp_path / "source"
        pkg.mkdir()
        (pkg / "replacement.txt").write_text("the tree the user did not ask for")
        aside = tmp_path / "source.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        reg._restore_moved_aside(aside, pkg, [], "the install step failed")

        assert (pkg / "my-edit.py").read_text() == "local work"
        assert not (pkg / "replacement.txt").exists(), "the replacement is discarded"
        assert not aside.exists()

    @pytest.mark.asyncio
    async def test_a_missing_moved_aside_is_a_no_op(self, tmp_path):
        """Scope: nothing to restore must not delete the tree that is there."""
        pkg = tmp_path / "source"
        pkg.mkdir()
        (pkg / "keep.txt").write_text("installed fine")
        reg._restore_moved_aside(None, pkg, [], "reason")
        assert (pkg / "keep.txt").exists()

    @pytest.mark.asyncio
    async def test_restoration_never_recursively_deletes(self, tmp_path, monkeypatch):
        """Two reviews pulled opposite ways here — no awaiting during cancellation, and
        no blocking rmtree on the loop thread — so the deletion itself had to go.

        The replacement is RENAMED to a `.partial-*` sibling, which the retention sweep
        already owns (it collects both the `stale` and `partial` prefixes). Two O(1)
        renames need neither a thread nor a loop, so the same call is safe on every path.
        """
        pkg = tmp_path / "source"
        pkg.mkdir()
        (pkg / "big-tree.bin").write_text("the replacement")
        aside = tmp_path / "source.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        def no_rmtree(*a, **k):
            raise AssertionError("restoration must not recursively delete")

        monkeypatch.setattr(reg.shutil, "rmtree", no_rmtree)

        def no_to_thread(*a, **k):
            raise AssertionError("restoration must not need the event loop")

        monkeypatch.setattr(reg.asyncio, "to_thread", no_to_thread)

        reg._restore_moved_aside(aside, pkg, [], "the install step failed")

        assert (pkg / "my-edit.py").read_text() == "local work"
        assert not (pkg / "big-tree.bin").exists(), "the replacement is out of the way"
        partial = [p for p in tmp_path.iterdir() if ".partial-" in p.name]
        assert len(partial) == 1 and (partial[0] / "big-tree.bin").exists(), (
            "the discarded replacement is left for the retention sweep, not deleted inline"
        )

    @pytest.mark.asyncio
    async def test_a_failed_rename_leaves_the_copy_recoverable(self, tmp_path, monkeypatch):
        pkg = tmp_path / "source"
        pkg.mkdir()
        aside = tmp_path / "source.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        def boom(*a, **k):
            raise OSError("locked")

        monkeypatch.setattr(pathlib.Path, "rename", boom)
        logs: list[str] = []
        reg._restore_moved_aside(aside, pkg, logs, "the install step failed")
        assert (aside / "my-edit.py").exists(), "the copy must survive for manual recovery"
        # Either rename can be the one that fails, so assert the substance: the log has
        # to say the copy is retained and where.
        assert any("retained" in line for line in logs), logs
        assert any(aside.name in line for line in logs), logs


class TestRestorationIsScopedToSameRepositoryMoves:
    """Two kinds of moved-aside checkout, opposite correct handling.

    An origin-mismatch move holds a DIFFERENT repository, and restoring it would hand
    the build the tree that gate refused. A pinned reinstall's move holds the SAME
    repository with the user's edits, and NOT restoring it loses them to the sweep.
    One list carrying both meanings meant whichever rule won was wrong for the other.
    """

    @pytest.mark.asyncio
    async def test_a_same_repo_move_is_reported_as_restorable(self, tmp_path, monkeypatch):
        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)
        monkeypatch.setattr(reg, "_clone_origin_url", _async_return(URL))

        async def fake_fetch(git_url, commit, d, log_lines, **kwargs):
            d.mkdir(parents=True, exist_ok=True)
            return None

        monkeypatch.setattr(reg, "_git_fetch_commit", fake_fetch)
        pending: list[pathlib.Path] = []
        restorable: list[pathlib.Path] = []
        result = await reg._git_clone_or_pull(
            URL,
            "main",
            dest,
            [],
            commit=SHA,
            pending_cleanup=pending,
            restorable_stale=restorable,
        )
        assert result is None
        assert len(pending) == 1, "still retained for recovery"
        assert restorable == pending, "and marked restorable, because it is the same repo"

    @pytest.mark.asyncio
    async def test_an_origin_mismatch_move_is_not_restorable(self, tmp_path, monkeypatch):
        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)
        # The existing checkout points somewhere else entirely.
        monkeypatch.setattr(reg, "_clone_origin_url", _async_return("https://other.example/x"))

        async def fake_clone(*a, **k):
            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            dest.mkdir(parents=True, exist_ok=True)
            return _P()

        monkeypatch.setattr(reg, "create_subprocess_limited", fake_clone)
        monkeypatch.setattr(reg, "wrap_argv", lambda argv, mode=None: (argv, None))
        monkeypatch.setattr(reg, "cgroup_scope_argv", lambda argv: argv)
        pending: list[pathlib.Path] = []
        restorable: list[pathlib.Path] = []
        await reg._git_clone_or_pull(
            URL,
            "main",
            dest,
            [],
            pending_cleanup=pending,
            restorable_stale=restorable,
        )
        assert restorable == [], (
            "restoring a different repository's checkout would defeat the "
            "origin-mismatch gate"
        )


class TestCandidatesRefuseBeforeAnyFallback:
    """`_resolve_registry_row`'s sibling. Clearing the seed candidates and then
    falling through to the external caches still let a same-named row answer -- and a
    tampered cache row missing `_registry` reads as official, so a provenance match
    would install an unpinned branch with the OWNER's credentials."""

    def test_no_candidate_is_offered_when_the_catalog_is_unreachable(self, monkeypatch):
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(reg.official_catalog, "fetch_document", lambda url: None)

        # A configured registry MUST exist for this to test anything: without one the
        # external loop iterates nothing and the assertion would hold even with the
        # refusal removed.
        class _Reg:
            name = "ext"
            repo = "o/ext"

        class _Cfg:
            registries = [_Reg()]

            @staticmethod
            def load():
                return _Cfg()

        import kiro_crew.config.loader as loader

        monkeypatch.setattr(loader, "KiroCrewConfig", _Cfg)
        # A markerless external row -- exactly the shape that reads as official.
        monkeypatch.setattr(
            reg,
            "_read_external_registry_cache",
            lambda *a, **k: [{"name": "dup", "gitUrl": "https://evil.example/x", "repo": "e/x"}],
        )
        monkeypatch.setattr(reg, "_apply_configured_branch", lambda rows, reg_cfg: None)
        assert reg._registry_app_candidates("dup") == []

    def test_external_candidates_still_resolve_when_the_catalog_answers(self, monkeypatch):
        """Scope: only a FAILED lookup refuses.

        Stubbed at the config boundary because the test environment configures no
        registries, so patching the cache reader alone leaves the loop with nothing
        to iterate and the assertion would hold for the wrong reason.
        """
        monkeypatch.setattr(reg, "_load_registry_file", lambda: [])
        monkeypatch.setattr(
            reg.official_catalog,
            "fetch_document",
            lambda url: {
                "schemaVersion": reg.official_catalog.SUPPORTED_SCHEMA_VERSION,
                "apps": [],
            },
        )

        class _Reg:
            name = "ext"
            repo = "o/ext"

        class _Cfg:
            registries = [_Reg()]

            @staticmethod
            def load():
                return _Cfg()

        import kiro_crew.config.loader as loader

        monkeypatch.setattr(loader, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(
            reg,
            "_read_external_registry_cache",
            lambda *a, **k: [{"name": "dup", "gitUrl": "https://ext.example/x", "repo": "e/x"}],
        )
        monkeypatch.setattr(reg, "_apply_configured_branch", lambda rows, reg_cfg: None)
        assert [c.get("name") for c in reg._registry_app_candidates("dup")] == ["dup"]


class TestRestorationStateSurvivesEveryExit:
    """`_clone_build_app_locked` has six exits and the state was attached only to the
    successful one, so a post-fetch failure dropped it and the caller's `finally` had
    nothing to restore from."""

    @pytest.mark.asyncio
    async def test_a_failure_exit_still_reports_the_restorable_path(self, monkeypatch):
        aside = pathlib.Path("/tmp/does-not-matter.stale-abcd")

        async def failing_locked(*a, **kwargs):
            # Fill the caller-owned list, then fail the way a containment or
            # admission rejection does.
            kwargs["restorable_stale"].append(aside)
            return {"ok": False, "error": "subdirectory escapes the clone root"}

        monkeypatch.setattr(reg, "_clone_build_app_locked", failing_locked)
        result = await reg._clone_build_app(URL, "demo", [])
        assert result["ok"] is False
        assert result.get("_restorable_stale") == [aside], (
            "a failing exit must still hand the caller something to restore"
        )

    @pytest.mark.asyncio
    async def test_no_restorable_path_means_no_key(self, monkeypatch):
        async def clean_locked(*a, **kwargs):
            return {"ok": True, "pkg_dir": pathlib.Path("/tmp/x")}

        monkeypatch.setattr(reg, "_clone_build_app_locked", clean_locked)
        result = await reg._clone_build_app(URL, "demo", [])
        assert "_restorable_stale" not in result


class TestInterruptionAndTornStateOnTheInstallPath:
    """The two ways a correct-looking rollback still loses or tears state."""

    @pytest.mark.asyncio
    async def test_the_interruption_path_does_not_touch_the_event_loop(
        self, tmp_path, monkeypatch
    ):
        """Awaiting during cancellation re-enters a loop being torn down, which CI
        reported as `RuntimeError: Event loop is closed` on three platforms at once.

        Drives the real interruption path with `asyncio.to_thread` sabotaged: the async
        restoration wrapper goes through it, so an `await` here explodes while the
        synchronous call does not. Asserting on the helper directly could not tell the
        two apart.
        """
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "replacement.txt").write_text("half-built")
        aside = tmp_path / "demo.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        async def cancelled_locked(*a, **kwargs):
            kwargs["restorable_stale"].append(aside)
            raise asyncio.CancelledError()

        monkeypatch.setattr(reg, "_clone_build_app_locked", cancelled_locked)
        monkeypatch.setattr(reg, "app_source_dir", lambda n: pkg)

        def exploding_to_thread(*a, **k):
            raise AssertionError("the interruption path must not use the event loop")

        monkeypatch.setattr(reg.asyncio, "to_thread", exploding_to_thread)

        with pytest.raises(asyncio.CancelledError):
            await reg._clone_build_app(URL, "demo", [])

        assert (pkg / "my-edit.py").read_text() == "local work"
        assert not (pkg / "replacement.txt").exists()
        assert not aside.exists()

    @pytest.mark.asyncio
    async def test_cancellation_restores_the_moved_aside_checkout(self, tmp_path, monkeypatch):
        """An `await` that is cancelled never reaches the line that hands the state to
        the caller, so it has to be restored where the state actually lives."""
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "replacement.txt").write_text("half-built")
        aside = tmp_path / "demo.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        async def cancelled_locked(*a, **kwargs):
            kwargs["restorable_stale"].append(aside)
            raise asyncio.CancelledError()

        monkeypatch.setattr(reg, "_clone_build_app_locked", cancelled_locked)
        monkeypatch.setattr(reg, "app_source_dir", lambda n: pkg)

        with pytest.raises(asyncio.CancelledError):
            await reg._clone_build_app(URL, "demo", [])

        assert (pkg / "my-edit.py").read_text() == "local work"
        assert not aside.exists()

    @pytest.mark.asyncio
    async def test_an_exception_also_restores(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        aside = tmp_path / "demo.stale-abcd1234"
        aside.mkdir()
        (aside / "my-edit.py").write_text("local work")

        async def raising_locked(*a, **kwargs):
            kwargs["restorable_stale"].append(aside)
            raise OSError("disk went away")

        monkeypatch.setattr(reg, "_clone_build_app_locked", raising_locked)
        monkeypatch.setattr(reg, "app_source_dir", lambda n: pkg)

        with pytest.raises(OSError):
            await reg._clone_build_app(URL, "demo", [])
        assert (pkg / "my-edit.py").exists()


class TestRequestPathPatternsRejectATrailingNewline:
    """`$` also matches before a trailing newline, and these two feed git argv and a
    filesystem join via `.match`. Same defect class as the catalog-side coordinate
    patterns, found as their remaining siblings."""

    @pytest.mark.parametrize("value", ["main\n", "refs/heads/main\n", "v1.0\n"])
    def test_ref_pattern_rejects_a_trailing_newline(self, value):
        from kiro_crew.apps import routes

        assert not routes._SAFE_REF_RE.match(value)

    @pytest.mark.parametrize("value", ["icon.png\n", "assets/logo.svg\n"])
    def test_path_pattern_rejects_a_trailing_newline(self, value):
        from kiro_crew.apps import routes

        assert not routes._SAFE_PATH_RE.match(value)

    @pytest.mark.parametrize("value", ["main", "refs/heads/main", "v1.0"])
    def test_ordinary_refs_still_pass(self, value):
        """Scope: tightening the anchor must not reject the values it exists to admit."""
        from kiro_crew.apps import routes

        assert routes._SAFE_REF_RE.match(value)

    @pytest.mark.parametrize("value", ["icon.png", "assets/logo.svg"])
    def test_ordinary_paths_still_pass(self, value):
        from kiro_crew.apps import routes

        assert routes._SAFE_PATH_RE.match(value)


class TestTrustBoundary:
    def test_a_catalog_row_never_mints_the_verified_badge(self):
        """The catalog's signature is not checked, so its curated author must not
        make an app read as first-party.

        Asserted at `_apply_trust_fields` rather than at the row's source because
        `list_registry` assigns `_index_author = entry["author"]` unconditionally:
        a rule stated upstream is a rule a later assignment can undo, and that is
        exactly what happened before this test existed.
        """
        rows = reg._apply_trust_fields(
            [{"name": "demo-app", "_catalog": True, "_index_author": "Kiro Crew"}]
        )
        assert rows[0]["verified"] is False
        assert rows[0]["provenance"] == "official"

    def test_a_seed_row_with_a_first_party_author_still_earns_the_badge(self):
        """The refusal above must be scoped to catalog rows, not a blanket change."""
        rows = reg._apply_trust_fields(
            [{"name": "demo-app", "_index_author": "Kiro Crew"}]
        )
        assert rows[0]["verified"] is True

    def test_an_external_row_cannot_forge_the_catalog_fast_path(self):
        """`_catalog` is on the row-projection allowlist, so an external registry's
        index can set it. Skipping the manifest fetch for such a row would let
        index-supplied display copy stand in for the app's own manifest."""
        assert reg._is_catalog_row({"_catalog": True}) is True
        assert reg._is_catalog_row({"_catalog": True, "_registry": "acme"}) is False
        assert reg._is_catalog_row({"_registry": "acme"}) is False
        assert reg._is_catalog_row({}) is False


class TestPinnedInstallRefusals:
    @pytest.mark.asyncio
    async def test_a_pinned_entry_never_falls_back_to_a_branch(self, monkeypatch):
        """The only failure mode here that is quiet AND reports success.

        `install_from_registry` reads `branch` with a default of "main". A path
        that ignored a malformed `commit` would clone that tip, succeed, and record
        the tip's commit as provenance -- a store that looks like it installs
        pinned bytes while installing today's default branch.
        """
        # `_catalog` (and no `_registry`) is the shape a real catalog install row
        # has; the pin is only read for such a row.
        entry = {
            "name": "demo-app",
            "gitUrl": URL,
            "repo": URL,
            "commit": "not-a-sha",
            "_catalog": True,
        }
        monkeypatch.setattr(reg, "_resolve_install_entry", lambda name: (entry, ""))

        called: list[Any] = []

        async def _must_not_run(*a, **kw):
            called.append((a, kw))
            raise AssertionError("clone/fetch must not be reached")

        monkeypatch.setattr(reg, "_clone_build_app", _must_not_run)
        result = await reg.install_from_registry("demo-app")
        assert result["ok"] is False
        assert "pinned commit" in result["error"]
        assert called == [], "nothing was fetched"

    @pytest.mark.parametrize(
        "value",
        [
            SHA + "\n",  # Python's `$` matches before a trailing newline
            SHA + "\r\n",
            "\n" + SHA,
            SHA.upper(),
            SHA[:39],
            SHA + "a",
            "main",
            "",
        ],
    )
    def test_the_install_paths_pin_check_is_no_weaker_than_the_catalogs(self, value):
        """Defence in depth is only depth if the outer layer is not the laxer one.

        This pattern also validates the SHA read back off disk by
        `_resolved_clone_commit`, which is what the post-fetch verification compares
        against -- so a value that merely looks like a commit would be reported as
        the landed one.
        """
        assert reg._COMMIT_SHA_RE.match(value) is None, value
        assert oc._COMMIT_RE.match(value) is None, "the two layers must agree"

    def test_both_pin_checks_accept_a_real_commit(self):
        """The tightening must not have broken the accept case."""
        assert reg._COMMIT_SHA_RE.match(SHA) is not None
        assert oc._COMMIT_RE.match(SHA) is not None
        assert reg._COMMIT_SHA_RE.match("b" * 64) is not None

    @pytest.mark.asyncio
    async def test_the_pin_reaches_the_fetch(self, monkeypatch):
        seen: dict[str, Any] = {}
        manifest_seen: dict[str, Any] = {}

        async def _capture(git_url, app_name, log_lines, **kw):
            seen.update(kw)
            seen["git_url"] = git_url
            return {"ok": False, "error": "stop here"}

        async def _capture_manifest(repo, branch, subdirectory="", **kw):
            manifest_seen.update(kw)
            return None

        entry = {
            "name": "demo-app",
            "gitUrl": URL,
            "repo": URL,
            "commit": SHA,
            "_catalog": True,
        }
        monkeypatch.setattr(reg, "_resolve_install_entry", lambda name: (entry, ""))
        # Third-party execution is disabled by default, and that gate sits between
        # the pin check and the fetch. It is unrelated to what this test asserts,
        # so it is neutralised rather than satisfied by rewriting config -- note
        # that the refusal test above needs no such patch, which is itself evidence
        # the pin check runs BEFORE this gate.
        monkeypatch.setattr(reg, "app_execution_denied", lambda *a, **kw: "")
        # The pinned path does a REAL manifest fetch before the clone (no local fast
        # path for pins), so this stub is load-bearing twice over: without it the test
        # spawns a real sandboxed git subprocess -- which fails DETERMINISTICALLY on
        # CI hosts where the sandbox backend is unavailable (AppArmor userns
        # restriction on ubuntu-latest; Windows fails closed) while passing on a dev
        # desk whose sandbox works, after a real network round-trip. A missing
        # manifest is tolerated by design, so returning None keeps the install
        # marching toward the clone this test is actually about.
        monkeypatch.setattr(reg, "_fetch_app_manifest", _capture_manifest)
        monkeypatch.setattr(reg, "_clone_build_app", _capture)
        result = await reg.install_from_registry("demo-app")
        assert result.get("error") == "stop here", f"returned before the fetch: {result}"
        assert seen["commit"] == SHA
        assert seen["git_url"] == URL
        # The pin must reach BOTH fetch layers: the manifest preflight and the clone.
        assert manifest_seen.get("commit") == SHA
