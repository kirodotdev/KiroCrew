"""App identity + install provenance for registry installs.

Two defects are covered here:

1. ``install_from_registry`` resolved a catalog entry by name, cloned its repo,
   then installed under the CLONED manifest's name with no check that the two
   agreed — so a repo listed as X could install, register and run as Y.
2. Only a bare ``registry:<name>`` marker was persisted, so an update
   re-resolved the bare name and a same-named entry from a different registry
   source could capture it.

Mirrors the collaborator-patching style of ``test_apps_registry.py`` /
``test_external_registry.py``: no real git, no real network, everything isolated
via ``tmp_path`` + ``monkeypatch`` (so no xdist group marker is needed).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps import manager, registry

GOOD_URL = "https://github.com/good-org/shared-name.git"
EVIL_URL = "https://github.com/evil-org/shared-name.git"


@pytest.fixture(autouse=True)
def _admit_registry_execution(monkeypatch):
    """These tests must reach the admitted post-clone install path."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)


@pytest.fixture()
def sel_calls(monkeypatch) -> MagicMock:
    """Capture SEL audit emissions from the registry install path."""
    recorder = MagicMock()
    monkeypatch.setattr(registry, "sel", lambda: recorder)
    return recorder


def _audited_operations(recorder: MagicMock) -> list[str]:
    return [c.kwargs.get("operation") for c in recorder.log_api_access.call_args_list]


def _manifest(name: str, *, version: str = "1.0.0", **extra) -> dict:
    return {
        "name": name,
        "version": version,
        "displayName": name,
        "description": "identity/provenance fixture",
        "author": "tester",
        **extra,
    }


