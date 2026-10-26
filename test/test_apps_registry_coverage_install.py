"""Behaviour coverage for ``install_from_registry``'s refusals: the refusal line's verb,
the post-rejection un-poison routine, and every refusal that returns before a clone.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import bundled_interpreter_fixture  # noqa: F401
from apps_registry_coverage_helpers import _DATA_IS_A_FILE, _asgi_backend, _fake_sandbox, _FakeProc

from kiro_crew.apps import registry


class TestRefusalLineVerb:
    """The streamed refusal line names the action the run performs -- "install" or
    "update", read from the installed record the way the page titles its log
    panel -- one whole string per verb, at every site that streams a refusal."""

    def test_one_whole_string_per_verb(self):
        assert registry._REFUSAL_LINES == {
            "install": "Refusing install: {reason}",
            "update": "Refusing update: {reason}",
        }
        assert registry._refusal_line("install", "why") == "Refusing install: why"
        assert registry._refusal_line("update", "why") == "Refusing update: why"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["install", "update"])
    async def test_the_build_pass_refusal_names_the_verb_it_was_given(
        self, tmp_path, monkeypatch, bundled_interpreter, verb
    ):
        """Same refusal as ``TestRunAppBuild``'s root-`data` test, on both verbs: the sentence
        returned as `error` is the same, only the streamed line's verb follows the
        action -- and the default is the fresh install every caller meant before
        the two were told apart."""
        home = tmp_path / "home"
        source = home / "app-sources" / "demo"
        source.mkdir(parents=True)
        (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        (source / "server.py").write_text("", encoding="utf-8")
        (source / "data").write_text("a file\n", encoding="utf-8")
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: home / "app-sources")
        monkeypatch.setattr(registry, "preserved_data_awaits", lambda name: False)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        result = await registry._run_app_build(
            source, "demo", log, manifest=_asgi_backend(), self_managed=False, verb=verb
        )
        assert result["ok"] is False
        assert result["error"] == _DATA_IS_A_FILE, result
        assert log == [f"Refusing {verb}: {_DATA_IS_A_FILE}"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["install", "update"])
    async def test_the_identity_refusal_names_the_verb_it_was_given(
        self, tmp_path, monkeypatch, verb
    ):
        monkeypatch.setattr(registry, "sel", lambda: MagicMock())
        clone = tmp_path / "app-sources" / "demo"
        clone.mkdir(parents=True)
        log: list[str] = []
        result = await registry._refuse_identity_mismatch(
            "demo",
            "other",
            "https://example.com/demo.git",
            clone,
            log,
            created_this_run=True,
            verb=verb,
        )
        assert result["ok"] is False
        assert log[-1] == f"Refusing {verb}: {result['error']}"
        # The reason's own verb follows too: "Refusing update: ... refusing to
        # install" would hand the reader the very contradiction the line removes.
        assert f"refusing to {verb} an app under an identity" in result["error"]


# ---------------------------------------------------------------------------
# Post-rejection un-poison
# ---------------------------------------------------------------------------


class TestUnpoisonRejectedCheckout:
    @pytest.mark.asyncio
    async def test_fresh_checkout_is_deleted_without_residue(self, tmp_path):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "app.json").write_text("{}", encoding="utf-8")
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=False, pre_pull_commit=""
        )
        assert not pkg.exists()

    @pytest.mark.asyncio
    async def test_previous_checkout_is_restored_into_the_slot(self, tmp_path):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()
        (stale / "mine.txt").write_text("local edit", encoding="utf-8")
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=False,
            pre_pull_commit="",
            restore_from=stale,
        )
        assert (pkg / "mine.txt").read_text(encoding="utf-8") == "local edit"
        assert any("Restored the previous checkout" in line for line in log)

    @pytest.mark.asyncio
    async def test_failed_restore_tells_the_user_where_the_files_are(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()

        def _boom(self, target):
            raise OSError("locked")

        monkeypatch.setattr(Path, "rename", _boom)
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=False,
            pre_pull_commit="",
            restore_from=stale,
        )
        assert any("retained there for manual recovery" in line for line in log)

    @pytest.mark.asyncio
    async def test_preexisting_checkout_is_rolled_back_to_its_pre_pull_commit(
        self, tmp_path, monkeypatch
    ):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        spawned = _fake_sandbox(monkeypatch, [_FakeProc(returncode=0), _FakeProc(returncode=0)])
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=True, pre_pull_commit="b" * 40
        )
        assert spawned[0] == ["git", "reset", "--keep", "b" * 40]
        assert spawned[1] == ["git", "--literal-pathspecs", "checkout", "--", "app.json"]
        assert pkg.is_dir()  # the workspace is preserved
        assert any("Rolled checkout back to pre-update commit" in line for line in log)

    @pytest.mark.asyncio
    async def test_failed_rollback_and_restore_both_warn(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=1), _FakeProc(returncode=1)])
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=True, pre_pull_commit="c" * 40
        )
        assert any("could not roll the checkout back" in line for line in log)
        assert any("could not restore app.json" in line for line in log)

    @pytest.mark.asyncio
    async def test_manifest_snapshot_restores_exact_pre_update_bytes(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "app.json").write_text('{"name": "evil"}', encoding="utf-8")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_snapshot=b'{"name": "demo"}',
        )
        assert (pkg / "app.json").read_bytes() == b'{"name": "demo"}'
        assert any("exact pre-update contents" in line for line in log)

    @pytest.mark.asyncio
    async def test_a_sandbox_failure_never_masks_the_refusal(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        pkg.mkdir()

        wrapped: list[list[str]] = []

        def _boom(cmd, mode=""):
            wrapped.append(list(cmd))
            raise RuntimeError("sandbox unavailable")

        monkeypatch.setattr(registry, "wrap_argv", _boom)
        log: list[str] = []
        await registry._unpoison_rejected_checkout(
            "demo", pkg, log, checkout_preexisted=True, pre_pull_commit="d" * 40
        )
        # The failed reset does not skip the manifest restore: both git steps were
        # attempted, neither claimed success, and the workspace is left in place.
        assert wrapped == [
            ["git", "reset", "--keep", "d" * 40],
            ["git", "--literal-pathspecs", "checkout", "--", "app.json"],
        ]
        assert log == []
        assert pkg.is_dir()

    @pytest.mark.asyncio
    async def test_custom_manifest_relpath_is_used(self, tmp_path, monkeypatch):
        pkg = tmp_path / "demo"
        (pkg / "sub").mkdir(parents=True)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            [],
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b"{}",
        )
        assert (pkg / "sub" / "app.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# Pre-clone checkout probe
# ---------------------------------------------------------------------------


class TestPreexistingCheckoutProbe:
    """The ``.git`` probe that decides whether a refusal preserves or deletes the
    checkout runs on every install and update, so it must stay off the event loop
    like the commit and manifest snapshots that follow it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preexisting", [True, False])
    async def test_git_probe_runs_off_the_loop_thread(self, tmp_path, monkeypatch, preexisting):
        pkg = tmp_path / "app-sources" / "demo"
        pkg.mkdir(parents=True)
        if preexisting:
            (pkg / ".git").mkdir()
        probe_threads: list[int] = []
        real_is_dir = Path.is_dir

        def _recording_is_dir(self, *args, **kwargs):
            if self == pkg / ".git":
                probe_threads.append(threading.get_ident())
            return real_is_dir(self, *args, **kwargs)

        commit_probes: list[Path] = []

        def _resolved_commit(path):
            commit_probes.append(path)
            return "abc123"

        async def _refuse_clone(*args, **kwargs):
            # Stop right after the pre-clone snapshot: nothing past it is under test.
            return {"ok": False, "name": "demo", "error": "clone refused"}

        monkeypatch.setattr(registry, "app_source_dir", lambda name: pkg)
        monkeypatch.setattr(registry, "_resolved_clone_commit", _resolved_commit)
        monkeypatch.setattr(registry, "_git_clone_or_pull", _refuse_clone)
        monkeypatch.setattr(registry, "sel", lambda: MagicMock())
        monkeypatch.setattr(Path, "is_dir", _recording_is_dir)

        loop_thread = threading.get_ident()
        result = await registry._clone_build_app("https://example.com/demo.git", "demo", [])

        assert result["error"] == "clone refused"
        assert probe_threads, "the pre-clone .git probe never ran"
        assert loop_thread not in probe_threads
        # The probe's answer still drives the pre-pull commit snapshot.
        assert commit_probes == ([pkg] if preexisting else [])


