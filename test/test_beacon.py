"""Tests for the anonymous usage beacon (kiro_crew.beacon).

Drives real production code — no reimplementation of the payload shape or the
suppression rules in the test, so drift in either fails here.
"""

from __future__ import annotations

import http.client
import importlib
import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import beacon, platform_compat
from kiro_crew.apps import install_receipt

# Captured before any fixture can monkeypatch the module attribute, so the
# dedicated tests below can exercise the REAL implementation.
_REAL_IS_DEFAULT_HOME = beacon.is_default_home

_STAMP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stamp-distribution.sh"
_NO_BASH_REASON = "no working bash on PATH (the stamping script only runs on build hosts)"


def _run_stamp(dist: str, pkg_dir) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["bash", str(_STAMP_SCRIPT), dist, str(pkg_dir)], capture_output=True, text=True
    )


def _probe_bash() -> bool:
    """Return whether bash can actually run this script.

    Not a `shutil.which("bash")` check: Windows ships a WSL launcher stub at
    that name which resolves, then fails with "install a distro" and cannot see
    a Windows path at all. So probe by running the real script in a temp dir and
    requiring the generated file, which is the capability the tests need.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_stamp("source", tmp)
            return proc.returncode == 0 and (Path(tmp) / "_build_info.py").is_file()
    except (OSError, subprocess.SubprocessError):
        return False


_HAVE_BASH = _probe_bash()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at tmp_path and neutralize ambient env.

    The real CI environment sets CI=1, which would otherwise suppress every
    send and make the positive-path tests vacuous.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(beacon, "is_ci", lambda: False)
    monkeypatch.delenv(beacon.DISABLE_ENV, raising=False)
    monkeypatch.delenv(beacon.DIST_ENV, raising=False)
    # Present the unstamped shape by default. A developer who has run a
    # packaging script has a real (gitignored) _build_info.py in the checkout,
    # which would otherwise outrank DIST_ENV and fail the env-var tests.
    monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", "")
    return tmp_path


class TestInstallId:
    def test_generated_once_and_stable(self, _isolated_home):
        first = beacon.install_id()
        assert len(first) == 32
        assert beacon.install_id() == first, "id must be stable across calls"

    def test_persisted_to_data_home(self, _isolated_home):
        ident = beacon.install_id()
        assert (_isolated_home / beacon.INSTALL_ID_FILE).read_text().strip() == ident

    def test_create_false_does_not_materialize(self, _isolated_home):
        assert beacon.install_id(create=False) == ""
        assert not (_isolated_home / beacon.INSTALL_ID_FILE).exists()

    def test_create_false_never_returns_process_local_fallback(
        self, _isolated_home, monkeypatch
    ):
        def denied():
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(beacon, "config_dir", denied)
        assert beacon.install_id(create=False) == ""
        assert beacon.install_id(create=True) == beacon._IN_MEMORY_ID

    def test_corrupt_id_is_regenerated(self, _isolated_home):
        (_isolated_home / beacon.INSTALL_ID_FILE).write_text("not-a-valid-id")
        fresh = beacon.install_id()
        assert len(fresh) == 32 and fresh != "not-a-valid-id"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX, reason="/dev/zero and os.mkfifo are POSIX-only"
    )
    def test_special_file_symlink_is_not_read(self, _isolated_home):
        """A symlink to /dev/zero must not turn the read into an infinite one.

        Regression test: ``read_text`` follows symlinks, so this allocated
        unboundedly until OOM — inside the gateway's beacon thread. Bounded with a
        real timeout because the failure mode is "never returns", which a plain
        assertion cannot catch.
        """
        import os
        import threading

        os.symlink("/dev/zero", _isolated_home / beacon.INSTALL_ID_FILE)
        result: dict = {}

        def probe():
            result["id"] = beacon.install_id(create=False)

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(10)
        assert not t.is_alive(), "read did not terminate — unbounded /dev/zero read"
        assert result["id"] == "", "a device node must be treated as absent"

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="os.mkfifo is POSIX-only")
    def test_fifo_does_not_block(self, _isolated_home):
        """A FIFO at the state path must be rejected without opening it."""
        import os
        import threading

        os.mkfifo(_isolated_home / beacon.STAMP_FILE)
        done = threading.Event()

        def probe():
            beacon.already_sent_today()
            done.set()

        threading.Thread(target=probe, daemon=True).start()
        assert done.wait(10), "opening a FIFO blocked forever"

    def test_oversized_state_file_is_bounded(self, _isolated_home):
        """A huge regular file must be read only up to the cap."""
        (_isolated_home / beacon.INSTALL_ID_FILE).write_text("a" * 2_000_000)
        # Far longer than a valid id, so it is corrupt -> regenerated, not returned.
        assert beacon.install_id(create=False) == ""

    def test_non_utf8_id_does_not_crash(self, _isolated_home):
        """A non-UTF-8 id file must be treated as corrupt, not raise.

        Regression test: a strict decode raises UnicodeDecodeError — a
        ValueError, NOT an OSError — so it escaped the handler and killed
        `kirocrew telemetry status` outright.
        """
        (_isolated_home / beacon.INSTALL_ID_FILE).write_bytes(b"\xff\xfe bad \x80")
        # status path: must report nothing rather than raise
        assert beacon.install_id(create=False) == ""
        # send path: must regenerate a valid id
        fresh = beacon.install_id()
        assert len(fresh) == 32
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        assert beacon.DISABLE_ENV in beacon.format_status(info)

    def test_id_is_not_derived_from_identity(self, _isolated_home, monkeypatch):
        """The id must not be a function of hostname/username.

        Guards the deliberate choice NOT to reuse handlers_system's owner hash,
        which is HMAC(salt, hostname + ":" + username).
        """
        import getpass
        import platform as _platform

        monkeypatch.setattr(_platform, "node", lambda: "host-alpha")
        monkeypatch.setattr(getpass, "getuser", lambda: "alice")
        a = beacon.install_id()
        (_isolated_home / beacon.INSTALL_ID_FILE).unlink()
        monkeypatch.setattr(_platform, "node", lambda: "host-beta")
        monkeypatch.setattr(getpass, "getuser", lambda: "bob")
        b = beacon.install_id()
        assert a != b, "a fresh id must be random, not identity-derived"


class TestPayloadAllowlist:
    EXPECTED_KEYS = {
        "id",
        "v",
        "py",
        "dist",
        "first_seen",
    }

    # Keys the payload once carried. Asserted ABSENT rather than merely omitted
    # above, because an EQUALITY check alone would pass if a future change both
    # added a field and updated EXPECTED_KEYS in one edit — which is exactly how a
    # field silently returns. A named absence set makes the re-addition of any of
    # these four a deliberate, visible act: the test names them, the module
    # docstring explains why they went, and the user-facing disclosure
    # (website/src/test/PrivacyPanel.test.tsx) asserts the same four are absent.
    REMOVED_KEYS = {"chan", "os", "arch", "gov"}

    def test_exactly_five_keys(self, _isolated_home):
        assert set(beacon.payload("1.2.3")) == self.EXPECTED_KEYS

    def test_removed_fields_are_not_on_the_wire(self, _isolated_home):
        """The four minimized-away fields must not reappear in payload or URL."""
        fields = beacon.payload("0.1.2-nightly.20260731t065756")
        assert self.REMOVED_KEYS.isdisjoint(fields)
        # Also check the composed URL: a field could be re-added at the URL layer
        # (a query param appended in beacon_url) without touching _fields.
        url = beacon.beacon_url("https://example.invalid", fields)
        for key in self.REMOVED_KEYS:
            assert f"{key}=" not in url

    def test_removed_helpers_are_gone(self, _isolated_home):
        """The producers are deleted, not merely unwired.

        A dormant ``channel()``/``governance_posture()`` left in the module is one
        call site away from being back on the wire, and would keep the removal
        looking reversible-by-accident. Deleting them makes a re-add a real code
        change that has to pass review.
        """
        for name in ("channel", "governance_posture"):
            assert not hasattr(beacon, name), f"beacon.{name} should be deleted"

    def test_no_value_leaks_identity_or_paths(self, _isolated_home, monkeypatch):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", "/Users/secret/my-private-repo")
        blob = json.dumps(beacon.payload("1.2.3"))
        for forbidden in ("secret", "my-private-repo", "/Users", "\\Users"):
            assert forbidden not in blob

    def test_python_minor_has_no_patch_component(self, _isolated_home):
        assert beacon.python_minor().count(".") == 1

    def test_distribution_clamped_to_known_set(self, _isolated_home, monkeypatch):
        monkeypatch.setenv(beacon.DIST_ENV, "definitely-not-a-channel")
        assert beacon.distribution() == beacon.DEFAULT_DISTRIBUTION
        monkeypatch.setenv(beacon.DIST_ENV, "DMG")
        assert beacon.distribution() == "dmg", "case-insensitive, still clamped"

    def test_first_seen_flips_after_a_send(self, _isolated_home, monkeypatch):
        assert beacon.payload("1.2.3")["first_seen"] == "1"
        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen())
        beacon.send("https://example.invalid", "1.2.3", enabled=True, acked=True)
        assert beacon.payload("1.2.3")["first_seen"] == "0"


class TestDistributionStamp:
    """`dist` must come from the ARTIFACT, not from the environment.

    A checkout ships no ``_build_info``, so every unstamped run reports
    "source". That is correct for a git clone and wrong for a package: when no
    packaging path stamps, the field is a constant and the channel breakdown
    answers nothing. These tests pin the precedence and the stamping script so
    the build and the metric cannot drift apart silently.

    Patched via the module-level ``_BAKED_DISTRIBUTION`` binding, never by
    writing a real ``_build_info.py``: that file lives inside the installed
    package, so it is process-wide shared state that races under the default
    ``-n auto`` and would corrupt sibling tests on other workers.
    """

    def test_unstamped_checkout_reports_source(self, _isolated_home, monkeypatch):
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", "")
        monkeypatch.delenv(beacon.DIST_ENV, raising=False)
        assert beacon.baked_distribution() == ""
        assert beacon.distribution() == "source"

    @pytest.mark.parametrize("dist", sorted(beacon.KNOWN_DISTRIBUTIONS))
    def test_baked_value_is_reported(self, _isolated_home, monkeypatch, dist):
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", dist)
        monkeypatch.delenv(beacon.DIST_ENV, raising=False)
        assert beacon.distribution() == dist

    def test_baked_value_outranks_the_env_var(self, _isolated_home, monkeypatch):
        """The env var must not be able to relabel a packaged install."""
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", "dmg")
        monkeypatch.setenv(beacon.DIST_ENV, "docker")
        assert beacon.distribution() == "dmg"

    def test_unknown_baked_value_falls_through(self, _isolated_home, monkeypatch):
        """A bad stamp must not put an unclamped value on the wire."""
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", "not-a-channel")
        monkeypatch.setenv(beacon.DIST_ENV, "wheel")
        assert beacon.baked_distribution() == ""
        assert beacon.distribution() == "wheel"
        assert beacon.payload("1.2.3")["dist"] in beacon.KNOWN_DISTRIBUTIONS

    @pytest.mark.skipif(not _HAVE_BASH, reason=_NO_BASH_REASON)
    def test_real_stamped_module_is_read(self, _isolated_home, monkeypatch, tmp_path):
        """The binding must actually come from a real generated module.

        Patching ``_BAKED_DISTRIBUTION`` everywhere else would still pass if the
        import were wired to the wrong name, so import a script-generated module
        from a temp dir and assert the value the packaging path would produce.
        """
        proc = _run_stamp("dmg", tmp_path)
        assert proc.returncode == 0, proc.stderr
        spec = importlib.util.spec_from_file_location("_stamped_probe", tmp_path / "_build_info.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.DISTRIBUTION == "dmg"
        monkeypatch.setattr(beacon, "_BAKED_DISTRIBUTION", module.DISTRIBUTION)
        assert beacon.distribution() == "dmg"

    def test_stamp_script_exists(self):
        """Asserted unconditionally: a deleted script must fail everywhere.

        The behavioral tests below need a working bash and skip without one, so
        without this the script could vanish and every platform lacking bash
        would still report green.
        """
        assert _STAMP_SCRIPT.is_file(), f"missing stamping script: {_STAMP_SCRIPT}"

    @pytest.mark.skipif(not _HAVE_BASH, reason=_NO_BASH_REASON)
    def test_stamp_script_covers_every_known_distribution(self, tmp_path):
        """Every KNOWN_DISTRIBUTIONS value must be accepted by the script.

        Pins the two lists together: adding a channel to the frozenset without
        teaching the script would otherwise fail only at release time.
        """
        for dist in sorted(beacon.KNOWN_DISTRIBUTIONS):
            pkg = tmp_path / dist
            pkg.mkdir()
            proc = _run_stamp(dist, pkg)
            assert proc.returncode == 0, f"{dist}: {proc.stderr}"
            assert f'DISTRIBUTION = "{dist}"' in (pkg / "_build_info.py").read_text()

    @pytest.mark.skipif(not _HAVE_BASH, reason=_NO_BASH_REASON)
    def test_stamp_script_rejects_an_unknown_value(self, tmp_path):
        """A typo must fail the build, not silently bake a rejected value.

        Skipped without a usable bash rather than left running: a broken bash
        also exits non-zero, so this would pass for the wrong reason.
        """
        proc = _run_stamp("flatpak", tmp_path)
        assert proc.returncode != 0
        assert not (tmp_path / "_build_info.py").exists()

    def test_generated_module_is_gitignored(self):
        """A committed stamp would mislabel every other build's beacon."""
        root = Path(__file__).resolve().parents[1]
        assert "src/kiro_crew/_build_info.py" in (root / ".gitignore").read_text()


