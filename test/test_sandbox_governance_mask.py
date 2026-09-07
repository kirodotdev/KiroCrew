"""The OS-level sandbox mask covers the crew data home's governance tree.

``security.sensitive_home_dirs()`` is the agent-TOOL gate: it is what
``is_sensitive_path`` refuses for a file_read/file_write tool call. The dir lists in
``sandbox.py`` are a separate, OS-level gate, and a spawned shell command reaches a path
fenced only by the first one. These tests pin the reconciliation between them:

* every crew-home entry on the tool gate has one of three sandbox dispositions,
* the ceilings are exposed READ-ONLY rather than hidden, in every mode,
* the deliberate read-write exceptions are exactly the declared set, in every mode.

The third is the one worth failing loudly: an entry that quietly moves from "masked" to
"exception" is a ceiling the agent can rewrite again.
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

from kiro_crew import sandbox, security

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


def _home() -> str:
    return os.path.expanduser("~")


def _crew_path(prefix: str, leaf: str) -> str:
    """Spell a crew-home target the way the production builders do.

    They join a SINGLE relative string onto the home, so the forward slashes inside it
    survive and Windows gains exactly one native separator. Joining prefix and leaf as
    separate components instead adds a second one, and the resulting mixed-separator
    string matches nothing the builders emit.
    """
    return os.path.join(_home(), f"{prefix}/{leaf}")


def _launcher_sets(mode: str) -> tuple[set[str], set[str], set[str]]:
    """``(hidden_dirs, readonly, hidden_files)`` as the generated launcher declares them."""
    script = sandbox._build_launcher_script(mode)

    def _grab(name: str) -> set[str]:
        match = re.search(rf"{name} = (\[.*?\])\n", script, re.S)
        assert match, f"{name} missing from the launcher"
        return set(json.loads(match.group(1)))

    return _grab("SENSITIVE_DIRS"), _grab("READONLY_DIRS"), _grab("SENSITIVE_FILES")


def _crew_sensitive_paths() -> list[str]:
    """Absolute crew-data-home paths the tool gate declares sensitive."""
    home = _home()
    return [
        os.path.join(home, rel)
        for rel in security.sensitive_home_dirs()
        if rel.startswith(_CREW_PREFIXES)
    ]


def _expected_exceptions() -> set[str]:
    home = _home()
    return {
        os.path.join(home, f"{prefix}/{leaf}")
        for prefix in _CREW_PREFIXES
        for leaf in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    }


class TestKeystonesAreSealedInEveryMode:
    """The ceiling files the issue named, on every backend the launcher feeds."""

    #: Named individually rather than looped from the module tuple: the point is that
    #: THESE paths are covered, so a test derived from the same tuple the production
    #: code reads would pass just as happily after someone emptied it.
    KEYSTONES = (
        "security_policy.json",
        "admission_policy.json",
        "app_admission.json",
        "profiles",
        "denied_commands.json",
        "computer_use.json",
        "oauth_endpoints.json",
        "aws_service_consent.json",
        # Recorded consent to deliver a scanner-flagged file (#7770). Sealing it
        # is the load-bearing half of the whole design: the deny-list tiers can
        # be evaded by runtime path construction, so only a kernel write denial
        # makes "the owner consents, never the agent" true rather than merely
        # intended.
        "file_delivery_consent.json",
        # The app dev-mode authorization record (#6907): sealing it is what
        # makes the operator-attestation flag unforgeable from an agent shell
        # — a sandboxed process cannot mint a grant however the toggle was
        # spelled.
        "apps/.dev-grants.json",
    )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", KEYSTONES)
    def test_linux_seals_the_ceiling_read_only(self, mode: str, prefix: str, leaf: str) -> None:
        hidden, readonly, _files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        assert target in readonly, f"{leaf} is writable through the {mode} sandbox"
        # Hiding a ceiling inverts its effect: an absent policy file resolves to the
        # permissive standalone default, and a script cron's ``boot_platform()`` runs
        # inside this namespace.
        assert target not in hidden, f"{leaf} must stay READABLE, not be masked"

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", KEYSTONES)
    def test_macos_denies_writes_to_the_ceiling(self, mode: str, prefix: str, leaf: str) -> None:
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-write* (literal "{target}"))' in profile
        assert f'(deny file-write* (subpath "{target}"))' in profile
        # A hardlink at a non-denied path would otherwise reach the same inode.
        assert f'(deny file-link (subpath "{target}"))' in profile
        assert f'(deny file-read* (subpath "{target}"))' not in profile

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_seal_survives_a_file_shaped_ceiling(self, mode: str) -> None:
        """The read-only loop must not guard on ``isdir``.

        ``security_policy.json`` is a plain file. An ``isdir`` guard skips it silently —
        no error, and the ceiling stays writable.
        """
        script = sandbox._build_launcher_script(mode)
        loop = script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]

        assert "os.path.exists(target)" in loop
        assert "os.path.isdir(target)" not in loop
        assert "_MS_REMOUNT | _MS_BIND | _MS_RDONLY" in loop


class TestSecretsAreMaskedInEveryMode:
    """Crew-home leaves with no in-sandbox reader are bind-masked, not merely sealed."""

    MASKED = (
        "token_signing.key",
        "refresh_chains.json",
        "kas",
        "mcp-apps",
        "ledger",
        "backup",
        "browser-cookies.txt",
        "playwright-storage-state.json",
        "playwright-extension-token",
        "ops_mission_control_secrets.json",
        "whatsapp",
        "apps/aws-control/data",
        "aws-control-staging",
        "workspace/md-notebook/pat",
        "data.sqlite3",
        "data.sqlite3-wal",
        "data.sqlite3-shm",
        "data.sqlite3-journal",
    )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_linux_masks_it(self, mode: str, prefix: str, leaf: str) -> None:
        hidden, _readonly, files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        # Both lists, because the child classifies by kind: a file entry reaching only
        # the directory loop is skipped by its ``isdir`` guard and stays readable.
        assert target in hidden, f"{leaf} readable through the {mode} sandbox"
        assert target in files, f"{leaf} would be skipped if it is a file"

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_macos_denies_reads(self, mode: str, prefix: str, leaf: str) -> None:
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-read* (subpath "{target}"))' in profile
        assert f'(deny file-link (subpath "{target}"))' in profile

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_macos_denies_writes_too(self, mode: str, prefix: str, leaf: str) -> None:
        """A read deny alone leaves the secret OVERWRITABLE.

        The Linux launcher binds an empty dir/file over the target, which blocks both
        directions in one rule. Seatbelt does not, and forging ``token_signing.key``
        needs no read at all — so the read deny on its own is not the control it looks
        like. Both spellings, because a leaf may be a plain file and no subpath rule
        addresses one.
        """
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-write* (literal "{target}"))' in profile


class TestTheReconciliationIsComplete:
    """No crew-home entry on the tool gate is left with no sandbox disposition."""

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_every_crew_sensitive_path_is_masked_sealed_or_a_declared_exception(
        self, mode: str
    ) -> None:
        hidden, readonly, _files = _launcher_sets(mode)
        exceptions = _expected_exceptions()

        unaccounted = [
            path
            for path in _crew_sensitive_paths()
            if path not in hidden and path not in readonly and path not in exceptions
        ]
        assert not unaccounted, (
            "crew-home paths the tool gate fences but the OS sandbox does not, and that "
            f"are not declared exceptions either: {unaccounted}"
        )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_read_write_exceptions_are_exactly_the_declared_set(self, mode: str) -> None:
        """A path drifting into the exception set is a ceiling the agent can rewrite.

        ``run`` is expected on the read-only list rather than fully read-write: it holds
        the launcher itself, so it must stay readable, and the voice-runtime rules
        already seal it. Every other exception is genuinely unrestricted.
        """
        hidden, readonly, _files = _launcher_sets(mode)
        unrestricted = {
            path for path in _crew_sensitive_paths() if path not in hidden and path not in readonly
        }
        declared = _expected_exceptions()

        assert (
            unrestricted <= declared
        ), f"undeclared read-write crew paths in {mode}: {sorted(unrestricted - declared)}"
        for path in declared - unrestricted:
            assert (
                path in readonly
            ), f"{path} is declared an exception but is neither read-write nor sealed"

    def test_the_exception_set_names_only_paths_the_tool_gate_fences(self) -> None:
        """An exception for a path nothing fences is dead weight that reads as a hole."""
        home = _home()
        fenced = {os.path.join(home, rel) for rel in security.sensitive_home_dirs()}

        for path in _expected_exceptions():
            assert path in fenced, f"{path} is exempted from a gate that never covered it"

    def test_the_three_dispositions_do_not_overlap(self) -> None:
        hidden = set(sandbox._CREW_HIDDEN_LEAVES)
        readonly = set(sandbox._CREW_READONLY_LEAVES)
        visible = set(sandbox._CREW_SANDBOX_VISIBLE_LEAVES)

        assert not hidden & readonly
        assert not hidden & visible
        assert not readonly & visible

    @pytest.mark.parametrize("mode", _MODES)
    def test_every_mode_carries_the_derived_set(self, mode: str) -> None:
        """The governance tree is masked at every level, the way ``.vault`` already is.

        A per-mode carve-out here would mean ``standard`` — the default — leaves the
        ceiling exposed, which is the configuration almost every install runs.
        """
        listing = {
            "standard": sandbox._STANDARD_DIRS,
            "cc": sandbox._CC_DIRS,
            "strict": sandbox._STRICT_DIRS,
        }[mode]

        for entry in sandbox._CREW_HIDDEN_DIRS:
            assert entry in listing, f"{entry} missing from the {mode} dir list"


class TestARelocatedDataHomeIsCoveredToo:
    """``KIROCREW_HOME`` outside ``$HOME`` must not escape the mask.

    Every dir-list entry is ``$HOME``-relative and joined with ``Path.home()``, so a
    managed fleet that relocates the data home would otherwise get no rule at all for the
    real governance tree — the ceiling left writable on exactly the installs most likely
    to have one.

    The expected target comes from ``config_dir()`` rather than from ``tmp_path`` spelled
    by hand: the resolver creates the directory and can resolve through a symlink, which
    is the ordinary case on macOS.
    """

    @staticmethod
    def _relocate(monkeypatch, tmp_path) -> str:
        from kiro_crew.config.paths import config_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-crew"))
        return str(config_dir())

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_launcher_masks_the_resolved_secret_leaves(self, mode, tmp_path, monkeypatch):
        root = self._relocate(monkeypatch, tmp_path)
        hidden, _readonly, files = _launcher_sets(mode)

        target = os.path.join(root, "token_signing.key")
        assert target in hidden, f"a relocated data home is unmasked in {mode}"
        # Both lists, because the child classifies by kind and this leaf is a file.
        assert target in files

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_launcher_seals_the_resolved_ceiling(self, mode, tmp_path, monkeypatch):
        root = self._relocate(monkeypatch, tmp_path)
        hidden, readonly, _files = _launcher_sets(mode)

        target = os.path.join(root, "security_policy.json")
        assert target in readonly, f"a relocated ceiling is writable in {mode}"
        assert target not in hidden, "it must stay readable — masking a ceiling removes it"

    @pytest.mark.parametrize("mode", _MODES)
    def test_the_seatbelt_profile_covers_the_resolved_paths(self, mode, tmp_path, monkeypatch):
        root = self._relocate(monkeypatch, tmp_path)
        profile = sandbox._build_seatbelt_profile(mode)

        ceiling = os.path.join(root, "security_policy.json")
        secret = os.path.join(root, "token_signing.key")
        assert f'(deny file-write* (literal "{ceiling}"))' in profile
        assert f'(deny file-read* (subpath "{secret}"))' in profile

    def test_the_default_layout_adds_no_duplicate_rule(self, monkeypatch, tmp_path):
        """De-duplication: where the two spellings match textually, no second rule."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "x" / ".kiro" / "crew"))
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path / "x"))

        assert sandbox._relocated_crew_targets(("security_policy.json",)) == []

    def test_a_resolution_failure_never_breaks_a_spawn(self, monkeypatch):
        """A spawn must not fail because the data home could not be resolved."""

        def _boom() -> object:
            raise RuntimeError("no home")

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(_boom))
        assert sandbox._relocated_crew_targets(("security_policy.json",)) == []