# ---------------------------------------------------------------------------
# install_from_registry — pre-clone refusals
# ---------------------------------------------------------------------------


class TestInstallFromRegistryRefusals:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's own no-side-effect guard, moved verbatim; no test in the class calls monkeypatch.undo()
    def _no_side_effects(self, monkeypatch, tmp_path):
        """Every test here must return before any clone/build/registration.

        ``app-sources`` is redirected at *tmp_path* as a belt-and-braces guard:
        the refusals all return before the stale-checkout sweep, and this makes
        a future regression fail loudly in the sandbox instead of quietly
        touching the real Kiro Crew home.
        """
        monkeypatch.setattr(registry, "sel", lambda: MagicMock())
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path / "app-sources")

        async def _never_clone(*a, **k):
            raise AssertionError("install must refuse before cloning")

        monkeypatch.setattr(registry, "_clone_build_app", _never_clone)

    @pytest.mark.asyncio
    async def test_provenance_mismatch_is_refused_and_audited(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_resolve_install_entry", lambda name: (None, "pinned elsewhere")
        )
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "name": "demo", "error": "pinned elsewhere"}

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_mask_the_refusal(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_resolve_install_entry", lambda name: (None, "pinned elsewhere")
        )
        broken = MagicMock()
        broken.log_api_access.side_effect = RuntimeError("sel down")
        monkeypatch.setattr(registry, "sel", lambda: broken)
        result = await registry.install_from_registry("demo")
        assert result["error"] == "pinned elsewhere"

    @pytest.mark.asyncio
    async def test_unknown_app_is_reported_as_not_found(self, monkeypatch):
        monkeypatch.setattr(registry, "_resolve_install_entry", lambda name: (None, ""))
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "error": "app 'demo' not found in registry"}

    @pytest.mark.asyncio
    async def test_entry_without_a_git_url_is_refused(self, monkeypatch):
        monkeypatch.setattr(registry, "_resolve_install_entry", lambda name: ({"name": "demo"}, ""))
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "error": "app 'demo' has no git URL configured"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("git_url", "reason"),
        [
            (
                "https://example.test/owner/repo.git?repo=A&access_token=secret-a",
                "query",
            ),
            (
                "https://example.test/owner/repo.git?repo=B&access_token=secret-b",
                "query",
            ),
            ("ssh://deploy@example.test/owner/repo.git#private-ref", "query"),
            (
                "deploy:password@example.invalid:owner/repo.git",
                "ambiguous Git transport",
            ),
            (
                "ssh://deploy:password@example.invalid/owner/repo.git",
                "ambiguous Git transport",
            ),
        ],
    )
    async def test_unsupported_clone_target_refuses_before_trust_or_fetch(
        self, monkeypatch, git_url, reason
    ):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": name, "gitUrl": git_url}, ""),
        )

        async def _never_fetch(*args, **kwargs):
            raise AssertionError("unsupported clone target must fail before fetch")

        def _never_check_trust(*args, **kwargs):
            raise AssertionError("unsupported clone target must fail before trust")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _never_fetch)
        monkeypatch.setattr(registry, "repository_bound_grant_denied", _never_check_trust)
        result = await registry.install_from_registry("demo")

        assert result["ok"] is False
        assert result["code"] == "invalid_registry_source"
        assert reason in result["error"]
        assert "secret" not in result["error"]
        assert git_url not in result["error"]

    @pytest.mark.asyncio
    async def test_trust_repository_mismatch_refuses_before_fetch_or_clone(self, monkeypatch):
        granted = "https://User:GrantedSecret@example.test/owner/consented.git"
        resolved = "https://User:ResolvedSecret@example.test/owner/rebound.git"
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": resolved}, ""),
        )
        monkeypatch.setattr(registry, "trusted_app_repository", lambda name: granted)
        monkeypatch.setattr(
            registry,
            "repository_bound_grant_denied",
            lambda name, **kwargs: (
                "execution trust does not match the current registry source; "
                "revoke the existing grant and grant it again"
            ),
        )
        audit = MagicMock()
        monkeypatch.setattr(registry, "sel", lambda: audit)

        result = await registry.install_from_registry("demo")

        assert result["ok"] is False
        assert result["code"] == "app_trust_repository_mismatch"
        # Clone coordinates are comparison inputs, not API/log data: they may
        # contain embedded credentials and must not be reflected on refusal.
        assert granted not in result["error"]
        assert resolved not in result["error"]
        assert "GrantedSecret" not in result["error"]
        assert "ResolvedSecret" not in result["error"]
        assert "grant it again" in result["error"]
        audit.log_api_access.assert_called_once_with(
            caller="app_install_from_registry",
            operation="trust_repository_mismatch",
            outcome="rejected",
            resources="name='demo'",
            error=result["error"],
        )

    @pytest.mark.asyncio
    async def test_legacy_name_grant_refuses_before_manifest_fetch_or_clone(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.config.loader import _invalidate_config_cache

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": ["demo"]}}),
            encoding="utf-8",
        )
        _invalidate_config_cache()
        secret = "ResolvedSecret"
        resolved = f"https://User:{secret}@example.test/owner/rebound.git"
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": resolved}, ""),
        )

        async def _never_fetch(*args, **kwargs):
            raise AssertionError("legacy trust must refuse before manifest fetch")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _never_fetch)
        audit = MagicMock()
        monkeypatch.setattr(registry, "sel", lambda: audit)

        result = await registry.install_from_registry("demo")

        assert result["ok"] is False
        assert result["code"] == "app_execution_denied"
        assert "predates repository binding" in result["error"]
        assert secret not in result["error"]
        assert resolved not in result["error"]
        audit.log_api_access.assert_called_once_with(
            caller="app_install_from_registry",
            operation="trust_repository_binding_required",
            outcome="rejected",
            resources="name='demo'",
            error=result["error"],
        )
        assert secret not in str(audit.log_api_access.call_args)

    @pytest.mark.asyncio
    async def test_local_grant_over_local_install_names_the_reinstall_route(
        self, tmp_path, monkeypatch
    ):
        """A re-grant here would record the local source again, so the refusal
        must not carry the code that reopens the consent dialog."""
        from kiro_crew.config.loader import _invalidate_config_cache

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_trusted": ["demo"],
                        "apps_trusted_local": ["demo"],
                        "apps_trusted_repositories": {},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()
        resolved = "https://example.test/owner/demo.git"
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": resolved}, ""),
        )
        monkeypatch.setattr(
            registry,
            "get_app",
            lambda name: {"name": "demo", "source": str(tmp_path / "src" / "demo")},
        )

        async def _never_fetch(*args, **kwargs):
            raise AssertionError("local-only trust must refuse before manifest fetch")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _never_fetch)
        audit = MagicMock()
        monkeypatch.setattr(registry, "sel", lambda: audit)

        result = await registry.install_from_registry("demo")

        assert result["ok"] is False
        assert result["code"] == "app_trust_local_only"
        assert "uninstall the app (its data is kept)" in result["error"]
        assert "App Store" in result["error"]
        assert resolved not in result["error"]
        audit.log_api_access.assert_called_once_with(
            caller="app_install_from_registry",
            operation="trust_repository_binding_required",
            outcome="rejected",
            resources="name='demo'",
            error=result["error"],
        )

    @pytest.mark.asyncio
    async def test_admission_denial_stops_before_the_clone(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="", **_k: "banned"
        )
        result = await registry.install_from_registry("demo")
        assert result["ok"] is False
        assert "blocked by admission policy: banned" in result["error"]

    @pytest.mark.asyncio
    async def test_client_only_app_asks_for_a_local_install(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            # A platform this host is not: "haiku" is never sys.platform.
            return {
                "name": "demo",
                "platform": {
                    "os": ["haiku"],
                    "installMode": "client",
                    "clientInstall": {"cmd": "brew install demo"},
                },
            }

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="", **_k: None
        )
        result = await registry.install_from_registry("demo")
        assert result["needsClientInstall"] is True
        assert result["clientInstall"] == {"cmd": "brew install demo"}
        assert result["platform"]["required"] == ["haiku"]

    @pytest.mark.asyncio
    async def test_min_version_gate_refuses_an_old_gateway(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo", "minKiroCrewVersion": "999.0.0"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="", **_k: None
        )
        monkeypatch.setattr("kiro_crew.apps.version.check_min_version", lambda mv: "needs 999.0.0")
        result = await registry.install_from_registry("demo")
        assert result == {"ok": False, "name": "demo", "error": "needs 999.0.0"}

    @pytest.mark.asyncio
    async def test_execution_denial_carries_the_consent_error_code(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_resolve_install_entry",
            lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
        )

        async def _manifest(*a, **k):
            return {"name": "demo"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(
            registry, "app_admission_denied", lambda name, manifest=None, action="", **_k: None
        )
        monkeypatch.setattr(
            registry,
            "app_execution_denied",
            lambda name, action="", caller="", repository=None: "needs a trust grant",
        )
        result = await registry.install_from_registry("demo")
        assert result["ok"] is False
        assert result["code"] == "app_execution_denied"
        assert "needs a trust grant" in result["error"]