class TestVersionClamp:
    """`v` must stay low-cardinality however the build stamped __version__.

    Guards a real defect: raw ``__version__`` carries a per-build timestamp on
    dev/nightly builds, which minted one CloudWatch metric per build and — worse
    — pushed real low-install releases past the aggregator's LIMIT 25, dropping
    them from CloudWatch AND the permanent rollup silently.
    """

    # The exact spellings observed in live beacon logs, plus the stable case.
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("0.1.2", "0.1.2"),
            ("0.1.2-nightly.20260731t065756", "0.1.2"),
            ("0.1.2.dev20260731065756", "0.1.2"),
            ("0.1.2-insider.4", "0.1.2"),
            ("0.1.2+local.abcdef", "0.1.2"),
            ("v0.1.2", "0.1.2"),
            ("1.2", "1.2.0"),
        ],
    )
    def test_release_strips_every_build_stamp(self, _isolated_home, raw, expected):
        assert beacon.release(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "probe", "not-a-version", None])
    def test_unparseable_version_is_one_bounded_bucket(self, _isolated_home, raw):
        assert beacon.release(raw) == beacon.UNKNOWN_VERSION

    def test_distinct_nightly_builds_collapse_to_one_value(self, _isolated_home):
        """The cardinality property itself, not just one spelling."""
        stamps = [f"0.1.2-nightly.20260731t0657{n:02d}" for n in range(50)]
        assert len({beacon.release(s) for s in stamps}) == 1

    def test_payload_sends_the_clamped_version_not_the_raw_one(self, _isolated_home):
        fields = beacon.payload("0.1.2-nightly.20260731t065756")
        assert fields["v"] == "0.1.2"
        assert "20260731" not in json.dumps(fields), "build stamp must not reach the wire"

    def test_prerelease_lanes_collapse_into_the_release_number(self, _isolated_home):
        """A nightly and a stable build of one release report the SAME `v`.

        With ``chan`` removed, ``release()`` is the ONLY thing standing between a
        per-build stamp and the wire — so the clamp is load-bearing for both
        cardinality and anonymity. Both spellings of every lane collapse to the
        release number, which is also what makes the channel recoverable from `v`
        for a genuinely pre-release version without sending a channel field.
        """
        one_release = [
            "0.1.2",
            "0.1.2-nightly.20260731t065756",
            "0.1.2.dev20260731065756",
            "0.1.2-insider.4",
            "0.1.2rc4",
            "0.1.2b1",
            "0.1.2+local.abcdef",
        ]
        assert {beacon.release(v) for v in one_release} == {"0.1.2"}

    def test_build_stamp_is_absent_from_the_url(self, _isolated_home):
        url = beacon.beacon_url(
            "https://e.invalid", beacon.payload("0.1.2-nightly.20260731t065756")
        )
        assert "20260731" not in url
        assert "v=0.1.2" in url

    def test_status_preview_matches_what_is_actually_sent(self, _isolated_home):
        """The preview shares _fields() with payload(), so it cannot drift."""
        info = beacon.status(
            "https://e.invalid",
            enabled=True,
            app_version="0.1.2-nightly.20260731t065756",
            acked=True,
        )
        preview = info["payload_preview"]
        sent = beacon.payload("0.1.2-nightly.20260731t065756")
        assert set(preview) == set(sent)
        assert {k: v for k, v in preview.items() if k != "id"} == {
            k: v for k, v in sent.items() if k != "id"
        }


