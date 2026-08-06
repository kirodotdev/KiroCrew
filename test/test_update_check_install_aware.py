"""The gateway update check must work — and fail honestly — on BOTH install layouts.

Before this, ``_do_update_check`` was git-only: a wheel install (the ``cli.sh``
managed venv, a cloud tarball) hit an early ``return``, the module cache stayed at
its initial ``available: False``, and the dashboard rendered "you're on the latest
version" for a check that had never run. Independently, the old ``_version_tuple``
coerced any prerelease to ``(0,)``, so ``0.1.2rc3`` and ``0.1.3rc2`` compared
EQUAL and no rc-to-rc step was ever detected.

These tests pin both halves plus the contract that makes the UI safe: a check that
could not complete sets ``error`` and leaves ``checked`` False.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from kiro_crew.dashboard.handlers import updates

# A well-formed manifest, shaped like the real feed document.
_FEED_TEMPLATE = {
    "algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
    "channel": "insider",
    "key_id": "sha256:d3a83f0c",
    "pub_date": "2026-08-05T07:49:33Z",
    "python_requires": ">=3.10",
    "schema": "kirocrew-cli-artifact-manifest-v1",
    "sha256": "ea681adb",
    "signature": "V9MGrlYt",
    "version": "0.1.3rc2",
    "wheel_url": "https://download.crew.kiro.dev/cli/insider/0.1.3rc2/x.whl",
}


def _manifest(**overrides: object) -> bytes:
    body = dict(_FEED_TEMPLATE)
    body.update(overrides)
    return json.dumps(body).encode()


def _stub_feed(monkeypatch, *, status: int = 200, body: bytes | None = None, exc=None):
    """Replace the single network seam. Records the URL that was requested."""
    seen: dict[str, str] = {}

    async def _fake(url: str) -> tuple[int, bytes]:
        seen["url"] = url
        if exc is not None:
            raise exc
        return status, _manifest() if body is None else body

    monkeypatch.setattr(updates, "_fetch_feed_bytes", _fake)
    return seen


@pytest.fixture(autouse=True)
def _wheel_install(monkeypatch, tmp_path):
    """Default every test in this module to a WHEEL install on the insider lane.

    A git checkout is opt-in per test (``_git_install``), because the interesting
    new behaviour is the layout that used to be skipped entirely.
    """
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    (tmp_path / "channel").write_text("insider\n")
    monkeypatch.setattr(updates, "config_dir", lambda: tmp_path)
    # Pin the packaging stamp rather than inheriting the ambient one: a checkout
    # has no `_build_info.py` and reports `source`, but an installed wheel reports
    # `wheel`, and the suite must not read differently depending on where it runs.
    monkeypatch.setattr(updates, "distribution", lambda: "wheel")
    original = dict(updates._update_info)
    yield
    updates._update_info.clear()
    updates._update_info.update(original)


class TestVersionOrdering:
    """PEP 440 ordering, which the old ``_version_tuple`` could not express."""

    def test_full_prerelease_chain_is_strictly_increasing(self):
        chain = [
            "0.1.2.dev20260805085917",
            "0.1.2a1",
            "0.1.2b2",
            "0.1.2rc1.dev3",
            "0.1.2rc1",
            "0.1.2rc3",
            "0.1.2",
            "0.1.2.post1",
            "0.1.3rc2",
            "0.1.3",
        ]
        for lower, higher in zip(chain, chain[1:]):
            assert updates._is_newer(higher, lower) is True, f"{higher} !> {lower}"
            assert updates._is_newer(lower, higher) is False, f"{lower} > {higher}"

    def test_the_exact_regression_rc_to_rc(self):
        # The reported case: gateway on 0.1.2rc3, insider feed at 0.1.3rc2. The old
        # comparator read both as (0,) and answered "up to date".
        assert updates._is_newer("0.1.3rc2", "0.1.2rc3") is True

    def test_identical_versions_are_not_newer(self):
        assert updates._is_newer("0.1.2rc3", "0.1.2rc3") is False

    def test_release_cores_are_zero_padded(self):
        assert updates._is_newer("0.1", "0.1.0") is False
        assert updates._is_newer("0.1.0", "0.1") is False
        assert updates._is_newer("0.1.0.1", "0.1") is True

    def test_semver_style_suffix_sorts_below_its_release(self):
        # The desktop lane stamps 0.3.0-insider.2 / 0.3.0-nightly.<stamp>.
        assert updates._is_newer("0.3.0", "0.3.0-insider.2") is True
        assert updates._is_newer("0.3.0-insider.3", "0.3.0-insider.2") is True
        assert updates._is_newer("0.3.0-insider.2", "0.3.0") is False

    def test_leading_v_is_tolerated(self):
        assert updates._is_newer("v0.1.3", "0.1.2") is True

    @pytest.mark.parametrize("junk", ["", "   ", "latest", "abc.def", None])
    def test_unparseable_returns_none_not_a_verdict(self, junk):
        assert updates._version_key(junk) is None
        assert updates._is_newer("0.1.3", junk) is None
        assert updates._is_newer(junk, "0.1.3") is None


class TestChannelResolution:
    def test_reads_the_channel_file_the_installer_wrote(self, tmp_path, monkeypatch):
        (tmp_path / "channel").write_text("  Insider \n")
        monkeypatch.setattr(updates, "config_dir", lambda: tmp_path)
        assert updates._release_channel() == "insider"

    def test_missing_file_falls_back_to_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updates, "config_dir", lambda: tmp_path / "nope")
        assert updates._release_channel() == "stable"

    def test_junk_falls_back_to_stable(self, tmp_path, monkeypatch):
        (tmp_path / "channel").write_text("../../etc/passwd")
        monkeypatch.setattr(updates, "config_dir", lambda: tmp_path)
        assert updates._release_channel() == "stable"

    def test_update_command_always_names_the_channel(self):
        # cli.sh defaults to stable and never reads the channel file, so a bare
        # re-run would silently move an insider install onto the stable lane.
        cmd = updates._wheel_update_command("insider", "https://download.example")
        assert "--channel insider" in cmd
        assert cmd.startswith("curl -fsSL --proto '=https' https://download.example/cli.sh")

    def test_update_command_pins_https(self):
        # The string is copied into a shell and pipes an installer into `sh`, and
        # the base is overridable via KIROCREW_CDN_BASE. Without --proto '=https'
        # an http:// override yields a command that fetches a script in plaintext
        # and executes it — an on-path attacker could swap the installer.
        cmd = updates._wheel_update_command("stable", "http://evil.example")
        assert "--proto '=https'" in cmd

    def test_cdn_override_moves_check_and_command_together(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://cdn.example/")
        feed, artifact = updates._cdn_bases()
        assert feed == artifact == "https://cdn.example"


class TestWheelInstallCheck:
    def test_reports_available_against_the_channel_feed(self, monkeypatch):
        seen = _stub_feed(monkeypatch, body=_manifest(version="0.1.3rc2"))
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        assert seen["url"] == "https://updates.crew.kiro.dev/feed/insider/latest-cli.json"
        assert info["available"] is True
        assert info["checked"] is True
        assert info["error"] == ""
        assert info["remote_version"] == "0.1.3rc2"
        assert info["install_kind"] == "wheel"
        assert info["self_updatable"] is False
        assert info["channel"] == "insider"
        assert "--channel insider" in str(info["update_command"])

    def test_reports_up_to_date_only_after_a_real_comparison(self, monkeypatch):
        _stub_feed(monkeypatch, body=_manifest(version="0.1.2rc3"))
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        assert info["available"] is False
        assert info["checked"] is True  # THIS is what licenses the UI success line
        assert info["error"] == ""

    def test_never_surfaces_installable_artifact_metadata(self, monkeypatch):
        _stub_feed(monkeypatch)
        monkeypatch.setattr(updates, "_local_version", "0.1.0")
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        # The manifest signature is NOT verified here, so nothing that could
        # redirect an install may leave this function.
        blob = json.dumps(info)
        assert "wheel_url" not in info
        assert "sha256" not in info
        assert "signature" not in info
        assert _FEED_TEMPLATE["wheel_url"] not in blob
        assert str(_FEED_TEMPLATE["sha256"]) not in blob

    def test_keeps_the_publication_date_when_well_formed(self, monkeypatch):
        _stub_feed(monkeypatch)
        monkeypatch.setattr(updates, "_local_version", "0.1.0")
        asyncio.run(updates._do_update_check())
        assert updates.get_update_info()["remote_pub_date"] == "2026-08-05T07:49:33Z"

    def test_drops_a_malformed_publication_date_without_failing(self, monkeypatch):
        _stub_feed(monkeypatch, body=_manifest(pub_date="<script>x</script>"))
        monkeypatch.setattr(updates, "_local_version", "0.1.0")
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert "remote_pub_date" not in info
        assert info["checked"] is True  # optional field, not a hard failure


class TestWheelInstallFailuresAreHonest:
    """Every failure must set ``error`` and leave ``checked`` False."""

    def _assert_failed(self, code: str) -> None:
        info = updates.get_update_info()
        assert info["error"] == code
        assert info["checked"] is False
        assert info["available"] is False
        # The install is still identified, so the UI can still tell the user HOW
        # to update even when it could not learn WHETHER to.
        assert info["install_kind"] == "wheel"
        assert "--channel insider" in str(info["update_command"])

    def test_network_error(self, monkeypatch):
        _stub_feed(monkeypatch, exc=aiohttp.ClientConnectionError("boom"))
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_unreachable")

    def test_timeout(self, monkeypatch):
        _stub_feed(monkeypatch, exc=asyncio.TimeoutError())
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_unreachable")

    def test_http_error_status(self, monkeypatch):
        _stub_feed(monkeypatch, status=403, body=b"<html>denied</html>")
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_unreachable")

    def test_not_json(self, monkeypatch):
        _stub_feed(monkeypatch, body=b"not json at all")
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_json_but_not_an_object(self, monkeypatch):
        _stub_feed(monkeypatch, body=b'["a", "list"]')
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_wrong_schema(self, monkeypatch):
        _stub_feed(monkeypatch, body=_manifest(schema="something-else-v9"))
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_channel_mismatch_is_refused(self, monkeypatch):
        # A mis-wired or swapped feed must not advertise another lane's build.
        _stub_feed(monkeypatch, body=_manifest(channel="stable"))
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    @pytest.mark.parametrize(
        "bad",
        ["", "0.1.3 rc2", "<b>0.1.3</b>", "0.1.3/../..", "x" * 65],
    )
    def test_version_charset_is_validated(self, monkeypatch, bad):
        _stub_feed(monkeypatch, body=_manifest(version=bad))
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_missing_version(self, monkeypatch):
        body = dict(_FEED_TEMPLATE)
        body.pop("version")
        _stub_feed(monkeypatch, body=json.dumps(body).encode())
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_oversized_body_is_detected_not_truncated(self, monkeypatch):
        padded = dict(_FEED_TEMPLATE)
        padded["signature"] = "A" * (updates._FEED_MAX_BYTES + 100)
        _stub_feed(monkeypatch, body=json.dumps(padded).encode())
        asyncio.run(updates._do_update_check())
        self._assert_failed("feed_malformed")

    def test_unparseable_local_version_is_not_up_to_date(self, monkeypatch):
        _stub_feed(monkeypatch)
        monkeypatch.setattr(updates, "_local_version", "not-a-version")
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert info["error"] == "version_unparseable"
        assert info["checked"] is False
        assert info["available"] is False

    def test_stale_state_never_survives_a_later_failure(self, monkeypatch):
        _stub_feed(monkeypatch, body=_manifest(version="0.1.3rc2"))
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())
        assert updates.get_update_info()["remote_version"] == "0.1.3rc2"

        _stub_feed(monkeypatch, exc=aiohttp.ClientConnectionError("boom"))
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert info["remote_version"] == ""  # no half-truth beside a fresh error
        assert info["available"] is False
        assert info["error"] == "feed_unreachable"


class TestGitCheckoutStillWorks:
    @pytest.fixture
    def _git_install(self, monkeypatch, tmp_path):
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        return tmp_path

    @staticmethod
    def _git_script(monkeypatch, outputs: list[tuple[int, bytes]]):
        """Feed scripted (returncode, stdout) pairs to successive git calls."""
        calls: list[tuple[str, ...]] = []
        queue = list(outputs)

        async def _exec(*args, **kwargs):
            calls.append(tuple(args))
            rc, out = queue.pop(0) if queue else (0, b"")

            class _Proc:
                returncode = rc

                async def communicate(self):
                    return (out, b"")

            return _Proc()

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _exec)
        return calls

    def test_detects_a_prerelease_bump_the_old_comparator_missed(
        self, _git_install, monkeypatch
    ):
        calls = self._git_script(
            monkeypatch,
            [
                (0, b""),  # git fetch
                (0, b"aaaa\n"),  # rev-parse HEAD
                (0, b"bbbb\n"),  # rev-parse @{u}
                (0, b'__version__ = "0.1.3rc2"\n'),  # git show
                (0, b"+### 0.1.3rc2\n+- thing\n"),  # git diff CHANGELOG.md
            ],
        )
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        assert info["install_kind"] == "git"
        assert info["self_updatable"] is True
        assert info["available"] is True
        assert info["checked"] is True
        assert info["error"] == ""
        assert info["remote_version"] == "0.1.3rc2"
        assert "### 0.1.3rc2" in str(info["changes"])
        assert info["channel"] == ""
        assert info["update_command"] == ""
        assert any("fetch" in c for c in calls)

    def test_git_fetch_failure_is_reported_not_swallowed(self, _git_install, monkeypatch):
        self._git_script(monkeypatch, [(128, b"")])
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert info["error"] == "git_fetch_failed"
        assert info["checked"] is False
        assert info["install_kind"] == "git"

    def test_missing_upstream_is_reported_not_up_to_date(self, _git_install, monkeypatch):
        self._git_script(
            monkeypatch,
            [(0, b""), (0, b"aaaa\n"), (0, b"")],  # no @{u}
        )
        asyncio.run(updates._do_update_check())
        info = updates.get_update_info()
        assert info["error"] == "git_read_failed"
        assert info["checked"] is False

    def test_unreadable_remote_version_is_reported(self, _git_install, monkeypatch):
        self._git_script(
            monkeypatch,
            [(0, b""), (0, b"aaaa\n"), (0, b"bbbb\n"), (0, b"# no version here\n")],
        )
        asyncio.run(updates._do_update_check())
        assert updates.get_update_info()["error"] == "git_read_failed"

    def test_a_git_checkout_never_touches_the_feed(self, _git_install, monkeypatch):
        # The autouse conftest guard would blow up on any real fetch; this asserts
        # the branch choice explicitly rather than relying on that.
        async def _boom(url: str):  # pragma: no cover - must not be called
            raise AssertionError("git checkout must not read the release feed")

        monkeypatch.setattr(updates, "_fetch_feed_bytes", _boom)
        self._git_script(
            monkeypatch,
            [
                (0, b""),
                (0, b"aaaa\n"),
                (0, b"aaaa\n"),
                (0, b'__version__ = "0.1.2rc3"\n'),
            ],
        )
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())
        assert updates.get_update_info()["checked"] is True


class TestExternallyManagedInstalls:
    """Desktop bundles and containers must not answer with the CLI feed.

    The desktop bundles EMBED this backend (``packaging/build-desktop.sh`` ships a
    PBS interpreter tree inside the .app / AppImage), so they run this code and
    would otherwise compare against the wrong release stream and then recommend an
    installer that does not apply. It is user-visible: the Settings nav dot is
    ``status.update_available || desktopUpdateAvailable``, so a false positive
    lights a badge whose destination reports "up to date".
    """

    @pytest.mark.parametrize(
        ("dist", "code"),
        [("dmg", "managed_by_app"), ("appimage", "managed_by_app"), ("docker", "managed_by_image")],
    )
    def test_defers_instead_of_guessing(self, monkeypatch, dist, code):
        def _boom(url: str):  # pragma: no cover - must not be called
            raise AssertionError(f"{dist} must not read the CLI release feed")

        monkeypatch.setattr(updates, "_fetch_feed_bytes", _boom)
        monkeypatch.setattr(updates, "distribution", lambda: dist)
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        assert info["install_kind"] == dist
        assert info["error"] == code
        assert info["self_updatable"] is False
        # Not available: this is what keeps the nav badge quiet. And not checked:
        # the gateway reached no verdict, so nothing may render "up to date".
        assert info["available"] is False
        assert info["checked"] is False
        # No command either — the app/image owns the upgrade, not a shell one-liner.
        assert info["update_command"] == ""

    def test_a_git_checkout_wins_over_a_desktop_stamp(self, monkeypatch, tmp_path):
        # A developer running the desktop build's own source tree is still a git
        # checkout, and `POST /api/update` works there.
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(updates, "distribution", lambda: "dmg")

        class _Proc:
            returncode = 128

            async def communicate(self):
                return (b"", b"")

        async def _exec(*a, **k):
            return _Proc()

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _exec)
        asyncio.run(updates._do_update_check())
        assert updates.get_update_info()["install_kind"] == "git"

    @pytest.mark.parametrize("dist", ["wheel", "source"])
    def test_feed_checkable_kinds_are_matched_by_exclusion(self, monkeypatch, dist):
        # `source` is the value an UNSTAMPED wheel reports: `_build_info.py` only
        # exists in artifacts built after the stamp landed, and every CLI wheel
        # released before it carries none. An `== "wheel"` allowlist would exclude
        # exactly the already-released installs this check exists to fix.
        _stub_feed(monkeypatch, body=_manifest(version="0.1.3rc2"))
        monkeypatch.setattr(updates, "distribution", lambda: dist)
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")
        asyncio.run(updates._do_update_check())

        info = updates.get_update_info()
        assert info["install_kind"] == dist  # reported verbatim, not flattened
        assert info["available"] is True
        assert info["checked"] is True
        assert "--channel insider" in str(info["update_command"])


class TestCheckIsRateLimitedEvenOnFailure:
    def test_failure_stamps_the_poll_clock(self, monkeypatch):
        # Otherwise an offline host turns the 12-hourly background poll into a hot
        # retry loop against the CDN.
        monkeypatch.setattr(updates, "_last_update_check", 0.0)
        _stub_feed(monkeypatch, exc=aiohttp.ClientConnectionError("boom"))
        asyncio.run(updates._do_update_check())
        assert updates._last_update_check > 0.0

    def test_overlapping_checks_are_single_flight(self, monkeypatch):
        # /api/status fires this as a background task on every poll until the
        # interval clock is stamped, and the clock is only stamped when a check
        # FINISHES — so concurrent polls used to stack one CDN fetch each, every
        # one holding a session for the full timeout.
        calls = {"n": 0}

        async def _slow(url: str) -> tuple[int, bytes]:
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return 200, _manifest(version="0.1.3rc2")

        monkeypatch.setattr(updates, "_fetch_feed_bytes", _slow)
        monkeypatch.setattr(updates, "_local_version", "0.1.2rc3")

        async def _drive() -> None:
            await asyncio.gather(*(updates._do_update_check() for _ in range(5)))

        asyncio.run(_drive())
        assert calls["n"] == 1
        # The winner's verdict still lands — the no-ops must not blank it.
        assert updates.get_update_info()["available"] is True

    def test_the_flag_is_released_even_when_the_check_raises(self, monkeypatch):
        # A stuck flag would wedge the check for the process's lifetime.
        async def _boom(url: str) -> tuple[int, bytes]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(updates, "_fetch_feed_bytes", _boom)
        asyncio.run(updates._do_update_check())
        assert updates._check_in_flight is False
        assert updates.get_update_info()["error"] == "unknown"


class TestAutoApplyGuard:
    """A wheel install must never drive the git-based auto-apply path."""

    @staticmethod
    def _orchestrator():
        from kiro_crew.slack.gateway import GatewayOrchestrator

        # __new__ without __init__: _check_for_updates only touches
        # dashboard_state and _auto_apply_update, and a real construction would
        # drag in credentials, the slot manager and the whole boot path.
        orch = object.__new__(GatewayOrchestrator)
        orch.dashboard_state = MagicMock()
        orch._auto_apply_update = AsyncMock()
        return orch

    def _run(self, info: dict[str, object], *, auto_update: bool):
        import kiro_crew.dashboard.handlers as handlers

        orch = self._orchestrator()
        cfg = MagicMock()
        cfg.auto_update = auto_update
        original = dict(handlers._update_info)
        try:
            handlers._update_info.clear()
            handlers._update_info.update(info)
            with patch.object(handlers, "_do_update_check", new_callable=AsyncMock):
                with patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg):
                    with patch(
                        "kiro_crew.platform.update_governance.update_required",
                        return_value=False,
                    ):
                        asyncio.run(orch._check_for_updates())
        finally:
            handlers._update_info.clear()
            handlers._update_info.update(original)
        return orch

    def test_wheel_install_notifies_instead_of_applying(self):
        orch = self._run(
            {"available": True, "self_updatable": False, "install_kind": "wheel"},
            auto_update=True,
        )
        orch._auto_apply_update.assert_not_awaited()
        orch.dashboard_state.push_refresh.assert_called_with("update_available")

    def test_git_checkout_still_auto_applies(self):
        orch = self._run(
            {"available": True, "self_updatable": True, "install_kind": "git"},
            auto_update=True,
        )
        orch._auto_apply_update.assert_awaited_once()

    def test_a_failed_check_does_not_claim_up_to_date(self, capsys):
        orch = self._run(
            {"available": False, "error": "feed_unreachable", "install_kind": "wheel"},
            auto_update=True,
        )
        orch._auto_apply_update.assert_not_awaited()
        assert "Already on latest version" not in capsys.readouterr().out

    def test_a_clean_check_still_reports_up_to_date(self, capsys):
        self._run({"available": False, "checked": True, "error": ""}, auto_update=True)
        assert "Already on latest version" in capsys.readouterr().out