class TestThirdPartyCredentialsKeepTheirExistingTiering:
    """The reconciliation must not quietly re-tier the non-crew credential entries."""

    def test_standard_still_exposes_the_developer_workflow_dirs(self) -> None:
        """``standard`` deliberately leaves ``.aws``/``.kube`` readable.

        Masking them here would break an ordinary build running in the default mode; the
        crew governance tree is masked at every level precisely because nothing
        legitimate reads it.
        """
        assert ".aws" not in sandbox._STANDARD_DIRS
        assert ".kube" not in sandbox._STANDARD_DIRS
        assert ".aws" in sandbox._CC_DIRS
        assert ".aws" in sandbox._STRICT_DIRS

    @pytest.mark.parametrize("mode", _MODES)
    def test_the_macos_write_deny_does_not_reach_the_refreshable_dirs(self, mode: str) -> None:
        """``.aws`` is masked but must stay WRITABLE where it is exposed.

        A tool refreshing a cached token rewrites it, so the crew-home write deny is
        scoped to the crew leaves rather than applied to every hidden entry. Without this
        the scoping is free to erode into a blanket rule.
        """
        profile = sandbox._build_seatbelt_profile(mode)

        for leaf in (".aws", ".gnupg", ".docker"):
            target = os.path.join(_home(), leaf)
            assert f'(deny file-write* (subpath "{target}"))' not in profile

    def test_the_sso_cookie_store_is_masked_at_the_credential_tiers(self) -> None:
        """``.midway`` is a live bearer credential, the same class as ``.aws``."""
        assert ".midway" in sandbox._STRICT_DIRS
        assert ".midway" in sandbox._CC_DIRS
        assert ".midway" not in sandbox._STANDARD_DIRS

    def test_the_agent_runtime_auth_stores_stay_visible(self) -> None:
        """kiro-cli / amazon-q identity stores are fenced at the tool gate only.

        The agent runtime is itself spawned inside this sandbox and resolves its own
        access token from that store, so masking it would break the agent's model auth
        rather than protect anything. ``security.py`` states this explicitly.
        """
        for listing in (sandbox._STRICT_DIRS, sandbox._CC_DIRS, sandbox._STANDARD_DIRS):
            assert ".local/share/kiro-cli" not in listing
            assert ".local/share/amazon-q" not in listing