def _write_clone(root: Path, manifest: dict, *, commit: str = "", branch: str = "main") -> Path:
    """Materialize a fake cloned app source tree, optionally with git refs."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    if commit:
        heads = root / ".git" / "refs" / "heads"
        heads.mkdir(parents=True, exist_ok=True)
        (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        (heads / branch).write_text(commit + "\n", encoding="utf-8")
    return root


def _patch_clone(monkeypatch, clone: Path, seen: dict | None = None) -> None:
    """Stub the clone+build step so it 'produces' *clone*, recording its args."""

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        if seen is not None:
            seen["git_url"] = git_url
            seen["branch"] = branch
        return {"ok": True, "pkg_dir": clone}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)


def _patch_prefetch(monkeypatch, manifest: dict | None) -> None:
    monkeypatch.setattr(registry, "_fetch_app_manifest", AsyncMock(return_value=manifest))


def _forbid_spawn(monkeypatch) -> None:
    """Any subprocess from here on means a refused repo got to execute."""

    async def _boom(*args, **kwargs):
        pytest.fail("a refused registry install must not spawn a subprocess")

    monkeypatch.setattr(registry, "create_subprocess_limited", _boom)


# ---------------------------------------------------------------------------
# (a) Name squatting — entry X whose repo declares Y
# ---------------------------------------------------------------------------


class TestIdentityGate:
    @pytest.mark.asyncio
    async def test_squatting_repo_refused_with_no_install_and_no_residue(
        self, tmp_path, monkeypatch, sel_calls
    ):
        """Entry 'card-app' pointing at a repo that declares 'other-app' must be
        refused before its install script runs, install nothing under either
        name, and leave no clone behind."""
        entry = {"name": "card-app", "gitUrl": EVIL_URL, "repo": EVIL_URL, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, _manifest("card-app"))
        # The clone's own manifest claims a DIFFERENT app, and carries a payload
        # that must never execute.
        clone = _write_clone(
            tmp_path / "app-sources" / "card-app",
            _manifest("other-app", setup={"onInstall": "touch /tmp/pwned"}),
        )
        _patch_clone(monkeypatch, clone)
        _forbid_spawn(monkeypatch)

        result = await registry.install_from_registry("card-app")

        assert result["ok"] is False
        # The error must name BOTH identities so an operator can see the swap.
        assert "card-app" in result["error"]
        assert "other-app" in result["error"]
        # Nothing installed under either identity.
        assert manager.get_app("card-app") is None
        assert manager.get_app("other-app") is None
        # No residue: a leftover clone would keep answering as this app via
        # _fetch_app_manifest, which prefers the persistent clone.
        assert not clone.exists()
        # And the refusal is audited.
        assert "identity_mismatch" in _audited_operations(sel_calls)

    @pytest.mark.asyncio
    async def test_manifest_without_a_name_is_refused_fail_closed(
        self, tmp_path, monkeypatch, sel_calls
    ):
        entry = {"name": "card-app", "gitUrl": EVIL_URL, "repo": EVIL_URL, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, _manifest("card-app"))
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / "app.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
        _patch_clone(monkeypatch, clone)
        _forbid_spawn(monkeypatch)

        result = await registry.install_from_registry("card-app")

        assert result["ok"] is False
        assert "<missing>" in result["error"]
        assert manager.get_app("card-app") is None
        assert "identity_mismatch" in _audited_operations(sel_calls)

    @pytest.mark.asyncio
    async def test_matching_repo_is_admitted(self, tmp_path, monkeypatch, sel_calls):
        """Control: an entry whose repo agrees on the name installs normally."""
        entry = {"name": "match-app", "gitUrl": GOOD_URL, "repo": GOOD_URL, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, _manifest("match-app"))
        _patch_clone(monkeypatch, _write_clone(tmp_path / "clone", _manifest("match-app")))

        result = await registry.install_from_registry("match-app")

        assert result["ok"] is True, result.get("error")
        assert "identity_mismatch" not in _audited_operations(sel_calls)


# ---------------------------------------------------------------------------
# (b) Fresh install persists structured provenance
# ---------------------------------------------------------------------------


class TestProvenanceCapture:
    @pytest.fixture()
    def signing_home(self, tmp_path, monkeypatch) -> Path:
        """An explicitly-isolated KIROCREW_HOME, so writing an admission policy
        below can never reach the developer's real ``~/.kiro/crew``."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr("kiro_crew.config.paths._resolved_home", None)
        assert manager.config_dir() == home
        return home

    @pytest.mark.asyncio
    async def test_fresh_install_persists_full_provenance(
        self, tmp_path, monkeypatch, sel_calls, signing_home
    ):
        secret = "s3cr3t"
        # A policy that carries the trust key but does NOT require a signature:
        # the app is admitted either way, and the signer is still recorded. This
        # exercises the real verified_signer(), so the signer in the provenance
        # is one the admission layer actually verified.
        (signing_home / "app_admission.json").write_text(
            json.dumps({"mode": "open", "trust_keys": {"acme": secret}}), encoding="utf-8"
        )

        from kiro_crew.apps.manifest import AppManifest

        manifest = _manifest("signed-app", signer="acme")
        manifest["signature"] = hmac.new(
            secret.encode(),
            AppManifest.from_dict(manifest).signing_payload(),
            hashlib.sha256,
        ).hexdigest()

        entry = {
            "name": "signed-app",
            "gitUrl": GOOD_URL,
            "repo": GOOD_URL,
            "branch": "main",
            "_registry": "acme-index",
        }
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, manifest)
        _patch_clone(monkeypatch, _write_clone(tmp_path / "clone", manifest, commit="a" * 40))

        result = await registry.install_from_registry("signed-app")
        assert result["ok"] is True, result.get("error")

        meta = manager.get_app("signed-app")
        assert meta is not None
        # The bare marker is still written (nothing downstream regresses)...
        assert meta["source"] == "registry:signed-app"
        # ...alongside the structured provenance that pins the actual source.
        assert meta["sourceUrl"] == GOOD_URL
        assert meta["sourceRegistry"] == "acme-index"
        assert meta["sourceCommit"] == "a" * 40
        assert meta["sourceSigner"] == "acme"

    @pytest.mark.asyncio
    async def test_bad_signature_records_no_signer(
        self, tmp_path, monkeypatch, sel_calls, signing_home
    ):
        """A manifest claiming a signer but carrying a signature that does not
        verify must not have that claim recorded as provenance."""
        (signing_home / "app_admission.json").write_text(
            json.dumps({"mode": "open", "trust_keys": {"acme": "s3cr3t"}}), encoding="utf-8"
        )
        manifest = _manifest("claim-app", signer="acme", signature="00" * 32)
        entry = {"name": "claim-app", "gitUrl": GOOD_URL, "repo": GOOD_URL, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, manifest)
        _patch_clone(monkeypatch, _write_clone(tmp_path / "clone", manifest))

        assert (await registry.install_from_registry("claim-app"))["ok"] is True

        meta = manager.get_app("claim-app")
        assert meta is not None
        assert meta.get("sourceSigner", "") == ""

    @pytest.mark.asyncio
    async def test_unsigned_bundled_install_records_source_without_signer(
        self, tmp_path, monkeypatch, sel_calls
    ):
        """A bundled (no ``_registry``) unsigned app still gets URL + commit; an
        empty registry id denotes the bundled catalog, and signer stays empty."""
        entry = {"name": "plain-app", "gitUrl": GOOD_URL, "repo": GOOD_URL, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        _patch_prefetch(monkeypatch, _manifest("plain-app"))
        _patch_clone(
            monkeypatch,
            _write_clone(tmp_path / "clone", _manifest("plain-app"), commit="b" * 40),
        )

        assert (await registry.install_from_registry("plain-app"))["ok"] is True

        meta = manager.get_app("plain-app")
        assert meta is not None
        assert meta["sourceUrl"] == GOOD_URL
        assert meta["sourceCommit"] == "b" * 40
        # Falsy fields are dropped by InstalledApp.to_dict, hence .get().
        assert meta.get("sourceRegistry", "") == ""
        assert meta.get("sourceSigner", "") == ""


class TestResolvedCloneCommit:
    def test_reads_loose_ref(self, tmp_path):
        clone = _write_clone(tmp_path / "c", _manifest("a-app"), commit="c" * 40)
        assert registry._resolved_clone_commit(clone) == "c" * 40

    def test_reads_packed_ref(self, tmp_path):
        clone = tmp_path / "c"
        (clone / ".git").mkdir(parents=True)
        (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (clone / ".git" / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{'d' * 40} refs/heads/main\n", encoding="utf-8"
        )
        assert registry._resolved_clone_commit(clone) == "d" * 40

    def test_reads_detached_head(self, tmp_path):
        clone = tmp_path / "c"
        (clone / ".git").mkdir(parents=True)
        (clone / ".git" / "HEAD").write_text("e" * 40 + "\n", encoding="utf-8")
        assert registry._resolved_clone_commit(clone) == "e" * 40

    def test_degrades_to_empty_without_git_metadata(self, tmp_path):
        # Provenance without a commit, never an exception on the install path.
        assert registry._resolved_clone_commit(tmp_path / "nope") == ""

    def test_rejects_a_non_sha_ref_value(self, tmp_path):
        clone = tmp_path / "c"
        heads = clone / ".git" / "refs" / "heads"
        heads.mkdir(parents=True)
        (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (heads / "main").write_text("not-a-sha\n", encoding="utf-8")
        assert registry._resolved_clone_commit(clone) == ""


# ---------------------------------------------------------------------------
# (c) Updates resolve through provenance, (d) legacy records keep old behaviour
# ---------------------------------------------------------------------------


def _install_with_provenance(
    tmp_path: Path,
    name: str,
    *,
    url: str = GOOD_URL,
    source_registry: str = "good-index",
    commit: str = "1" * 40,
) -> None:
    """Put *name* on disk as if a previous provenance-aware install ran."""
    source = _write_clone(tmp_path / "installed-src" / name, _manifest(name))
    assert manager.install_app(source).ok
    assert manager.set_app_provenance(
        name,
        source=f"registry:{name}",
        url=url,
        registry=source_registry,
        commit=commit,
    )


class TestUpdateResolution:
    @pytest.mark.asyncio
    async def test_same_named_entry_from_another_source_cannot_capture_update(
        self, tmp_path, monkeypatch, sel_calls
    ):
        _install_with_provenance(tmp_path, "shared-name")
        # Only a hostile row is on offer: same name, different repo and registry.
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda n: [
                {
                    "name": "shared-name",
                    "gitUrl": EVIL_URL,
                    "repo": EVIL_URL,
                    "branch": "main",
                    "_registry": "evil-index",
                }
            ],
        )
        monkeypatch.setattr(
            registry, "_clone_build_app", AsyncMock(side_effect=AssertionError("cloned!"))
        )

        result = await registry.install_from_registry("shared-name")

        assert result["ok"] is False
        assert "different source" in result["error"]
        assert "provenance_mismatch" in _audited_operations(sel_calls)
        # Provenance is untouched — the hostile row never took the slot.
        meta = manager.get_app("shared-name")
        assert meta is not None
        assert meta["sourceUrl"] == GOOD_URL
        assert meta["sourceRegistry"] == "good-index"

    @pytest.mark.asyncio
    async def test_update_picks_the_entry_matching_recorded_provenance(
        self, tmp_path, monkeypatch, sel_calls
    ):
        """With a hostile same-named row listed FIRST — where the old
        first-match-wins lookup would have landed — the update must still follow
        the recorded provenance to the legitimate row."""
        _install_with_provenance(tmp_path, "shared-name")
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda n: [
                {
                    "name": "shared-name",
                    "gitUrl": EVIL_URL,
                    "repo": EVIL_URL,
                    "branch": "main",
                    "_registry": "evil-index",
                },
                {
                    "name": "shared-name",
                    "gitUrl": GOOD_URL,
                    "repo": GOOD_URL,
                    "branch": "main",
                    "_registry": "good-index",
                },
            ],
        )
        _patch_prefetch(monkeypatch, _manifest("shared-name", version="2.0.0"))
        seen: dict = {}
        _patch_clone(
            monkeypatch,
            _write_clone(
                tmp_path / "clone", _manifest("shared-name", version="2.0.0"), commit="2" * 40
            ),
            seen,
        )

        result = await registry.install_from_registry("shared-name")

        assert result["ok"] is True, result.get("error")
        assert seen["git_url"] == GOOD_URL
        meta = manager.get_app("shared-name")
        assert meta is not None
        assert meta["version"] == "2.0.0"
        assert meta["sourceUrl"] == GOOD_URL
        assert meta["sourceRegistry"] == "good-index"
        # The pinned commit advances with the update.
        assert meta["sourceCommit"] == "2" * 40

    @pytest.mark.asyncio
    async def test_legacy_marker_app_updates_by_bare_name_and_self_heals(
        self, tmp_path, monkeypatch, sel_calls
    ):
        """An app installed before provenance capture carries only
        ``registry:<name>``. It must keep updating via the historical bare-name
        lookup (no migration required) and gain provenance on success."""
        source = _write_clone(tmp_path / "legacy-src", _manifest("legacy-app"))
        assert manager.install_app(source).ok
        assert manager.set_app_source("legacy-app", "registry:legacy-app")
        legacy = manager.get_app("legacy-app")
        assert legacy is not None and legacy.get("sourceUrl", "") == ""

        entry = {
            "name": "legacy-app",
            "gitUrl": GOOD_URL,
            "repo": GOOD_URL,
            "branch": "main",
            "_registry": "good-index",
        }
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)

        def _no_pinning(name):
            pytest.fail("a legacy record must not enter provenance-pinned resolution")

        monkeypatch.setattr(registry, "_registry_app_candidates", _no_pinning)
        _patch_prefetch(monkeypatch, _manifest("legacy-app", version="1.1.0"))
        seen: dict = {}
        _patch_clone(
            monkeypatch,
            _write_clone(
                tmp_path / "clone", _manifest("legacy-app", version="1.1.0"), commit="3" * 40
            ),
            seen,
        )

        result = await registry.install_from_registry("legacy-app")

        assert result["ok"] is True, result.get("error")
        assert seen["git_url"] == GOOD_URL
        meta = manager.get_app("legacy-app")
        assert meta is not None
        assert meta["version"] == "1.1.0"
        # Self-healed: the update recorded the provenance the record lacked.
        assert meta["sourceUrl"] == GOOD_URL
        assert meta["sourceRegistry"] == "good-index"
        assert meta["sourceCommit"] == "3" * 40