class TestSuppression:
    def test_env_opt_out_wins_over_enabled(self, _isolated_home, monkeypatch):
        monkeypatch.setenv(beacon.DISABLE_ENV, "1")
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert not ok and beacon.DISABLE_ENV in reason

    def test_config_toggle_off(self, _isolated_home):
        ok, reason, _code = beacon.should_send(enabled=False, acked=True)
        assert not ok and "disabled" in reason

    def test_ci_suppressed(self, _isolated_home, monkeypatch):
        monkeypatch.setattr(beacon, "is_ci", lambda: True)
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert not ok and "CI" in reason

    def test_non_default_home_suppressed(self, _isolated_home, monkeypatch):
        monkeypatch.setattr(beacon, "is_default_home", lambda: False)
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert not ok and "KIROCREW_HOME" in reason


class TestFirstEgressPrivacyGate:
    """The first heartbeat waits until the opt-out has actually been offered.

    The gateway starts the beacon thread at boot, long before the dashboard has
    rendered anything, so without this gate a fresh install pings before the user
    could possibly decline: an opt-out offered only after the fact.
    """

    def test_unacked_first_send_is_withheld(self, _isolated_home):
        ok, _reason, code = beacon.should_send(enabled=True, acked=False)
        assert not ok and code == "awaiting_privacy_ack"

    def test_acked_first_send_is_permitted(self, _isolated_home):
        assert beacon.should_send(enabled=True, acked=True).ok is True

    def test_no_http_request_is_made_while_unacked(self, _isolated_home, monkeypatch):
        """Asserted at the ``send`` boundary: the verdict is what gates egress."""
        calls: list[str] = []
        monkeypatch.setattr(
            beacon.urllib.request,
            "urlopen",
            lambda req, **_kw: calls.append(req.full_url),
        )
        assert (
            beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=False) is False
        )
        assert calls == []

    def test_established_install_still_sends_when_unacked(
        self, _isolated_home, monkeypatch
    ):
        """The gate is FIRST-egress only.

        An install that has sent before has already been past the disclosure (or
        predates the field entirely), so keying the daily heartbeat on the flag
        would silence it permanently rather than once.
        """
        (_isolated_home / beacon.STAMP_FILE).write_text("2020-01-01")
        assert beacon.is_first_send() is False
        assert beacon.should_send(enabled=True, acked=False).ok is True

    def test_unacked_verdict_does_not_mask_a_more_actionable_reason(
        self, _isolated_home, monkeypatch
    ):
        """A pod/CI host reports THAT, not the ack; the remedy differs."""
        monkeypatch.setattr(beacon, "is_ci", lambda: True)
        assert beacon.should_send(enabled=True, acked=False).code == "ci"

    def test_install_receipts_share_the_gate(self, _isolated_home):
        """A second egress route must not bypass the first-egress consent gate."""
        assert (
            install_receipt.should_send(enabled=True, official=True, acked=False).ok
            is False
        )
        assert (
            install_receipt.should_send(enabled=True, official=True, acked=True).ok
            is True
        )


class TestReasonCodes:
    """The panel renders ``reason_code``; ``reason`` is operator prose only.

    A raw backend sentence interpolated into the UI cannot be translated, and the
    dashboard ships in 10 languages.
    """

    def test_status_reports_a_known_code(self, _isolated_home):
        info = beacon.status(
            "https://e.invalid", enabled=True, app_version="1.2.3", acked=True
        )
        assert info["reason_code"] in beacon.REASONS

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"enabled": False, "acked": True}, "disabled"),
            ({"enabled": True, "acked": False}, "awaiting_privacy_ack"),
            ({"enabled": True, "acked": True}, "ready"),
        ],
    )
    def test_each_suppression_has_its_own_code(self, _isolated_home, kwargs, expected):
        assert beacon.should_send(**kwargs).code == expected

    def test_already_sent_today_has_a_code(self, _isolated_home):
        (_isolated_home / beacon.STAMP_FILE).write_text(beacon._today())
        assert beacon.should_send(enabled=True, acked=True).code == "already_sent_today"

    def test_no_endpoint_has_a_code(self, _isolated_home):
        info = beacon.status("", enabled=True, app_version="1.2.3", acked=True)
        assert info["reason_code"] == "no_endpoint"


