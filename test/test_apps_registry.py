"""Registry listing tests: trust fields, storefront rows and the git-install build step.
They cover ``_entry_git_url`` tolerance, the minimal child environment's Windows case
folding, the ``_apply_trust_fields`` API trust boundary, external registries on the online
catalog path, where and with which interpreter a git install builds, the registry keys
``_merge_manifest`` projects, and a catalog failure that never breaks the store. The other
registry topics live beside this file: subprocess-timeout reaping in
``test_apps_registry_timeout_reaping.py``, the identity and admission gates in
``test_apps_registry_identity_admission.py``, git transport hardening in
``test_apps_registry_git_transport.py`` and the post-script desktop gates in
``test_apps_registry_desktop_layout.py``. Their shared autouse fixtures are in
``apps_registry_helpers.py``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.manifest import AppManifest


def test_entry_git_url_tolerates_non_string_values():
    """An external index can carry an object-valued gitUrl; the resolver must
    degrade to "no URL" instead of crashing every caller with AttributeError."""
    assert registry._entry_git_url({"gitUrl": {"evil": True}}) == ""
    assert registry._entry_git_url({"gitUrl": ["x"], "repo": None}) == ""
    assert registry._entry_git_url({"repo": 42}) == ""
    assert registry._entry_git_url({"gitUrl": " https://ok.example/r.git "}) == "https://ok.example/r.git"


class TestMinimalEnvHonorsWindowsCaseInsensitivity:
    """Windows env names are case-INSENSITIVE and `os.environ` upper-cases keys.

    So `os.environ.items()` yields `SYSTEMROOT`, never the `SystemRoot` spelling
    Microsoft documents and that the allowlist writes. A literal membership test
    therefore dropped exactly the variables the list carries for Windows — silently
    at the boundary, fatally in the child: a Windows process without `SystemRoot`
    cannot resolve side-by-side assemblies and dies before `main()`.
    """

    def test_upper_cased_windows_keys_are_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            registry.os,
            "environ",
            {"SYSTEMROOT": r"C:\Windows", "USERPROFILE": r"C:\Users\me", "TEMP": r"C:\Temp"},
        )
        env = registry.minimal_env()
        assert env["SYSTEMROOT"] == r"C:\Windows"
        assert env["USERPROFILE"] == r"C:\Users\me"
        assert env["TEMP"] == r"C:\Temp"

    def test_folding_did_not_admit_secrets(self, monkeypatch) -> None:
        """The fold widens CASE, never the key set."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            registry.os,
            "environ",
            {"SYSTEMROOT": r"C:\Windows", "GITHUB_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "s"},
        )
        env = registry.minimal_env()
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_posix_matching_stays_exact(self, monkeypatch) -> None:
        """`PATH` and `Path` are DIFFERENT variables on POSIX.

        Folding there would let a lookalike through, so the fold is Windows-only.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(registry.os, "environ", {"PATH": "/usr/bin", "Path": "/sneaky"})
        env = registry.minimal_env()
        assert env["PATH"] == "/usr/bin"
        assert "Path" not in env


class TestApplyTrustFields:
    """``_apply_trust_fields`` is the API trust boundary of
    ``GET /api/apps/registry``: ``provenance``/``verified`` are
    computed server-side where the ``_registry`` tag is authoritative, and
    ``featured`` is stripped from external rows. Every branch below mirrors a
    spoof that must be blocked server-side, not by scattered client-side checks.
    """

    def test_external_entry_is_never_verified_despite_spoofed_fields(self):
        """An external index publishing author/origin/featured spoofs gains
        nothing: the row is external because the server tagged it."""
        entry = {
            "name": "evil-app",
            "_registry": "evil-registry",
            "author": "KiroCrew",       # brand-ok: author-spoof fixture
            "origin": "builtin",        # origin spoof
            "featured": True,           # spotlight self-flag
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False
        assert "featured" not in out

    def test_external_entry_cannot_pre_seed_trust_fields(self):
        """Index-published ``provenance``/``verified`` values are OVERWRITTEN,
        not merely defaulted — otherwise an index could ship them directly."""
        entry = {
            "name": "evil-app",
            "_registry": "evil-registry",
            "provenance": "official",
            "verified": True,
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False

    def test_stargazers_count_is_stripped_from_external_rows(self):
        """A trust cue only the publisher may mint — the ``featured``
        precedent: an external index self-reporting ``★ 50K`` on a hostile
        app would render identically to a signed-catalog count, so the field
        is dropped entirely from external rows regardless of validity."""
        entry = {"name": "ext-app", "_registry": "labs", "stargazersCount": 1234}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_stargazers_count_survives_on_non_external_rows(self):
        """Catalog/seed rows (no ``_registry`` marker) keep a sanitized count;
        zero is a real count, distinct from the absent field."""
        entry = {"name": "cat-app", "stargazersCount": 1234}
        (out,) = registry._apply_trust_fields([entry])
        assert out["stargazersCount"] == 1234
        entry_zero = {"name": "cat-app-2", "stargazersCount": 0}
        (out_zero,) = registry._apply_trust_fields([entry_zero])
        assert out_zero["stargazersCount"] == 0

    @pytest.mark.parametrize(
        "bad",
        [-1, "1234", True, False, 3.5, None, [12], {"n": 12}, 9_007_199_254_740_992],
        ids=["negative", "string", "true", "false", "float", "none", "list", "dict", "over-js-max"],
    )
    def test_stargazers_count_non_int_values_are_dropped(self, bad):
        """The shape check runs on the boundary EVERY row crosses
        (``_resolve_manifest`` passes the row through unchanged on a failed
        manifest fetch, so the allowlist projection is not a guaranteed
        exit) — a malformed count is dropped, never coerced. The upper bound
        is the JS safe-integer range: Python accepts a 309-digit int that
        JavaScript renders as hundreds of digits."""
        entry = {"name": "seed-app", "stargazersCount": bad}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_stargazers_count_at_the_js_safe_integer_bound_survives(self):
        entry = {"name": "cat-app", "stargazersCount": 9_007_199_254_740_991}
        (out,) = registry._apply_trust_fields([entry])
        assert out["stargazersCount"] == 9_007_199_254_740_991

    def test_stargazers_count_sanitized_on_non_external_rows_too(self):
        """Sanitization applies to every row, not just the external branch."""
        entry = {"name": "seed-app", "stargazersCount": "9999"}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_builtin_row_without_stargazers_count_is_unaffected(self):
        entry = {"name": "demo", "origin": "builtin"}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out
        assert out["provenance"] == "builtin"
        assert out["verified"] is True

    def test_clone_target_is_server_overwritten_and_prefers_git_url(self):
        """The modal must show what install will clone, not the legacy alias.

        External rows legitimately carry both fields with different values;
        ``_entry_git_url`` makes ``gitUrl`` authoritative. An index-supplied
        ``trustRepository`` is ignored rather than becoming consent authority.
        """
        entry = {
            "name": "external-app",
            "_registry": "labs",
            "repo": "https://example.test/display/alias",
            "gitUrl": "HTTPS://Clone.Example.test/Owner/App.git/",
            "trustRepository": "https://evil.example/spoof",
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["trustRepository"] == "https://clone.example.test/Owner/App"

    def test_clone_target_normalization_strips_userinfo_preserves_port_and_path(self):
        target = "HTTPS://User:SeCrEt@EXAMPLE.COM:08443/Owner/Repo.git/"

        assert registry._normalize_git_target(target) == (
            "https://example.com:08443/Owner/Repo"
        )

    def test_metadata_normalization_redacts_colon_bearing_ssh_userinfo(self):
        target = "SSH://Git:T%2FAB@[2001:DB8::A]:2222/Owner/Repo"

        assert registry._normalize_git_target(target) == (
            "ssh://Git@[2001:db8::a]:2222/Owner/Repo"
        )

    def test_clone_target_normalization_drops_query_and_fragment_secrets(self):
        assert registry._normalize_git_target(
            "HTTPS://EXAMPLE.COM?Ref=Case#Frag"
        ) == "https://example.com"

    def test_clone_target_userinfo_is_not_repository_identity(self):
        assert registry._same_git_target(
            "https://User:Secret@EXAMPLE.COM/o/r",
            "https://user:secret@example.com/o/r",
        )

    def test_clone_target_scp_username_is_preserved_without_folding_path(self):
        assert registry._normalize_git_target("Git@EXAMPLE.COM:Owner/Repo") == (
            "Git@EXAMPLE.COM:Owner/Repo"
        )
        assert not registry._same_git_target(
            "Git@EXAMPLE.COM:Owner/Repo", "EXAMPLE.COM:owner/Repo"
        )

    @pytest.mark.parametrize(
        "target,expected",
        [
            (
                "https://user:token@example.test/Owner/Repo.git",
                "https://example.test/Owner/Repo.git",
            ),
            (
                "https://example.test/Owner/Repo.git?access_token=secret#private",
                "https://example.test/Owner/Repo.git",
            ),
            (
                "ssh://deploy@example.test/Owner/Repo.git",
                "ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "ssh://deploy:password@example.test/Owner/Repo.git",
                "ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "git+ssh://deploy:password@example.test/Owner/Repo.git",
                "git+ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "deploy@example.test:Owner/Repo.git",
                "deploy@example.test:Owner/Repo.git",
            ),
            (
                "deploy:password@example.test:Owner/Repo.git",
                "deploy@example.test:Owner/Repo.git",
            ),
            (
                "deploy:password@[unterminated:Owner/Repo.git",
                "deploy@[unterminated:Owner/Repo.git",
            ),
        ],
    )
    def test_clone_target_secret_stripping_preserves_ssh_routing(
        self, target: str, expected: str
    ):
        assert registry._strip_git_target_userinfo(target) == expected

    def test_colon_bearing_ssh_userinfo_is_never_a_repository_identity(self):
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://deploy:new@example.test/Owner/Repo.git",
        )
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://release:old@example.test/Owner/Repo.git",
        )
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://deploy:old@example.test/Owner/Repo.git",
        )

    @pytest.mark.parametrize("suffix", ["?repo=A", "#private-ref"])
    def test_unsupported_suffix_is_never_a_repository_identity(self, suffix):
        ambiguous = f"https://example.test/Owner/Repo.git{suffix}"
        safe = "https://example.test/Owner/Repo.git"

        assert not registry._same_git_target(ambiguous, safe)
        assert not registry._same_git_target(ambiguous, ambiguous)

    def test_ambiguous_scp_prefix_is_never_a_repository_identity(self):
        raw = "deploy:password@example.invalid:Owner/Repo.git"
        sanitized = "deploy@example.invalid:Owner/Repo.git"

        assert registry._git_target_has_ambiguous_scp_prefix(raw)
        assert not registry._same_git_target(raw, sanitized)
        assert not registry._same_git_target(raw, raw)

    def test_ambiguous_ssh_userinfo_is_never_a_repository_identity(self):
        raw = "ssh://deploy:password@example.invalid/Owner/Repo.git"
        sanitized = "ssh://deploy@example.invalid/Owner/Repo.git"

        assert registry._git_target_has_ambiguous_ssh_userinfo(raw)
        assert not registry._same_git_target(raw, sanitized)
        assert not registry._same_git_target(raw, raw)

    def test_clone_target_scp_parser_handles_long_unterminated_authority_linearly(self):
        """An attacker-sized invalid SCP target stays local/unmodified.

        This shape made the former nested/alternating fullmatch backtrack over
        the entire authority.  The deterministic parser performs only bounded
        ``find``/scan passes, so increasing the input cannot cause regex DoS.
        """
        target = "user@[" + ("a" * 200_000) + ":Owner/Repo"

        assert registry._strip_git_target_userinfo(target) == target
        assert registry._normalize_git_target(target) == target

    def test_clone_target_ports_are_not_silently_equivalent(self):
        assert not registry._same_git_target(
            "https://example.com:8443/o/r",
            "https://example.com:8444/o/r",
        )
        assert not registry._same_git_target(
            "https://example.com:443/o/r",
            "https://example.com/o/r",
        )

    def test_clone_target_ipv6_host_case_is_cosmetic_but_path_case_is_not(self):
        assert registry._same_git_target(
            "ssh://git@[2001:DB8::A]:2222/Owner/Repo",
            "SSH://git@[2001:db8::a]:2222/Owner/Repo",
        )
        assert not registry._same_git_target(
            "ssh://git@[2001:DB8::A]:2222/Owner/Repo",
            "ssh://git@[2001:db8::a]:2222/owner/Repo",
        )

    @pytest.mark.parametrize(
        "target",
        [
            r"C:\Work\Owner\Repo",
            "/Tmp/Owner/Repo",
        ],
    )
    def test_clone_target_non_uri_forms_are_not_guessed(self, target: str):
        assert registry._normalize_git_target(target) == target

    def test_clone_target_malformed_unbracketed_ipv6_is_fail_conservative(self):
        assert registry._normalize_git_target(
            "SSH://2001:DB8::1/Owner/Repo"
        ) == "ssh://2001:DB8::1/Owner/Repo"

    def test_clone_target_spoof_is_removed_when_no_repository_resolves(self):
        entry = {
            "name": "local-app",
            "trustRepository": "https://evil.example/spoof",
        }
        (out,) = registry._apply_trust_fields([entry])
        assert "trustRepository" not in out

    def test_storefront_clone_coordinates_never_expose_userinfo(self):
        secret = "SuperSecret"
        entry = {
            "name": "credentialed-app",
            "gitUrl": f"HTTPS://User:{secret}@Clone.Example.test/Owner/App.git",
            "repo": f"https://Alias:{secret}@display.example.test/Owner/App",
        }

        (out,) = registry._apply_trust_fields([entry])

        assert out["trustRepository"] == "https://clone.example.test/Owner/App"
        assert out["gitUrl"] == "HTTPS://Clone.Example.test/Owner/App.git"
        assert out["repo"] == "https://display.example.test/Owner/App"
        assert secret not in str(out)

    @pytest.mark.parametrize("suffix", ["?repo=A&access_token=secret", "#private"])
    def test_storefront_never_mints_proof_for_unsupported_clone_suffix(self, suffix):
        entry = {
            "name": "ambiguous-app",
            "gitUrl": f"https://clone.example.test/Owner/App.git{suffix}",
        }

        (out,) = registry._apply_trust_fields([entry])

        assert "trustRepository" not in out
        assert "secret" not in str(out)

    @pytest.mark.parametrize(
        ("raw", "sanitized"),
        [
            (
                "deploy:password@example.invalid:Owner/App.git",
                "deploy@example.invalid:Owner/App.git",
            ),
            (
                "ssh://deploy:password@example.invalid/Owner/App.git",
                "ssh://deploy@example.invalid/Owner/App.git",
            ),
        ],
    )
    def test_storefront_never_mints_proof_for_ambiguous_git_target(
        self, raw, sanitized
    ):
        entry = {"name": "ambiguous-app", "gitUrl": raw}

        (out,) = registry._apply_trust_fields([entry])

        assert "trustRepository" not in out
        assert out["gitUrl"] == sanitized
        assert raw not in str(out)

    def test_legacy_query_selected_repository_cannot_resolve_as_trust_proof(self):
        installed = {
            "name": "legacy-app",
            "source": "registry:legacy-app",
            "sourceUrl": "https://clone.example.test/Owner/App.git?repo=A",
        }
        assert registry.resolve_installed_trust_repository(installed) == (False, "")

        installed.pop("sourceUrl")
        assert registry.resolve_installed_trust_repository(
            installed,
            registry_entry={
                "gitUrl": "https://clone.example.test/Owner/App.git?repo=B"
            },
        ) == (False, "")

    @pytest.mark.parametrize(
        "raw",
        [
            "deploy:password@example.invalid:Owner/App.git",
            "ssh://deploy:password@example.invalid/Owner/App.git",
        ],
    )
    def test_legacy_ambiguous_repository_cannot_resolve_as_trust_proof(self, raw):
        installed = {
            "name": "legacy-app",
            "source": "registry:legacy-app",
            "sourceUrl": raw,
        }
        assert registry.resolve_installed_trust_repository(installed) == (False, "")

        installed.pop("sourceUrl")
        assert registry.resolve_installed_trust_repository(
            installed, registry_entry={"gitUrl": raw}
        ) == (False, "")

    def test_legacy_installed_row_uses_current_authoritative_clone_target(self):
        entry = {
            "name": "legacy-app",
            "repo": "https://example.test/display/alias",
            "gitUrl": "https://clone.example.test/Owner/current-app.git",
        }
        installed = {
            "legacy-app": {
                "name": "legacy-app",
                "source": "registry:legacy-app",
            }
        }

        bindings = registry._trust_repository_bindings([entry], installed)
        [out] = registry._apply_trust_fields(
            [entry], trust_repositories=bindings
        )

        assert out["trustRepository"] == (
            "https://clone.example.test/Owner/current-app"
        )

    def test_local_installed_row_does_not_inherit_same_named_registry_target(self):
        entry = {
            "name": "local-app",
            "gitUrl": "https://clone.example.test/Owner/registry-app.git",
        }
        installed = {
            "local-app": {
                "name": "local-app",
                "source": "C:/operator/local-app",
                "origin": "local",
            }
        }

        bindings = registry._trust_repository_bindings([entry], installed)
        [out] = registry._apply_trust_fields(
            [entry], trust_repositories=bindings
        )

        assert "trustRepository" not in out

    def test_core_kirocrew_index_author_is_verified(self):
        """``verified`` derives from the INDEX-declared author snapshot
        (``_index_author``, taken by ``list_registry`` pre-merge)."""
        entry = {"name": "good-app", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is True

    def test_manifest_author_alone_never_mints_verified(self):
        """A third-party core repo publishing ``"author": "kirocrew"`` in its
        app.json gains nothing: the merged ``author`` display field is not
        consulted, only the pre-merge index snapshot is."""
        entry = {"name": "sneaky", "author": "KiroCrew"}  # merged, no snapshot  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is False
        entry = {"name": "sneaky2", "author": "KiroCrew", "_index_author": "third-party"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is False

    def test_core_third_party_author_is_not_verified_and_keeps_featured(self):
        entry = {"name": "community-app", "_index_author": "someone", "featured": 2}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is False
        assert out["featured"] == 2  # curator flag preserved for core entries

    def test_builtin_origin_is_verified_builtin(self):
        entry = {"name": "builtin-app", "origin": "builtin", "author": "x"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "builtin"
        assert out["verified"] is True

    def test_non_string_index_author_does_not_crash_and_is_not_verified(self):
        """External registries are user-supplied JSON; a mistyped author must
        degrade to unverified, not raise."""
        entry = {"name": "weird", "_index_author": 42}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is False

    def test_bundled_seed_row_is_official_not_a_separate_value(self):
        """The bundled ``app-registry.json`` is the OFFLINE SEED of the list we
        publish, not a different kind of app, so it carries the same provenance
        a signed remote catalog will. Giving the seed its own value would put a
        weaker integrity guarantee — it rides on the install artifact and cannot
        be revoked before the next release — behind a label the client cannot
        tell apart from the stronger one. Provenance names WHOSE list an app is
        on; how the list arrived is a separate axis."""
        entry = {"name": "launchdarkly", "repo": "https://example.com/org/app"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"

    def test_index_author_snapshot_never_leaks_into_payload(self):
        entry = {"name": "x", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert "_index_author" not in out

    def test_two_word_org_spelling_is_verified(self):
        """The product name is two words, and both the bundled catalog and the
        official published catalog state the org that way. A single-token-only
        comparison silently un-verified every first-party app whose index row
        spelled the org correctly."""
        entry = {"name": "spec-builder", "_index_author": "Kiro Crew"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is True

    @pytest.mark.parametrize(
        "spelling",
        [
            "Ｋｉｒｏ　Ｃｒｅｗ",  # fullwidth, ideographic space
            "kiro\u200bcrew",  # zero-width space
            "Kiro\u00adCrew",  # soft hyphen
            "  kiro   crew  ",  # padded, doubled inner space
            "KIROCREW",
        ],
    )
    def test_first_party_spelling_variants_still_verify(self, spelling):
        """A row we ship or sign may legitimately name us in a non-ASCII form.
        Folding (NFKC + drop category-Cf + collapse whitespace) keeps the mark
        instead of dropping it on a spelling difference a human cannot see."""
        entry = {"name": "app", "_index_author": spelling}
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is True

    def test_folding_does_not_grant_the_mark_to_an_external_row(self):
        """The fold widens the match, so pin the short-circuit that keeps it
        harmless: a tagged row is unverified BEFORE the author is consulted."""
        entry = {"name": "app", "_registry": "labs", "_index_author": "Kiro Crew"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False

    def test_near_miss_author_is_not_verified(self):
        """Folding must not blur a DIFFERENT name into ours."""
        for name in ("kiro crews", "kiro-crew", "kirocrew labs", "crew kiro"):
            entry = {"name": "app", "_index_author": name}
            (out,) = registry._apply_trust_fields([entry])
            assert out["verified"] is False, name

    def test_registry_tag_is_kept_in_payload(self):
        """``_registry`` stays in the row — the external-source label text and
        older clients still need it. The change ADDS fields only."""
        entry = {"name": "ext", "_registry": "labs"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["_registry"] == "labs"

    @pytest.mark.asyncio
    async def test_list_registry_stamps_trust_fields(self, monkeypatch):
        """End-to-end: every row returned by ``list_registry`` carries the
        server-computed fields; external spoofs and a manifest-published
        ``author: "kirocrew"`` are all neutralized."""
        core = {"name": "core-app", "author": "KiroCrew", "featured": 1}  # brand-ok: author-spoof fixture
        # Third-party core entry whose REPO manifest claims the first-party
        # author (index declares none) — must not mint the badge.
        sneaky = {"name": "sneaky-app"}
        # Index entry trying to pre-seed the internal snapshot key directly.
        preseed = {"name": "preseed-app", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        ext = {
            "name": "ext-app",
            "_registry": "labs",
            "author": "KiroCrew",  # brand-ok: author-spoof fixture
            "origin": "builtin",
            "featured": True,
        }
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [core, sneaky, preseed]
        )

        async def _fake_external():
            return [ext]

        async def _fake_resolve(entry):
            # Simulate the app.json merge overwriting the display author.
            if entry["name"] == "sneaky-app":
                return {**entry, "author": "KiroCrew"}  # brand-ok: author-spoof fixture
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        rows = {r["name"]: r for r in await registry.list_registry()}
        assert rows["core-app"]["provenance"] == "official"
        assert rows["core-app"]["verified"] is True
        assert rows["core-app"]["featured"] == 1
        # Manifest-published author does not mint the badge.
        assert rows["sneaky-app"]["verified"] is False
        # Pre-seeded snapshot key is overwritten from the entry's own author
        # (absent here) before the manifest merge.
        assert rows["preseed-app"]["verified"] is False
        assert rows["ext-app"]["provenance"] == "external"
        assert rows["ext-app"]["verified"] is False
        assert "featured" not in rows["ext-app"]
        # The internal snapshot key never leaks into the API payload.
        assert all("_index_author" not in r for r in rows.values())


# ---------------------------------------------------------------------------
# External registries must surface on the ONLINE catalog path.
#
# Regression: handle_registry prefers list_catalog_apps when the published
# catalog is reachable and only falls back to list_registry (the sole path that
# merged external registries) when the catalog is empty. So a configured
# external app was silently dropped from the store the moment the catalog came
# online. list_catalog_apps now appends external-registry rows itself.
# ---------------------------------------------------------------------------
_PINNED_SHA = "a" * 40


def _pinned_catalog_entry(name: str) -> dict[str, Any]:
    """A catalog entry `official_catalog.inventory` accepts as installable.

    The real `inventory` runs over this, so the coordinates have to satisfy its
    validation (https clone URL, full-length lowercase-hex pin) rather than being
    waved through by a stub.
    """
    return {
        "name": name,
        "source": {
            "type": "git",
            "url": f"https://github.com/org/{name}",
            "ref": _PINNED_SHA,
        },
    }


class TestCatalogAppsIncludesExternalRegistries:
    @pytest.mark.asyncio
    async def test_external_registry_app_appears_when_catalog_is_online(self, monkeypatch):
        """With a NON-EMPTY catalog (so the catalog path is taken), a configured
        external app still shows up — tagged external, not-installed — and a
        same-named catalog row wins the dedup."""
        # Catalog is reachable and non-empty: this forces the list_catalog_apps
        # path rather than the offline list_registry fallback.
        catalog_rows = [
            {"name": "catalog-app", "displayName": "Catalog App"},
            # Collision: the catalog also lists a name an external registry uses.
            {"name": "shared-app", "displayName": "Official Shared App"},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        # Seed only matters for the git-row installable filter; keep it empty.
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        external_rows = [
            {"name": "labs-app", "repo": "x", "_registry": "labs"},
            # Same name as a catalog row — the catalog row must win.
            {"name": "shared-app", "repo": "y", "_registry": "labs"},
        ]

        async def _fake_external():
            return external_rows

        async def _fake_resolve(entry):
            # Manifests already present in the index fixture; return as-is.
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}

        # The external-only app is present, tagged external, and not installed.
        assert "labs-app" in rows
        assert rows["labs-app"]["_registry"] == "labs"
        assert rows["labs-app"]["provenance"] == "external"
        assert rows["labs-app"]["verified"] is False
        assert rows["labs-app"]["installed"] is False
        # The collision resolves to the catalog row (official), not the external one.
        assert rows["shared-app"]["displayName"] == "Official Shared App"
        assert rows["shared-app"]["provenance"] != "external"
        # The plain catalog app is untouched.
        assert "catalog-app" in rows
        # The internal snapshot key never leaks into the API payload.
        assert all("_index_author" not in r for r in rows.values())

    @pytest.mark.asyncio
    async def test_detect_installed_only_external_app_reads_installed_on_catalog_path(
        self, monkeypatch
    ):
        """Install-status PARITY with the offline path: an external app known ONLY
        via its detectInstalled probe (absent from installed_map) must read
        installed=True on the ONLINE catalog path too, because that path now runs
        the same probe and passes the resulting `detected` into enrichment."""
        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "catalog-app", "displayName": "Catalog App"}],
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        async def _fake_external():
            return [
                {
                    "name": "detect-app",
                    "repo": "z",
                    "_registry": "labs",
                    "detectInstalled": "true",
                }
            ]

        async def _fake_resolve(entry):
            return entry

        # Stand in for the real subprocess probe: report installed the same way
        # _detect_installed_probe would for a returncode-0 command.
        async def _fake_probe(entries, installed_map):
            return {e["name"] for e in entries if e.get("detectInstalled")}

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)
        monkeypatch.setattr(registry, "_detect_installed_probe", _fake_probe)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert rows["detect-app"]["installed"] is True

    @pytest.mark.asyncio
    async def test_external_row_cannot_shadow_filtered_catalog_git_name(self, monkeypatch):
        """GPT BLOCK regression: a catalog `git` row dropped by the installability
        filter must still RESERVE its name, so an external row with the same name
        is deduped away and can never become the row install-by-name resolves."""
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            # git source, and NOT in the seed installable set below -> filtered out.
            {"name": "filtered-git", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        # The catalog pins nothing either, so the row stays filtered -- and the
        # listing never reaches for a real fetch.
        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", lambda: []
        )

        async def _fake_external():
            # External registry tries to claim the filtered-out catalog name.
            return [{"name": "filtered-git", "repo": "evil", "_registry": "labs"}]

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        # The catalog git row was filtered out AND the external row was reserved
        # away, so the name is absent entirely -- crucially it never appears as an
        # EXTERNAL row pointing at the "evil" repo.
        assert rows.get("filtered-git", {}).get("provenance") != "external"
        assert "filtered-git" not in rows

    # -----------------------------------------------------------------------
    # Regression: the storefront intersected every catalog `git` row with the
    # BUNDLED SEED, which ships only at release cadence. `inventory` was added so
    # the catalog itself could supply validated pinned coordinates, and the install
    # path honours them (`inventory_for_install`) -- but this listing was written a
    # day earlier and still asked the seed. The two resolvers disagreed: install
    # accepted a catalog-only app while the store hid it, so a freshly published
    # app was undiscoverable until a release shipped a new seed.
    # -----------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_a_catalog_pinned_git_row_is_listed_without_a_seed_entry(self, monkeypatch):
        """A `git` row the catalog PINS is listed even though the seed omits it."""
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            {"name": "pinned-app", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        # The seed names NOTHING -- the catalog alone has to carry this row.
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [_pinned_catalog_entry("pinned-app")],
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "pinned-app" in rows, "a catalog-pinned git row must reach the store"
        # It is an official row, and still unverified -- listing it does not mint
        # the first-party badge from a document trusted only as far as TLS.
        assert rows["pinned-app"]["provenance"] != "external"
        assert rows["pinned-app"]["verified"] is False
        assert rows["pinned-app"]["trustRepository"] == (
            "https://github.com/org/pinned-app"
        )
        assert "keep-app" in rows

    @pytest.mark.asyncio
    async def test_a_name_only_the_local_cache_claims_is_not_listed(self, monkeypatch):
        """The unlock is authorised by the FRESH document, never by the cache.

        `list_catalog_rows` reads the cache under the data home, which is
        agent-writable. A planted row there must not BECOME a listed row: it would
        render with official provenance and dedupe the real same-named external row
        out of the listing, so a consent prompt would describe an official app while
        the name grant it produces installs the external one.
        """
        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "planted-app", "source": {"type": "git"}}],
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        # The fetched document pins a DIFFERENT app, so it never authorises
        # "planted-app" -- only the cache claims that name.
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [_pinned_catalog_entry("honest-app")],
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "planted-app" not in rows

    @pytest.mark.asyncio
    async def test_an_unreachable_catalog_degrades_to_the_seed(self, monkeypatch):
        """A listing must never fail because the catalog cannot be reached.

        Without the fresh document there is nothing authorising the catalog-only
        row, so it stays filtered -- the listing this path produced before the
        catalog could supply coordinates -- and the rest of the store still renders.
        """
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            {"name": "pinned-app", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        def _unavailable():
            raise registry.official_catalog.CatalogUnavailable("cdn is down")

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", _unavailable
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "pinned-app" not in rows
        assert "keep-app" in rows, "the rest of the store still renders"

    @pytest.mark.asyncio
    async def test_no_fresh_fetch_when_the_seed_already_covers_every_git_row(
        self, monkeypatch
    ):
        """The storefront is the hot path, so the fetch is paid only when it can
        change the answer. With every `git` row already seeded there is nothing to
        unlock, and the listing must stay at one cached read."""
        catalog_rows = [{"name": "seeded-app", "source": {"type": "git"}}]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [
                {
                    "name": "seeded-app",
                    "repo": "https://example.test/display/alias",
                    "gitUrl": "https://clone.example.test/owner/seeded-app.git",
                }
            ],
        )
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        calls: list[int] = []

        def _counting():
            calls.append(1)
            return []

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", _counting
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert calls == [], "the seed already answers, so no fetch may be paid"
        assert "seeded-app" in rows
        assert rows["seeded-app"]["trustRepository"] == (
            "https://clone.example.test/owner/seeded-app"
        )


# ---------------------------------------------------------------------------
# Git-install build step: the interpreter, and where the build runs.
#
# Both properties below were broken and NEITHER had a test, which is why they
# survived — and both fail SILENTLY, reporting a successful install that installed
# nothing the gateway can import.
# ---------------------------------------------------------------------------


def _build_cmds_for(tmp_path, monkeypatch, files: dict[str, str]) -> list[list[str]]:
    """Run ``_run_app_build``'s command planning without executing anything.

    Captures the argv list rather than asserting on side effects: the point of both
    tests is WHICH command would run, and executing a real pip install in a unit test
    would be both slow and environment-dependent.
    """
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    captured: list[list[str]] = []

    class _EmptyStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Ok:
        returncode = 0
        stdout = _EmptyStdout()

        async def wait(self):
            return 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return _Ok()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_exec)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="standard", **_k: (list(cmd), None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
    _pin_pip_importable(monkeypatch)
    return captured


def _pin_pip_importable(monkeypatch) -> None:
    """Make the planner see a gateway interpreter that HAS ``pip``.

    ``_run_app_build`` probes ``importlib.util.find_spec("pip")`` on the running
    interpreter and soft-skips the Python build when it is absent. That probe
    reads the test host's own packaging: a venv created by ``uv`` (or
    ``--without-pip``) ships no ``pip`` module, so without this pin every
    "a pip command is planned" assertion fails on such a host while the same
    test passes on a stdlib venv. The soft-skip branch has its own test that
    pins the opposite answer.
    """
    import site

    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)
    real_find_spec = importlib.util.find_spec

    def _with_pip(name, *args, **kwargs):
        if name == "pip":
            return importlib.machinery.ModuleSpec("pip", loader=None)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(registry.importlib.util, "find_spec", _with_pip)


@pytest.mark.asyncio
async def test_python_build_uses_the_running_interpreter_not_path_pip(tmp_path, monkeypatch):
    """A Python app must install into the interpreter that will IMPORT it.

    ``shutil.which("pip")`` resolves to whatever pip is first on PATH, which is
    routinely NOT the gateway's: ``bin/kirocrew`` execs ``.venv/bin/kirocrew`` without
    putting the venv's ``bin/`` on PATH, and ``service_path()`` prepends
    ``~/.local/bin`` ahead of it.

    The failure mode is silent, which is what made it survive. Measured on a host whose
    first pip was 3.7 and whose gateway venv was 3.12: a *compatible-but-different* pip
    (3.10) reported "Successfully installed", the build reported success, and the package
    landed in ``~/.local/lib/python3.10/site-packages`` — invisible to the gateway, and
    a venv sets ``ENABLE_USER_SITE = False`` so there is no fallback.

    Asserting ``sys.executable`` rather than "not the string 'pip'" so the test states
    the property (install into THIS interpreter) instead of banning one spelling.
    """
    captured = _build_cmds_for(
        tmp_path, monkeypatch, {"pyproject.toml": "[project]\nname='x'\nversion='0'\n"}
    )
    # A PATH pip that is emphatically not us — the old code would have used it.
    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}")

    await registry._run_app_build(
        tmp_path, "x", [], manifest=AppManifest.from_dict({}), self_managed=False
    )

    assert captured, "a pyproject.toml must produce a build command"
    argv = captured[0]
    assert argv[0] == sys.executable, f"build must use the running interpreter, got {argv[0]!r}"
    assert argv[1:4] == ["-s", "-m", "pip"], f"expected `-s -m pip`, got {argv[1:4]!r}"


@pytest.mark.asyncio
async def test_python_build_soft_skips_when_the_interpreter_has_no_pip(tmp_path, monkeypatch):
    """A gateway interpreter without a ``pip`` module must skip the Python build,
    not abort the whole registry install.

    The docstring promises "no npm / no pip" is a soft skip, and the npm branch
    honours it. Planning ``[sys.executable, "-m", "pip", "install", "."]`` without
    checking availability breaks that contract: a venv created with
    ``--without-pip`` (or any minimal runtime) has no ``pip`` module, so ``-m pip``
    exits non-zero and the install fails with ``build failed (exit N)``.
    ``importlib.util.find_spec("pip")`` checks the gateway interpreter and lets the
    build soft-skip with a logged warning when pip is absent, mirroring npm.
    """
    log_lines: list[str] = []
    captured: list[list[str]] = []

    async def _fake_exec(*argv, **_kwargs):  # pragma: no cover - must NOT run
        captured.append(list(argv))
        raise AssertionError("no build command may be planned when pip is unavailable")

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_exec)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")

    # This interpreter has no importable `pip` — exactly a `--without-pip` venv.
    real_find_spec = importlib.util.find_spec

    def _no_pip(name, *args, **kwargs):
        if name == "pip":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(registry.importlib.util, "find_spec", _no_pip)

    result = await registry._run_app_build(
        tmp_path, "x", log_lines, manifest=AppManifest.from_dict({}), self_managed=False
    )

    assert result == {"ok": True}, f"a pip-less interpreter must soft-skip, got {result}"
    assert captured == [], f"no build command may be planned, got {captured}"
    assert any(
        "pip not available" in line for line in log_lines
    ), f"the skip must be logged, log was {log_lines}"


@pytest.mark.asyncio
async def test_a_monorepo_subdirectory_is_built_not_the_clone_root(tmp_path, monkeypatch):
    """The build must run where the package IS, not at the clone root.

    A monorepo registry entry declares ``subdirectory``; joining that only AFTER the
    build would make the build look for pyproject.toml at the clone root,
    found none, logged "No build step detected — using source as-is" and returned
    ok=True having installed nothing.
    """
    captured: list = []
    manifests: list = []

    async def _fake_build(build_dir, app_name, log_lines, *, manifest, self_managed, verb):
        captured.append(build_dir)
        # The build's desktop gate decides from the manifest the identity gate
        # already read, so the caller must hand that over rather than re-reading
        # an app-writable file -- and from the entry's ownership, which no
        # manifest field carries. The verb of the run rides along for the
        # refusal lines the build streams.
        manifests.append(manifest)
        return {"ok": True}

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, **kwargs):
        sub = pkg_dir / "apps" / "my-tool"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "pyproject.toml").write_text("[project]\n", "utf-8")
        # The identity gate reads app.json from the declared subdirectory and
        # fails closed on a mismatch — the cloned repo must declare the name.
        (sub / "app.json").write_text(json.dumps({"name": "my-tool"}), "utf-8")
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    await registry._clone_build_app_locked(
        "https://example.invalid/r.git",
        "my-tool",
        [],
        subdirectory="apps/my-tool",
        pending_cleanup=[],
    )

    assert captured, "the build must be attempted"
    assert (
        captured[0].name == "my-tool" and captured[0].parent.name == "apps"
    ), f"build ran in {captured[0]} — expected the declared subdirectory"
    assert [m.name for m in manifests] == ["my-tool"], (
        f"the build must receive the cloned manifest, got {manifests}"
    )


@pytest.mark.asyncio
async def test_a_traversing_subdirectory_does_not_choose_the_build_dir(tmp_path, monkeypatch):
    """``subdirectory`` is untrusted index content, so it must not escape the clone.

    The identity gate joins ``subdirectory`` under the clone root with a
    containment check and FAILS CLOSED on an escaping value — no build command
    may run in a directory chosen by a traversing path.
    """
    captured: list = []

    async def _fake_build(build_dir, app_name, log_lines):
        captured.append(build_dir)
        return {"ok": True}

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, **kwargs):
        pkg_dir.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    result = await registry._clone_build_app_locked(
        "https://example.invalid/r.git",
        "evil",
        [],
        subdirectory="../../etc",
        pending_cleanup=[],
    )

    assert result["ok"] is False
    assert "unsafe subdirectory" in result["error"]
    assert captured == [], f"build ran despite a traversing subdirectory: {captured}"


class TestMergeManifestProjectsRegistryKeys:
    """``_merge_manifest`` starts from an explicit projection of the index row.

    An index row is untrusted content, so a key an index invents must not ride
    into the API payload just because the merge started from a copy of the row.
    """

    MANIFEST = {"name": "demo-app", "displayName": "Demo App", "version": "1.2.3"}

    def test_unknown_index_key_does_not_reach_the_row(self):
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "surpriseKey": "whatever an index felt like publishing",
            "__proto__": {"polluted": True},
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert "surpriseKey" not in out
        assert "__proto__" not in out

    def test_index_cannot_publish_trust_or_install_state(self):
        """These are stamped server-side after the merge; an index value for
        them must not survive to be read before that happens."""
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "provenance": "builtin",
            "verified": True,
            "installed": True,
            "enabled": True,
            "origin": "builtin",
            "lifecycle": "locked",
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        for key in ("provenance", "verified", "installed", "enabled", "origin", "lifecycle"):
            assert key not in out, key

    def test_index_cannot_override_manifest_display_copy(self):
        """Display fields come from the fetched app.json, so an index row that
        publishes its own must not win — nor survive alongside."""
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "displayName": "Index Said This",
            "description": "index copy",
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out["displayName"] == "Demo App"
        assert "description" not in out  # manifest carried none, so neither does the row

    @pytest.mark.parametrize(
        "key,value",
        [
            ("gitUrl", "https://example.com/org/app.git"),
            ("repo", "DemoRepo"),
            ("branch", "release"),
            ("subdirectory", "apps/demo"),
            ("resources", "app"),
            ("detectInstalled", "which demo"),
            ("managed", True),
            ("featured", 2),
            ("stargazersCount", 42),
            ("_registry", "labs"),
        ],
    )
    def test_registry_owned_keys_survive(self, key, value):
        """Each of these has a reader — the clone path, the install path, the
        spotlight, or the trust stamp. Dropping one breaks that reader."""
        entry = {"name": "demo-app", key: value}
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out[key] == value

    def test_index_author_snapshot_survives_the_merge(self):
        """``_apply_trust_fields`` runs AFTER the merge and consumes this key to
        decide the verified mark, so the projection has to carry it through."""
        entry = {"name": "demo-app", "_index_author": "Kiro Crew"}
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out["_index_author"] == "Kiro Crew"

    def test_dark_icon_path_becomes_a_blob_url(self):
        """A raster icon cannot repaint from theme tokens, so an app may ship a
        dark variant; it routes through the same proxy as the light one."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        manifest = {**self.MANIFEST, "iconPath": "a/i.png", "iconPathDark": "a/i-dark.png"}
        out = registry._merge_manifest(entry, manifest)
        assert out["iconUrl"] == "/api/apps/blob?repo=DemoRepo&path=a/i.png"
        assert out["iconUrlDark"] == "/api/apps/blob?repo=DemoRepo&path=a/i-dark.png"

    def test_dark_icon_is_omitted_when_absent(self):
        """Absence must not publish an empty string: the client treats a falsy
        dark variant as "fall back to the light one", and an empty key would
        also widen the payload for every app that ships one icon."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        out = registry._merge_manifest(entry, {**self.MANIFEST, "iconPath": "a/i.png"})
        assert "iconUrlDark" not in out

    def test_manifest_declared_icon_url_is_never_copied(self):
        """An index-fetched manifest is untrusted content. Honouring an absolute
        ``iconUrl`` from it would let a third party point the store's <img> at
        any host; only repo-relative paths rewritten through our proxy are used."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        manifest = {
            **self.MANIFEST,
            "iconUrl": "https://evil.example/track.png",
            "iconUrlDark": "https://evil.example/track-dark.png",
        }
        out = registry._merge_manifest(entry, manifest)
        assert "iconUrl" not in out
        assert "iconUrlDark" not in out


