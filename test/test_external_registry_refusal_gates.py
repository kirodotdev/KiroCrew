"""The gates that refuse a registry install, and what their cleanup may touch.

An index ``subdirectory`` that escapes the checkout is refused and the refusal never
writes outside it, the manifest restore re-validates the subdirectory at write time,
and the containment, identity and admission refusals restore only the restorable
stale checkouts (``_restorable_stale``).

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401
from external_registry_helpers import cache_dir_fixture  # noqa: F401

# ---------------------------------------------------------------------------
# Untrusted index `subdirectory` path-traversal gate (CWE-22 → RCE).
# ---------------------------------------------------------------------------


class TestRegistrySubdirTraversalGate:
    def test_safe_subdirs_accepted(self):
        from kiro_crew.apps.registry import _is_safe_registry_subdir

        for ok in ["", None, "apps", "apps/widget", "a/b/c", ".config", "v2.0"]:
            assert _is_safe_registry_subdir(ok) is True, ok

    def test_unsafe_subdirs_rejected(self):
        from kiro_crew.apps.registry import _is_safe_registry_subdir

        for bad in [
            "/etc",
            "/tmp/victim",
            "../../victim",
            "apps/../../etc",
            "..",
            ".",
            "a/./b",
            "C:\\Windows",
            "a\\b",
            "with\x00nul",
            123,
            ["not", "a", "str"],
        ]:
            assert _is_safe_registry_subdir(bad) is False, bad

    def test_contained_join_blocks_symlink_escape(self, tmp_path):
        import os

        from kiro_crew.apps.registry import _contained_join

        root = tmp_path / "clone"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # A hostile clone ships a symlink pointing outside the clone root.
        link = root / "sub"
        os.symlink(outside, link)
        assert _contained_join(root, "sub") is None
        # A genuine contained subdir resolves fine.
        (root / "real").mkdir()
        assert _contained_join(root, "real") == (root / "real").resolve()
        # Empty subdir returns the root unchanged.
        assert _contained_join(root, "") == root

    @pytest.mark.asyncio
    async def test_fresh_fetch_drops_unsafe_subdir_entry(self, cache_dir, monkeypatch):
        # An index that lists an app with a traversing subdirectory must have
        # that entry dropped before it is cached or listed.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [
                {"name": "good-app", "repo": repo, "subdirectory": "apps/good"},
                {"name": "evil-app", "repo": repo, "subdirectory": "../../etc"},
            ]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "main"

        entries = await reg._fetch_and_cache_external_registry(_Reg())
        names = {e["name"] for e in entries}
        assert "good-app" in names
        assert "evil-app" not in names

    def test_cache_read_drops_unsafe_subdir_entry(self, cache_dir):
        # Even a hand-tampered cache file with an absolute subdirectory is
        # filtered on read (the single read chokepoint).
        from kiro_crew.apps.registry import (
            _read_external_registry_cache,
            _write_external_registry_cache,
        )

        _write_external_registry_cache(
            "acme",
            [
                {"name": "good-app", "repo": "r", "subdirectory": "sub"},
                {"name": "evil-app", "repo": "r", "subdirectory": "/etc"},
            ],
        )
        got = _read_external_registry_cache("acme", ignore_ttl=True)
        names = {e["name"] for e in got}
        assert names == {"good-app"}

    @pytest.mark.asyncio
    async def test_install_refuses_symlink_escape_subdir(self, tmp_path):
        # Defense-in-depth: even if a traversing subdir reached install (e.g. a
        # symlink inside the clone), _contained_join refuses it at use time.
        import os

        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "src"
        pkg_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "app.json").write_text('{"name": "evil"}', encoding="utf-8")
        os.symlink(outside, pkg_dir / "sub")

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "evil-app",
                    "repo": "https://github.com/acme/apps.git",
                    "branch": "main",
                    "subdirectory": "sub",
                    "_registry": "acme",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "evil-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=AsyncMock(return_value={"ok": True, "pkg_dir": pkg_dir}),
            ),
        ):
            result = await install_from_registry("evil-app")

        assert not result["ok"]
        assert "unsafe subdirectory" in result["error"]


class TestSubdirectoryEscapeRefusalNeverWritesOutsideCheckout:
    """Regression: the subdirectory-containment refusal must never join the
    raw, untrusted ``subdirectory`` onto ``pkg_dir`` for a filesystem write.

    GPT 5.6 finding: on a containment refusal (``_contained_join`` returns
    ``None``), the cleanup call built ``manifest_relpath`` from the raw
    ``subdirectory`` value instead of the safely-contained path. When the
    checkout PRE-existed, ``_unpoison_rejected_checkout`` writes the manifest
    snapshot to ``pkg_dir / manifest_relpath`` — and a build step that
    replaces ``subdirectory`` with a symlink pointing outside the checkout
    made that write escape the sandbox.
    """

    @pytest.mark.asyncio
    async def test_escaping_subdirectory_does_not_write_outside_checkout(self, tmp_path):
        """A symlinked subdirectory that escapes containment must not cause
        any write outside the cloned checkout, even when the checkout
        pre-existed (update path)."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")

        # A directory OUTSIDE the checkout that a malicious build step wants
        # the manifest-restore write to land in.
        outside = tmp_path / "outside"
        outside.mkdir()

        # subdirectory "evil" is a symlink escaping app_source — this is what
        # a compromised/malicious build step would do.
        evil_link = app_source / "evil"
        evil_link.symlink_to(outside)

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _GitProc:
            returncode = 1

            async def communicate(self):
                return (b"", None)

        build_result = {
            "ok": True,
            "pkg_dir": app_source,
            # Simulates an UPDATE of a pre-existing app checkout — the branch
            # of _unpoison_rejected_checkout that performs the vulnerable
            # manifest write.
            "_checkout_preexisted": True,
            "_pre_pull_commit": "deadbeef",
            "_pre_update_manifest": b'{"name": "testapp", "pwned": true}',
            "_pending_stale_cleanup": [],
        }

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                new=AsyncMock(return_value=_GitProc()),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        # The manifest-restore write must NEVER have followed the symlink out
        # of the checkout — "outside" must remain exactly as it was.
        assert list(outside.iterdir()) == [], (
            "containment refusal must not write into a symlink-escaped "
            "subdirectory outside the checkout"
        )