class TestEnvOptOutProbe:
    """``is_env_opted_out`` backs the dashboard toggle's disabled state.

    The privacy panel must distinguish "off because the stored flag is false"
    (a toggle can flip it) from "off because the environment pins it" (a config
    write would be accepted and then have no effect).
    """

    def test_false_when_unset(self, _isolated_home):
        assert beacon.is_env_opted_out() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_true_for_each_truthy_spelling(self, _isolated_home, monkeypatch, value):
        monkeypatch.setenv(beacon.DISABLE_ENV, value)
        assert beacon.is_env_opted_out() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_false_for_non_truthy(self, _isolated_home, monkeypatch, value):
        monkeypatch.setenv(beacon.DISABLE_ENV, value)
        assert beacon.is_env_opted_out() is False

    def test_agrees_with_should_send(self, _isolated_home, monkeypatch):
        """The probe and the real suppression rule must never disagree."""
        monkeypatch.setenv(beacon.DISABLE_ENV, "1")
        ok, _reason, _code = beacon.should_send(enabled=True, acked=True)
        assert beacon.is_env_opted_out() is True and not ok


class TestDefaultHomeDetection:
    """Exercises the REAL is_default_home (the suppression fixture stubs it)."""

    @pytest.fixture(autouse=True)
    def _unstub(self, monkeypatch):
        monkeypatch.setattr(beacon, "is_default_home", _REAL_IS_DEFAULT_HOME)

    def test_dev_home_is_not_default(self, monkeypatch, tmp_path):
        """is_default_home must NOT compare against config_dir().

        config_dir() honors KIROCREW_HOME, so comparing the two would always
        match and the dev-home/pod suppression would never fire. This test
        failed against exactly that bug during development.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "dev-home"))
        assert beacon.is_default_home() is False

    def test_unset_home_is_default(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        assert beacon.is_default_home() is True

    def test_real_home_spelled_explicitly_is_default(self, monkeypatch):
        from pathlib import Path

        from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME

        real = Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF
        monkeypatch.setenv("KIROCREW_HOME", str(real))
        assert beacon.is_default_home() is True


class TestThrottle:
    def test_second_send_same_day_suppressed(self, _isolated_home, monkeypatch):
        calls = []
        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen(calls))
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True, acked=True) is True
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True, acked=True) is False
        assert len(calls) == 1, "at most one request per day"

    def test_stamp_does_not_follow_a_symlink(self, _isolated_home, monkeypatch):
        """A symlink planted at the stamp path must not have its target clobbered.

        Regression test: `path.write_text` FOLLOWS a symlink, so a link at
        beacon_last_sent would have its TARGET truncated and overwritten with
        today's date on the first successful beacon. atomic_write renames over the
        path, replacing the link itself.
        """
        victim = _isolated_home / "important.txt"
        victim.write_text("USER DATA")
        (_isolated_home / beacon.STAMP_FILE).symlink_to("important.txt")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen())
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True) is True

        assert victim.read_text() == "USER DATA", "symlink target was clobbered"
        assert not (_isolated_home / beacon.STAMP_FILE).is_symlink()
        assert beacon.already_sent_today() is True

    def test_failed_send_is_not_stamped(self, _isolated_home, monkeypatch):
        def boom(*_a, **_k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True, acked=True) is False
        assert not beacon.already_sent_today(), "a failure must retry later"


class TestUrlAndTransport:
    def test_id_in_path_fields_in_query(self, _isolated_home):
        url = beacon.beacon_url("https://e.invalid", beacon.payload("1.2.3"))
        head, _, query = url.partition("?")
        assert head.startswith(f"https://e.invalid/b/{beacon.BEACON_SCHEMA}/")
        assert "id=" not in query, "id belongs in the path (clean dedup key)"
        for key in ("v=", "py=", "dist=", "first_seen="):
            assert key in query

    def test_non_https_endpoint_rejected(self, _isolated_home):
        with pytest.raises(ValueError, match="https"):
            beacon.beacon_url("http://e.invalid", beacon.payload("1.2.3"))

    def test_malformed_id_rejected(self, _isolated_home):
        with pytest.raises(ValueError, match="malformed"):
            beacon.beacon_url("https://e.invalid", {"id": "short", "v": "1"})

    def test_empty_endpoint_never_sends(self, _isolated_home, monkeypatch):
        calls = []
        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen(calls))
        assert beacon.send("", "1.2.3", enabled=True, acked=True) is False
        assert calls == []

    def test_send_never_raises_on_any_error(self, _isolated_home, monkeypatch):
        for exc in (
            urllib.error.URLError("x"),
            OSError("y"),
            TimeoutError("z"),
            # NOT an OSError/ValueError subclass — needs naming explicitly.
            http.client.InvalidURL("bad host"),
            http.client.HTTPException("protocol error"),
        ):

            def boom(*_a, _e=exc, **_k):
                raise _e

            monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
            assert beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True) is False

    def test_unwritable_data_home_is_silent(self, _isolated_home, monkeypatch):
        """An unwritable data home must not propagate out of send()/status().

        Regression test: should_send() and payload() probe the filesystem, and
        they used to run OUTSIDE send()'s try, so a PermissionError from
        config_dir() escaped into the gateway's daemon thread (traceback on every
        boot) and made `kirocrew telemetry status` crash — while the module
        documents an in-memory fallback for exactly this case.
        """

        def denied(*_a, **_k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(beacon, "config_dir", denied)
        # already_sent_today() swallows OSError on its own, so drive the probe
        # that does NOT: the stamp/id lookups reached via payload() + status().
        monkeypatch.setattr(beacon, "already_sent_today", denied)

        assert beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True) is False
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        assert info["would_send"] is False
        assert "could not read the data home" in str(info["reason"])
        # Still renderable — a diagnostic must work when things are broken.
        assert beacon.DISABLE_ENV in beacon.format_status(info)

    def test_no_passwd_entry_is_silent(self, _isolated_home, monkeypatch):
        """Path.home() raises RuntimeError (not OSError) when the UID has no
        passwd entry — normal in a container. It must not escape either."""
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(beacon, "is_default_home", lambda: _REAL_IS_DEFAULT_HOME())

        def no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(beacon.Path, "home", staticmethod(no_home))
        monkeypatch.setattr(beacon, "config_dir", no_home)
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True) is False
        assert beacon.is_first_send() is True  # unreadable state → treat as first

    def test_malformed_https_endpoint_is_silent(self, _isolated_home):
        """A host with a space passes the https:// check but breaks urlopen.

        Regression test: http.client.InvalidURL is not an OSError or ValueError,
        so it used to escape send() into the gateway's detached daemon thread,
        where threading.excepthook printed a traceback on every boot — violating
        this function's documented silent-on-failure contract. Drives the REAL
        urlopen (no stub), because the bug was in the except tuple itself.
        """
        assert beacon.send("https://exa mple.invalid", "1.2.3", enabled=True, acked=True) is False


class TestFailOpen:
    """Telemetry must NEVER block, delay, or break a user action.

    This is the load-bearing property of the whole feature: a beacon that can
    fail a turn, delay a boot, or surface an error is worse than no beacon. Each
    test here drives a real failure mode through the real ``send()``.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            urllib.error.URLError(ConnectionRefusedError(61, "refused")),
            urllib.error.URLError(ssl.SSLError("handshake failed")),
            urllib.error.HTTPError("u", 500, "server error", {}, None),
            urllib.error.HTTPError("u", 403, "forbidden", {}, None),
            TimeoutError("timed out"),
            http.client.BadStatusLine("\x16\x03\x01"),  # captive portal / TLS bytes
            http.client.InvalidURL("space in host"),
            OSError(101, "Network unreachable"),
            OSError(28, "No space left on device"),
        ],
        ids=[
            "dns",
            "refused",
            "tls",
            "http500",
            "http403",
            "timeout",
            "captive-portal",
            "bad-url",
            "unreachable",
            "disk-full",
        ],
    )
    def test_every_transport_failure_returns_false_silently(self, _isolated_home, monkeypatch, exc):
        def boom(*_a, **_k):
            raise exc

        monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True) is False

    def test_a_hanging_beacon_does_not_delay_the_caller(self, _isolated_home, monkeypatch):
        """The gateway starts the beacon on a thread and never joins it.

        Pins the boot-path contract: even a beacon that hangs far past its own
        timeout costs the caller only the thread spawn.

        The hang is RELEASED at the end rather than left running. ``beacon.send``
        resolves state through the module-global ``config_dir``, which
        ``_isolated_home`` repoints per test -- so a thread still inside ``send``
        when this test ends goes on to mint ``beacon_install_id`` in whichever
        LATER test's home is installed by then, and
        ``TestStatusOutput.test_status_does_not_create_id`` then fails for
        something this test did. Reproduced by running this file followed by any
        second file; a uuid4 stack trace showed the id being minted on this thread.
        """
        released = threading.Event()

        def hang(*_a, **_k):
            # Indistinguishable from a 30s hang at the assertion below -- still
            # unfinished when it runs -- but releasable before teardown.
            released.wait(30)
            raise OSError("released")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", hang)
        start = time.monotonic()
        thread = threading.Thread(
            target=beacon.send,
            args=("https://e.invalid", "1.2.3"),
            kwargs={"enabled": True, "acked": True},
            daemon=True,
        )
        thread.start()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"spawning the beacon cost {elapsed:.2f}s"
        assert thread.daemon, "must not pin interpreter exit"
        released.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "the beacon thread outlived its test"

    def test_gateway_wiring_is_detached_and_daemon(self):
        """The gateway must never await the beacon.

        Guards against a refactor to ``await asyncio.to_thread(beacon.send, ...)``,
        which would silently reintroduce up to HTTP_TIMEOUT_SECS of boot delay.
        """
        import inspect

        from kiro_crew.slack import gateway

        src = inspect.getsource(gateway.run_gateway)
        assert "beacon.send" in src
        assert "daemon=True" in src
        assert "await asyncio.to_thread(\n                beacon.send" not in src
        assert "await beacon.send" not in src