class TestCatalogFailureNeverBreaksTheStore:
    """`list_registry` runs inside `GET /api/apps/registry` with no try/except
    above it, so anything escaping the catalog step is a 500 for the whole store.

    The catalog is an ENHANCEMENT to a listing that is already complete without
    it, which is what makes containment at this seam correct rather than merely
    defensive: the fallback is not a degraded guess, it is exactly what the store
    rendered before the catalog existed. Review found three separate escape
    routes inside the module, each narrower than this seam -- these tests pin the
    seam so a fourth one cannot reach a user.
    """

    async def _rows(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "seed-app"}]
        )

        async def _no_external():
            return []

        async def _passthrough(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _no_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _passthrough)
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        return {r["name"]: r for r in await registry.list_registry()}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("unexpected"),
            TypeError("a field was not the type we assumed"),
            KeyError("name"),
            AttributeError("None has no attribute get"),
            ValueError("bad value"),
        ],
        ids=lambda e: type(e).__name__,
    )
    async def test_a_raising_loader_still_returns_the_seed(self, exc, monkeypatch):
        def boom():
            raise exc

        monkeypatch.setattr(registry.official_catalog, "fetch_inventory_entries", boom)
        rows = await self._rows(monkeypatch)
        assert "seed-app" in rows, "the seed listing must survive a catalog failure"

    @pytest.mark.asyncio
    async def test_a_raising_annotate_still_returns_the_seed(self, monkeypatch):
        """The overlay is the half that touches untrusted field types, so it is
        the half most likely to raise on a document we did not anticipate."""
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [{"name": "seed-app"}],
        )

        def boom(rows, entries):
            raise TypeError("hostile field type")

        monkeypatch.setattr(registry.official_catalog, "annotate", boom)
        rows = await self._rows(monkeypatch)
        assert "seed-app" in rows

    @pytest.mark.asyncio
    async def test_the_failure_is_logged_rather_than_swallowed(
        self, monkeypatch, caplog
    ):
        """A broad catch is only acceptable because it is loud: without the
        traceback this would hide our own bugs instead of a bad document.

        Patched at `fetch_inventory_entries` because that is the source the listing
        now uses; the cache-fed loader is not on this path at all.
        """

        def boom():
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", boom
        )
        with caplog.at_level("WARNING", logger=registry.logger.name):
            await self._rows(monkeypatch)
        assert any("catalog" in r.message for r in caplog.records)
        assert any(r.exc_info for r in caplog.records), "expected a traceback"
