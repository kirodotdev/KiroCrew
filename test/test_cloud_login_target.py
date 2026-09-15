"""The Kiro login target survives every managed launch path.

Every managed launch (setup cloud step, ``cloud launch``, dashboard Remote
Crew) signs the crew in as a NAMED identity: an IAM Identity Center user gets
their own organization's portal, never the Builder ID one, and a valid session
for the wrong identity reads as a mismatch, not as "signed in". These tests pin
the target at each boundary it crosses: the shared model, the login module's
identity-aware probe and pinned no-fallback, the durable launch job, the
engine's start AND resume, the owner-only HTTP boundary, and the CLI's
inherit/override resolution.

Placeholders only — no real start URL, account or device code appears here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.cloud import launch_engine as le
from kiro_crew.cloud import launch_job as lj
from kiro_crew.cloud import login, ssm
from kiro_crew.cloud.login_target import (
    ACCOUNT_TYPE_IDENTITY_CENTER,
    KiroLoginTarget,
    LoginTargetError,
    identity_matches_target,
    normalize_start_url,
    parse_whoami_output,
    target_from_whoami,
)

IDC = KiroLoginTarget(
    license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
)
IDC_WHOAMI = json.dumps(
    {
        "email": "user@example.com",
        "accountType": "IamIdentityCenter",
        "startUrl": "https://example.awsapps.com/start",
    }
)
BUILDER_WHOAMI = json.dumps({"email": "user@example.com", "accountType": "BuilderId"})


# ── shared model ─────────────────────────────────────────────────────────────


class TestLoginTargetValidation:
    def test_default_target_is_builder_id_and_backward_compatible(self):
        t = KiroLoginTarget.from_fields()
        assert t.is_default and not t.is_identity_center
        assert t.login_kwargs() == {"identity_provider": "", "license_": "", "idp_region": ""}
        assert KiroLoginTarget.from_dict(None) == t
        assert KiroLoginTarget.from_dict({}) == t

    def test_recovery_command_names_the_target(self):
        """The logout-then-login line a mismatch points at signs in AS THE
        TARGET: Identity Center flags for a pinned target, the bare command for
        the Builder ID default."""
        assert (
            KiroLoginTarget().recovery_command() == "kirocrew cloud logout && kirocrew cloud login"
        )
        assert IDC.recovery_command() == (
            "kirocrew cloud logout && kirocrew cloud login "
            f"--identity-provider {IDC.start_url} --license pro --idp-region us-east-1"
        )

    def test_pro_target_normalizes(self):
        t = KiroLoginTarget.from_fields(
            license="PRO", start_url="EXAMPLE.awsapps.com/start/", region="US-EAST-1"
        )
        assert t == IDC
        # A start URL alone implies pro.
        assert (
            KiroLoginTarget.from_fields(
                start_url="https://example.awsapps.com/start", region="us-east-1"
            )
            == IDC
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(license="pro"),  # pro without a start URL
            dict(
                license="enterprise",
                start_url="https://example.awsapps.com/start",
                region="us-east-1",
            ),
            dict(start_url="https://example.awsapps.com/start"),  # no region
            dict(region="us-east-1"),  # region without a URL
            dict(start_url="https://example.awsapps.com/start", region="useast1"),
            dict(start_url="http://example.awsapps.com/start", region="us-east-1"),
            dict(start_url="https://user:pw@example.awsapps.com/start", region="us-east-1"),
            dict(start_url="https://example.awsapps.com:8443/start", region="us-east-1"),
            dict(start_url="https://example.awsapps.com:abc/start", region="us-east-1"),
            dict(start_url="https://[example.awsapps.com/start", region="us-east-1"),
            dict(start_url="https://example.awsapps.com/start?x=1", region="us-east-1"),
            dict(start_url="https://example.awsapps.com/start#frag", region="us-east-1"),
            dict(start_url="https://localhost/start", region="us-east-1"),  # single label
            dict(start_url="https://example.awsapps.com/start; rm -rf /", region="us-east-1"),
            dict(start_url="https://example.awsapps.com/start$(id)", region="us-east-1"),
            dict(start_url="https://exa\x00mple.awsapps.com/start", region="us-east-1"),
        ],
    )
    def test_rejects_malformed(self, kwargs):
        with pytest.raises(LoginTargetError):
            KiroLoginTarget.from_fields(**kwargs)

    @pytest.mark.parametrize(
        "raw, canonical",
        [
            # Other partitions and custom domains are real Identity Center
            # portals; the host is not pinned to <org>.awsapps.com.
            (
                "https://example.awsapps-us-gov.com/start",
                "https://example.awsapps-us-gov.com/start",
            ),
            ("https://example.awsapps.cn/start", "https://example.awsapps.cn/start"),
            ("sso.example.com/start", "https://sso.example.com/start"),
            ("https://example.awsapps.com/start/", "https://example.awsapps.com/start"),
            ("https://example.awsapps.com", "https://example.awsapps.com/start"),
            ("https://example.awsapps.com/portal", "https://example.awsapps.com/portal"),
        ],
    )
    def test_accepts_any_safe_portal_host(self, raw, canonical):
        """Refuse what is unsafe, not what is unfamiliar: https, no userinfo /
        port / query / fragment / shell characters, a real hostname -- and
        otherwise the organization's own portal URL, whatever partition or
        domain it lives on."""
        assert normalize_start_url(raw) == canonical

    def test_round_trip_and_malformed_persisted_target_is_refused_not_defaulted(self):
        """A persisted target that NAMED an identity but fails validation is
        an error for the caller to handle, never a quiet Builder ID; a file with
        no target, or with every field empty, still loads as the default."""
        assert KiroLoginTarget.from_dict(IDC.to_dict()) == IDC
        with pytest.raises(LoginTargetError):
            KiroLoginTarget.from_dict({"license": "pro"})  # incomplete on disk
        with pytest.raises(LoginTargetError):
            KiroLoginTarget.from_dict({"start_url": "http://example.awsapps.com/start"})
        assert KiroLoginTarget.from_dict("nonsense") == KiroLoginTarget()  # type: ignore[arg-type]
        assert KiroLoginTarget.from_dict({"license": "", "start_url": "", "region": ""}) == (
            KiroLoginTarget()
        )

    def test_normalize_start_url_shape(self):
        assert (
            normalize_start_url("example.awsapps.com/start") == "https://example.awsapps.com/start"
        )
        with pytest.raises(LoginTargetError):
            normalize_start_url("")


class TestWhoamiParsing:
    def test_leading_json_only_and_bounded(self):
        raw = IDC_WHOAMI + "\nProfile:\narn:aws:codewhisperer:us-east-1:123456789012:profile/ABC\n"
        ident = parse_whoami_output(raw)
        assert ident == {
            "email": "user@example.com",
            "account_type": "IamIdentityCenter",
            "start_url": "https://example.awsapps.com/start",
        }
        assert parse_whoami_output("not json") == {}
        assert parse_whoami_output('{"email": 5}') == {}
        assert len(parse_whoami_output(json.dumps({"startUrl": "x" * 1000}))["start_url"]) == 200

    def test_target_from_whoami(self):
        assert target_from_whoami(parse_whoami_output(IDC_WHOAMI), region="us-east-1") == IDC
        # No region known: still an Identity Center target, region left for the caller.
        t = target_from_whoami(parse_whoami_output(IDC_WHOAMI))
        assert t is not None and t.is_identity_center and t.region == ""
        assert target_from_whoami(parse_whoami_output(BUILDER_WHOAMI)) == KiroLoginTarget()
        assert target_from_whoami({}) == KiroLoginTarget()

    @pytest.mark.parametrize(
        "start_url", ["", "http://example.awsapps.com/start", "https://bad host/start"]
    )
    def test_identity_center_without_a_readable_portal_is_unknown_not_builder_id(self, start_url):
        """whoami says Identity Center but names no usable portal: the machine is
        signed in to SOME organization, and which one is unknown. That is not
        the Builder ID default -- resolving it there would sign the crew in
        outside the organization -- so the helper answers None for the caller
        to refuse or ask."""
        identity = {"account_type": "IamIdentityCenter", "start_url": start_url}
        assert target_from_whoami(identity) is None
        assert target_from_whoami(identity, region="us-east-1") is None


class TestIdentityMatch:
    def test_identity_center_requires_same_portal(self):
        assert identity_matches_target(parse_whoami_output(IDC_WHOAMI), IDC)
        other = {
            "account_type": ACCOUNT_TYPE_IDENTITY_CENTER,
            "start_url": "https://other.awsapps.com/start",
        }
        assert not identity_matches_target(other, IDC)
        # A valid Builder ID session is NOT a match for an Identity Center target.
        assert not identity_matches_target(parse_whoami_output(BUILDER_WHOAMI), IDC)

    def test_default_target_accepts_builder_id_but_not_identity_center(self):
        assert identity_matches_target(parse_whoami_output(BUILDER_WHOAMI), KiroLoginTarget())
        assert not identity_matches_target(parse_whoami_output(IDC_WHOAMI), KiroLoginTarget())

    @pytest.mark.parametrize("account_type", ["SocialGoogle", "SocialGithub"])
    def test_default_target_accepts_the_social_family(self, account_type):
        """The default flow's own fallback is the social-provider callback, and
        kiro-cli reports the session it produces as ``Social<Provider>`` (the
        spelling the dashboard's account modal keys on). That session satisfies
        the default target -- otherwise the wait after a successful Google or
        GitHub sign-in would read its own result as a mismatch -- and never an
        Identity Center one."""
        identity = parse_whoami_output(
            json.dumps({"email": "user@example.com", "accountType": account_type})
        )
        assert identity_matches_target(identity, KiroLoginTarget())
        assert not identity_matches_target(identity, IDC)

    @pytest.mark.parametrize("account_type", ["ApiKey", "SomethingNew", ""])
    def test_default_target_is_an_allowlist_not_not_identity_center(self, account_type):
        """A reused instance can carry an ``ApiKey`` session (``KIRO_API_KEY`` in
        its environment) -- or a type this code has never seen. Neither is the
        Builder ID family, so a default-target launch must not adopt it as
        "already signed in" and run the crew under the wrong usage pool. The
        match is a closed allowlist; the wrong answer here fails open."""
        identity = {"account_type": account_type, "email": "someone@example.com"}
        assert not identity_matches_target(identity, KiroLoginTarget())
        assert not identity_matches_target(
            identity,
            KiroLoginTarget.from_fields(license="pro", start_url=IDC.start_url, region="us-east-1"),
        )


# ── login module: identity-aware probe, target threading, pinned no-fallback ──


def _ssm_script(monkeypatch, *, whoami_ok: bool, whoami_json: str, login_out: str = ""):
    """Answer each remote command by shape: auth check, whoami json, device login."""
    calls: list[str] = []

    def fake(instance_id, cmd, profile="", region="", **kw):
        calls.append(cmd)
        if "whoami --format json" in cmd:
            if not whoami_ok:
                return ssm.CommandResult("Failed", login._NOAUTH_SENTINEL, "", 1)
            return ssm.CommandResult("Success", whoami_json, "", 0)
        if '"$KIRO" whoami' in cmd:
            sentinel = login._TOKEN_PRESENT_SENTINEL if whoami_ok else login._NOAUTH_SENTINEL
            return ssm.CommandResult(
                "Success" if whoami_ok else "Failed", sentinel, "", 0 if whoami_ok else 1
            )
        if "login --use-device-flow" in cmd:
            return ssm.CommandResult("Success", login_out, "", 0)
        return ssm.CommandResult("Success", "", "", 0)

    monkeypatch.setattr(ssm, "run_command", fake)
    return calls


class TestLoginModule:
    def test_wrong_identity_session_is_not_logged_in_for_that_target(self, monkeypatch):
        _ssm_script(monkeypatch, whoami_ok=True, whoami_json=BUILDER_WHOAMI)
        assert login.is_logged_in("i-1", "p", "r") is True  # NO target: legacy "some session"
        assert login.is_logged_in("i-1", "p", "r", target=IDC) is False  # identity check: wrong one
        assert login.remote_identity_state("i-1", "p", "r", target=IDC) == "mismatch"

    def test_every_supplied_target_is_verified_including_builder_id(self, monkeypatch):
        """A reused instance carrying an Identity Center session must not be
        adopted by a launch that asked for Builder ID (wrong license and
        models). The default target is a supplied target; only a caller that
        passes NO target asks the legacy wildcard question."""
        _ssm_script(monkeypatch, whoami_ok=True, whoami_json=IDC_WHOAMI)
        assert login.is_logged_in("i-1", "p", "r") is True  # wildcard, no target supplied
        assert login.is_logged_in("i-1", "p", "r", target=KiroLoginTarget()) is False
        assert login.remote_identity_state("i-1", "p", "r", target=KiroLoginTarget()) == "mismatch"
        # And the matching family still passes for the default target.
        _ssm_script(monkeypatch, whoami_ok=True, whoami_json=BUILDER_WHOAMI)
        assert login.is_logged_in("i-1", "p", "r", target=KiroLoginTarget()) is True

    def test_matching_identity_short_circuits(self, monkeypatch):
        _ssm_script(monkeypatch, whoami_ok=True, whoami_json=IDC_WHOAMI)
        assert login.is_logged_in("i-1", "p", "r", target=IDC) is True
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        assert prompt.already_logged_in

    def test_social_session_satisfies_the_default_target(self, monkeypatch):
        """The session the default flow's own social-callback fallback produces
        reads as a match for the default target (the wait that follows a Google
        or GitHub sign-in succeeds on its own result) and as a mismatch for an
        Identity Center target, which never accepts another family."""
        social = json.dumps({"email": "user@example.com", "accountType": "SocialGoogle"})
        _ssm_script(monkeypatch, whoami_ok=True, whoami_json=social)
        assert login.remote_identity_state("i-1", "p", "r", target=KiroLoginTarget()) == "match"
        assert login.is_logged_in("i-1", "p", "r", target=KiroLoginTarget()) is True
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False)
        assert prompt.already_logged_in
        assert login.remote_identity_state("i-1", "p", "r", target=IDC) == "mismatch"

    def test_remote_identity_distinguishes_absent_and_unknown(self, monkeypatch):
        _ssm_script(monkeypatch, whoami_ok=False, whoami_json="")
        assert login.remote_identity_state("i-1", "p", "r", target=IDC) == "absent"
        monkeypatch.setattr(
            ssm, "run_command", lambda *a, **k: ssm.CommandResult("TimedOut", "", "", -1)
        )
        assert login.remote_identity_state("i-1", "p", "r", target=IDC) == "unknown"
        # send-command itself failing (throttled / denied) is the same unknown,
        # surfaced as the documented None rather than an escaping exception.
        from kiro_crew.cloud.aws import AWSError

        def throttled(*a, **k):
            raise AWSError("ThrottlingException: Rate exceeded", action="ssm:SendCommand")

        monkeypatch.setattr(ssm, "run_command", throttled)
        assert login.remote_identity("i-1", "p", "r") is None
        assert login.remote_identity_state("i-1", "p", "r", target=IDC) == "unknown"

    def test_target_flags_reach_the_remote_command_on_start_and_resume(self, monkeypatch):
        calls = _ssm_script(
            monkeypatch,
            whoami_ok=False,
            whoami_json="",
            login_out="Open https://device.example/activate and enter code ABCD-EFGH",
        )
        login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        login.resume_login_daemon("i-1", "p", "r", target=IDC)
        login_cmds = [c for c in calls if "login --use-device-flow" in c]
        assert len(login_cmds) == 2
        for cmd in login_cmds:
            assert "--identity-provider https://example.awsapps.com/start" in cmd
            assert "--license pro" in cmd
            assert "--region us-east-1" in cmd

    def test_default_target_emits_exactly_the_old_command(self, monkeypatch):
        calls = _ssm_script(monkeypatch, whoami_ok=False, whoami_json="", login_out="")
        monkeypatch.setattr(login, "_start_callback_login", lambda *a, **k: login.LoginPrompt())
        login.start_device_login("i-1", "p", "r", open_browser=False)
        cmd = next(c for c in calls if "login --use-device-flow" in c)
        assert "--identity-provider" not in cmd and "--license" not in cmd and "--region" not in cmd

    def test_already_logged_in_over_a_wrong_identity_session_is_a_mismatch_not_success(
        self, monkeypatch
    ):
        """kiro-cli ignores a login over a LIVE session and prints "already
        logged in" whatever that session's identity is. `start_device_login`
        must verify that session against the resolved target before reporting
        success -- for a Builder ID target on an Identity Center box and the
        reverse alike -- so no caller (CLI, wizard, launch engine) can record a
        wrong-identity sign-in as done."""
        # Box holds an Identity Center session; the launch asked for Builder ID.
        _ssm_script(
            monkeypatch, whoami_ok=True, whoami_json=IDC_WHOAMI, login_out="Already logged in."
        )
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False)
        assert not prompt.already_logged_in and not prompt.actionable
        assert "different Kiro identity" in prompt.error and "cloud logout" in prompt.error
        # Box holds a Builder ID session; the launch asked for Identity Center.
        _ssm_script(
            monkeypatch, whoami_ok=True, whoami_json=BUILDER_WHOAMI, login_out="Already logged in."
        )
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        assert not prompt.already_logged_in and "different Kiro identity" in prompt.error
        # Matching session: the short-circuit is genuine success.
        _ssm_script(
            monkeypatch, whoami_ok=True, whoami_json=IDC_WHOAMI, login_out="Already logged in."
        )
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        assert prompt.already_logged_in and prompt.actionable and not prompt.error

    def test_pinned_identity_never_falls_back_to_callback_login(self, monkeypatch):
        _ssm_script(monkeypatch, whoami_ok=False, whoami_json="", login_out="")  # no device prompt
        fell_back = []
        monkeypatch.setattr(
            login,
            "_start_callback_login",
            lambda *a, **k: fell_back.append(1) or login.LoginPrompt(url="https://social"),
        )
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        assert not fell_back
        assert not prompt.url and "Identity Center" in prompt.error
        # The default target still may.
        login.start_device_login("i-1", "p", "r", open_browser=False)
        assert fell_back

    def test_pinned_identity_login_runs_under_a_pty_driver(self):
        """kiro-cli prompts for the Identity Center start URL and region (the
        flags only prefill them) and reads the answers from a TTY; a pinned
        login therefore runs under the pty driver. The driver is staged in a
        FRESH private directory (``mktemp -d``, 0700, unpredictable name) -- never
        a fixed /tmp path a second local user could pre-create or symlink -- and a
        failure to stage it aborts the launch with a sentinel instead of running
        whatever sits at the path. Builder ID keeps the plain background process."""
        pinned = login._device_login_command(replace_existing=False, **IDC.login_kwargs())
        assert 'mktemp -d "${TMPDIR:-/tmp}/kirocrew-login-pty.XXXXXXXX"' in pinned
        assert "python3" in pinned and "--use-device-flow --identity-provider" in pinned
        assert "stdbuf" not in pinned
        assert "/tmp/kirocrew-kiro-login-pty.py" not in pinned
        # Staging failures (no directory, write failed) print the sentinel and stop.
        assert pinned.count(login._DRIVER_SETUP_FAILED_SENTINEL) == 2
        assert pinned.index("umask 077") < pinned.index("mktemp -d")
        # The directory is reaped once the login process exits.
        assert 'rm -rf "$(dirname "$0")"' in pinned
        plain = login._device_login_command(replace_existing=False)
        assert "python3" not in plain and "nohup stdbuf" in plain and "mktemp" not in plain

    def test_driver_staging_failure_is_reported_by_name(self, monkeypatch):
        _ssm_script(
            monkeypatch,
            whoami_ok=False,
            whoami_json="",
            login_out=login._DRIVER_SETUP_FAILED_SENTINEL,
        )
        prompt = login.start_device_login("i-1", "p", "r", open_browser=False, target=IDC)
        assert not prompt.actionable and "private temporary directory" in prompt.error

    def test_parse_login_output_ignores_the_pinned_start_url(self):
        echoed = (
            f"Enter Start URL {IDC.start_url}\n"
            "Confirm the following code in the browser\nCode: ABCD-EFGH\n"
            f"Open this URL: {IDC.start_url}/#/device?user_code=ABCD-EFGH\n"
        )
        p = login.parse_login_output(echoed, ignore_url=IDC.start_url)
        assert p.url.endswith("user_code=ABCD-EFGH") and p.code == "ABCD-EFGH"
        # Only the echo, no verification URL: not actionable, not "already in".
        only_echo = f"? Enter Start URL › {IDC.start_url}\nerror: failed to construct request\n"
        p = login.parse_login_output(only_echo, ignore_url=IDC.start_url)
        assert not p.url and not p.already_logged_in
        # Without the hint the echo would have been taken as the URL — the guard matters.
        assert login.parse_login_output(only_echo).url == IDC.start_url

    @pytest.mark.skipif(
        importlib.util.find_spec("termios") is None,
        reason="the driver runs on the Linux instance; the host's stdlib has no pty",
    )
    def test_pty_driver_answers_prefilled_prompts_and_filters_their_echo(self, tmp_path):
        """Real pty run: a fake kiro-cli that prompts like 2.21.2 (reads a line
        from the tty for start URL and region, then prints the device code)."""
        import subprocess
        import sys

        driver = tmp_path / "driver.py"
        driver.write_text(login._LOGIN_PTY_DRIVER, encoding="utf-8")
        fake = tmp_path / "fake-kiro"
        fake.write_text(
            "import sys\n"
            "def ask(label, default):\n"
            "    sys.stdout.write('? ' + label + ' \\u203a ' + default + '\\n'); sys.stdout.flush()\n"
            "    ans = sys.stdin.readline().strip() or default\n"
            "    sys.stdout.write('\\u2714 ' + label + ' \\u00b7 ' + ans + '\\n'); sys.stdout.flush()\n"
            "    return ans\n"
            "url = ask('Enter Start URL', sys.argv[sys.argv.index('--identity-provider') + 1])\n"
            "reg = ask('Enter Region', sys.argv[sys.argv.index('--region') + 1])\n"
            "if not reg: sys.exit('error: region must be a valid host label')\n"
            "print('\\x1b[2mConfirm the following code in the browser\\x1b[0m')\n"
            "print('Code: WXYZ-1234')\n"
            "print('Open this URL: https://device.example/start/#/device?user_code=WXYZ-1234')\n",
            encoding="utf-8",
        )
        res = subprocess.run(
            [sys.executable, str(driver), sys.executable, str(fake), "login", "--use-device-flow"]
            + ["--identity-provider", IDC.start_url, "--license", "pro", "--region", "us-east-1"],
            capture_output=True,
            timeout=30,
        )
        out = res.stdout.decode()
        assert res.returncode == 0, out + res.stderr.decode()
        assert "Enter Start URL" not in out and "Enter Region" not in out
        assert IDC.start_url not in out
        assert "\x1b[" not in out
        prompt = login.parse_login_output(out, ignore_url=IDC.start_url)
        assert prompt.code == "WXYZ-1234" and prompt.url.endswith("user_code=WXYZ-1234")


# ── launch job durability ────────────────────────────────────────────────────


class TestLaunchJobDurability:
    def test_round_trip_and_legacy_file_loads_as_default(self, tmp_path):
        store = lj.LaunchJobStore(root=tmp_path / "jobs")
        job = store.create(profile="p", region="us-west-2", size_key="balanced", login_target=IDC)
        assert lj.LaunchJob.from_dict(job.to_dict()).login_target == IDC
        reloaded = lj.LaunchJobStore(root=tmp_path / "jobs").get(job.id)
        assert reloaded is not None and reloaded.login_target == IDC
        legacy = job.to_dict()
        del legacy["login_target"]
        assert lj.LaunchJob.from_dict(legacy).login_target == KiroLoginTarget()
        # The persisted target never carries a device code or token.
        assert set(job.to_dict()["login_target"]) == {"license", "start_url", "region"}

    def test_unreadable_persisted_target_fails_the_job_instead_of_defaulting(self, tmp_path):
        """A pending Identity Center job reloaded by a release whose validation
        rejects its persisted target must not resume its device flow
        as Builder ID: it loads as a terminal FAILED job carrying the reason,
        the store's reaper leaves it alone, and the runner returns it untouched."""
        store = lj.LaunchJobStore(root=tmp_path / "jobs")
        job = store.create(profile="p", region="us-west-2", size_key="balanced", login_target=IDC)
        stale = job.to_dict()
        stale["status"] = lj.AWAITING_SIGNIN
        stale["login_target"] = {"license": "pro", "start_url": "", "region": ""}
        loaded = lj.LaunchJob.from_dict(stale)
        assert loaded.status == lj.FAILED and loaded.terminal
        assert "identity target" in loaded.error and "Builder" not in loaded.error
        assert loaded.login_target == KiroLoginTarget()

        class _NeverTouched:
            def __getattr__(self, name):
                raise AssertionError(f"engine used on a terminal job: {name}")

        assert lj.run_launch(loaded, store, _NeverTouched()) is loaded  # type: ignore[arg-type]


# ── engine: start AND resume carry the target; versioned protocol ────────────


class TestEngineForwarding:
    def test_real_handle_forwards_target_on_start_resume_and_poll(self, monkeypatch):
        seen: dict[str, object] = {}

        def fake_start(iid, profile, region, *, open_browser, target=None, **kw):
            seen["start"] = target
            return SimpleNamespace(
                already_logged_in=False,
                url="https://device",
                code="X",
                ports=[],
                close=lambda: None,
            )

        def fake_resume(iid, profile, region, *, target=None, **kw):
            seen["resume"] = target

        def fake_wait(iid, profile, region, *, attempts, target=None):
            seen["wait"] = target
            return True

        monkeypatch.setattr(le.login, "start_device_login", fake_start)
        monkeypatch.setattr(le.login, "resume_login_daemon", fake_resume)
        monkeypatch.setattr(le.login, "wait_until_logged_in", fake_wait)
        handle = le.RealLaunchEngine().begin_signin(
            instance_id="i-1", profile="p", region="r", login_target=IDC
        )
        assert handle.wait(threading.Event()) is True
        assert seen == {"start": IDC, "resume": IDC, "wait": IDC}

    def test_runner_passes_target_to_a_target_aware_engine(self):
        class Engine:
            def begin_signin(self, *, instance_id, profile, region, login_target=None):
                self.got = login_target
                return SimpleNamespace(
                    already_logged_in=True,
                    url="",
                    code="",
                    ports=[],
                    wait=lambda c: True,
                    close=lambda: None,
                )

        eng = Engine()
        job = lj.LaunchJob(
            id="j",
            profile="p",
            region="r",
            size_key="balanced",
            instance_id="i-1",
            login_target=IDC,
        )
        lj._begin_signin_with_target(eng, job)
        assert eng.got == IDC

    def test_runner_refuses_non_default_target_on_a_legacy_engine(self):
        class LegacyEngine:
            def begin_signin(self, *, instance_id, profile, region):
                return SimpleNamespace(
                    already_logged_in=True,
                    url="",
                    code="",
                    ports=[],
                    wait=lambda c: True,
                    close=lambda: None,
                )

        job = lj.LaunchJob(
            id="j",
            profile="p",
            region="r",
            size_key="balanced",
            instance_id="i-1",
            login_target=IDC,
        )
        with pytest.raises(RuntimeError, match="login_target"):
            lj._begin_signin_with_target(LegacyEngine(), job)
        # Default target: legacy engine keeps working untouched.
        job.login_target = KiroLoginTarget()
        assert lj._begin_signin_with_target(LegacyEngine(), job).already_logged_in


# ── dashboard boundary ───────────────────────────────────────────────────────


def _h_state(tmp_path):
    from kiro_crew.cloud import launch_job as _lj

    class _Handle:
        already_logged_in = True
        url = ""
        code = ""
        ports: list = []

        def wait(self, cancel):
            return True

        def close(self):
            pass

    class _Engine:
        def preflight(self, profile, region):
            pass

        def provision(self, *, tag, size_key, profile, region):
            return "i-0123456789abcdef0"

        def begin_signin(self, *, instance_id, profile, region, login_target=None):
            return _Handle()

        def register(self, *, instance_id, tag, profile, region):
            pass

        def teardown(self, *, tag, profile, region):
            return True

    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_engine=_Engine(),
        cloud_launch_store=_lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )


