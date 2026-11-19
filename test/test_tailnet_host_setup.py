"""Setting Tailscale up on a headless Linux gateway host from the phone-access card.

On a remote host (an EC2 Remote Crew reached over SSM) the card's download link
installs Tailscale on the browser's computer, not on the box. These tests pin
that such a host is offered an on-box setup instead, that the setup runs only
fixed commands, that it signs in with a login URL and never an auth key, and
that every other host keeps the download link. No real subprocess runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet, tailnet_host_setup
from kiro_crew.dashboard.handlers import tailnet_host_setup as host_handler
from kiro_crew.dashboard.handlers import tailnet_mobile

_AL2023 = 'NAME="Amazon Linux"\nID="amzn"\nVERSION_ID="2023"\n'
_UBUNTU = 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n'
_AUTH_URL = "https://login.tailscale.com/a/abc123"


def _linux_host(monkeypatch, tmp_path, *, release: str = _AL2023, euid: int = 1000):
    path = tmp_path / "os-release"
    path.write_text(release, encoding="utf-8")
    real_read = tailnet_host_setup._read_os_release
    monkeypatch.setattr(tailnet_host_setup, "_read_os_release", lambda: real_read(str(path)))
    monkeypatch.setattr(tailnet_host_setup, "_IS_LINUX", True)
    monkeypatch.setattr(tailnet_host_setup, "_euid", lambda: euid)
    sudo = tmp_path / "sudo"
    sudo.write_text("", encoding="utf-8")
    monkeypatch.setattr(tailnet_host_setup, "_SUDO", str(sudo))
    return str(sudo)


class _Runs:
    """Records every argv handed to ``subprocess.run``; answers with *returncode*."""

    def __init__(self, returncode: int = 0, fail_on: str = "") -> None:
        self.argvs: list[list[str]] = []
        self.inputs: list[bytes | None] = []
        self.returncode = returncode
        self.fail_on = fail_on

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        self.inputs.append(kw.get("input"))
        rc = 1 if self.fail_on and self.fail_on in argv else self.returncode
        return subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=b"boom" if rc else b"")


class TestHostPlan:
    def test_headless_al2023_with_passwordless_sudo_gets_a_dnf_plan(
        self, monkeypatch, tmp_path
    ) -> None:
        sudo = _linux_host(monkeypatch, tmp_path)
        runs = _Runs()
        monkeypatch.setattr(tailnet_host_setup.subprocess, "run", runs)
        plan = tailnet_host_setup.host_plan(environ={})
        assert plan == tailnet_host_setup.HostPlan(
            manager="dnf",
            repo="https://pkgs.tailscale.com/stable/amazon-linux/2023",
            prefix=(sudo, "-n"),
        )
        assert runs.argvs == [[sudo, "-n", "true"]]

    def test_sudo_is_asked_once_per_process(self, monkeypatch, tmp_path) -> None:
        _linux_host(monkeypatch, tmp_path)
        runs = _Runs()
        monkeypatch.setattr(tailnet_host_setup.subprocess, "run", runs)
        tailnet_host_setup.host_plan(environ={})
        tailnet_host_setup.host_plan(environ={})
        assert len(runs.argvs) == 1

    def test_ubuntu_gets_an_apt_plan_for_its_codename(self, monkeypatch, tmp_path) -> None:
        _linux_host(monkeypatch, tmp_path, release=_UBUNTU, euid=0)
        plan = tailnet_host_setup.host_plan(environ={})
        assert plan is not None
        assert (plan.manager, plan.repo, plan.prefix) == (
            "apt",
            "https://pkgs.tailscale.com/stable/ubuntu/noble",
            (),
        )

    @pytest.mark.parametrize(
        "case",
        ["desktop", "not_linux", "al2", "unknown_distro", "sudo_needs_password", "bad_codename"],
    )
    def test_every_other_host_keeps_the_download_link(self, monkeypatch, tmp_path, case) -> None:
        release = {
            "al2": 'ID="amzn"\nVERSION_ID="2"\n',
            "unknown_distro": 'ID="arch"\nVERSION_ID="rolling"\n',
            "bad_codename": "ID=ubuntu\nVERSION_CODENAME=no/../ble\n",
        }.get(case, _AL2023)
        _linux_host(monkeypatch, tmp_path, release=release)
        monkeypatch.setattr(
            tailnet_host_setup.subprocess,
            "run",
            _Runs(returncode=1 if case == "sudo_needs_password" else 0),
        )
        if case == "not_linux":
            monkeypatch.setattr(tailnet_host_setup, "_IS_LINUX", False)
        env = {"DISPLAY": ":0"} if case == "desktop" else {}
        assert tailnet_host_setup.host_plan(environ=env) is None


class TestAuthUrl:
    @pytest.mark.parametrize(
        "raw",
        ["", "http://login.tailscale.com/a/x", "javascript:alert(1)", "https://", "https://a b"],
    )
    def test_only_an_https_url_is_shown(self, raw) -> None:
        assert tailnet_host_setup._auth_url_from_status({"AuthURL": raw}) == ""

    def test_a_pending_login_url_is_returned(self) -> None:
        status = {"BackendState": "NeedsLogin", "AuthURL": _AUTH_URL}
        assert tailnet_host_setup._auth_url_from_status(status) == _AUTH_URL


def test_a_sudo_probe_that_could_not_run_is_asked_again(monkeypatch, tmp_path) -> None:
    _linux_host(monkeypatch, tmp_path)
    calls: list[int] = []

    def _boom(argv, **_kw):
        calls.append(1)
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(tailnet_host_setup.subprocess, "run", _boom)
    assert tailnet_host_setup.host_plan(environ={}) is None
    assert tailnet_host_setup.host_plan(environ={}) is None
    assert len(calls) == 2


_DNF_PLAN = tailnet_host_setup.HostPlan(
    manager="dnf",
    repo="https://pkgs.tailscale.com/stable/amazon-linux/2023",
    prefix=("/usr/bin/sudo", "-n"),
)


class _FakeLogin:
    def __init__(self) -> None:
        self.argv: list[str] = []
        self.kwargs: dict = {}

    def __call__(self, argv, **kw):
        self.argv = list(argv)
        self.kwargs = kw
        return self

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class TestRunHostSetup:
    def _setup(self, monkeypatch, *, installed: bool, statuses: list[object]):
        runs = _Runs()
        monkeypatch.setattr(tailnet_host_setup.subprocess, "run", runs)
        downloads: list[str] = []

        def _download(url):
            downloads.append(url)
            return b"[tailscale-stable]\n"

        monkeypatch.setattr(tailnet_host_setup, "_download", _download)
        cli_answers = iter(
            [None if not installed else "/usr/bin/tailscale"] + ["/usr/bin/tailscale"] * 5
        )
        monkeypatch.setattr(tailnet, "_cli_path", lambda: next(cli_answers))
        status_iter = iter(statuses)
        monkeypatch.setattr(tailnet, "_run_json", lambda args: next(status_iter, statuses[-1]))
        monkeypatch.setattr(tailnet_host_setup.getpass, "getuser", lambda: "ec2-user")
        login = _FakeLogin()
        monkeypatch.setattr(tailnet_host_setup.subprocess, "Popen", login)
        return runs, downloads, login

    def test_fresh_box_installs_starts_sets_operator_and_returns_the_login_url(
        self, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG)
        runs, downloads, login = self._setup(
            monkeypatch,
            installed=False,
            statuses=[
                {"BackendState": "NeedsLogin"},
                {"BackendState": "NeedsLogin", "AuthURL": _AUTH_URL},
            ],
        )
        result = tailnet_host_setup.run_host_setup(_DNF_PLAN)

        assert result == tailnet_host_setup.HostSetupResult(True, "", auth_url=_AUTH_URL)
        assert downloads == ["https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo"]
        sudo = ["/usr/bin/sudo", "-n"]
        repo = "/etc/yum.repos.d/tailscale.repo"
        # The repo file reaches root on stdin, never through a file root reads.
        assert runs.argvs[:2] == [
            [*sudo, "/usr/bin/tee", repo],
            [*sudo, "/usr/bin/chmod", "0644", repo],
        ]
        assert runs.inputs[0] == b"[tailscale-stable]\n"
        assert runs.argvs[2:] == [
            [*sudo, "/usr/bin/dnf", "install", "-y", "tailscale"],
            [*sudo, "/usr/bin/systemctl", "enable", "--now", "tailscaled"],
            [*sudo, "/usr/bin/tailscale", "set", "--operator=ec2-user"],
        ]
        # Sign-in uses no auth key, runs as the gateway user, and its output
        # (which carries the login URL) is discarded.
        assert login.argv == ["/usr/bin/tailscale", "login"]
        assert login.kwargs["stdout"] is subprocess.DEVNULL
        assert login.kwargs["stderr"] is subprocess.DEVNULL
        assert _AUTH_URL not in caplog.text

    def test_installed_box_skips_the_install(self, monkeypatch) -> None:
        runs, downloads, login = self._setup(
            monkeypatch,
            installed=True,
            statuses=[{"BackendState": "NeedsLogin", "AuthURL": _AUTH_URL}],
        )
        result = tailnet_host_setup.run_host_setup(_DNF_PLAN)
        assert result.auth_url == _AUTH_URL
        assert downloads == []
        assert [argv[2] for argv in runs.argvs] == ["/usr/bin/systemctl", "/usr/bin/tailscale"]
        # The daemon already holds a login URL, so no second login is started.
        assert login.argv == []

    def test_a_failed_step_stops_and_reports_it(self, monkeypatch) -> None:
        runs, _downloads, _login = self._setup(
            monkeypatch, installed=False, statuses=[{"BackendState": "NeedsLogin"}]
        )
        runs.fail_on = "/usr/bin/dnf"
        result = tailnet_host_setup.run_host_setup(_DNF_PLAN)
        assert not result.ok
        assert "dnf install -y tailscale" in result.detail and "boom" in result.detail
        assert not any("/usr/bin/systemctl" in argv for argv in runs.argvs)

    def test_apt_plan_adds_the_keyring_and_list(self, monkeypatch) -> None:
        runs, downloads, _login = self._setup(
            monkeypatch, installed=False, statuses=[{"BackendState": "Running"}]
        )
        plan = tailnet_host_setup.HostPlan(
            manager="apt", repo="https://pkgs.tailscale.com/stable/ubuntu/noble", prefix=()
        )
        assert tailnet_host_setup.run_host_setup(plan).ok
        assert downloads == [
            "https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg",
            "https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list",
        ]
        keyring = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
        listing = "/etc/apt/sources.list.d/tailscale.list"
        assert runs.argvs[:4] == [
            ["/usr/bin/tee", keyring],
            ["/usr/bin/chmod", "0644", keyring],
            ["/usr/bin/tee", listing],
            ["/usr/bin/chmod", "0644", listing],
        ]
        assert runs.argvs[4:6] == [
            ["/usr/bin/apt-get", "update"],
            ["/usr/bin/apt-get", "install", "-y", "tailscale"],
        ]

    @pytest.mark.parametrize("manager", ["dnf", "apt"])
    def test_no_root_step_names_a_path_the_gateway_user_can_write(
        self, monkeypatch, manager
    ) -> None:
        """An agent runs as the gateway's user and could swap such a file before
        root reads it, so every root argv names only fixed system paths."""
        runs, _downloads, _login = self._setup(
            monkeypatch, installed=False, statuses=[{"BackendState": "Running"}]
        )
        plan = tailnet_host_setup.HostPlan(
            manager=manager,
            repo="https://pkgs.tailscale.com/stable/x/y",
            prefix=("/usr/bin/sudo", "-n"),
        )
        assert tailnet_host_setup.run_host_setup(plan).ok
        allowed = (
            "/etc/yum.repos.d/",
            "/etc/apt/sources.list.d/",
            "/usr/share/keyrings/",
            "/usr/bin/",
        )
        for argv in runs.argvs:
            for arg in argv:
                if arg.startswith("/"):
                    assert arg.startswith(allowed), (argv, arg)

    def test_download_refuses_any_other_origin(self) -> None:
        with pytest.raises(ValueError):
            tailnet_host_setup._download("https://example.com/tailscale.repo")

    def test_a_second_setup_while_one_runs_is_refused(self, monkeypatch) -> None:
        runs, _downloads, _login = self._setup(
            monkeypatch, installed=True, statuses=[{"BackendState": "Running"}]
        )
        assert tailnet_host_setup._SETUP_LOCK.acquire(blocking=False)
        try:
            result = tailnet_host_setup.run_host_setup(_DNF_PLAN)
        finally:
            tailnet_host_setup._SETUP_LOCK.release()
        assert not result.ok and "already running" in result.detail
        assert runs.argvs == []

    def test_a_running_login_is_reused_not_restarted(self, monkeypatch) -> None:
        _runs, _downloads, login = self._setup(
            monkeypatch,
            installed=True,
            statuses=[
                {"BackendState": "NeedsLogin"},
                {"BackendState": "NeedsLogin", "AuthURL": _AUTH_URL},
            ],
        )
        monkeypatch.setattr(tailnet_host_setup, "_login_proc", _FakeLogin())
        assert tailnet_host_setup.run_host_setup(_DNF_PLAN).auth_url == _AUTH_URL
        assert login.argv == []


# ── The endpoints ──────────────────────────────────────────────────────────


def _request():
    class _Req:
        def __init__(self) -> None:
            self.app = {"port": 5476, "state": SimpleNamespace(owner_id="owner")}
            self.remote = "127.0.0.1"
            self.headers: dict[str, str] = {}
            self._items = {"app": "", "user": "owner"}

        def get(self, key, default=None):
            return self._items.get(key, default)

        def __contains__(self, key):
            return key in self._items

        def __getitem__(self, key):
            return self._items[key]

    return _Req()


def _live(step: str):
    probe = tailnet.DaemonProbe(
        name="", installed=step != "install", reachable=True, logged_in=False, detail=""
    )
    return tailnet_mobile._LiveState(
        step=step,  # type: ignore[arg-type]
        probe=probe,
        published=None,
        serve_detail="",
        trusted=False,
        keep_awake=True,
        pinned=False,
        startup_host="",
    )


@pytest.fixture
def _owner():
    with (
        patch.object(tailnet_mobile, "is_host_owner_dashboard_request", return_value=True),
        patch.object(tailnet_mobile, "_is_restricted_session", return_value=False),
        patch.object(tailnet_mobile, "_audit"),
    ):
        yield


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.mark.usefixtures("_owner")
class TestStatusOffersHostSetup:
    def _status(self, step: str, plan, auth_url: str = "") -> dict:
        async def _fake_live(_request, _port):
            return _live(step)

        with (
            patch.object(tailnet_mobile, "_live_state", _fake_live),
            patch.object(tailnet_host_setup, "host_plan", return_value=plan),
            patch.object(tailnet_host_setup, "current_auth_url", return_value=auth_url),
        ):
            return _body(asyncio.run(tailnet_mobile.api_tailnet_mobile_status(_request())))

    def test_headless_host_on_install_is_offered_host_setup(self) -> None:
        body = self._status("install", _DNF_PLAN)
        assert body["host_setup"] is True
        assert body["auth_url"] == ""

    def test_sign_in_carries_the_pending_login_url(self) -> None:
        body = self._status("sign_in", _DNF_PLAN, auth_url=_AUTH_URL)
        assert (body["host_setup"], body["auth_url"]) == (True, _AUTH_URL)

    def test_desktop_host_keeps_the_download_link(self) -> None:
        body = self._status("install", None, auth_url=_AUTH_URL)
        assert (body["host_setup"], body["auth_url"]) == (False, "")
        assert body["download_url"] == tailnet_mobile.TAILSCALE_DOWNLOAD_URL

    def test_later_steps_never_probe_the_host(self) -> None:
        with patch.object(tailnet_host_setup, "host_plan") as plan:
            body = self._status("trust_off", _DNF_PLAN)
        assert body["host_setup"] is False
        plan.assert_not_called()


@pytest.mark.usefixtures("_owner")
class TestHostSetupEndpoint:
    def _post(self, step: str, plan, result=None):
        async def _fake_live(_request, _port):
            return _live(step)

        with (
            patch.object(tailnet_mobile, "_live_state", _fake_live),
            patch.object(tailnet_host_setup, "host_plan", return_value=plan),
            patch.object(tailnet_host_setup, "run_host_setup", return_value=result) as run,
        ):
            resp = asyncio.run(host_handler.api_tailnet_mobile_host_setup(_request()))
        return resp, run

    def test_runs_setup_and_keeps_the_login_url_out_of_the_body(self) -> None:
        resp, run = self._post(
            "install", _DNF_PLAN, tailnet_host_setup.HostSetupResult(True, "", _AUTH_URL)
        )
        assert resp.status == 200
        # The card reads the link from the status query; this body never carries it.
        assert _AUTH_URL not in resp.body.decode()
        run.assert_called_once_with(_DNF_PLAN)

    def test_a_ready_host_is_refused(self) -> None:
        resp, run = self._post("ready", _DNF_PLAN)
        assert (resp.status, _body(resp)["code"]) == (409, "not_needed")
        run.assert_not_called()

    def test_policy_pinned_host_is_refused(self) -> None:
        resp, run = self._post("pinned", _DNF_PLAN)
        assert resp.status == 409
        run.assert_not_called()

    def test_unsupported_host_is_refused(self) -> None:
        resp, run = self._post("install", None)
        assert (resp.status, _body(resp)["code"]) == (409, "unsupported_host")
        run.assert_not_called()

    def test_failure_returns_the_step_error(self) -> None:
        resp, _run = self._post(
            "start_daemon", _DNF_PLAN, tailnet_host_setup.HostSetupResult(False, "`x` failed: boom")
        )
        assert (resp.status, _body(resp)["detail"]) == (409, "`x` failed: boom")


def test_non_owner_is_refused_before_anything_runs() -> None:
    with (
        patch.object(tailnet_mobile, "is_host_owner_dashboard_request", return_value=False),
        patch.object(tailnet_mobile, "_is_restricted_session", return_value=False),
        patch.object(tailnet_mobile, "_audit"),
        patch.object(tailnet_host_setup, "run_host_setup") as run,
    ):
        resp = asyncio.run(host_handler.api_tailnet_mobile_host_setup(_request()))
    assert resp.status == 403
    run.assert_not_called()