class TestStatusOutput:
    def test_status_does_not_create_id(self, _isolated_home):
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        assert info["install_id"] == "(not yet generated)"
        assert not (_isolated_home / beacon.INSTALL_ID_FILE).exists()

    def test_empty_endpoint_reports_would_not_send(self, _isolated_home):
        """status() must agree with send(), which returns early on no endpoint.

        Reachable whenever __post_init__ clears a non-https endpoint — precisely
        when an operator runs `telemetry status` to find out why nothing is sent.
        """
        info = beacon.status("", enabled=True, app_version="1.2.3", acked=True)
        assert info["would_send"] is False
        assert "endpoint" in str(info["reason"])

    def test_formatted_status_discloses_optout_and_exclusions(self, _isolated_home):
        text = beacon.format_status(
            beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        )
        expected_optout = f"""  To opt out, choose one:

    1. Kiro Crew CLI (recommended)
       kirocrew telemetry disable

    2. Environment variable (choose your shell)
       macOS / Linux
         export {beacon.DISABLE_ENV}=1
       Windows PowerShell
         $env:{beacon.DISABLE_ENV} = '1'
       Windows Command Prompt
         set {beacon.DISABLE_ENV}=1

    3. Configuration file
       Set telemetry.beacon_enabled to false"""
        assert text.endswith(expected_optout)
        for claim in ("prompts", "credentials", "hostname", "IP address"):
            assert claim in text


class TestTelemetryCliWrite:
    """`telemetry disable/enable` rewrites the user's WHOLE config.json."""

    def _args(self, action):
        import argparse

        return argparse.Namespace(telemetry_action=action)

    def test_toggle_preserves_unrelated_config(self, _isolated_home, monkeypatch):
        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"command": "kirocrew"}, "timezone": "UTC"}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
        _telemetry(self._args("disable"))
        data = json.loads(cfg.read_text())
        assert data["telemetry"]["beacon_enabled"] is False
        # The user's own values must survive. (load() also performs a migration
        # write-back that fills in defaults for other keys — pre-existing
        # behavior, so assert the values we set, not the exact section shape.)
        assert data["slack"]["command"] == "kirocrew", "must not drop other settings"
        assert data["timezone"] == "UTC"

    @pytest.mark.parametrize(
        "raw", ['[{"important": "data"}]', '"a string"', "42"], ids=["array", "str", "num"]
    )
    def test_non_object_config_is_never_overwritten(self, _isolated_home, monkeypatch, raw):
        """A config.json that is valid JSON but not an object must not be replaced.

        Regression test: the toggle used to coerce non-dict data to ``{}``, then
        write — silently destroying the file's contents AND printing success. A
        privacy toggle must never be a data-loss path.
        """
        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(raw)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        with pytest.raises(SystemExit) as exc:
            _telemetry(self._args("disable"))
        assert exc.value.code == 1
        assert cfg.read_text() == raw, "the original file must be untouched"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX permission bits. Windows has no mode bits — it enforces "
        "owner-only with a DACL, covered by test_lockdown_is_enforced_cross_platform",
    )
    def test_preserves_existing_config_permissions(self, _isolated_home, monkeypatch):
        """A telemetry toggle must not widen who can read config.json.

        Regression test: atomic_write creates a NEW file and renames it over the
        old one, so without an explicit mode an operator's tightened 0600 became
        the umask default (0644 on a typical host) — and config.json can hold
        inline credentials, so a privacy toggle would have leaked them to every
        other local user.
        """
        import os
        import stat as _stat

        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"bot_token": "xoxb-secret"}}))
        os.chmod(cfg, 0o600)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        _telemetry(self._args("disable"))

        mode = _stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600, f"mode widened to {oct(mode)}"
        assert not mode & 0o077, "group/other must not gain access"

    def test_lockdown_is_enforced_cross_platform(self, _isolated_home, monkeypatch):
        """atomic_write's `mode` is POSIX-only, so the lockdown must be explicit.

        Regression test: `mode=` routes through fchmod_safe, a documented NO-OP on
        Windows, so the replacement file would inherit the directory ACL — and a
        permissive data home would expose a config.json holding inline
        credentials. restrict_to_owner must be called for the secret case.
        """
        import os

        from kiro_crew import cli_commands

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"bot_token": "xoxb-secret"}}))
        os.chmod(cfg, 0o600)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        calls: list[str] = []
        real = cli_commands.platform_compat.restrict_to_owner
        monkeypatch.setattr(
            cli_commands.platform_compat,
            "restrict_to_owner",
            lambda p: (calls.append(str(p)), real(p))[1],
        )
        _telemetry = cli_commands._telemetry
        _telemetry(self._args("disable"))

        assert str(cfg) in calls, "owner-only lockdown must be applied explicitly"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX permission bits; see test_lockdown_is_enforced_cross_platform",
    )
    def test_new_config_is_created_owner_only(self, _isolated_home, monkeypatch):
        """A config.json this command creates must start owner-only."""
        import stat as _stat

        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        assert not cfg.exists()
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        _telemetry(self._args("disable"))

        assert not _stat.S_IMODE(cfg.stat().st_mode) & 0o077

    def test_uses_atomic_write_not_write_text(self, _isolated_home, monkeypatch):
        """The toggle must route through atomic_write, never path.write_text.

        Regression test: it used to call ``path.write_text``, which truncates in
        place — a disk-full or interrupted write mid-rewrite of the user's WHOLE
        config.json would leave a partial file and every later load would
        silently discard their configuration. ``atomic_write`` writes a temp file
        and renames, so a failure leaves the original untouched.

        Asserted at the call site rather than by simulating a failed write,
        because ``KiroCrewConfig.load()`` performs its own migration write-back
        that rewrites config.json independently of this code path.
        """
        from kiro_crew import cli_commands

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"timezone": "UTC"}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        calls: list[dict] = []

        def spy(path, content, **kwargs):
            calls.append({"path": str(path), **kwargs})
            from kiro_crew.atomic_write import atomic_write as real

            real(path, content, **kwargs)

        monkeypatch.setattr(cli_commands, "atomic_write", spy)
        cli_commands._telemetry(self._args("disable"))

        assert calls, "toggle must write through atomic_write"
        assert calls[0]["path"] == str(cfg)
        assert calls[0].get("fsync") is True, "rename must be durable"
        assert json.loads(cfg.read_text())["telemetry"]["beacon_enabled"] is False

    @pytest.mark.parametrize("section", ["telemetry", "dashboard"])
    @pytest.mark.parametrize("value", [[], "on", 3, True])
    def test_refuses_rather_than_replacing_a_non_object_section(
        self, _isolated_home, monkeypatch, section, value
    ):
        """A present-but-wrong-type section is a refusal, never a silent replace.

        Coercing it to ``{}`` would discard whatever the user had under that key
        and then print success. The toggle writes BOTH sections, so each needs the
        same guard the whole-file check already applies.

        ``KiroCrewConfig.load`` is stubbed out because it runs FIRST in
        ``_telemetry`` and its own migration write-back already replaces a
        malformed section with defaults, so the guard would never see the bad
        value through a live load. Stubbing it reproduces the case the guard
        actually covers: the write-back did not persist (a read-only data home),
        leaving the malformed value on disk at read time.
        """
        from kiro_crew import cli_commands

        cfg = _isolated_home / "config.json"
        original = json.dumps({"timezone": "UTC", section: value})
        cfg.write_text(original)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
        monkeypatch.setattr(cli_commands.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock()))

        with pytest.raises(SystemExit) as excinfo:
            cli_commands._telemetry(self._args("disable"))

        assert excinfo.value.code == 1
        assert cfg.read_text() == original, "the user's file must be untouched"

    def test_absent_sections_are_created(self, _isolated_home, monkeypatch):
        """Absent is not malformed: create both sections and record the choice."""
        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"timezone": "UTC"}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        _telemetry(self._args("disable"))

        written = json.loads(cfg.read_text())
        assert written["telemetry"]["beacon_enabled"] is False
        # An explicit CLI choice IS the informed decision, so it releases the
        # first-egress gate even though no dashboard screen was ever shown.
        assert written["dashboard"]["privacy_acked"] is True
        assert written["timezone"] == "UTC", "unrelated keys must survive"


