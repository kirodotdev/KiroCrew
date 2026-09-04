"""Edition-pinned app registries and the registry trust tier.

Two seams, one purpose: let a deployment publish its app catalog from a repo it
controls instead of shipping the catalog inside the client.

``AppsLoader.default_registries()`` supplies registries the edition pins, merged
into the operator's ``config.registries`` at every consumption site.
``ExternalRegistryConfig.trust`` decides whether the apps such a registry lists
clone with the machine's git credentials or credential-free.

The trust tier retracts a real defense, so most of this file is about its
boundary: it must never widen WHICH hosts can be cloned, must never be settable
by the index itself, must fail closed on anything unrecognised, and must leave the
public default byte-identical to reading ``config.registries`` alone.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_INDEX,
    _TRUST_OWNER,
    _configured_registry_hosts,
    _effective_registries,
    _is_owner_designated_repo,
    _owner_tier_confirmed,
    _pinned_registries,
    _registry_trust_tier,
    is_clone_host_trusted,
)
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import ExternalRegistryConfig, KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultAppsLoader

FORGE = "https://forge.example.com/org/app-registry.git"
SIBLING = "https://forge.example.com/org/some-private-app.git"
FORGE_HOST = "forge.example.com"


class _Loader(DefaultAppsLoader):
    """A companion apps loader that pins registries."""

    def __init__(self, rows) -> None:
        self._rows = rows

    def default_registries(self):
        return self._rows


def _with_loader(monkeypatch, rows):
    """Compose a context whose apps_loader pins *rows*.

    Patches the name in ``apps.registry`` rather than in ``platform.context``:
    the module imports ``current_context`` by value, so rebinding the origin
    would leave this module's reference untouched.
    """
    base = build_default_context(KiroCrewConfig())
    ctx = dataclasses.replace(base, apps_loader=_Loader(rows))
    monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)


def _with_config(monkeypatch, registries):
    """Pin what ``KiroCrewConfig.load()`` reports for ``registries``.

    Patches the loader module attribute, which is what ``_effective_registries``
    resolves at call time — the same config boundary the rest of the registry
    tests stub.
    """
    cfg = KiroCrewConfig()
    cfg.registries = list(registries)
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))


# ---------------------------------------------------------------------------
# The public default changes nothing
# ---------------------------------------------------------------------------


class TestPublicDefaultUnchanged:
    def test_default_loader_pins_no_registry(self):
        assert DefaultAppsLoader().default_registries() == []

    def test_effective_list_is_config_alone(self, monkeypatch):
        """With no edition rows, the merge must be the config list itself."""
        configured = [ExternalRegistryConfig(name="mine", repo=FORGE, branch="main")]
        _with_config(monkeypatch, configured)
        _with_loader(monkeypatch, [])
        assert _effective_registries() == configured

    def test_trust_defaults_to_index(self):
        assert ExternalRegistryConfig(repo=FORGE).trust == _TRUST_INDEX

    def test_untiered_registry_keeps_the_anonymous_posture(self, monkeypatch):
        """A registry with no explicit trust must not designate its apps."""
        _with_config(monkeypatch, [ExternalRegistryConfig(name="pub", repo=FORGE)])
        _with_loader(monkeypatch, [])
        entry = {"_registry": "pub", "gitUrl": SIBLING}
        assert _is_owner_designated_repo(entry) is False


# ---------------------------------------------------------------------------
# Seam 1: edition-pinned registries
# ---------------------------------------------------------------------------


class TestPinnedRegistries:
    def test_pinned_row_is_materialised_with_defaults(self, monkeypatch):
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        (row,) = _pinned_registries()
        assert (row.name, row.repo, row.branch, row.trust) == (
            "official",
            FORGE,
            "main",
            _TRUST_INDEX,
        )

    def test_pinned_and_configured_are_both_in_force(self, monkeypatch):
        _with_config(monkeypatch, [ExternalRegistryConfig(name="mine", repo=SIBLING)])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        names = [r.name for r in _effective_registries()]
        assert names == ["official", "mine"]

    def test_a_contested_name_serves_neither_row(self, monkeypatch):
        """Same name, DIFFERENT repos: refuse both rather than mis-attribute.

        The index cache is keyed by registry NAME, so serving either row would
        read the other repository's cached index under the winner's identity, and
        every reader stamps `_registry` from the registry it asked for — apps the
        winning repo does not list, presented as its own.
        """
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="official", repo="https://evil.example.com/x.git")],
        )
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _effective_registries() == []

    def test_a_contested_name_does_not_leak_the_tier(self, monkeypatch):
        """Neither row is served, so the tier resolves to the restrictive default."""
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="official", repo="https://evil.example.com/x.git")],
        )
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _registry_trust_tier("official") == _TRUST_INDEX

    def test_the_same_name_at_the_same_repo_is_not_contested(self, monkeypatch):
        """An operator row that already agreed is superseded, not a conflict.

        The shared cache is correct here, so refusing would disable a registry
        for no reason.
        """
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="official", repo=FORGE, trust=_TRUST_INDEX)],
        )
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        rows = _effective_registries()
        assert [(r.name, r.repo, r.trust) for r in rows] == [("official", FORGE, _TRUST_OWNER)]

    def test_the_same_repo_on_a_different_branch_is_contested(self, monkeypatch):
        """The index is fetched from ONE branch, so branch is part of its identity.

        Same name and repo but different refs list different apps, and the cache
        is keyed by name — so the pinned row would read the other branch's index.
        """
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="official", repo=FORGE, branch="staging")],
        )
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "branch": "main"}])
        assert _effective_registries() == []

    @pytest.mark.parametrize(
        "bad_repo",
        [
            "http://forge.example.com/org/registry.git",
            "git://forge.example.com/org/registry.git",
            "ext::sh -c whoami",
            "https://forge.example.com/org/../../etc/passwd",
            "https://forge.example.com/org/r.git; rm -rf /",
            # Credential-bearing: refused outright, not redacted, because a pinned
            # repo also reaches the GET endpoint and the SEL trail.
            "https://ghp_secrettoken@forge.example.com/org/r.git",
            "https://user:token@forge.example.com/org/r.git",
            "ssh://user:token@forge.example.com/org/r.git",
        ],
    )
    def test_a_pinned_repo_on_an_unsupported_transport_is_dropped(self, monkeypatch, bad_repo):
        """An edition is trusted code, but a misconfigured one must not downgrade
        the transport carrying installable app code, nor smuggle a credential into
        a value this module hands to the dashboard and the audit log."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": bad_repo}])
        assert _pinned_registries() == []
        assert _effective_registries() == []

    @pytest.mark.parametrize(
        "good_repo",
        [
            "https://forge.example.com/org/registry.git",
            # `git@host` is a conventional USERNAME for ssh, not a secret.
            "ssh://git@forge.example.com/org/registry.git",
            "git@forge.example.com:org/registry.git",
            "LegacyBareName",
        ],
    )
    def test_a_pinned_repo_on_a_supported_transport_is_kept(self, monkeypatch, good_repo):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": good_repo}])
        assert [r.repo for r in _pinned_registries()] == [good_repo]

    def test_duplicate_pinned_names_are_all_dropped(self, monkeypatch):
        """A name selects the index cache file, so two rows sharing one would
        overwrite each other and serve entries from the wrong repository.

        Both are dropped rather than picking a winner: an edition shipping two
        registries under one name has a bug, and choosing one would hide it
        behind intermittently wrong listings.
        """
        _with_config(monkeypatch, [])
        _with_loader(
            monkeypatch,
            [
                {"name": "official", "repo": FORGE},
                {"name": "official", "repo": SIBLING},
                {"name": "distinct", "repo": "https://forge.example.com/org/other.git"},
            ],
        )
        assert [r.name for r in _pinned_registries()] == ["distinct"]

    def test_duplicate_unnamed_pinned_repos_are_all_dropped(self, monkeypatch):
        """An unnamed row keys on its repo, so two identical repos also collide."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"repo": FORGE}, {"repo": FORGE}])
        assert _pinned_registries() == []

    def test_case_distinct_pinned_names_collide(self, monkeypatch):
        """`Official` and `official` are ONE cache file on Windows and macOS.

        Keying the collision on the raw name would let both survive and overwrite
        each other's index there while working on Linux — a corruption that only
        appears on some platforms. The rule is derived from the cache path, so the
        answer is the same everywhere.
        """
        _with_config(monkeypatch, [])
        _with_loader(
            monkeypatch,
            [{"name": "Official", "repo": FORGE}, {"name": "official", "repo": SIBLING}],
        )
        assert _pinned_registries() == []

    def test_a_case_distinct_operator_name_is_contested(self, monkeypatch):
        """The same rule applies across the pinned/operator boundary."""
        _with_config(monkeypatch, [ExternalRegistryConfig(name="OFFICIAL", repo=SIBLING)])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        assert _effective_registries() == []

    def test_an_uncontested_operator_row_survives_a_collision_elsewhere(self, monkeypatch):
        _with_config(
            monkeypatch,
            [
                ExternalRegistryConfig(name="official", repo="https://evil.example.com/x.git"),
                ExternalRegistryConfig(name="mine", repo=SIBLING),
            ],
        )
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        assert [r.name for r in _effective_registries()] == ["mine"]

    def test_pinned_host_joins_the_trusted_set(self, monkeypatch):
        """A pinned registry's host is trusted for cloning, like a configured one."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        # Exact set, not membership: this asserts the pinned host is the ONLY one
        # contributed, so a merge that leaked an extra host would fail here.
        assert _configured_registry_hosts() == frozenset({FORGE_HOST})
        assert is_clone_host_trusted(SIBLING) is True

    @pytest.mark.parametrize(
        "row",
        [
            "not-a-dict",
            {"name": "no-repo"},
            {"name": "blank-repo", "repo": "   "},
            {"repo": None},
        ],
    )
    def test_malformed_pinned_row_is_dropped_not_raised(self, monkeypatch, row):
        """The list feeds security gates, so it must keep answering."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [row])
        assert _pinned_registries() == []
        assert _effective_registries() == []

    @pytest.mark.parametrize(
        "bogus",
        [42, True, object(), "a-string", b"bytes", {"name": "x", "repo": FORGE}],
    )
    def test_a_non_list_return_cannot_make_the_ssrf_gate_throw(self, monkeypatch, bogus):
        """A companion bug must degrade the list, not break `is_clone_host_trusted`.

        The gate re-enters `_configured_registry_hosts()` inside its own except
        clause, so an exception escaping the merge would leave the SSRF check
        raising rather than answering.
        """

        class _Bogus(DefaultAppsLoader):
            def default_registries(self):
                return bogus

        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(base, apps_loader=_Bogus())
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        _with_config(monkeypatch, [])
        assert _pinned_registries() == []
        assert _effective_registries() == []
        assert _configured_registry_hosts() == frozenset()
        assert is_clone_host_trusted("https://github.com/o/r.git") is True
        assert is_clone_host_trusted("https://attacker.example.net/x.git") is False

    def test_loader_failure_degrades_to_config(self, monkeypatch):
        class _Broken(DefaultAppsLoader):
            def default_registries(self):
                raise RuntimeError("companion exploded")

        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(base, apps_loader=_Broken())
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        configured = [ExternalRegistryConfig(name="mine", repo=FORGE)]
        _with_config(monkeypatch, configured)
        assert _effective_registries() == configured

    def test_config_failure_degrades_to_pinned(self, monkeypatch):
        def _boom():
            raise OSError("config.json unreadable")

        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(_boom))
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE}])
        rows = _effective_registries()
        assert [r.name for r in rows] == ["official"]


# ---------------------------------------------------------------------------
# Seam 2: the trust tier
# ---------------------------------------------------------------------------


class TestTrustTier:
    """The tier is resolved from operator/edition config, and nothing else."""

    def test_owner_tier_resolves(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _registry_trust_tier("official") == _TRUST_OWNER

    def test_the_tier_alone_does_not_designate_a_listed_app(self, monkeypatch):
        """The AUTOMATIC path must stay credential-free even at the owner tier.

        `_is_owner_designated_repo` runs on browse/refresh clones, which no
        per-repo owner action gates. The tier is honoured only by
        `_owner_tier_confirmed`, on install, after a fresh index re-read.
        """
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        entry = {"_registry": "official", "gitUrl": SIBLING}
        assert _is_owner_designated_repo(entry) is False

    def test_same_repo_carve_out_still_holds(self, monkeypatch):
        """The pre-existing ground must keep working unchanged."""
        _with_config(monkeypatch, [ExternalRegistryConfig(name="mine", repo=FORGE)])
        _with_loader(monkeypatch, [])
        assert _is_owner_designated_repo({"_registry": "mine", "gitUrl": FORGE}) is True
        assert _is_owner_designated_repo({"_registry": "mine", "gitUrl": SIBLING}) is False

    def test_an_operator_typed_tier_is_ignored(self, monkeypatch):
        """`config.json` is agent-writable, so a tier from it is not an assertion.

        `security.py` states it directly, with the check inline:
        `is_sensitive_bash_command("echo x > …/config.json")` is None. A
        prompt-injected shell could therefore mint `owner` — and the same write
        also adds its chosen host to the trusted set and lets it control the index
        `_owner_tier_confirmed` re-fetches, so every layer downstream would
        already be satisfied by that one write. Only a build-pinned row may carry
        the tier.
        """
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="mine", repo=FORGE, trust=_TRUST_OWNER)],
        )
        _with_loader(monkeypatch, [])
        assert _registry_trust_tier("mine") == _TRUST_INDEX
        assert _is_owner_designated_repo({"_registry": "mine", "gitUrl": SIBLING}) is False

    @pytest.mark.asyncio
    async def test_an_operator_typed_tier_cannot_confirm_an_install(self, monkeypatch):
        """The install path must not escalate for a config-declared tier either."""
        _with_config(
            monkeypatch,
            [ExternalRegistryConfig(name="mine", repo=FORGE, trust=_TRUST_OWNER)],
        )
        _with_loader(monkeypatch, [])

        async def _fetch(repo, branch):
            return [{"name": "app", "gitUrl": SIBLING, "branch": "main"}]

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)
        entry = {"_registry": "mine", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is False

    @pytest.mark.parametrize("bogus", ["Owner", "OWNER", " owner", "owner ", "trusted", "yes", ""])
    def test_unrecognised_tier_reads_as_index(self, monkeypatch, bogus):
        """Fail closed: only the exact token grants."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": bogus}])
        assert _registry_trust_tier("official") == _TRUST_INDEX

    @pytest.mark.parametrize("bogus", [42, 0, None, True, {"a": 1}, ["owner"], object()])
    def test_non_string_tier_reads_as_index(self, monkeypatch, bogus):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": bogus}])
        assert _registry_trust_tier("official") == _TRUST_INDEX

    def test_unknown_registry_name_reads_as_index(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _registry_trust_tier("some-other-registry") == _TRUST_INDEX
        assert _registry_trust_tier("") == _TRUST_INDEX

    def test_the_index_cannot_promote_its_own_tier(self, monkeypatch):
        """An index row carrying trust: owner must be ignored."""
        _with_config(monkeypatch, [ExternalRegistryConfig(name="pub", repo=FORGE)])
        _with_loader(monkeypatch, [])
        entry = {"_registry": "pub", "gitUrl": SIBLING, "trust": _TRUST_OWNER}
        assert _is_owner_designated_repo(entry) is False

    def test_a_bundled_entry_is_unaffected(self, monkeypatch):
        """No ``_registry`` tag means bundled — already owner-designated upstream."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _is_owner_designated_repo({"gitUrl": SIBLING}) is False

    def test_an_entry_with_no_url_is_never_designated(self, monkeypatch):
        _with_config(monkeypatch, [ExternalRegistryConfig(name="mine", repo=FORGE)])
        _with_loader(monkeypatch, [])
        assert _is_owner_designated_repo({"_registry": "mine"}) is False

    def test_the_tier_survives_a_config_disk_round_trip(self, monkeypatch, tmp_path):
        """A tier read back from config.json must not silently become 'index'.

        The PUT persists ``trust``; if ``load()`` drops it the feature is dead and
        the next replace-all PUT erases the operator's setting.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "registries": [
                        {"name": "mine", "repo": FORGE, "branch": "main", "trust": "owner"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
        loaded = loader_mod.KiroCrewConfig.load()
        assert [r.trust for r in loaded.registries] == ["owner"]

    def test_a_config_predating_the_field_reads_as_index(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"registries": [{"name": "mine", "repo": FORGE, "branch": "main"}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
        loaded = loader_mod.KiroCrewConfig.load()
        assert [r.trust for r in loaded.registries] == [_TRUST_INDEX]


class TestOwnerTierConfirmation:
    """The install-only escalation, and why it re-reads the index.

    The row reaching this decision came from ``_read_external_registry_cache`` —
    agent-writable content. Believing its URL because the registry is owner-tier
    would relocate the confused-deputy read from the index to its cache.
    """

    def _pin_owner_registry(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])

    def _fresh_index(self, monkeypatch, rows):
        async def _fetch(repo, branch):
            return rows

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)

    @pytest.mark.asyncio
    async def test_a_freshly_confirmed_url_is_designated(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"name": "app", "gitUrl": SIBLING, "branch": "main"}])
        entry = {"_registry": "official", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is True

    @pytest.mark.asyncio
    async def test_a_swapped_branch_is_refused(self, monkeypatch):
        """Branch is an install coordinate and survives from the cache.

        `_apply_configured_branch` forces the configured branch only onto
        SAME-repo entries, and an owner-tier registry's apps are cross-repo by
        definition — so a poisoned row could keep the curated URL and swap the
        ref. Matching the URL alone would clone that ref with credentials.
        """
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"name": "app", "gitUrl": SIBLING, "branch": "main"}])
        poisoned = {
            "_registry": "official",
            "name": "app",
            "gitUrl": SIBLING,
            "branch": "attacker-branch",
        }
        assert await _owner_tier_confirmed(poisoned) is False

    @pytest.mark.asyncio
    async def test_a_swapped_subdirectory_is_refused(self, monkeypatch):
        """Subdirectory selects which app.json and setup script run."""
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(
            monkeypatch,
            [{"name": "app", "gitUrl": SIBLING, "branch": "main", "subdirectory": "apps/ok"}],
        )
        poisoned = {
            "_registry": "official",
            "name": "app",
            "gitUrl": SIBLING,
            "branch": "main",
            "subdirectory": "apps/evil",
        }
        assert await _owner_tier_confirmed(poisoned) is False

    @pytest.mark.asyncio
    async def test_a_swapped_name_is_refused(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"name": "app", "gitUrl": SIBLING, "branch": "main"}])
        poisoned = {
            "_registry": "official",
            "name": "other-app",
            "gitUrl": SIBLING,
            "branch": "main",
        }
        assert await _owner_tier_confirmed(poisoned) is False

    @pytest.mark.asyncio
    async def test_a_cache_only_url_is_refused(self, monkeypatch):
        """THE regression test: a poisoned cache row the fresh index does not list.

        Anything able to write ``_registry_official.json`` could otherwise name a
        private repo on the operator's own forge and have it cloned with the
        gateway's identity.
        """
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"name": "app", "gitUrl": SIBLING, "branch": "main"}])
        poisoned = {
            "_registry": "official",
            "name": "app",
            "gitUrl": "https://forge.example.com/org/private.git",
            "branch": "main",
        }
        assert await _owner_tier_confirmed(poisoned) is False

    @pytest.mark.asyncio
    async def test_a_url_differing_only_in_case_is_refused(self, monkeypatch):
        """Byte-identical, not normalised — the same rule as the same-repo ground."""
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"name": "app", "gitUrl": SIBLING, "branch": "main"}])
        entry = {
            "_registry": "official",
            "name": "app",
            "gitUrl": SIBLING.upper(),
            "branch": "main",
        }
        assert await _owner_tier_confirmed(entry) is False

    @pytest.mark.asyncio
    async def test_an_unreachable_index_is_refused(self, monkeypatch):
        """Fail closed, and never fall back to the cache."""
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, None)
        assert await _owner_tier_confirmed({"_registry": "official", "gitUrl": SIBLING}) is False

    @pytest.mark.asyncio
    async def test_a_raising_index_fetch_is_refused(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)

        async def _boom(repo, branch):
            raise OSError("forge unreachable")

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _boom)
        assert await _owner_tier_confirmed({"_registry": "official", "gitUrl": SIBLING}) is False

    @pytest.mark.asyncio
    async def test_a_malformed_fresh_row_is_skipped(self, monkeypatch):
        """A non-dict row in the fresh index must not abort the search.

        The entry carries the branch `_apply_configured_branch` would have given
        it (a cross-repo row with no declaration inherits the registry branch),
        because the fresh rows are normalised the same way before comparison.
        """
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, ["oops", None, 42, {"name": "app", "gitUrl": SIBLING}])
        entry = {"_registry": "official", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is True

    @pytest.mark.asyncio
    async def test_an_index_tier_registry_is_never_confirmed(self, monkeypatch):
        """No fetch should even be attempted at the default tier."""
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_INDEX}])
        called = False

        async def _fetch(repo, branch):
            nonlocal called
            called = True
            return [{"gitUrl": SIBLING}]

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)
        assert await _owner_tier_confirmed({"_registry": "official", "gitUrl": SIBLING}) is False
        assert called is False

    @pytest.mark.asyncio
    async def test_a_bundled_entry_is_never_confirmed(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"gitUrl": SIBLING}])
        assert await _owner_tier_confirmed({"gitUrl": SIBLING}) is False

    @pytest.mark.asyncio
    async def test_an_entry_with_no_url_is_never_confirmed(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"gitUrl": SIBLING}])
        assert await _owner_tier_confirmed({"_registry": "official"}) is False

    @pytest.mark.asyncio
    async def test_an_unknown_registry_is_never_confirmed(self, monkeypatch):
        self._pin_owner_registry(monkeypatch)
        self._fresh_index(monkeypatch, [{"gitUrl": SIBLING}])
        assert await _owner_tier_confirmed({"_registry": "ghost", "gitUrl": SIBLING}) is False