class TestAppBackendOwnedLeaves:
    """An app's OWN backend gets its state leaves back; nothing else changes (#8762).

    The md-notebook leaves are masked to fence agent subprocesses, but the Notes
    backend is itself a sandboxed spawn and is those files' only legitimate
    reader/writer. Masking it from itself made every attach/clone fail on the
    registry's final atomic rename. The spawn passes the resolved leaf paths as
    ``extra_visible_dirs``; these tests pin that the exemption lifts exactly those
    targets, on both platform builders, and that a default build keeps the mask.
    """

    LEAVES = (
        "workspace/md-notebook/pat",
        "workspace/md-notebook/vaults.json",
        "workspace/md-notebook/settings.json",
    )

    def test_the_helper_resolves_both_home_spellings(self) -> None:
        targets = sandbox.app_backend_visible_targets("md-notebook")

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                assert _crew_path(prefix, leaf) in targets

    def test_an_app_with_no_owned_leaves_gets_no_exemption(self) -> None:
        assert sandbox.app_backend_visible_targets("meetings") == ()
        assert sandbox.app_backend_visible_targets("no-such-app") == ()

    @_POSIX_ONLY
    def test_linux_unhides_the_owned_leaves_for_this_spawn(self) -> None:
        script = sandbox._build_launcher_script(
            "standard", extra_visible_dirs=sandbox.app_backend_visible_targets("md-notebook")
        )
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        hidden = set(json.loads(match.group(1)))

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                assert _crew_path(prefix, leaf) not in hidden, f"{leaf} still masked"

    def test_macos_drops_every_rule_for_the_owned_leaves(self) -> None:
        profile = sandbox._build_seatbelt_profile(
            "standard", extra_visible_dirs=sandbox.app_backend_visible_targets("md-notebook")
        )

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                target = _crew_path(prefix, leaf)
                # Read AND write, because the EPERM the issue reports is the write
                # side: the staged sibling temp is written fine and the rename onto
                # the masked literal is what Seatbelt refuses.
                assert f'"{target}"' not in profile, f"{leaf} still carries a deny rule"

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", LEAVES)
    def test_a_default_build_keeps_the_mask(self, mode: str, prefix: str, leaf: str) -> None:
        """No exemption without the spawn asking: agent subprocesses stay fenced."""
        hidden, _readonly, files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        assert target in hidden
        assert target in files

    def test_the_backend_spawn_passes_the_owned_leaves(self) -> None:
        """`apps/backend.py` must thread the helper's result into ``wrap_argv``.

        Asserted structurally rather than by a full spawn, which needs a manifest, a
        reserved port, and a live interpreter: the call site must derive its visible
        set from the helper, keyed by the app being spawned — and only behind the
        immutable-provenance gate, so a third-party install that claimed the builtin's
        name cannot spawn with the builtin's credential leaves unmasked.
        """
        import inspect

        from kiro_crew.apps import backend as backend_mod

        src = inspect.getsource(backend_mod._start_app_backend_body)
        assert "app_backend_visible_targets(app_name)" in src
        assert "is_builtin_app(app_root=execution_path, app_name=app_name)" in src