def _h_req(method, path, *, state, slack=False, body=None):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["state"] = state
    headers = {"X-Session-Key": "slack:x"} if slack else {}
    req = make_mocked_request(method, path, headers=headers, app=app)
    req["user"] = "owner-1"
    req["app"] = ""
    if body is not None:

        async def _json():
            return body

        req.json = _json  # type: ignore[assignment]
    return req


def _h_body(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.mark.asyncio
class TestHandlerBoundary:
    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        from kiro_crew.dashboard import handlers_cloud as hc

        monkeypatch.setattr(hc.sys, "platform", "linux")

    def _state(self, tmp_path):
        return _h_state(tmp_path)

    async def _post(self, tmp_path, body):
        from kiro_crew.dashboard import handlers_cloud as hc

        state = self._state(tmp_path)
        resp = await hc.api_cloud_launch_create(
            _h_req("POST", "/api/cloud/launch", state=state, body=body)
        )
        return resp.status, _h_body(resp), state

    async def test_valid_target_is_persisted(self, tmp_path):
        status, body, state = await self._post(
            tmp_path,
            {
                "profile": "p",
                "region": "us-west-2",
                "size_key": "balanced",
                "login_target": IDC.to_dict(),
            },
        )
        assert status == 202, body
        assert body["login_target"] == IDC.to_dict()
        assert state.cloud_launch_store.get(body["id"]).login_target == IDC

    async def test_pre_seam_body_is_builder_id(self, tmp_path):
        status, body, _ = await self._post(
            tmp_path, {"profile": "p", "region": "us-west-2", "size_key": "balanced"}
        )
        assert status == 202
        assert body["login_target"] == KiroLoginTarget().to_dict()

    @pytest.mark.parametrize(
        "target",
        [
            {"license": "pro"},
            {"start_url": "http://example.awsapps.com/start", "region": "us-east-1"},
            {"start_url": "https://example.awsapps.com/start", "region": "nowhere"},
            {"start_url": "https://example.awsapps.com/start$(id)", "region": "us-east-1"},
            "not-an-object",
        ],
    )
    async def test_invalid_target_is_a_coded_400_not_a_fallback(
        self, tmp_path, target, monkeypatch
    ):
        from kiro_crew.dashboard import handlers_cloud as hc

        audits: list[tuple] = []
        monkeypatch.setattr(hc, "_audit", lambda *a, **kw: audits.append((a, kw)))
        status, body, state = await self._post(
            tmp_path,
            {"profile": "p", "region": "us-west-2", "size_key": "balanced", "login_target": target},
        )
        assert status == 400
        assert body["code"] == "invalid_login_target"
        assert state.cloud_launch_store.list() == []  # nothing persisted
        # Every rejection of a login target -- wrong type or failed validation --
        # leaves the same denied event in the audit log.
        assert [(a[0], a[1]) for a, _ in audits] == [("launch_create", "denied")]
        assert "invalid login target" in audits[0][1]["error"]

    async def test_non_owner_denied_before_target_parsing(self, tmp_path):
        from kiro_crew.dashboard import handlers_cloud as hc

        resp = await hc.api_cloud_launch_create(
            _h_req(
                "POST",
                "/api/cloud/launch",
                state=self._state(tmp_path),
                slack=True,
                body={"login_target": "x"},
            )
        )
        assert resp.status == 403

    async def test_identity_endpoint_is_owner_only_and_suggests_target(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_cloud as hc

        async def fake_bin():
            return "/bin/kiro-cli"

        async def fake_whoami(_bin):
            return parse_whoami_output(IDC_WHOAMI)

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions._resolve_kiro_bin_for_spawn", fake_bin
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions._fetch_whoami_or_none", fake_whoami
        )
        resp = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path))
        )
        assert resp.status == 200
        data = _h_body(resp)
        assert data["discovery"] == "read"
        assert data["identity"] == {"account_type": "IamIdentityCenter", "start_url": IDC.start_url}
        assert (
            data["suggested_target"]["start_url"] == IDC.start_url
            and data["suggested_target"]["region"] == ""
        )
        assert "email" not in data["identity"]
        denied = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path), slack=True)
        )
        assert denied.status == 403

    async def test_identity_endpoint_reaches_kiro_cli_only_via_sessions_helper(
        self, tmp_path, monkeypatch
    ):
        """The cloud handler never imports the ACP layer itself.

        ``fetch_local_identity`` in the sessions module is the one dashboard
        door to ``kiro-cli whoami``; the agent-sdk-boundary gate fails any new
        ``kiro_crew.acp`` import outside it, and a missing kiro-cli must read as
        "no identity", not an error.
        """
        import inspect

        from kiro_crew.dashboard import handlers_cloud as hc
        from kiro_crew.dashboard.handlers import sessions as sessions_mod

        assert "kiro_crew.acp" not in inspect.getsource(hc)

        async def no_bin():
            return None

        monkeypatch.setattr(sessions_mod, "_resolve_kiro_bin_for_spawn", no_bin)
        assert await sessions_mod.fetch_local_identity() == {}
        resp = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path))
        )
        assert resp.status == 200
        body = _h_body(resp)
        # No kiro-cli is a READ answer: nothing to inherit, default suggested.
        assert body["discovery"] == "read" and body["identity"] == {}
        assert body["suggested_target"] == KiroLoginTarget().to_dict()

    async def test_identity_endpoint_reports_unknown_when_whoami_cannot_answer(
        self, tmp_path, monkeypatch
    ):
        """A whoami that could not answer (timeout, failure to start, an error
        exit with no identity) is UNKNOWN: the endpoint says so and suggests no
        target, so the form cannot present the Builder ID default as though the
        launching computer's sign-in had been read. The credit readout's own
        wrapper keeps its ``{}`` contract."""
        from kiro_crew.dashboard import handlers_cloud as hc
        from kiro_crew.dashboard.handlers import sessions as sessions_mod

        async def a_bin():
            return "/bin/kiro-cli"

        async def cannot_answer(_bin):
            return None

        monkeypatch.setattr(sessions_mod, "_resolve_kiro_bin_for_spawn", a_bin)
        monkeypatch.setattr(sessions_mod, "_fetch_whoami_or_none", cannot_answer)
        assert await sessions_mod.fetch_local_identity() is None
        assert await sessions_mod._fetch_whoami("/bin/kiro-cli") == {}
        resp = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path))
        )
        assert resp.status == 200
        body = _h_body(resp)
        assert body == {"identity": None, "suggested_target": None, "discovery": "unknown"}

        # A handler-level exception in discovery is unknown too, not a default.
        async def blows_up():
            raise RuntimeError("sandbox wrapper broke")

        monkeypatch.setattr(hc, "fetch_local_identity", blows_up)
        resp = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path))
        )
        assert _h_body(resp)["discovery"] == "unknown"

        # Identity Center named, portal unreadable: unknown too, and the default
        # is NOT suggested. The account type is still reported so the form can
        # say what it saw.
        async def idc_no_portal():
            return {"account_type": "IamIdentityCenter", "start_url": "", "email": "u@example.com"}

        monkeypatch.setattr(hc, "fetch_local_identity", idc_no_portal)
        resp = await hc.api_cloud_identity(
            _h_req("GET", "/api/cloud/identity", state=self._state(tmp_path))
        )
        body = _h_body(resp)
        assert body["discovery"] == "unknown" and body["suggested_target"] is None
        assert body["identity"] == {"account_type": "IamIdentityCenter", "start_url": ""}

    async def test_inner_whoami_fetch_keeps_failure_distinct_from_no_identity(self, monkeypatch):
        """Through the real subprocess path: a nonzero exit with no identity and
        a timeout are ``None``; a clean exit with no identity is ``{}``; an
        identity is returned whatever the exit code."""
        import asyncio

        from kiro_crew.dashboard.handlers import sessions as sessions_mod

        class _Proc:
            def __init__(self, rc, out):
                self.returncode = rc
                self._out = out

            async def communicate(self):
                return self._out, b""

            def kill(self):
                pass

            async def wait(self):
                return self.returncode

        monkeypatch.setattr(sessions_mod, "_wrap_argv_whoami", lambda b: (["kiro-cli"], None))
        monkeypatch.setattr(sessions_mod, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(sessions_mod, "scrub_agent_subprocess_env", lambda: {})
        current: dict[str, object] = {}

        async def fake_spawn(*argv, **kw):
            return current["proc"]

        monkeypatch.setattr(sessions_mod, "create_subprocess_limited", fake_spawn)
        current["proc"] = _Proc(1, b"Error: session expired")
        assert await sessions_mod._fetch_whoami_or_none("/bin/kiro-cli") is None
        current["proc"] = _Proc(0, b"{}")
        assert await sessions_mod._fetch_whoami_or_none("/bin/kiro-cli") == {}
        current["proc"] = _Proc(1, IDC_WHOAMI.encode())
        got = await sessions_mod._fetch_whoami_or_none("/bin/kiro-cli")
        assert got is not None and got["account_type"] == "IamIdentityCenter"

        class _Hangs(_Proc):
            async def communicate(self):
                raise asyncio.TimeoutError()

        current["proc"] = _Hangs(None, b"")
        assert await sessions_mod._fetch_whoami_or_none("/bin/kiro-cli") is None


# ── CLI resolution + setup delegation ────────────────────────────────────────


class TestCliResolution:
    def _args(self, **kw):
        base = dict(identity_provider="", license="", idp_region="", no_inherit_identity=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_explicit_flags_win_and_are_validated(self, monkeypatch):
        from kiro_crew import cli_cloud

        monkeypatch.setattr(
            cli_cloud, "discover_local_identity", lambda: parse_whoami_output(BUILDER_WHOAMI)
        )
        t = cli_cloud._resolve_login_target(
            self._args(identity_provider=IDC.start_url, license="pro", idp_region="us-east-1"),
            inherit=True,
        )
        assert t == IDC
        with pytest.raises(LoginTargetError):
            cli_cloud._resolve_login_target(self._args(license="pro"), inherit=True)

    def test_inherits_identity_center_without_region_and_no_inherit_opts_out(self, monkeypatch):
        from kiro_crew import cli_cloud

        monkeypatch.setattr(
            cli_cloud, "discover_local_identity", lambda: parse_whoami_output(IDC_WHOAMI)
        )
        t = cli_cloud._resolve_login_target(self._args(), inherit=True)
        assert t.is_identity_center and t.start_url == IDC.start_url and t.region == ""
        assert cli_cloud._resolve_login_target(self._args(), inherit=False) == KiroLoginTarget()
        monkeypatch.setattr(
            cli_cloud, "discover_local_identity", lambda: parse_whoami_output(BUILDER_WHOAMI)
        )
        assert cli_cloud._resolve_login_target(self._args(), inherit=True) == KiroLoginTarget()

    def test_idp_region_alone_completes_the_inherited_target(self, monkeypatch):
        """``--idp-region`` is the one field whoami cannot report; passing it by
        itself COMPLETES the inherited Identity Center target rather than
        replacing it, so the strict 'region without a start URL' refusal only
        fires when there is no inherited Identity Center to attach it to."""
        from kiro_crew import cli_cloud

        monkeypatch.setattr(
            cli_cloud, "discover_local_identity", lambda: parse_whoami_output(IDC_WHOAMI)
        )
        t = cli_cloud._resolve_login_target(self._args(idp_region="us-east-1"), inherit=True)
        assert t == IDC
        # No inherited Identity Center to attach it to → still the strict refusal.
        with pytest.raises(LoginTargetError):
            cli_cloud._resolve_login_target(self._args(idp_region="us-east-1"), inherit=False)
        monkeypatch.setattr(
            cli_cloud, "discover_local_identity", lambda: parse_whoami_output(BUILDER_WHOAMI)
        )
        with pytest.raises(LoginTargetError):
            cli_cloud._resolve_login_target(self._args(idp_region="us-east-1"), inherit=True)

    def test_launch_threads_target_into_wizard(self, monkeypatch):
        from kiro_crew import cli_cloud

        seen = {}
        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("p", "r"))
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: seen.update(kw) or 0)
        args = self._args(identity_provider=IDC.start_url, license="pro", idp_region="us-east-1")
        assert cli_cloud._cloud_launch(args) == 0
        assert seen["login_target"] == IDC

    def test_failed_discovery_is_unknown_not_builder_id(self, monkeypatch):
        """A ``whoami`` that could not run leaves the identity UNKNOWN. Under
        inheritance the resolver refuses (naming both ways forward) rather than
        signing the crew in as Builder ID; ``--no-inherit-identity`` and explicit
        flags still resolve without consulting discovery's answer."""
        from kiro_crew import cli_cloud

        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: None)
        with pytest.raises(LoginTargetError, match="--no-inherit-identity"):
            cli_cloud._resolve_login_target(self._args(), inherit=True)
        with pytest.raises(LoginTargetError, match="--identity-provider"):
            cli_cloud._resolve_login_target(self._args(idp_region="us-east-1"), inherit=True)
        assert cli_cloud._resolve_login_target(self._args(), inherit=False) == KiroLoginTarget()
        t = cli_cloud._resolve_login_target(
            self._args(identity_provider=IDC.start_url, license="pro", idp_region="us-east-1"),
            inherit=True,
        )
        assert t == IDC
        # whoami that RAN and reports no Identity Center sign-in is a known answer:
        # the Builder ID default applies.
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: {})
        assert cli_cloud._resolve_login_target(self._args(), inherit=True) == KiroLoginTarget()
        # Identity Center with no readable portal: which organization is unknown,
        # so the same refusal, never the default.
        monkeypatch.setattr(
            cli_cloud,
            "discover_local_identity",
            lambda: {"account_type": "IamIdentityCenter", "start_url": ""},
        )
        with pytest.raises(LoginTargetError, match="no readable start URL"):
            cli_cloud._resolve_login_target(self._args(), inherit=True)
        with pytest.raises(LoginTargetError, match="--no-inherit-identity"):
            cli_cloud._resolve_login_target(self._args(idp_region="us-east-1"), inherit=True)
        assert cli_cloud._resolve_login_target(self._args(), inherit=False) == KiroLoginTarget()
        assert (
            cli_cloud._resolve_login_target(
                self._args(identity_provider=IDC.start_url, license="pro", idp_region="us-east-1"),
                inherit=True,
            )
            == IDC
        )

    def test_launch_refuses_on_failed_discovery_before_the_wizard(self, monkeypatch):
        from kiro_crew import cli_cloud

        monkeypatch.setattr(cli_cloud, "_resolve", lambda a: ("p", "r"))
        monkeypatch.setattr(cli_cloud, "discover_local_identity", lambda: None)
        reached = []
        monkeypatch.setattr(cli_cloud.wizard, "launch", lambda **kw: reached.append(kw) or 0)
        assert cli_cloud._cloud_launch(self._args(yes=True)) == 2
        assert reached == []
        assert cli_cloud._cloud_launch(self._args(yes=True, no_inherit_identity=True)) == 0
        assert reached[0]["login_target"] == KiroLoginTarget()

    def test_discover_local_identity_reports_could_not_run_as_none(self, monkeypatch):
        import subprocess

        from kiro_crew.cloud import login_target as lt

        def timed_out(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 0))

        monkeypatch.setattr(lt.subprocess, "run", timed_out)
        assert lt.discover_local_identity("/x/kiro-cli") is None

        def not_startable(*a, **kw):
            raise OSError("exec failed")

        monkeypatch.setattr(lt.subprocess, "run", not_startable)
        assert lt.discover_local_identity("/x/kiro-cli") is None

        def resolve_blows_up():
            raise RuntimeError("resolver broken")

        monkeypatch.setattr(lt, "resolve_kiro_cli", resolve_blows_up)
        assert lt.discover_local_identity() is None
        # No kiro-cli on this machine: there is no local sign-in, a KNOWN answer.
        monkeypatch.setattr(lt, "resolve_kiro_cli", lambda: None)
        assert lt.discover_local_identity() == {}

        class _Ran:
            returncode = 0
            stdout = IDC_WHOAMI
            stderr = ""

        monkeypatch.setattr(lt.subprocess, "run", lambda *a, **kw: _Ran())
        assert lt.discover_local_identity("/x/kiro-cli") == parse_whoami_output(IDC_WHOAMI)

        class _Errored:
            returncode = 1
            stdout = ""
            stderr = "Error: session expired, run `kiro-cli login`"

        monkeypatch.setattr(lt.subprocess, "run", lambda *a, **kw: _Errored())
        # An expired or broken local session is UNKNOWN, not "nothing to inherit".
        assert lt.discover_local_identity("/x/kiro-cli") is None

        class _CleanEmpty:
            returncode = 0
            stdout = "{}"
            stderr = ""

        monkeypatch.setattr(lt.subprocess, "run", lambda *a, **kw: _CleanEmpty())
        assert lt.discover_local_identity("/x/kiro-cli") == {}

    def test_setup_namespace_inherits_by_default(self):
        import inspect

        from kiro_crew import cli_setup

        src = inspect.getsource(cli_setup._maybe_setup_cloud)
        assert "no_inherit_identity=False" in src and "identity_provider=" in src

    def test_launch_and_login_parsers_share_identity_flags(self):
        import sys
        from unittest.mock import patch

        def _parse(argv):
            with (
                patch.object(sys, "argv", ["kirocrew", *argv]),
                patch("kiro_crew.cli.handle_cloud", return_value=0) as h,
            ):
                from kiro_crew.cli import main

                with pytest.raises(SystemExit):
                    main()
                h.assert_called_once()
                return h.call_args[0][0]

        a = _parse(
            [
                "cloud",
                "launch",
                "--identity-provider",
                IDC.start_url,
                "--license",
                "pro",
                "--idp-region",
                "us-east-1",
            ]
        )
        assert (a.identity_provider, a.license, a.idp_region) == (IDC.start_url, "pro", "us-east-1")
        assert a.no_inherit_identity is False
        b = _parse(["cloud", "launch", "--no-inherit-identity"])
        assert b.no_inherit_identity is True and b.identity_provider == ""
        c = _parse(["cloud", "login", "--license", "free"])
        assert c.license == "free"


# ── wizard: region completion never downgrades ───────────────────────────────


class TestWizardTargetCompletion:
    def test_assume_yes_without_region_refuses_instead_of_builder_id(self, monkeypatch):
        from kiro_crew.cloud import wizard

        monkeypatch.setattr(
            wizard.CloudConfig,
            "load",
            staticmethod(lambda: SimpleNamespace(profile="p", region="r")),
        )
        provisioned = []
        monkeypatch.setattr(wizard.ui, "fail", lambda m: provisioned.append(("fail", m)))
        rc = wizard.launch(
            assume_yes=True,
            login_target=KiroLoginTarget(license="pro", start_url=IDC.start_url, region=""),
        )
        assert rc == 2
        assert any("--idp-region" in m for _, m in provisioned)