class TestSnapshotAndPortabilityRegistration:
    """The install id must never ride an export/snapshot to another machine.

    Enforced by NON-SELECTION, not by a basename filter. An earlier revision put
    the beacon filenames in ``EXPORT_EXCLUDE`` / ``NEVER_SNAPSHOT_FILES``, but
    both sets are matched by BASENAME over the workspace/, plan_memory/ and
    skills/ trees — so they would have silently dropped any USER file sharing the
    name, while protecting nothing (the root paths are never selected anyway).
    """

    def test_beacon_names_are_not_basename_filtered(self):
        from kiro_crew.portability import EXPORT_EXCLUDE
        from kiro_crew.snapshot import NEVER_SNAPSHOT_FILES

        for name in (beacon.INSTALL_ID_FILE, beacon.STAMP_FILE):
            assert name not in EXPORT_EXCLUDE, (
                f"{name} in EXPORT_EXCLUDE would drop a user's workspace file "
                "with the same basename"
            )
            assert name not in NEVER_SNAPSHOT_FILES, (
                f"{name} in NEVER_SNAPSHOT_FILES would drop a user's workspace "
                "file with the same basename"
            )

    def test_root_export_allowlist_excludes_beacon_state(self):
        """Root-level export copies a fixed allowlist; beacon files aren't on it."""
        import inspect

        from kiro_crew import portability

        src = inspect.getsource(portability.create_export_zip)
        assert beacon.INSTALL_ID_FILE not in src
        assert beacon.STAMP_FILE not in src

    def test_snapshot_components_never_name_beacon_state(self):
        from kiro_crew.snapshot import CORE_FILES

        listed = {f for files in CORE_FILES.values() for f in files}
        assert beacon.INSTALL_ID_FILE not in listed
        assert beacon.STAMP_FILE not in listed

    def test_workspace_file_with_beacon_name_survives_export(self):
        """A user file merely SHARING the name must not be filtered out."""
        from pathlib import PurePosixPath

        from kiro_crew.portability import _is_excluded

        for rel in (
            f"workspace/proj/{beacon.INSTALL_ID_FILE}",
            f"plan_memory/{beacon.STAMP_FILE}",
        ):
            assert not _is_excluded(PurePosixPath(rel)), rel


class TestConfigDefaults:
    def test_beacon_on_by_default_with_https_endpoint(self):
        from kiro_crew.config.loader import TelemetryConfig

        cfg = TelemetryConfig()
        assert cfg.beacon_enabled is True
        assert cfg.beacon_endpoint.startswith("https://")

    def test_a_default_install_actually_sends(self, _isolated_home):
        """DEFAULT-ON, end to end — the whole suppression chain, not just the flag.

        The stored flag being True is necessary but not sufficient: this change
        added a governance suppression ABOVE the flag in ``should_send``, so a
        wrong ``capability_default`` (or a probe that failed closed) would silence
        every install in the field while ``beacon_enabled`` still read True. That
        failure is invisible in a flag assertion and would look like a collapse in
        Daily Active Instances, so assert the actual verdict.

        The fixture already neutralizes the CI and data-home suppressions (both
        fire in the test environment for reasons unrelated to defaults).
        """
        from kiro_crew.config.loader import TelemetryConfig

        cfg = TelemetryConfig()
        ok, reason, _code = beacon.should_send(enabled=cfg.beacon_enabled, acked=True)
        assert ok is True, f"a default install must send, got: {reason}"
        assert reason == "ready"

    def test_ungoverned_default_is_not_pinned_off(self, _isolated_home):
        """``capabilities.telemetry`` has capability_default=True.

        A standalone install has no ceiling at all, and a fleet policy that
        governs OTHER scopes but says nothing about telemetry must also leave the
        documented default-on behavior intact — that is what the capability
        default buys, and getting it backwards would turn any governed fleet into
        a silent opt-out.
        """
        assert beacon.is_governance_pinned_off() is False

    def test_non_https_endpoint_is_cleared(self):
        from kiro_crew.config.loader import TelemetryConfig

        assert TelemetryConfig(beacon_endpoint="http://insecure.invalid").beacon_endpoint == ""

    def test_unusable_https_endpoints_are_cleared(self):
        """A startswith('https://') test is not enough.

        A host containing whitespace passes that check and also passes
        beacon_url's scheme check, then fails only inside urlopen — deep in the
        beacon thread. Reject it at config load instead.
        """
        from kiro_crew.config.loader import TelemetryConfig

        for bad in (
            "https://exa mple.invalid",  # whitespace in host
            "https://",  # no netloc
            "https:///path-only",  # empty netloc
        ):
            assert TelemetryConfig(beacon_endpoint=bad).beacon_endpoint == "", bad

    def test_local_metrics_switch_stays_off(self):
        """The beacon must not ride the local-only telemetry.enabled switch."""
        from kiro_crew.config.loader import TelemetryConfig

        assert TelemetryConfig().enabled is False