class TestCredentialGrantIsAudited:
    """An escalation that leaves no record is not reviewable after the fact."""

    def test_the_same_repo_ground_audits(self, monkeypatch):
        events: list[tuple[str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append((kw.get("operation", ""), kw.get("resources", "")))

        monkeypatch.setattr(reg_mod, "_sel_fn", lambda: _Sel())
        reg_mod._sel_credential_grant("install_from_registry", FORGE)
        assert events == [("install_from_registry", f"owner_designated_clone url={FORGE}")]

    def test_the_owner_tier_ground_audits_under_its_own_operation(self, monkeypatch):
        """A distinct operation name, so the two grounds are separable in the log."""
        events: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw.get("operation", ""))

        monkeypatch.setattr(reg_mod, "_sel_fn", lambda: _Sel())
        reg_mod._sel_credential_grant("install_from_registry_owner_tier", SIBLING)
        assert events == ["install_from_registry_owner_tier"]

    def test_a_failing_audit_never_breaks_the_grant(self, monkeypatch):
        class _Sel:
            def log_api_access(self, **kw):
                raise RuntimeError("audit sink down")

        monkeypatch.setattr(reg_mod, "_sel_fn", lambda: _Sel())
        reg_mod._sel_credential_grant("install_from_registry", FORGE)


class TestCredentialRefusalIsAudited:
    """A refused escalation is the record an incident responder wants.

    `_owner_tier_confirmed` returns False when a fresh read of the registry's
    index does not list the coordinates the local row claims — which is what a
    poisoned cache looks like from here. Left to a rotating log alone, that event
    ages out.
    """

    def _sink(self, monkeypatch):
        seen: list[tuple[str, str, str]] = []

        class _Sel:
            def log_api_access(self, **kw):
                seen.append(
                    (kw.get("operation", ""), kw.get("outcome", ""), kw.get("resources", ""))
                )

        monkeypatch.setattr(reg_mod, "_sel_fn", lambda: _Sel())
        return seen

    def _pin_owner(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])

    @pytest.mark.asyncio
    async def test_a_coordinate_mismatch_is_recorded_as_denied(self, monkeypatch):
        self._pin_owner(monkeypatch)
        seen = self._sink(monkeypatch)

        async def _fetch(repo, branch):
            return [{"name": "app", "gitUrl": SIBLING, "branch": "main"}]

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)
        poisoned = {
            "_registry": "official",
            "name": "app",
            "gitUrl": "https://forge.example.com/org/private.git",
            "branch": "main",
        }
        assert await _owner_tier_confirmed(poisoned) is False
        assert len(seen) == 1
        op, outcome, resources = seen[0]
        assert outcome == "denied"
        assert "coordinates_not_in_fresh_index" in resources
        assert op == "install_from_registry_owner_tier"

    @pytest.mark.asyncio
    async def test_an_unreachable_index_is_recorded_as_denied(self, monkeypatch):
        self._pin_owner(monkeypatch)
        seen = self._sink(monkeypatch)

        async def _boom(repo, branch):
            raise OSError("forge unreachable")

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _boom)
        entry = {"_registry": "official", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is False
        assert [o for _, o, _ in seen] == ["denied"]
        assert "index_unreadable" in seen[0][2]

    @pytest.mark.asyncio
    async def test_a_benign_non_escalation_is_not_recorded(self, monkeypatch):
        """A default-tier registry is not a credential decision.

        Recording these would put a row in SEL for every browse and bury the
        refusals that matter.
        """
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_INDEX}])
        seen = self._sink(monkeypatch)
        entry = {"_registry": "official", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is False
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_confirmed_escalation_is_recorded_once_as_granted(self, monkeypatch):
        """The grant is logged by the install site, not twice by the predicate."""
        self._pin_owner(monkeypatch)
        seen = self._sink(monkeypatch)

        async def _fetch(repo, branch):
            return [{"name": "app", "gitUrl": SIBLING, "branch": "main"}]

        monkeypatch.setattr(reg_mod, "_fetch_external_registry_index", _fetch)
        entry = {"_registry": "official", "name": "app", "gitUrl": SIBLING, "branch": "main"}
        assert await _owner_tier_confirmed(entry) is True
        assert seen == []


class TestLoggedUrlsCarryNoCredentials:
    """Clone URLs are index-supplied and may embed credentials.

    They reach the SEL audit trail (dashboard-readable) and warning logs, both of
    which persist, so the credential must be stripped — while keeping enough of
    the URL that the record still says WHICH repository was cloned.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "https://user:token@forge.example.com/o/r.git",
                "https://[redacted]@forge.example.com/o/r.git",
            ),
            (
                "https://token@forge.example.com/o/r.git",
                "https://[redacted]@forge.example.com/o/r.git",
            ),
            ("ssh://git@forge.example.com/o/r.git", "ssh://[redacted]@forge.example.com/o/r.git"),
            ("git@forge.example.com:o/r.git", "[redacted]@forge.example.com:o/r.git"),
            # No userinfo: unchanged, so the common case stays fully readable.
            ("https://forge.example.com/o/r.git", "https://forge.example.com/o/r.git"),
            ("", ""),
        ],
    )
    def test_userinfo_is_stripped_and_the_repository_kept(self, raw, expected):
        assert reg_mod._redact_url_userinfo(raw) == expected

    def test_the_sel_audit_record_carries_no_credential(self, monkeypatch):
        seen: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                seen.append(kw.get("resources", ""))

        monkeypatch.setattr(reg_mod, "_sel_fn", lambda: _Sel())
        reg_mod._sel_credential_grant(
            "install_from_registry", "https://user:token@forge.example.com/o/r.git"
        )
        assert seen == ["owner_designated_clone url=https://[redacted]@forge.example.com/o/r.git"]
        assert "token" not in seen[0]


class TestTrustTierDoesNotWidenHostTrust:
    """The tier changes the credential posture, never the reachable host set."""

    def test_owner_tier_does_not_make_an_unrelated_host_cloneable(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert is_clone_host_trusted("https://attacker.example.net/x.git") is False

    def test_trusted_host_set_is_identical_across_tiers(self, monkeypatch):
        _with_config(monkeypatch, [])
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_INDEX}])
        as_index = _configured_registry_hosts()
        _with_loader(monkeypatch, [{"name": "official", "repo": FORGE, "trust": _TRUST_OWNER}])
        assert _configured_registry_hosts() == as_index