class TestUnpoisonRejectedCheckoutRevalidatesSubdirectoryAtWriteTime:
    """Regression: every call site that reaches ``_unpoison_rejected_checkout``
    with ``checkout_preexisted=True`` (identity/admission gates before AND
    after the install script runs) built ``manifest_relpath`` from the raw
    ``subdirectory`` and trusted a containment check an EARLIER gate had
    already made. A build step or ``onInstall`` script — which runs with
    write access to the checkout between some of those gates — can replace
    the subdirectory with a symlink escaping the checkout after that earlier
    check passed, and this cleanup runs unsandboxed as the Kiro Crew process.
    ``_unpoison_rejected_checkout`` must re-verify containment itself, at the
    point of the write, rather than relying on every call site to remember.
    """

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_subdirectory_escapes_at_write_time(self, tmp_path):
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        pkg_dir.mkdir()

        outside = tmp_path / "outside"
        outside.mkdir()

        # An earlier gate saw a real directory here and passed containment;
        # by the time this cleanup runs it has been replaced with a symlink
        # escaping pkg_dir — e.g. by onInstall, which ran in between.
        (pkg_dir / "sub").symlink_to(outside)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",  # skip the git-reset branch; irrelevant here
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert list(outside.iterdir()) == [], (
            "manifest restore must not write through a symlink that escapes "
            "pkg_dir, even when it was planted after an earlier containment check"
        )
        assert any("no longer resolves inside" in line for line in log_lines)

    @pytest.mark.asyncio
    async def test_manifest_restore_still_runs_when_subdirectory_stays_contained(self, tmp_path):
        """Control: a legitimate, still-contained subdirectory must still get
        its manifest restored — the new guard must not break the happy path."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        sub = pkg_dir / "sub"
        sub.mkdir(parents=True)
        (sub / "app.json").write_text('{"name": "testapp", "pwned": true}', encoding="utf-8")

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp"}',
        )

        assert (sub / "app.json").read_text(encoding="utf-8") == '{"name": "testapp"}'

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_leaf_is_symlinked_inside_subdirectory(
        self, tmp_path
    ):
        """GPT 5.6 round-5 finding: the old guard checked containment of
        ``subdirectory`` (the directory) only, never the manifest LEAF
        (``subdirectory/app.json``). A build step or ``onInstall`` script can
        leave ``subdirectory`` itself an ordinary, correctly-contained
        directory while replacing just its ``app.json`` with a symlink
        escaping ``pkg_dir`` — that passed the old check and the raw write
        then followed the symlink outside the checkout."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        sub = pkg_dir / "sub"
        sub.mkdir(parents=True)

        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "security_policy.json"
        target.write_text("untouched", encoding="utf-8")

        # `sub` itself is a real, contained directory — only its app.json
        # leaf is a symlink escaping pkg_dir.
        (sub / "app.json").symlink_to(target)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert target.read_text(encoding="utf-8") == "untouched", (
            "the manifest restore must not follow a symlink planted at the "
            "manifest leaf, even when the containing subdirectory is itself "
            "a real, contained directory"
        )
        assert any("no longer resolves inside" in line for line in log_lines)

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_leaf_is_symlinked_no_subdirectory(self, tmp_path):
        """Same attack, no ``subdirectory`` at all: the old guard was gated on
        ``if subdirectory`` and never ran when subdirectory=="", so a
        symlinked ``app.json`` at the checkout root was never re-checked
        before the write."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        pkg_dir.mkdir()

        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "security_policy.json"
        target.write_text("untouched", encoding="utf-8")

        (pkg_dir / "app.json").symlink_to(target)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert target.read_text(encoding="utf-8") == "untouched", (
            "the manifest restore must not follow a symlinked app.json even "
            "with no subdirectory declared"
        )
        assert any("no longer resolves inside" in line for line in log_lines)


class TestContainmentRefusalRestoreFromRespectsRestorableStale:
    """Regression: the subdirectory-containment refusal's cleanup
    (``_checkout_preexisted=False`` branch) must filter ``restore_from`` by
    ``_restorable_stale`` the same way every other restoration site in this
    module does.

    Round-4 security review finding: the refusal passed the first
    ``_pending_stale_cleanup`` entry to ``_unpoison_rejected_checkout``
    unfiltered. When ``_clone_build_app_locked`` forces
    ``_checkout_preexisted=False`` after an origin-mismatch (non-restorable,
    different-repository) move-aside + a successful fresh clone/build, and
    that fresh clone's declared ``subdirectory`` then fails containment, the
    unfiltered ``restore_from`` renamed the origin-mismatched stale checkout
    back into ``pkg_dir`` — exactly the "hand the build the tree the gate
    refused" case ``_restorable_stale`` exists to prevent elsewhere in this
    file.
    """

    @staticmethod
    def _build_result(app_source, stale_dir, *, restorable):
        result = {
            "ok": True,
            "pkg_dir": app_source,
            "_checkout_preexisted": False,
            "_pending_stale_cleanup": [stale_dir],
        }
        if restorable:
            result["_restorable_stale"] = [stale_dir]
        return result

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_containment_refusal(self, tmp_path):
        """A non-restorable (origin-mismatch) pending stale must NOT be
        restored into pkg_dir when the freshly cloned repo's declared
        subdirectory fails containment."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (app_source / "evil").symlink_to(outside)

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=False)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_containment_refusal(self, tmp_path):
        """Control: a restorable (same-repository, branch-drift) pending
        stale IS still restored into pkg_dir on the same containment
        refusal — the fix must narrow, not remove, this path."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (app_source / "evil").symlink_to(outside)

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=True)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestIdentityMismatchPreBuildRestoreFromRespectsRestorableStale:
    """Regression: the pre-build IDENTITY GATE inside ``_clone_build_app_locked``
    must filter ``restore_from`` through the local ``restorable_stale`` list
    before handing it to ``_refuse_identity_mismatch`` — the same class of bug
    fixed at the containment refusal above, just at a different call site.

    Round-4 security review finding (step 10, site 1/3): this gate computed
    ``restore_from`` as ``pending_cleanup[0] if pending_cleanup else None``
    with no ``restorable_stale`` filter, so an origin-mismatched move-aside
    could be restored into ``pkg_dir`` for a repo whose cloned ``app.json``
    this very gate is refusing for declaring the wrong name.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            # Wrong name — trips the identity gate, not the admission gate.
            (dest / "app.json").write_text(json.dumps({"name": "wrong-name"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert "declares" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestIdentityMismatchPostBuildRestoreFromRespectsRestorableStale:
    """Regression: the post-build IDENTITY GATE inside ``install_from_registry``
    (the re-check that runs right after ``_clone_build_app`` returns
    ``ok=True``) must filter ``restore_from`` through
    ``build_result["_restorable_stale"]`` before handing it to
    ``_refuse_identity_mismatch`` — same class of bug, third call site.
    """

    @staticmethod
    def _build_result(app_source, stale_dir, *, restorable):
        result = {
            "ok": True,
            "pkg_dir": app_source,
            "_checkout_preexisted": False,
            "_pending_stale_cleanup": [stale_dir],
        }
        if restorable:
            result["_restorable_stale"] = [stale_dir]
        return result

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        # A build step rewrote app.json to a different name than the registry
        # entry declares — the post-build re-check must catch this.
        (app_source / "app.json").write_text('{"name": "wrong-name"}', encoding="utf-8")

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=False)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "declares" in result.get("error", "")
        assert not app_source.exists(), "the rejected clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "wrong-name"}', encoding="utf-8")

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=True)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestAdmissionGatePreBuildRestoreFromRespectsRestorableStale:
    """Regression: the pre-build ADMISSION GATE (second pass, on the cloned
    manifest) inside ``_clone_build_app_locked`` must filter ``restore_from``
    through the local ``restorable_stale`` list before handing it to
    ``_unpoison_rejected_checkout`` — same class of bug, second call site.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            # Correct name — passes the identity gate so the admission gate
            # (patched to deny below) is the one that fires.
            (dest / "app.json").write_text(json.dumps({"name": "testapp"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_admission_denial(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value="unsigned"),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert "admission policy" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_admission_denial(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value="unsigned"),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"