class TestForeignMaskShadowGuard:
    """A carve-out spelling beneath a FOREIGN mask is refused, not carved (#8795).

    ``_hidden_path_contains_visible_path`` cancels any hidden mask entry that
    CONTAINS a visible path, so an ``extra_visible_dirs`` spelling planted beneath
    an independently masked directory (a data home relocated under a credential
    tree) would take that whole foreign mask down with it. Both crew-home carve-out
    producers — the policy-cache site in ``apps/backend.py`` and
    ``app_backend_visible_targets`` — must refuse such a spelling and keep the mask;
    their consumers fail closed on the masked path, which is strictly safer.
    """

    def test_a_path_beneath_a_masked_credential_tree_is_shadowed(self) -> None:
        # ``.gnupg`` is masked at EVERY tier, standard included, so a data home
        # planted beneath it shadows at the tier the app-backend spawn asks for.
        planted = os.path.join(_home(), ".gnupg", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted)

    def test_standard_mode_ignores_a_strict_only_mask(self) -> None:
        """``standard`` deliberately leaves ``~/.aws`` visible; there is no mask
        to take down, so an ungoverned standard-mode carve-out must be allowed
        (refusing would break md-notebook and cache-only backends for nothing)."""
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert not sandbox.carveout_shadowed_by_foreign_mask(planted, mode="standard")

    def test_strict_mode_refuses_beneath_a_strict_mask(self) -> None:
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="strict")

    def test_cc_mode_refuses_beneath_a_cc_mask(self) -> None:
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="cc")

    def test_a_sibling_prefix_is_not_an_ancestor(self) -> None:
        """``~/.gnupg-backup`` shares ``~/.gnupg``'s string prefix but is not
        beneath it — a ``startswith`` misimplementation reds here."""
        planted = os.path.join(_home(), ".gnupg-backup", "crew", "policy_cache")
        assert not sandbox.carveout_shadowed_by_foreign_mask(planted)

    def test_the_producer_threads_its_mode_to_the_guard(self, monkeypatch, tmp_path) -> None:
        """``app_backend_visible_targets(mode=...)`` must reach the guard: the
        same relocated spelling under a strict-only mask survives a standard
        spawn and is dropped from a strict one."""
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".aws" / "relocated-crew"))
        reloc = str(tmp_path / ".aws" / "relocated-crew" / "workspace" / "md-notebook" / "pat")

        assert reloc in sandbox.app_backend_visible_targets("md-notebook")
        assert reloc not in sandbox.app_backend_visible_targets("md-notebook", mode="strict")

    def test_a_governance_floor_clamps_the_check_up(self, monkeypatch) -> None:
        """A ``sandbox.min_level`` floor raises the tier the masks are built for,
        so the shadow check must judge against the clamped tier, not the ask."""
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "strict")
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="standard")

    def test_a_masked_entry_itself_is_not_shadowed(self) -> None:
        """Equality is the carve-out's whole job; only a PROPER ancestor is foreign."""
        for target in (
            _crew_path(".kiro/crew", "workspace/md-notebook/pat"),
            # The default policy-cache spelling IS a masked entry (every tier list
            # carries ``.kiro/crew/policy_cache``); the cache-only carve-out must
            # keep working on a default layout.
            _crew_path(".kiro/crew", "policy_cache"),
        ):
            assert not sandbox.carveout_shadowed_by_foreign_mask(target), target

    def test_an_unmasked_location_is_not_shadowed(self) -> None:
        assert not sandbox.carveout_shadowed_by_foreign_mask(
            os.path.join(_home(), "projects", "notes")
        )

    def test_an_unresolvable_home_fails_toward_refusal(self, monkeypatch) -> None:
        """No mask universe to check means no carve-out, never a carve-anyway."""

        def _boom() -> object:
            raise RuntimeError("no home")

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(_boom))
        assert sandbox.carveout_shadowed_by_foreign_mask("/anywhere/at/all")

    def test_the_md_notebook_carveout_drops_a_shadowed_spelling(
        self, monkeypatch, tmp_path
    ) -> None:
        """A data home relocated beneath a masked tree keeps that tree's mask.

        ``.gnupg`` is masked at the ``standard`` tier the app-backend spawn asks
        for. ``Path.home`` is pinned to ``tmp_path`` so the resolver (which
        CREATES the relocated directory) never writes beneath the real home.
        """
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))

        # Positive first, so the absence loop below cannot pass vacuously: the
        # relocated spelling really is minted by the resolver and really does
        # trip the guard.
        shadowed_leaf = str(
            tmp_path / ".gnupg" / "relocated-crew" / "workspace" / "md-notebook" / "pat"
        )
        assert shadowed_leaf in sandbox._relocated_crew_targets(("workspace/md-notebook/pat",))
        assert sandbox.carveout_shadowed_by_foreign_mask(shadowed_leaf)

        targets = sandbox.app_backend_visible_targets("md-notebook")

        shadowed_root = str(tmp_path / ".gnupg") + os.sep
        assert targets, "the default home spellings must survive the refusal"
        for target in targets:
            assert not target.startswith(shadowed_root), f"{target} would unmask ~/.gnupg"
        # The refusal is per-spelling: the default-home entries are still carved.
        assert os.path.join(str(tmp_path), ".kiro/crew/workspace/md-notebook/pat") in targets

    def test_the_cache_site_guards_its_carveout(self) -> None:
        """`apps/backend.py` must thread the cache path through the shadow guard.

        Asserted structurally rather than by a full spawn (which needs a manifest, a
        reserved port, and a live interpreter), mirroring the sibling test above: the
        cache-only carve-out must consult the guard before widening ``_visible``.
        """
        import inspect

        from kiro_crew.apps import backend as backend_mod

        src = inspect.getsource(backend_mod._start_app_backend_body)
        assert "carveout_shadowed_by_foreign_mask(_cache_target)" in src