def _fake_urlopen(calls: list | None = None):
    """Return a urlopen stand-in recording called URLs, usable as a CM."""

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _open(req, *_a, **_k):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        return _Resp()

    return _open


class TestGovernancePin:
    """``capabilities.telemetry`` is the enterprise opt-out the app cannot undo.

    The Settings toggle, the CLI and the env var are all operator controls (and
    the agent can reach the first two). A managed fleet needs one the running app
    cannot lift, so the ceiling is read from the trust-root ``security_policy.json``
    and enforced here at the send gate. These tests drive
    ``governance_permits`` through the real evaluator rather than stubbing the
    probe, so a change to the scope catalog or the composition algebra fails here.
    """

    def _install_policy(self, monkeypatch, doc):
        """Install ``doc`` as the boot-frozen ceiling for the duration of a test."""
        from kiro_crew.platform import context as pc
        from kiro_crew.platform.governance import parse_policy

        ceiling = parse_policy(doc) if doc is not None else None

        class _Ctx:
            governance = ceiling

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())

    def test_ungoverned_host_is_not_pinned(self, _isolated_home, monkeypatch):
        self._install_policy(monkeypatch, None)
        assert beacon.is_governance_pinned_off() is False
        assert beacon.should_send(enabled=True, acked=True)[0] is True

    def test_policy_silent_about_telemetry_still_permits(self, _isolated_home, monkeypatch):
        """capability_default=True — a ceiling that governs other scopes must not
        incidentally disable a documented default-on behavior."""
        self._install_policy(
            monkeypatch,
            {"version": 1, "boot": {"fail_closed": True}, "apps": {"mode": "deny", "deny": ["x"]}},
        )
        assert beacon.is_governance_pinned_off() is False
        assert beacon.should_send(enabled=True, acked=True)[0] is True

    def test_policy_pin_blocks_the_send(self, _isolated_home, monkeypatch):
        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        assert beacon.is_governance_pinned_off() is True
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert ok is False
        assert "governance" in reason and "capabilities.telemetry" in reason

    def test_pin_beats_the_config_flag_and_reaches_send(self, _isolated_home, monkeypatch):
        """The whole point: enabled=True in config must not produce a request.

        Asserts at the ``send`` boundary, not just ``should_send``, because the
        gate only matters if the HTTP call never happens.
        """
        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        calls: list[str] = []

        def _explode(req, timeout=None):  # pragma: no cover - must never run
            calls.append(getattr(req, "full_url", "?"))
            raise AssertionError("a pinned host must not open a connection")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", _explode)
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True, acked=True) is False
        assert calls == []

    def test_pin_is_ranked_above_the_config_flag(self, _isolated_home, monkeypatch):
        """With BOTH the pin and a false flag, the reason names the policy.

        An admin debugging a managed host needs to see why they cannot change it,
        not the local value that is now irrelevant.
        """
        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        _ok, reason, _code = beacon.should_send(enabled=False, acked=True)
        assert "governance" in reason
        assert "beacon_enabled" not in reason

    def test_env_var_still_outranks_the_pin(self, _isolated_home, monkeypatch):
        """The env var is checked first and needs no policy resolution at all.

        An opted-out host must not even resolve the ceiling (which touches disk).
        """
        monkeypatch.setenv(beacon.DISABLE_ENV, "1")
        monkeypatch.setattr(
            beacon,
            "is_governance_pinned_off",
            lambda: (_ for _ in ()).throw(AssertionError("must not be consulted")),
        )
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert ok is False
        assert beacon.DISABLE_ENV in reason

    def test_a_transient_profile_race_is_not_an_admin_pin(self, _isolated_home, monkeypatch):
        """A deny-all PROFILE on an UNGOVERNED host must not read as a policy pin.

        ``resolve_active_scope`` hands back a synthetic ``_deny_all_unloaded:…``
        profile when the profile store is unprimed and another thread holds its
        non-blocking reload lock. There is no policy on such a host, so reporting
        a pin would make the CLI, the PATCH 403 and ``governanceOverrideNote`` all
        blame an administrator who does not exist.

        This is the failure the fail-open ``except`` CANNOT catch: it arrives as an
        ordinary permitted=False ``Decision``, not an exception — which is why the
        probe keys on ``layer``, not on ``permitted`` alone.
        """
        from kiro_crew.platform import governance_profiles as gp

        monkeypatch.setattr(
            gp, "resolve_active_scope", lambda *a, **k: gp.deny_all_profile("_deny_all_unloaded:x")
        )
        from kiro_crew.platform import context as pc

        class _Ctx:
            governance = None

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())
        assert beacon.is_governance_pinned_off() is False
        # ...and the beacon still sends, since nothing legitimately suppresses it.
        assert beacon.should_send(enabled=True, acked=True)[0] is True

    def test_a_real_policy_pin_is_still_detected(self, _isolated_home, monkeypatch):
        """The layer check must not weaken the control it guards.

        Paired with the test above: narrowing to ``layer == "policy"`` is only
        correct if a genuine Level-1 pin still reports ``policy``. ``resolve``
        wraps the CapabilityGate's own ``layer="both"`` into a policy-layer
        Decision, so this asserts the wrapping rather than assuming it.
        """
        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        from kiro_crew.platform.governance import parse_policy, resolve

        decision = resolve(
            parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {"telemetry": {"enabled": False}},
                }
            ),
            None,
            "capabilities.telemetry",
            "",
        )
        assert decision.permitted is False
        assert decision.layer == "policy", "the probe's layer check depends on this"
        assert beacon.is_governance_pinned_off() is True

    def test_evaluation_error_fails_CLOSED(self, _isolated_home, monkeypatch):
        """An unevaluable ceiling must NOT permit the egress.

        Caught by the CI GPT reviewer, which was right and reversed an earlier
        fail-open revision of this probe. The two dispositions look symmetric and
        are not: a wrong DENY loses one heartbeat, but a wrong PERMIT egresses from
        a fleet that explicitly forbade egress — breaking the exact promise the
        administrator was given. That puts this with ``capabilities.publish`` /
        ``theme_install`` (fail_closed=True), and ``fail_closed`` additionally makes
        ``governance_permits`` audit the degrade as a critical SEL event.

        Asserted at the ``should_send`` boundary too, since the probe only matters
        if it actually suppresses.
        """
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.platform.governance import parse_policy

        # A governed fleet whose profile resolution breaks mid-evaluation.
        monkeypatch.setattr(
            gp,
            "resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        from kiro_crew.platform import context as pc

        class _Ctx:
            governance = parse_policy({"version": 1, "boot": {"fail_closed": True}})

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())
        monkeypatch.setattr(beacon, "already_sent_today", lambda: False)
        assert beacon.is_governance_pinned_off() is True
        ok, reason, _code = beacon.should_send(enabled=True, acked=True)
        assert ok is False
        assert "governance" in reason

    def test_an_unexpected_probe_error_also_fails_closed(self, _isolated_home, monkeypatch):
        """The probe's own ``except`` takes the same disposition as a degrade.

        ``governance_permits`` converts its internal errors into a Decision, so this
        handler is only reached when something outside that contract raises (e.g.
        ``vet_and_audit``'s evaluation, or a ``PlatformCompositionError``, which is
        documented to propagate). That is still an unevaluable ceiling, so it must
        not permit the egress.
        """
        from kiro_crew.platform import governance_profiles as gp

        monkeypatch.setattr(
            beacon,
            "governance_permits",
            lambda *a, **k: (_ for _ in ()).throw(gp.PlatformCompositionError("boom")),
        )
        assert beacon.is_governance_pinned_off() is True

    def test_the_enforcement_decision_is_SEL_audited(self, _isolated_home, monkeypatch):
        """A suppressed heartbeat must leave a forensic record.

        Raised by the CI GPT reviewer: the decision that stops an egress on a
        governed fleet is exactly the one an operator needs in the audit trail.
        Routed through ``vet_and_audit`` — the existing audited seam — rather than a
        hand-rolled SEL write, so this chokepoint's record shape cannot drift from
        the messaging chokepoints that already use it.
        """
        from unittest.mock import MagicMock

        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        monkeypatch.setattr(beacon, "already_sent_today", lambda: False)
        fake = MagicMock()
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: fake)

        beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True)

        calls = fake.log_governance_decision.call_args_list
        assert len(calls) == 1, "the enforcement decision must be audited exactly once"
        kwargs = calls[0][1]
        assert kwargs["scope"] == "capabilities.telemetry"
        assert kwargs["outcome"] == "denied"
        assert kwargs["tool_name"] == "beacon_send"

    def test_every_enforcement_site_audits_with_its_own_tool_name(
        self, _isolated_home, monkeypatch
    ):
        """All FOUR enforcement sites audit, each identifiably.

        The CI reviewer caught two unaudited CLI refusals after the send gate was
        wired; there were in fact three (the dashboard PATCH too). A per-site
        ``tool_name`` is what makes the trail answer "which control refused",
        rather than just "something did".
        """
        import argparse
        from unittest.mock import MagicMock

        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        monkeypatch.setattr(beacon, "already_sent_today", lambda: False)
        import kiro_crew.sel as sel_mod

        def _tools_for(action):
            fake = MagicMock()
            monkeypatch.setattr(sel_mod, "sel", lambda: fake)
            try:
                action()
            except SystemExit:
                pass  # the CLI refusals exit(1) by design
            return [c[1].get("tool_name") for c in fake.log_governance_decision.call_args_list]

        from kiro_crew.cli_commands import _telemetry
        from kiro_crew.cli_config import _config_cmd
        from kiro_crew.dashboard.handlers.core import _beacon_governance_pinned_off

        assert _tools_for(lambda: beacon.send("https://e.invalid", "1.2.3", enabled=True, acked=True)) == [
            "beacon_send"
        ]
        assert _tools_for(lambda: _telemetry(argparse.Namespace(telemetry_action="enable"))) == [
            "telemetry_enable_cli"
        ]
        assert _tools_for(
            lambda: _config_cmd(
                argparse.Namespace(
                    config_action="set",
                    key="telemetry.beacon_enabled",
                    value="true",
                    local=False,
                    file=None,
                )
            )
        ) == ["config_set_cli"]
        assert _tools_for(_beacon_governance_pinned_off) == ["config_patch_dashboard"]

    def test_the_read_only_probe_is_NOT_audited(self, _isolated_home, monkeypatch):
        """...but merely INSPECTING status must not write to the SEL trail.

        ``status`` backs ``GET /api/telemetry/beacon``, which the Privacy panel
        refetches, so auditing it would append HMAC-chained rows per inspection at a
        multiple of the one decision per boot that actually governs anything. Same
        disposition the channels gate uses for its hot-path default-permit.
        """
        from unittest.mock import MagicMock

        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        fake = MagicMock()
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: fake)

        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)

        assert info["governance_pinned_off"] is True, "still reports the pin"
        assert fake.log_governance_decision.call_args_list == []

    def test_status_reports_the_pin_for_the_cli(self, _isolated_home, monkeypatch):
        self._install_policy(
            monkeypatch,
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"telemetry": {"enabled": False}},
            },
        )
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        assert info["governance_pinned_off"] is True
        rendered = beacon.format_status(info)
        assert "administrator" in rendered
        assert "capabilities.telemetry" in rendered

    def test_status_omits_the_pin_notice_when_ungoverned(self, _isolated_home, monkeypatch):
        self._install_policy(monkeypatch, None)
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3", acked=True)
        assert info["governance_pinned_off"] is False
        assert "administrator" not in beacon.format_status(info)


