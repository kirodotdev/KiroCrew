"""The update flow end to end: the manifest fetch, promotion over the live symlink, the shadow
build's guards and verification, and the orchestration with the heavy steps stubbed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from wheel_engine_helpers import (
    _ARTIFACT_BASE,
    _manifest,
    _raw,
    _stub_children,
)

from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import ManagedVenvLayout, WheelUpdateError, promote
from kiro_crew.platform_compat import IS_POSIX

# The engine is POSIX-only by construction: cli.sh is a POSIX installer, and
# running_from_managed_venv() answers False on Windows, so no production path
# reaches this module there. The tests lean on POSIX semantics throughout —
# atomic rename over a symlink (WinError 5 on Windows), 0o600 mode bits, and
# venv bin/ layout — so they are skipped as a module rather than papered over
# with per-test shims that would test nothing real.
pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")


_FEED_BASE = "https://updates.crew.kiro.dev"


class TestManifestFetchOrchestration:
    def test_fetch_verified_manifest_wires_fetch_parse_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        raw = _raw(_manifest())
        monkeypatch.setattr(
            wheel_engine,
            "_fetch_bytes",
            lambda url, cap, timeout, **_kw: (calls.append("fetch"), raw)[1],
        )
        monkeypatch.setattr(
            wheel_engine,
            "_verify_signature",
            lambda canonical, signature, workdir, **_kw: calls.append("verify"),
        )
        payload = wheel_engine.fetch_verified_manifest(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            workdir=tmp_path,
        )
        assert calls == ["fetch", "verify"]
        assert payload["version"] == "9.9.9"


class TestPromotion:
    def test_promote_creates_stable_link(self, tmp_path: Path) -> None:
        tree = tmp_path / "crew-venv-1.0.0"
        tree.mkdir()
        stable = tmp_path / "crew-venv-current"
        promote(tree, stable)
        assert stable.is_symlink()
        assert stable.resolve() == tree.resolve()

    def test_promote_replaces_existing_link(self, tmp_path: Path) -> None:
        old = tmp_path / "crew-venv-1.0.0"
        new = tmp_path / "crew-venv-2.0.0"
        old.mkdir()
        new.mkdir()
        stable = tmp_path / "crew-venv-current"
        promote(old, stable)
        promote(new, stable)
        assert stable.resolve() == new.resolve()
        # No temp link litter after either promotion.
        leftovers = [p for p in tmp_path.iterdir() if ".new" in p.name]
        assert leftovers == []

    def test_promote_refuses_real_directory_at_stable_name(self, tmp_path: Path) -> None:
        tree = tmp_path / "crew-venv-1.0.0"
        tree.mkdir()
        stable = tmp_path / "crew-venv-current"
        stable.mkdir()  # corrupt state: the stable name must always be a symlink
        with pytest.raises(WheelUpdateError, match="not a symlink"):
            promote(tree, stable)


class TestShadowBuildGuards:
    def test_refuses_symlink_at_shadow_path(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        shadow = tmp_path / "crew-venv-9.9.9"
        shadow.symlink_to(target)
        with pytest.raises(WheelUpdateError, match="not a plain directory"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", shadow)

    def test_refuses_to_remove_a_non_venv_directory(self, tmp_path: Path) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        shadow.mkdir()
        (shadow / "user-data.txt").write_text("precious")
        with pytest.raises(WheelUpdateError, match="not a virtual"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", shadow)
        assert (shadow / "user-data.txt").exists()

    def test_refuses_to_rebuild_the_promoted_tree(self, tmp_path: Path) -> None:
        """A directory that IS the stable link's target is promoted, not leftover."""
        live = tmp_path / "crew-venv-9.9.9"
        live.mkdir()
        (live / "pyvenv.cfg").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(live)
        with pytest.raises(WheelUpdateError, match="already promoted"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", live, stable_link=stable)
        assert live.exists(), "the live tree must never be removed"
        assert stable.resolve() == live.resolve()

    def test_refuses_a_completed_tree_without_sentinel(self, tmp_path: Path) -> None:
        """No marker and no sentinel: this engine did not build it. Refused."""
        tree = tmp_path / "crew-venv-9.9.9"
        (tree / "bin").mkdir(parents=True)
        (tree / "pyvenv.cfg").write_text("")
        with pytest.raises(WheelUpdateError, match="refusing to remove"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tree)
        assert tree.exists(), "a sentinel-less tree must never be removed"

    def test_refuses_an_unrelated_sibling_venv(self, tmp_path: Path) -> None:
        """A custom KIROCREW_VENV shares a parent with unrelated venvs; a name
        collision must never delete someone else's environment."""
        foreign = tmp_path / "crew-venv-9.9.9"
        (foreign / "bin").mkdir(parents=True)
        (foreign / "pyvenv.cfg").write_text("")
        (foreign / "precious-data.txt").write_text("not ours")
        with pytest.raises(WheelUpdateError, match="refusing to remove"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", foreign)
        assert (foreign / "precious-data.txt").exists()

    def test_own_incomplete_debris_is_clearable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sentinel present = our own interrupted build; a retry clears it."""
        tree = tmp_path / "crew-venv-9.9.9"
        tree.mkdir()
        (tree / "pyvenv.cfg").write_text("")
        (tree / wheel_engine._SHADOW_SENTINEL).write_text("")
        calls: list[str] = []
        monkeypatch.setattr(
            wheel_engine,
            "_run",
            lambda argv, timeout, step, ctx=None: calls.append(step),
        )
        wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tree)
        assert "venv creation" in calls, "the retry must rebuild after clearing debris"
        assert (
            tree / wheel_engine._SHADOW_SENTINEL
        ).exists(), "a fresh build must re-claim ownership until verification passes"

    def test_refuses_when_disk_space_is_low(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import shutil as _shutil

        usage = _shutil.disk_usage(tmp_path)
        monkeypatch.setattr(
            wheel_engine.shutil,
            "disk_usage",
            lambda p: type(usage)(usage.total, usage.total - 1024, 1024),
        )
        with pytest.raises(WheelUpdateError, match="disk space"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tmp_path / "crew-venv-9.9.9")


class TestShadowVerification:
    def _stub_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        stdout: str = "9.9.9\n",
        stderr: str = "",
    ) -> None:
        # Bytes, as the real probe returns them: it runs without text mode and
        # decodes the UTF-8 its `-X utf8` child writes.
        def answer(argv: list[str]) -> tuple[int, bytes, bytes]:
            if argv[-2:] == ["pip", "check"]:
                return 0, b"No broken requirements found.\n", b""
            return returncode, stdout.encode("utf-8"), stderr.encode("utf-8")

        _stub_children(monkeypatch, answer)

    def test_version_match_with_console_script_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        (shadow / "bin" / "kirocrew").write_text("")
        self._stub_probe(monkeypatch)
        wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_version_mismatch_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        (shadow / "bin" / "kirocrew").write_text("")
        self._stub_probe(monkeypatch, stdout="1.0.0\n")
        with pytest.raises(WheelUpdateError, match="not promoting"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_import_failure_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        self._stub_probe(monkeypatch, returncode=1, stdout="", stderr="ImportError: boom")
        with pytest.raises(WheelUpdateError, match="cannot import"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_missing_console_script_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        self._stub_probe(monkeypatch)
        with pytest.raises(WheelUpdateError, match="console script"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")


class TestApplyWheelUpdateOrchestration:
    """The full flow with the heavy steps stubbed: ordering and refusal seams."""

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        feed_version: str = "9.9.9",
        real_manifest: bool = False,
        python_requires: str = ">=3.10",
    ) -> tuple[ManagedVenvLayout, list[str]]:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)

        calls: list[str] = []

        def fake_manifest(**kwargs: object) -> dict[str, str]:
            calls.append("manifest")
            return {
                "version": feed_version,
                "sha256": "a" * 64,
                "python_requires": python_requires,
                "wheel_url": f"{_ARTIFACT_BASE}/cli/stable/{feed_version}/"
                f"kirocrew-{feed_version}-py3-none-any.whl",
            }

        def fake_download(payload: dict[str, str], dest: Path, **_kw: object) -> Path:
            calls.append("download")
            out = dest / "kirocrew.whl"
            out.write_bytes(b"wheel")
            return out

        def fake_build(
            wheel: Path, shadow: Path, stable_link: Path | None = None, **_kw: object
        ) -> None:
            calls.append("build")
            (shadow / "bin").mkdir(parents=True)
            (shadow / "pyvenv.cfg").write_text("")
            (shadow / wheel_engine._SHADOW_SENTINEL).write_text("")

        def fake_verify(shadow: Path, version: str, **_kw: object) -> None:
            calls.append("verify")

        if not real_manifest:
            monkeypatch.setattr(wheel_engine, "fetch_verified_manifest", fake_manifest)
        monkeypatch.setattr(wheel_engine, "download_verified_wheel", fake_download)
        monkeypatch.setattr(wheel_engine, "build_shadow_venv", fake_build)
        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", fake_verify)
        monkeypatch.setattr(wheel_engine, "repoint_launcher_symlink", lambda layout: True)
        return layout, calls

    def test_happy_path_promotes_in_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path)
        promoted = wheel_engine.apply_wheel_update(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            expected_version="9.9.9",
        )
        assert calls == ["manifest", "download", "build", "verify"]
        assert promoted == layout.versioned_tree("9.9.9")
        assert layout.stable_link.resolve() == promoted.resolve()
        assert not (
            promoted / wheel_engine._SHADOW_SENTINEL
        ).exists(), "a promoted tree must never carry the incomplete sentinel"

    def test_already_promoted_completes_launcher_handoff_without_rebuilding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A handoff interrupted after promote but before launcher-repoint must
        recover: the stable link already targets this version's tree, so a
        rebuild would hit build_shadow_venv's stable-target refusal and strand
        the launcher on the old venv forever. Instead, finish the one remaining
        step (repoint) and return."""
        layout, calls = self._wire(monkeypatch, tmp_path)
        # Simulate the crashed-after-promote state: the versioned tree exists
        # and the stable link already points at it, but the launcher was never
        # repointed.
        promoted = layout.versioned_tree("9.9.9")
        (promoted / "bin").mkdir(parents=True)
        (promoted / "pyvenv.cfg").write_text("")
        (promoted / "bin" / "python3").write_text("")
        (promoted / "bin" / "python3").chmod(0o755)
        # A sentinel stranded by a failed post-promote unlink is cleared here.
        (promoted / wheel_engine._SHADOW_SENTINEL).write_text("")
        os.symlink(str(promoted.resolve()), str(layout.stable_link))
        repointed: list[bool] = []
        monkeypatch.setattr(
            wheel_engine, "repoint_launcher_symlink", lambda layout: repointed.append(True) or True
        )

        result = wheel_engine.apply_wheel_update(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            expected_version="9.9.9",
        )
        assert result == promoted
        assert repointed == [True], "the launcher handoff must be completed"
        assert calls == [], "recovery must NOT re-fetch, re-download, or rebuild"
        assert not (promoted / wheel_engine._SHADOW_SENTINEL).exists()

    def test_a_dangling_stable_link_is_rebuilt_not_reported_promoted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A link naming a tree that is gone would otherwise restart into nothing."""
        layout, calls = self._wire(monkeypatch, tmp_path)
        os.symlink(str(layout.versioned_tree("9.9.9")), str(layout.stable_link))
        result = self._apply()
        assert calls == ["manifest", "download", "build", "verify"]
        assert layout.stable_link.resolve(strict=True) == result.resolve()

    def test_a_dangling_stable_link_is_repointed_even_when_the_apply_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """As cli.sh does: a link naming a tree that is gone is repointed at a tree
        that exists (here the legacy venv) before anything else, so the launcher
        and a service start work again whatever the rest of the apply does."""
        layout, _calls = self._wire(monkeypatch, tmp_path)
        python = layout.legacy / "bin" / "python3"
        python.write_text("", encoding="utf-8")
        python.chmod(0o755)
        os.symlink(str(layout.versioned_tree("9.9.9")), str(layout.stable_link))

        def offline(**_kw: object) -> dict[str, str]:
            raise WheelUpdateError("could not fetch the manifest")

        monkeypatch.setattr(wheel_engine, "fetch_verified_manifest", offline)
        with pytest.raises(WheelUpdateError, match="could not fetch"):
            self._apply()
        assert layout.stable_link.resolve(strict=True) == layout.legacy.resolve()

    def test_feed_moving_between_check_and_apply_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path, feed_version="10.0.0")
        with pytest.raises(WheelUpdateError, match="re-run the update"):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
            )
        assert "download" not in calls, "nothing may download once the verdict is stale"
        assert not layout.stable_link.exists(), "a refused update must not touch the stable link"

    def test_second_concurrent_update_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The update lock admits ONE writer; a second run refuses cleanly."""
        import os as _os

        from kiro_crew.platform_compat import try_acquire_lock as _try

        layout, calls = self._wire(monkeypatch, tmp_path)
        lock_path = layout.stable_link.with_name(f"{layout.legacy.name}.update.lock")
        holder = _os.open(str(lock_path), _os.O_CREAT | _os.O_RDWR, 0o600)
        try:
            assert _try(holder, exclusive=True)
            locked: list[bool] = []
            with pytest.raises(wheel_engine.WheelUpdateBusy, match="already in progress"):
                wheel_engine.apply_wheel_update(
                    channel="stable",
                    feed_base=_FEED_BASE,
                    artifact_base=_ARTIFACT_BASE,
                    expected_version="9.9.9",
                    on_locked=lambda: locked.append(True),
                )
            assert calls == [], "a refused run must do no work at all"
            assert locked == [], "a run that lost the lock reports no progress"
            with pytest.raises(wheel_engine.WheelUpdateBusy):
                wheel_engine.hold_update_lock()
        finally:
            _os.close(holder)

    def test_a_cancel_before_promotion_leaves_the_stable_link_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cancel that lands while the tree is verified wins over the promotion."""
        layout, calls = self._wire(monkeypatch, tmp_path)
        os.symlink(str(layout.legacy), str(layout.stable_link))
        cancel = wheel_engine.ApplyCancel()

        def verify_then_cancel(shadow: Path, version: str, **_kw: object) -> None:
            calls.append("verify")
            cancel.set("shutdown")

        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", verify_then_cancel)
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
                cancel=cancel,
            )
        assert calls == ["manifest", "download", "build", "verify"]
        assert layout.stable_link.resolve() == layout.legacy.resolve()
        assert not layout.versioned_tree("9.9.9").exists(), "the partial tree is set aside"
        tombstones = [p for p in tmp_path.iterdir() if ".deleting-" in p.name]
        assert len(tombstones) == 1, "a cancel only renames; the deletion is the next sweep's"
        wheel_engine._sweep_layout_debris(layout)
        assert not tombstones[0].exists()

    def test_a_cancel_before_the_build_builds_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real manifest fetch, so the cancel it checks first is what is pinned.
        layout, calls = self._wire(monkeypatch, tmp_path, real_manifest=True)
        fetched: list[str] = []
        monkeypatch.setattr(
            wheel_engine, "_fetch_bytes", lambda url, cap, timeout: fetched.append(url) or b""
        )
        cancel = wheel_engine.ApplyCancel()
        cancel.set()
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
                cancel=cancel,
            )
        assert fetched == [], "nothing is fetched once the cancel is set"
        assert calls == []
        assert not layout.versioned_tree("9.9.9").exists()
        assert not layout.stable_link.exists()

    def _apply(self, **kwargs: object) -> Path:
        return wheel_engine.apply_wheel_update(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            expected_version="9.9.9",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_a_python_floor_this_host_fails_refuses_before_any_download(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path, python_requires=">=3.99")
        with pytest.raises(
            wheel_engine.WheelUpdateIncompatible, match="requires Python >= 3.99"
        ) as info:
            self._apply()
        assert (info.value.version, info.value.sha256) == ("9.9.9", "a" * 64)
        assert calls == ["manifest"], "an unbuildable release downloads nothing"
        assert not layout.stable_link.exists()

    def test_a_no_wheel_build_failure_is_an_ordinary_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pip's "No matching distribution" can be an unreachable index too, so it is
        never read as "this release can never build here": only the signed
        metadata decides that."""
        layout, _calls = self._wire(monkeypatch, tmp_path)

        def no_wheel(
            wheel: Path, shadow: Path, stable_link: Path | None = None, **_kw: object
        ) -> None:
            shadow.mkdir()
            (shadow / wheel_engine._SHADOW_SENTINEL).write_text("")
            raise WheelUpdateError("pip found no prebuilt wheel of numpy>=2.0")

        monkeypatch.setattr(wheel_engine, "build_shadow_venv", no_wheel)
        with pytest.raises(WheelUpdateError) as info:
            self._apply()
        assert not isinstance(info.value, wheel_engine.WheelUpdateIncompatible)
        assert not layout.versioned_tree("9.9.9").exists(), "the failed tree is cleared"

    def test_a_deadline_cancel_removes_the_partial_tree_at_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The process keeps running after a deadline, so no tombstone is left."""
        _layout, _calls = self._wire(monkeypatch, tmp_path)
        cancel = wheel_engine.ApplyCancel()

        def verify_then_expire(shadow: Path, version: str, **_kw: object) -> None:
            cancel.set("deadline")

        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", verify_then_expire)
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            self._apply(cancel=cancel)
        assert not [p for p in tmp_path.iterdir() if ".deleting-" in p.name]

    def test_a_lock_the_filesystem_cannot_take_is_a_failure_not_busy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import errno as _errno
        import fcntl as _fcntl

        layout, _calls = self._wire(monkeypatch, tmp_path)

        def no_locks(fd: int, op: int) -> None:
            raise OSError(_errno.ENOLCK, "No locks available")

        monkeypatch.setattr(_fcntl, "flock", no_locks)
        with pytest.raises(WheelUpdateError) as info:
            self._apply()
        assert not isinstance(info.value, wheel_engine.WheelUpdateBusy)
        assert "cannot lock" in str(info.value)
        assert str(wheel_engine._update_lock_path(layout)) in str(info.value)

    def test_a_cancel_set_while_queued_never_reports_promoted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Even an already-promoted release is not reported once the cancel is set."""
        layout, calls = self._wire(monkeypatch, tmp_path)
        cancel = wheel_engine.ApplyCancel()
        cancel.set("shutdown")
        locked: list[bool] = []
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            self._apply(cancel=cancel, on_locked=lambda: locked.append(True))
        assert calls == [] and locked == []

    def test_a_held_lock_is_used_and_left_to_its_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path)
        fd = wheel_engine.hold_update_lock()
        try:
            self._apply(held_lock_fd=fd)
            assert calls == ["manifest", "download", "build", "verify"]
            with pytest.raises(wheel_engine.WheelUpdateBusy):
                wheel_engine.hold_update_lock()
        finally:
            wheel_engine.release_update_lock(fd)
        wheel_engine.release_update_lock(wheel_engine.hold_update_lock())

    def test_a_failed_memory_snapshot_refuses_the_promotion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, _calls = self._wire(monkeypatch, tmp_path)

        def refuse() -> None:
            raise WheelUpdateError("the pre-update memory snapshot failed")

        with pytest.raises(WheelUpdateError, match="memory snapshot failed"):
            self._apply(before_promote=refuse)
        assert not layout.stable_link.exists()
        assert not layout.versioned_tree("9.9.9").exists(), "the unpromoted tree is cleared"

    def test_progress_starts_only_once_the_lock_is_held(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _layout, _calls = self._wire(monkeypatch, tmp_path)
        events: list[str] = []
        self._apply(on_locked=lambda: events.append("locked"), progress=events.append)
        assert events[0] == "locked"

    def test_failed_verification_never_promotes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path)

        def failing_verify(shadow: Path, version: str, **_kw: object) -> None:
            raise WheelUpdateError("shadow venv cannot import kiro_crew — not promoting")

        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", failing_verify)
        with pytest.raises(WheelUpdateError, match="not promoting"):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
            )
        assert not layout.stable_link.exists(), "a failed verification must not promote"