class TestGenericConfigSetterIsGated:
    """`kirocrew config set` is a FOURTH write path to telemetry.beacon_enabled.

    The dashboard PATCH and `telemetry enable` are the obvious two, but the
    generic setter reaches the same key — and `--local` writes config.local.json,
    which takes PRECEDENCE over the base file. Leaving it ungated would make it
    the one remaining way to store `true` on a pinned host, which is exactly the
    false-promise-on-a-privacy-control failure the 403 exists to prevent.
    """

    def _args(self, key, value, local=False):
        import argparse

        return argparse.Namespace(config_action="set", key=key, value=value, local=local, file=None)

    def _pin(self, monkeypatch, pinned):
        from kiro_crew import beacon as beacon_mod

        # **kwargs, not a bare lambda: the enforcement call sites pass
        # ``audit_tool=`` so the decision is SEL-audited, and a fixed-arity stub
        # would fail with a TypeError that looks like a production bug.
        monkeypatch.setattr(beacon_mod, "is_governance_pinned_off", lambda **_kwargs: pinned)

    @pytest.mark.parametrize("local", [False, True])
    def test_enable_is_refused_under_a_pin(self, _isolated_home, monkeypatch, local):
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, True)
        with pytest.raises(SystemExit) as exc:
            _config_cmd(self._args("telemetry.beacon_enabled", "true", local=local))
        assert exc.value.code == 1
        # Nothing persisted, in EITHER file — the refusal precedes both writes.
        for name in ("config.json", "config.local.json"):
            path = _isolated_home / name
            if path.exists():
                assert "beacon_enabled" not in path.read_text(encoding="utf-8")

    def test_disable_is_still_allowed_under_a_pin(self, _isolated_home, monkeypatch):
        """Tightest-wins: a narrower local choice composes with the ceiling."""
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, True)
        _config_cmd(self._args("telemetry.beacon_enabled", "false"))
        data = json.loads((_isolated_home / "config.json").read_text(encoding="utf-8"))
        assert data["telemetry"]["beacon_enabled"] is False

    def test_unpinned_host_can_still_enable(self, _isolated_home, monkeypatch):
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, False)
        _config_cmd(self._args("telemetry.beacon_enabled", "true"))
        data = json.loads((_isolated_home / "config.json").read_text(encoding="utf-8"))
        assert data["telemetry"]["beacon_enabled"] is True
