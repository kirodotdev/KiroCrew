"""Coverage for the thinly-tested command dispatchers in ``kiro_crew.cli_server``.

Every file of this family injects a fake at the seam the product actually reaches:

* ``subprocess.run`` / ``os.execvp`` — the CLI shells out to git, journalctl and
  tail. A CI runner has no installed systemd unit, no launchd, and (for the
  update path) no writable checkout, so every spawn is replaced by a recorder.
* ``current_platform`` / ``svc_linux.UNIT_PATH`` / ``svc_macos.STDOUT_LOG`` —
  patched so the platform-specific log sources are exercised on any host without
  a ``skipif``. Nothing here reads a real journal or plist.
* ``sel()`` — replaced with a recorder so the audit contract can be asserted
  directly instead of inferred from the security log on disk.

The gateway/taskrunner coroutines are driven with ``asyncio.run`` (the repo does
not enable pytest-asyncio auto mode).

This file pins ``_update`` -- the git checkout path, the gateway poke, the hardened
subprocess calls, and the wheel path's dispatch, feed validation and installer -- and
the ``status`` count and memory lines. ``test_cli_server_more_coverage_client.py``
pins the client side (``resolve_client_port_ex``, the dashboard health probe, the
``token`` and ``logout`` refusals and port ownership, and the owner-verified secret
transport), and ``test_cli_server_more_coverage_lifecycle.py`` pins ``_stop`` and
``restart``, the service and sandbox commands, ``_logs_cmd``, ``_gateway`` and
``_run_task``. The SEL recorder fixture and the fake ``urlopen`` response they share
live in ``cli_server_more_coverage_helpers.py``.
"""

import subprocess
import sys
import types
import urllib.error
from pathlib import Path

import pytest
from cli_server_more_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Resp,
    sel_rec_fixture,
)

from kiro_crew import cli_server, kiro_cli
from kiro_crew.platform.update_layout import InstallLayout

# --------------------------------------------------------------------------
# _update — git checkout path
# --------------------------------------------------------------------------


# The OID the stub pins origin/<branch> to: what every later git call and the
# reset itself must name, in place of the branch name.
_PIN = "0123456789abcdef0123456789abcdef01234567"


class _GitStub:
    """Routes ``subprocess.run`` by argv prefix so each branch is reachable."""

    def __init__(self, **rc: int) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []
        self.status_out = ""
        # Behind-only by default: these tests model a fast-forwardable
        # checkout, which the divergence guard waves through.
        self.rev_list_out = "0\t5\n"
        self.show_out: bytes | None = None
        self.show_fails = False

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            # The upstream pin, taken right after the fetch.
            return subprocess.CompletedProcess(
                argv, self.rc.get("rev_parse_verify", 0), _PIN + "\n", "unknown revision"
            )
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, self.rc.get("rev_parse", 0), "main\n", "")
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, self.rc.get("fetch", 0), "", "no remote")
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, self.rc.get("diff", 1), "", "")
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(
                argv, self.rc.get("rev_list", 0), self.rev_list_out, ""
            )
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, self.status_out, "")
        if argv[:2] == ["git", "show"]:
            # The pre-reset interpreter-floor gate reads pyproject/setup.cfg out
            # of the fetched commit. BYTES, like the real call. Absent by
            # default, in git's own words for "not in this revision" -- the gate
            # tells that apart from a failed read, so the wording is the contract
            # -- and so the gate does not fire unless a test hands it a floor.
            if self.show_fails:
                return subprocess.CompletedProcess(
                    argv,
                    128,
                    b"",
                    b"fatal: not a git repository (or any of the parent directories)",
                )
            if self.show_out is None:
                path = argv[2].split(":", 1)[1]
                return subprocess.CompletedProcess(
                    argv,
                    128,
                    b"",
                    f"fatal: path '{path}' does not exist in '{argv[2].split(':')[0]}'".encode(),
                )
            return subprocess.CompletedProcess(argv, 0, self.show_out, b"")
        if argv[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(argv, self.rc.get("reset", 0), "", "dirty")
        if Path(argv[0]).name == "kiro-cli":
            # The pinned absolute path is argv0, never the bare name.
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:] == ["-I", "-X", "utf8", "-c", "import kiro_crew"]:
            # The full-reinstall success contract probes the target interpreter
            # in isolation from the caller's CWD and PYTHONPATH.
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "pip" in argv:
            # BYTES, like the real call: the install captures without text=True so
            # a non-UTF-8 console cannot make pip's own error message undecodable.
            return subprocess.CompletedProcess(argv, self.rc.get("pip", 0), b"", b"wheel error")
        return subprocess.CompletedProcess(argv, self.rc.get("setup", 0), "", "")


@pytest.fixture
def git_checkout(monkeypatch, tmp_path):
    """A KIROCREW_PROJECT_DIR that looks like a git checkout, with git stubbed out."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    # ``.git/HEAD``, not just ``.git/``: the install shape is derived by asking
    # git and falling back to the on-disk markers of a working tree's own root,
    # and a bare ``.git`` directory is refused by both on purpose.
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    # The git lane requires provenance as well as the path probe; this fixture
    # exercises the git path with a fabricated tree the test process does not
    # run from, so provenance is declared rather than derived.
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.running_from_checkout",
        lambda root, **kw: True,
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.resolve_remote_url",
        lambda p, remote="", branch="": "https://github.com/kirodotdev/KiroCrew.git",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )
    monkeypatch.setattr(cli_server.shutil, "which", lambda name: None)
    # The optional kiro-cli step spawns only a PINNED install (resolved with the
    # inherited PATH excluded); none by default, so the host's own install
    # cannot leak into these tests. Tests that want the step reachable resolve
    # one explicitly.
    monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: None)
    monkeypatch.setattr(cli_server, "build_frontend_sync", lambda p: None)
    monkeypatch.setattr("kiro_crew.cli._ensure_node", lambda *a: None)
    # Pin the install ROUTE. The real probe reads the test interpreter's own
    # Scripts dir, so on a Windows dev box running from a checkout these tests
    # would silently take the dependency-only branch instead of the reinstall
    # they assert. `kirocrew update`'s own substitute behaviour is covered in
    # test/test_dep_sync.py.
    monkeypatch.setattr(cli_server.dep_sync, "locked_console_scripts", lambda target: [])
    # A successful fake pip install must satisfy the shared artifact postcondition.
    # The test interpreter is a real executable on every supported platform;
    # dep_sync's own tests cover the path calculation and missing-script failure.
    monkeypatch.setattr(
        cli_server.dep_sync, "console_script_path", lambda target: Path(sys.executable)
    )
    # And the foreign-venv guard, which now runs before either install branch.
    # Its probe RUNS the target interpreter, which _GitStub intercepts into an
    # empty answer — read as "cannot be shown to serve this checkout" and refused.
    # dep_sync's own tests own that guard's behaviour.
    monkeypatch.setattr(cli_server.dep_sync, "venv_not_mapped_to", lambda origin, repo: None)
    # The post-update gateway poke reaches a loopback socket that does not exist
    # under test; neutralize it here so the branch-coverage tests stay hermetic.
    # Its own success/best-effort contract is asserted directly in
    # TestUpdateGatewayPoke.
    monkeypatch.setattr(cli_server, "_revalidate_gateway_update_check", lambda: None)
    return proj


class TestUpdateGitPath:
    """Every early-exit branch of the git update, plus the full success path."""

    def test_branch_detection_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(rev_parse=128))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "Could not determine current branch" in capsys.readouterr().out

    def test_detached_head_is_treated_as_mainline_and_pin_is_checked_first(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()

        def _run(argv, **kw):
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, "HEAD\n", "")
            return stub(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _run)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "remote not on the fleet allowlist",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "blocked by security policy" in out
        assert "fleet allowlist" in out
        # Blocked before any fetch touched the tree.
        assert not any(c[:2] == ["git", "fetch"] for c in stub.calls)

    def test_fetch_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(fetch=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git fetch failed" in capsys.readouterr().out

    def test_no_new_commits_returns_without_resetting(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub(diff=0)
        monkeypatch.setattr(subprocess, "run", stub)
        cli_server._update()
        assert "Already up to date" in capsys.readouterr().out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_ahead_only_checkout_is_never_reset(self, monkeypatch, git_checkout, capsys) -> None:
        """The CLI is the strictest surface: ahead-only has nothing to pull.

        ``origin/<branch>`` is an ancestor of HEAD here, so a reset could only
        REMOVE the local commits — refused even though the tree content
        differs from the upstream.
        """
        stub = _GitStub()
        stub.rev_list_out = "2\t0\n"  # 2 ahead, 0 behind
        monkeypatch.setattr(subprocess, "run", stub)
        cli_server._update()
        out = capsys.readouterr().out
        assert "Already up to date!" in out
        assert "2 local commit(s) ahead" in out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_diverged_checkout_refuses_without_force(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        stub.rev_list_out = "3\t2\n"  # 3 ahead, 2 behind — diverged
        monkeypatch.setattr(subprocess, "run", stub)
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "diverged" in out
        assert "kirocrew update --force" in out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_diverged_checkout_resets_under_force(self, monkeypatch, git_checkout, capsys) -> None:
        stub = _GitStub()
        stub.rev_list_out = "3\t2\n"  # 3 ahead, 2 behind — diverged
        monkeypatch.setattr(subprocess, "run", stub)
        cli_server._update(force=True)
        out = capsys.readouterr().out
        assert "--force: discarding 3 local commit(s)" in out
        assert any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_a_floor_git_cannot_read_refuses_before_the_reset(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """A failed `git show` is not "no floor declared": the reset must not run."""
        from kiro_crew import dep_sync

        stub = _GitStub()
        stub.show_fails = True
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr(dep_sync, "interpreter_version", lambda *a, **k: (3, 11, 9))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Could not read the incoming revision's interpreter requirement" in out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_a_revision_the_venv_cannot_run_is_refused_before_the_reset(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """The floor is judged on the FETCHED commit, before the tree moves.

        pip would refuse the same revision during the reinstall, but only after
        `reset --hard` had already moved the checkout to code this interpreter
        cannot import -- the stranded state every later run then repeats.
        """
        from kiro_crew import dep_sync

        stub = _GitStub()
        stub.show_out = b'[project]\nname = "kirocrew"\nrequires-python = ">=3.12"\n'
        # Tracked edits present: the refusal must land BEFORE the operator is
        # asked whether to discard them, so the prompt is never reached.
        stub.status_out = " M src/a.py\n"
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr(dep_sync, "interpreter_version", lambda *a, **k: (3, 11, 9))

        def _never_prompt(prompt=""):
            raise AssertionError("discard prompt reached after a floor refusal")

        monkeypatch.setattr("builtins.input", _never_prompt)
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Refusing to update" in out
        assert ">=3.12" in out and "3.11.9" in out
        # Read from the PINNED commit about to be applied, and the tree left alone.
        assert any(c[:3] == ["git", "show", f"{_PIN}:pyproject.toml"] for c in stub.calls)
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_a_venv_that_meets_the_incoming_floor_still_resets(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        from kiro_crew import dep_sync

        stub = _GitStub()
        stub.show_out = b'[project]\nname = "kirocrew"\nrequires-python = ">=3.12"\n'
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr(dep_sync, "interpreter_version", lambda *a, **k: (3, 12, 0))
        cli_server._update()
        assert any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_every_judgment_and_the_reset_name_the_pinned_commit(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """Check and apply describe ONE revision, by construction.

        The floor read, the divergence counts and the reset all take the OID
        pinned right after the fetch, never the origin/<branch> name a fetch
        from another terminal could move between them.
        """
        stub = _GitStub()
        monkeypatch.setattr(subprocess, "run", stub)
        cli_server._update()
        # The FULL remote-tracking ref: a short `origin/main` resolves a tag of
        # that name first, which a fetch auto-follows from the remote.
        assert ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"] in stub.calls
        assert not any(
            c[:3] == ["git", "rev-parse", "--verify"] and "origin/main^{commit}" in c
            for c in stub.calls
        )
        assert ["git", "reset", "--hard", _PIN] in stub.calls
        assert not any("origin/main" in c for c in stub.calls if c[:2] == ["git", "reset"])
        assert any(c[:3] == ["git", "show", f"{_PIN}:pyproject.toml"] for c in stub.calls)
        rev_lists = [c for c in stub.calls if c[:2] == ["git", "rev-list"]]
        assert rev_lists and all(any(_PIN in arg for arg in c) for c in rev_lists)
        assert any(c[:2] == ["git", "diff"] and _PIN in c for c in stub.calls)

    def test_an_unresolvable_upstream_pin_exits_before_any_judgment(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub(rev_parse_verify=128)
        monkeypatch.setattr(subprocess, "run", stub)
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "Could not resolve origin/main" in capsys.readouterr().out
        assert not any(
            c[:2] in (["git", "diff"], ["git", "rev-list"], ["git", "reset"]) for c in stub.calls
        )

    def test_unreadable_divergence_refuses_the_reset(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """Fail closed: a guard that cannot count must not wave the reset through."""
        stub = _GitStub(rev_list=128)
        monkeypatch.setattr(subprocess, "run", stub)
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "Could not compare HEAD against origin/main" in capsys.readouterr().out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_local_changes_prompt_and_abort_leaves_tree_alone(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        stub.status_out = " M src/a.py\n?? scratch.txt\n"
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "will be discarded" in out
        assert " M src/a.py" in out
        assert "?? scratch.txt" not in out  # untracked files are preserved, not listed
        assert "Aborted." in out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_local_changes_confirmed_proceeds_to_reset(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        stub.status_out = " M src/a.py\n"
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr("builtins.input", lambda prompt="": "Y")
        cli_server._update()
        assert any(c[:2] == ["git", "reset"] for c in stub.calls)
        assert "Kiro Crew updated!" in capsys.readouterr().out

    def test_reset_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(reset=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git reset failed" in capsys.readouterr().out

    def test_pip_install_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(pip=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        # pip's own reason reaches the console, not just the exit code.
        assert "wheel error" in capsys.readouterr().out

    def test_success_updates_kiro_cli_and_refreshes_agent_config(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/usr/bin/kiro-cli")
        built: list[Path] = []
        monkeypatch.setattr(cli_server, "build_frontend_sync", lambda p: built.append(p))
        cli_server._update()
        out = capsys.readouterr().out
        assert "Kiro Crew updated!" in out
        assert "Agent config refreshed" in out
        assert built == [git_checkout]
        assert ["/usr/bin/kiro-cli", "update"] in stub.calls
        assert any("setup" in c for c in stub.calls)

    def test_agent_config_refresh_failure_only_warns(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(setup=1))
        cli_server._update()  # non-fatal
        out = capsys.readouterr().out
        assert "Kiro Crew updated!" in out
        assert "Agent config refresh failed" in out

    def test_success_pokes_the_running_gateway_to_revalidate(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """A completed git update reconciles the running gateway's badge.

        The fixture neutralizes the poke by default; this row restores a spy so
        the success path is shown to reach it.
        """
        monkeypatch.setattr(subprocess, "run", _GitStub())
        poked: list[bool] = []
        monkeypatch.setattr(
            cli_server, "_revalidate_gateway_update_check", lambda: poked.append(True)
        )
        cli_server._update()
        assert "Kiro Crew updated!" in capsys.readouterr().out
        assert poked == [True], "the completed update never poked the gateway"


class TestUpdateGatewayPoke:
    """``_revalidate_gateway_update_check`` — best-effort, never fatal.

    The update has already succeeded by the time this runs, so every branch here
    proves the same contract from a different angle: it may print, but it must
    never raise and never change the exit code.
    """

    @staticmethod
    def _resp(status: int = 200):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        r = _Resp()
        r.status = status
        return r

    def test_success_reports_the_refresh(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8674)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda port, dial_host: "s3cret")
        captured: dict = {}

        def _send(req, port, timeout, *, operation):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["secret"] = req.headers.get("X-local-secret")
            captured["port"] = port
            captured["operation"] = operation
            return self._resp()

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _send)
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: pytest.fail("unverified send")
        )
        cli_server._revalidate_gateway_update_check()
        assert "Update badge refreshed" in capsys.readouterr().out
        # Pin the wire contract: dropping the secret header or retargeting the
        # URL/method must fail this test, not slip through green.
        assert captured["url"] == "http://127.0.0.1:8674/api/update/revalidate"
        assert captured["method"] == "POST"
        assert captured["secret"] == "s3cret"
        assert captured["port"] == 8674
        assert captured["operation"] == "gateway_update_revalidate"

    def test_owned_port_with_a_foreign_connected_peer_sends_no_secret(
        self, monkeypatch, capsys, tmp_path, sel_rec
    ) -> None:
        """The port check passing is not enough: the CONNECTED peer must be ours.

        The attest-then-send race: the gateway dies after a port-owner
        check and a co-resident listener binds the freed port before the send
        connects. The proof must run on the connection that carries the secret,
        so a foreign peer on that connection is refused before any byte.
        """
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8674)
        monkeypatch.setattr(cli_server, "_gateway_owns_port", lambda port: True, raising=False)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda port, dial_host: "s3cret")
        monkeypatch.setattr(
            cli_server, "dashboard_socket_path", lambda _p: tmp_path / "absent.sock"
        )
        monkeypatch.setattr(
            cli_server,
            "_connected_peer_verdict",
            lambda _s: cli_server._ConnectedPeerVerdict.MISMATCH,
        )
        sent: list[object] = []

        def _tcp(req, timeout, *, verify_peer):
            verify_peer(object())  # runs on the connected socket, before any byte
            sent.append(req)
            return self._resp()

        monkeypatch.setattr(cli_server, "tcp_verified_urlopen", _tcp)
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: sent.append(a))
        cli_server._revalidate_gateway_update_check()  # must not raise
        assert sent == [], "sent the secret to an unproven connected peer"
        assert "reconciles on next check" in capsys.readouterr().out
        denied = [c for c in sel_rec.calls if c.get("outcome") == "denied"]
        assert denied and denied[-1]["operation"] == "gateway_update_revalidate"

    def test_no_gateway_running_is_silent_success(self, monkeypatch, capsys) -> None:
        """No secret means no gateway to reach; the next boot re-checks anyway."""
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8674)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda port, dial_host: "")
        called: list[bool] = []
        monkeypatch.setattr(
            cli_server, "_owner_verified_secret_urlopen", lambda *a, **k: called.append(True)
        )
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: called.append(True))
        cli_server._revalidate_gateway_update_check()  # must not raise
        assert called == [], "attempted a call with no secret to authenticate it"

    def test_transport_failure_is_swallowed(self, monkeypatch, capsys) -> None:
        """A gateway that refuses the connection must not fail the update."""
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8674)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda port, dial_host: "s3cret")

        def _boom(req, port, timeout, *, operation):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _boom)
        cli_server._revalidate_gateway_update_check()  # must not raise
        assert "reconciles on next check" in capsys.readouterr().out

    def test_peer_refusal_is_swallowed(self, monkeypatch, capsys) -> None:
        """A refused peer is a best-effort miss, not a crash: the update stands."""
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8674)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda port, dial_host: "s3cret")

        def _refuse(req, port, timeout, *, operation):
            raise cli_server._GatewayPeerRefused("peer not proven")

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _refuse)
        cli_server._revalidate_gateway_update_check()  # must not raise
        assert "reconciles on next check" in capsys.readouterr().out


class TestUpdateSubprocessHardening:
    """The six ``subprocess.run`` calls in ``_update()``.

    Two gap classes: (a) every captured-output call must also pass
    ``stdin=subprocess.DEVNULL`` so a child prompt can never block invisibly on
    the parent terminal; (b) ``TimeoutExpired`` — which ``subprocess.run``
    RAISES rather than returns — must land on the same branch that the site's
    existing failure path already takes, never escape ``_update()``.
    """

    @staticmethod
    def _timeout_on(prefix, inner):
        """A subprocess.run stand-in raising TimeoutExpired for one argv prefix."""

        def _run(argv, **kw):
            if list(argv[: len(prefix)]) == list(prefix):
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 0))
            return inner(argv, **kw)

        return _run

    def test_all_six_calls_pass_stdin_devnull(self, monkeypatch, git_checkout, capsys) -> None:
        """Gap class (a): assert on the kwargs actually passed to subprocess.run."""
        stub = _GitStub()
        recorded: list[tuple[list[str], dict]] = []

        def _run(argv, **kw):
            recorded.append((list(argv), kw))
            return stub(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _run)
        # A pinned kiro-cli makes the sixth (best-effort) site reachable.
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/usr/bin/kiro-cli")
        cli_server._update()
        assert "Kiro Crew updated!" in capsys.readouterr().out

        six = [
            ["git", "rev-parse"],
            ["git", "fetch"],
            ["git", "diff"],
            ["git", "status"],
            ["git", "reset"],
            ["/usr/bin/kiro-cli", "update"],
        ]
        for prefix in six:
            matching = [kw for argv, kw in recorded if argv[: len(prefix)] == prefix]
            assert matching, f"call {prefix} never ran"
            for kw in matching:
                assert kw.get("stdin") is subprocess.DEVNULL, f"{prefix} ran without stdin=DEVNULL"

    def test_fetch_timeout_takes_the_existing_failure_branch(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """Gap class (b), exit-1 sites: a timeout must not traceback out.

        RED-BEFORE: on unmodified main this raises TimeoutExpired straight
        through ``kirocrew update`` instead of the SystemExit(1) the fetch
        failure path already defines.
        """
        monkeypatch.setattr(subprocess, "run", self._timeout_on(["git", "fetch"], _GitStub()))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git fetch timed out" in capsys.readouterr().out

    def test_diff_timeout_proceeds_like_a_nonzero_exit(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """A diff timeout takes the branch a non-zero exit already takes —
        proceed to the divergence guard — rather than tracing back or lying
        "up to date"."""
        stub = _GitStub()
        monkeypatch.setattr(subprocess, "run", self._timeout_on(["git", "diff"], stub))
        cli_server._update()
        out = capsys.readouterr().out
        assert "git diff timed out" in out
        # The update still completed through the guard + reset path.
        assert "Kiro Crew updated!" in out
        assert any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_status_timeout_refuses_the_reset(self, monkeypatch, git_checkout, capsys) -> None:
        """The status call guards the hard reset's data discard: an unreadable
        answer fails closed, mirroring the unreadable-divergence guard."""
        stub = _GitStub()
        monkeypatch.setattr(subprocess, "run", self._timeout_on(["git", "status"], stub))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git status timed out" in capsys.readouterr().out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_kiro_cli_update_timeout_degrades_best_effort(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        """The backend update's result is not inspected today, so its timeout
        warns and the update continues."""
        stub = _GitStub()
        monkeypatch.setattr(
            subprocess, "run", self._timeout_on(["/usr/bin/kiro-cli", "update"], stub)
        )
        monkeypatch.setattr(kiro_cli, "resolve_kiro_cli", lambda **kw: "/usr/bin/kiro-cli")
        cli_server._update()
        out = capsys.readouterr().out
        assert "kiro-cli update timed out" in out
        assert "Kiro Crew updated!" in out


class TestUpdateWheelDispatch:
    """No git checkout means the wheel path gets a correctly shaped layout."""

    def test_layout_is_built_from_the_distribution(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        # Both bindings: the capability derives WHO owns the update from the
        # stamp, and cli_server names the layout kind from its own import.
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "wheel")
        seen: list[InstallLayout] = []
        monkeypatch.setattr(cli_server, "_update_wheel", lambda layout: seen.append(layout))
        cli_server._update()
        assert len(seen) == 1
        assert seen[0].kind == "wheel"
        assert seen[0].is_git is False
        assert seen[0].is_externally_managed is False

    def test_unknown_distribution_defaults_to_wheel(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "")
        seen: list[InstallLayout] = []
        monkeypatch.setattr(cli_server, "_update_wheel", lambda layout: seen.append(layout))
        cli_server._update()
        assert seen[0].kind == "wheel"


# --------------------------------------------------------------------------
# _update_wheel — feed validation and installer failures
# --------------------------------------------------------------------------


_LAYOUT = InstallLayout(
    kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
)


@pytest.fixture
def wheel_feed(monkeypatch):
    """Pin the CDN bases, channel, and installer command; return a payload setter."""
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.cdn_bases",
        lambda: ("https://cdn.example.com", "https://cdn.example.com"),
    )
    monkeypatch.setattr("kiro_crew.platform.update_layout.release_channel", lambda: "stable")
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.wheel_update_command",
        lambda channel=None: "curl -fsSL https://cdn.example.com/cli.sh | sh",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )

    def _no_network(*a, **k):
        raise AssertionError("the feed must not be fetched on this path")

    # Default-deny so a validation branch that is supposed to exit BEFORE the
    # fetch can never reach the real CDN from a test runner.
    monkeypatch.setattr("urllib.request.urlopen", _no_network)

    def _serve(payload: bytes) -> None:
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(payload))

    return _serve


class TestUpdateWheelFeedValidation:
    """A hostile or broken feed must never reach the installer."""

    def test_pinned_feed_base_blocks_the_update(self, monkeypatch, wheel_feed, capsys) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "source pinned to an internal mirror",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "internal mirror" in capsys.readouterr().out

    def test_pinned_artifact_base_blocks_even_when_the_feed_is_allowed(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases",
            lambda: ("https://feed.example.com", "https://artifacts.example.com"),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "" if "feed" in url else "artifact host not allowed",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "artifact host not allowed" in capsys.readouterr().out

    def test_cdn_base_with_shell_metacharacters_is_refused(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases",
            lambda: ("https://cdn.example.com;id", "https://cdn.example.com"),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "disallowed characters" in capsys.readouterr().out

    def test_oversized_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"x" * (65536 + 10))
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "too large" in capsys.readouterr().out

    def test_non_json_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"<html>404</html>")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "not valid JSON" in capsys.readouterr().out

    def test_json_array_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"[]")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "unexpected format" in capsys.readouterr().out

    def test_channel_mismatch_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "insider", "version": "9.9.9"}'
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "Feed channel mismatch" in capsys.readouterr().out

    def test_a_non_string_version_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable", '
            b'"version": 999}'
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "No version in release feed" in capsys.readouterr().out

    def test_missing_version_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b'{"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable"}')
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "No version in release feed" in capsys.readouterr().out


class _FakeInstaller:
    """A ``subprocess.Popen`` stand-in for the CLI's installer spawn.

    ``pid`` is above every platform's ``pid_max`` (see
    ``test_update_provider._UNALLOCATABLE_PID``), so a stop path that reached
    the real kill helpers could not signal a live process.
    """

    pid = 99_999_999_999

    def __init__(self, argv, *, returncode: int = 0, wait_raises=None, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self._returncode = returncode
        self._wait_raises = wait_raises

    def wait(self, timeout=None):
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._returncode

    def poll(self):
        return self._returncode


def _installer(monkeypatch, **behaviour) -> list[_FakeInstaller]:
    spawned: list[_FakeInstaller] = []

    def _popen(argv, **kwargs):
        proc = _FakeInstaller(argv, **behaviour, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return spawned


class TestUpdateWheelInstaller:
    """Once the feed is trusted, the installer's failure modes stay actionable."""

    @pytest.fixture(autouse=True)
    def _newer_feed(self, wheel_feed, monkeypatch):
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "stable", "version": "999.0.0"}'
        )
        monkeypatch.setattr(sys, "platform", "linux")
        # These tests exercise the installer re-run, which is the path a pipx
        # install takes. A plain-pip install refuses before it (covered in
        # test_update_wheel_dispatch.py), so pin the pipx shape here.
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: False)
        monkeypatch.setattr(wheel_engine, "running_from_pipx", lambda: True)
        # The host's real AppArmor state is never read; a test that needs the
        # re-attach branch sets it itself.
        monkeypatch.setattr(wheel_apply, "userns_reattach_needed", lambda _version: False)

    def test_windows_refuses_the_posix_installer(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "not supported on Windows" in out
        assert "cli.sh | sh" in out  # manual command still printed

    def test_missing_sh_prints_the_manual_command(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise FileNotFoundError("sh")

        monkeypatch.setattr(subprocess, "Popen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "'sh' not found" in out
        assert "cli.sh | sh" in out

    def test_installer_timeout_prints_the_manual_command(self, monkeypatch, capsys) -> None:
        spawned = _installer(
            monkeypatch, wait_raises=subprocess.TimeoutExpired(cmd="sh", timeout=300)
        )
        stops: list[tuple] = []
        monkeypatch.setattr(
            cli_server.platform_compat,
            "terminate_and_reap_sync",
            lambda proc, **kw: stops.append((proc, kw)),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "timed out" in capsys.readouterr().out
        # Stopped gracefully (SIGTERM + rollback grace), never killed outright.
        assert [proc for proc, _ in stops] == spawned
        assert stops[0][1]["grace"] > 0

    def test_installer_runs_in_its_own_session(self, monkeypatch) -> None:
        # One group to stop, never the CLI's own.
        spawned = _installer(monkeypatch)
        cli_server._update_wheel(_LAYOUT)
        assert spawned[0].argv[:2] == ["sh", "-c"]
        assert spawned[0].kwargs.get("start_new_session") is True

    def test_ctrl_c_stops_the_installer_gracefully(self, monkeypatch) -> None:
        # The installer is outside the terminal's foreground group, so Ctrl-C
        # reaches only the CLI, which must pass the stop on.
        spawned = _installer(monkeypatch, wait_raises=KeyboardInterrupt())
        stops: list = []
        monkeypatch.setattr(
            cli_server.platform_compat,
            "terminate_and_reap_sync",
            lambda proc, **kw: stops.append(proc),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 130
        assert stops == spawned

    @pytest.mark.parametrize(
        ("wait_raises", "code", "shown"),
        [
            (subprocess.TimeoutExpired(cmd="sh", timeout=300), 1, "Try running manually"),
            (KeyboardInterrupt(), 130, "Stopping the installer"),
        ],
    )
    def test_a_second_ctrl_c_during_the_stop_still_exits_cleanly(
        self, monkeypatch, capsys, wait_raises, code, shown
    ) -> None:
        # terminate_and_reap_sync re-raises a Ctrl-C it absorbed mid-stop; the
        # arm must still reach its own exit code, not a traceback.
        _installer(monkeypatch, wait_raises=wait_raises)

        def stop_then_reraise(proc, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            cli_server.platform_compat, "terminate_and_reap_sync", stop_then_reraise
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == code
        assert shown in capsys.readouterr().out

    def test_installer_nonzero_exit_is_surfaced(self, monkeypatch, capsys) -> None:
        _installer(monkeypatch, returncode=17)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "exited with code 17" in capsys.readouterr().out

    def test_shadow_failure_redacts_credentialed_urls(self, monkeypatch, capsys) -> None:
        """A failed shadow update must not print URL credentials.

        The failure text quotes the URL the engine tried, and the fallback
        installer command embeds the CDN base — with a token-bearing
        KIROCREW_CDN_BASE either would land credentials in terminal history.
        """
        from kiro_crew.platform import wheel_engine
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        # _update_wheel imports these from wheel_engine lazily (kept off the
        # gateway boot path), so patch the SOURCE module, not cli_server —
        # the function-local `from wheel_engine import ...` resolves to the
        # patched value.
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)

        def _boom(**kw):
            raise WheelUpdateError(
                "could not fetch https://user:tok-SECRET99@cdn.example.com/w.whl: boom"
            )

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _boom)
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.wheel_update_command",
            lambda channel=None: (
                "curl -fsSL https://user:tok-SECRET99@cdn.example.com/cli.sh | sh"
            ),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "tok-SECRET99" not in out
        assert "was not modified" in out

    def test_shadow_oserror_takes_the_failure_path_not_a_traceback(
        self, monkeypatch, capsys
    ) -> None:
        """A raw OSError from the staging filesystem must not escape as a traceback.

        The engine wraps its own I/O failures in WheelUpdateError, but a full
        or unwritable disk at mkdir/tempdir time raises OSError outside those
        conversion sites — the CLI must route it through the same
        operator-facing failure path (redacted message + fallback command +
        exit 1).
        """
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)

        def _boom(**kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "No space left on device" in out
        assert "was not modified" in out

    def test_shadow_success_snapshots_before_promotion_and_names_the_profile_remedy(
        self, monkeypatch, capsys
    ) -> None:
        """The managed venv takes the shared path: the memory snapshot runs as the
        engine's last step before promotion, and a promotion that moves the launcher
        a userns AppArmor profile applies to prints how to re-attach it."""
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(wheel_apply, "userns_reattach_needed", lambda _version: True)
        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", lambda: (3, ""))
        early = []
        monkeypatch.setattr(cli_server, "_snapshot_memory_or_exit", lambda: early.append(True))
        seen: dict[str, object] = {}

        def _apply(**kw):
            seen.update(kw)
            kw["before_promote"]()
            return Path("/x/crew-venv-999.0.0")

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _apply)
        cli_server._update_wheel(_LAYOUT)
        out = capsys.readouterr().out
        assert early == [], "the snapshot is the engine's step, not a separate one"
        assert "Memory snapshot: 3 store(s) copied" in out
        assert "kirocrew service install" in out
        # Said after the update finished: it is installed, and only the re-attach is left.
        assert "999.0.0 is installed" in out
        assert "Run `kirocrew update`" not in out

    def test_a_feed_version_outside_the_release_grammar_is_an_update_failure(
        self, wheel_feed, monkeypatch, capsys, tmp_path
    ) -> None:
        """An unsigned feed version that cannot name a tree is refused by the REAL
        engine as an update failure: an exit, never a traceback, nothing built."""
        from kiro_crew.platform import wheel_engine

        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "stable", "version": "999/0"}'
        )
        layout = wheel_engine.ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
        monkeypatch.setattr(wheel_engine, "hold_update_lock", lambda: -1)
        monkeypatch.setattr(wheel_engine, "release_update_lock", lambda _fd: None)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "fails validation" in out
        assert "was not modified" in out
        assert list(tmp_path.iterdir()) == []

    def test_a_reattach_probe_that_refuses_the_version_is_an_update_failure(
        self, monkeypatch, capsys
    ) -> None:
        """The AppArmor probe names the promoted tree too, so it runs inside the
        failure handling rather than ahead of it."""
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)

        def _refuse(_version: str) -> bool:
            raise wheel_engine.WheelUpdateError("release version '999/0' fails validation")

        monkeypatch.setattr(wheel_apply, "userns_reattach_needed", _refuse)
        monkeypatch.setattr(
            wheel_engine,
            "apply_wheel_update",
            lambda **kw: pytest.fail("nothing is applied after a refusal"),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "fails validation" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("exc_name", "message"),
        [
            ("WheelUpdateBusy", "another kirocrew update is already in progress"),
            ("WheelUpdateSnapshotFailed", "Pre-update memory snapshot failed: 1 store(s)"),
        ],
    )
    def test_shadow_refusals_never_offer_the_installer_rerun(
        self, monkeypatch, capsys, exc_name, message
    ) -> None:
        """The installer re-run would race the other update's promotion (busy), or
        perform exactly the un-copied update the snapshot refusal stops."""
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)

        def _refuse(**kw):
            raise getattr(wheel_engine, exc_name)(message)

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _refuse)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert message in out
        assert "re-running the installer" not in out

    def test_an_incompatible_release_names_the_installer_rerun(self, monkeypatch, capsys) -> None:
        """Only the installer moves an install onto a Python the release accepts."""
        from kiro_crew.platform import wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)

        def _refuse(**kw):
            raise wheel_engine.WheelUpdateIncompatible(
                "kirocrew 999.0.0 requires Python >= 3.99", version="999.0.0", sha256="a" * 64
            )

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _refuse)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "requires Python >= 3.99" in out
        assert "re-run the installer" in out.lower() and "cli.sh | sh" in out
        assert "was not modified" not in out

    def test_shadow_children_get_this_shells_environment_and_memory_is_checked_first(
        self, monkeypatch, capsys
    ) -> None:
        from kiro_crew.platform import wheel_apply, wheel_engine

        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        seen: dict[str, object] = {}

        def _apply(**kw):
            seen.update(kw)
            return Path("/x/crew-venv-999.0.0")

        monkeypatch.setattr(wheel_engine, "apply_wheel_update", _apply)
        cli_server._update_wheel(_LAYOUT)
        assert seen["trusted_env"] is False, "a toolchain on the operator's PATH reaches pip"
        assert seen["preflight"] is wheel_apply.check_memory_ready

    def test_success_reports_the_new_version_and_restart_hint(self, monkeypatch, capsys) -> None:
        spawned = _installer(monkeypatch)
        cli_server._update_wheel(_LAYOUT)
        out = capsys.readouterr().out
        assert "updated to 999.0.0" in out
        assert "kirocrew restart" in out
        assert spawned[0].argv[:2] == ["sh", "-c"]

    def test_unparseable_remote_version_updates_anyway(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        """``_is_newer`` returning None must fail OPEN — an update is safer than a stall."""
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "stable", "version": "not-a-version"}'
        )
        _installer(monkeypatch)
        cli_server._update_wheel(_LAYOUT)
        out = capsys.readouterr().out
        assert "Could not compare versions" in out
        assert "updated to not-a-version" in out

    def test_already_latest_returns_without_running_the_installer(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        from kiro_crew import __version__ as local_version

        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable", '
            + f'"version": "{local_version}"'.encode()
            + b"}"
        )

        def unreachable(*a, **k):  # pragma: no cover - proves the early return
            raise AssertionError("installer must not run when already current")

        monkeypatch.setattr(subprocess, "run", unreachable)
        cli_server._update_wheel(_LAYOUT)
        assert "Already on the latest version" in capsys.readouterr().out


class TestStatusCountLines:
    """`kirocrew status` renders the two cached counts `/api/status` publishes,
    and an unknown count (``null`` while the shared cache has not refreshed)
    as a dash rather than ``None`` or a fabricated zero."""

    def test_known_counts_print_as_numbers(self) -> None:
        data = {"cron_jobs": 3, "lessons": 42}
        assert cli_server._format_count(data, "cron_jobs") == "3"
        assert cli_server._format_count(data, "lessons") == "42"

    def test_null_count_prints_a_dash_not_none(self) -> None:
        # Fails on the previous head, which printed ``Lessons: None`` for a
        # payload whose count was still unknown.
        data = {"cron_jobs": None, "lessons": None}
        assert cli_server._format_count(data, "cron_jobs") == "—"
        assert cli_server._format_count(data, "lessons") == "—"

    def test_missing_key_prints_a_dash_not_zero(self) -> None:
        assert cli_server._format_count({"uptime": "1h"}, "lessons") == "—"

    def test_status_reads_the_cron_jobs_key(self, monkeypatch, capsys) -> None:
        # Fails on the previous head, which read a ``crons`` key the snapshot
        # never emits and so always printed ``Cron jobs: 0``.
        import io
        import json
        from contextlib import contextmanager
        from types import SimpleNamespace

        payload = {"uptime": "1h", "cron_jobs": 7, "lessons": None}

        @contextmanager
        def _urlopen(url, timeout):
            yield io.BytesIO(json.dumps(payload).encode())

        monkeypatch.setattr(cli_server, "loopback_urlopen", _urlopen)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 7777)
        cli_server._status(SimpleNamespace(port=None))
        out = capsys.readouterr().out
        assert "Cron jobs:   7" in out
        assert "Lessons:     —" in out
        assert "None" not in out


class TestStatusMemoryLine:
    """`kirocrew status` prints the gateway RSS and the session ceiling from the
    two fields `/api/status` publishes for it."""

    def test_formats_rss_and_ceiling(self) -> None:
        line = cli_server._format_memory_line({"gateway_rss_mb": 412, "watchdog_rss_max_mb": 1536})
        assert line == "412 MiB rss, session ceiling 1536 MiB"

    def test_zero_ceiling_reads_as_disabled_not_as_a_bound(self) -> None:
        line = cli_server._format_memory_line({"gateway_rss_mb": 412, "watchdog_rss_max_mb": 0})
        assert "disabled" in line and "watchdog_rss_max_mb" in line
        assert "0 MiB" not in line

    def test_older_gateway_without_the_fields_prints_dashes(self) -> None:
        line = cli_server._format_memory_line({"uptime": "1h"})
        assert line == "— rss, session ceiling —"

    def test_status_prints_the_line(self, monkeypatch, capsys) -> None:
        import io
        import json
        from contextlib import contextmanager
        from types import SimpleNamespace

        payload = {"uptime": "1h", "gateway_rss_mb": 412, "watchdog_rss_max_mb": 1536}

        @contextmanager
        def _urlopen(url, timeout):
            yield io.BytesIO(json.dumps(payload).encode())

        monkeypatch.setattr(cli_server, "loopback_urlopen", _urlopen)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 7777)
        cli_server._status(SimpleNamespace(port=None))
        out = capsys.readouterr().out
        assert "Memory:      412 MiB rss, session ceiling 1536 MiB" in out


class TestApproveBusyVsDown:
    """The update-approve and file-delivery-approve CLI paths report a slow
    gateway as busy, and a refused connection as not running. Both carry a
    single-use nonce, so the busy line points at the dashboard, never a retry.
    """

    def test_update_approve_timeout_reports_busy(self, monkeypatch, capsys) -> None:
        from kiro_crew.platform import update_capability, update_stepup

        monkeypatch.setattr(
            update_capability,
            "derive_capability",
            lambda: types.SimpleNamespace(managed_by="source"),
        )
        pending = types.SimpleNamespace(
            version="1.2.3", channel="stable", expires_in=60, nonce="n" * 64
        )
        monkeypatch.setattr(update_stepup, "read_pending", lambda: pending)
        # resolve_client_port / read_local_secret / _owner_verified_secret_urlopen
        # are bound by more than one mirror-family member, so they are patched
        # on the facade (cli_server), which routes the setattr to the owner.
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 7777)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _p, **_kw: "")

        def _timeout(*a, **k):
            raise TimeoutError("timed out")

        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", _timeout)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_approve()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "busy" in out
        assert "is not running" not in out

    def test_file_delivery_approve_timeout_reports_busy(self, monkeypatch, capsys) -> None:
        from kiro_crew import file_delivery_consent
        from kiro_crew.dashboard import urls as dashboard_urls

        pending = types.SimpleNamespace(destination_class="owner", expires_in=60, nonce="n" * 64)
        monkeypatch.setattr(file_delivery_consent, "read_pending_grant", lambda: pending)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 7777)
        monkeypatch.setattr(cli_server, "read_local_secret", lambda _p, **_kw: "")
        # No usable unix socket path -> the TCP loopback transport is used, which
        # is the seam this stub raises a timeout at.
        monkeypatch.setattr(
            dashboard_urls, "dashboard_socket_path", lambda _p: (_ for _ in ()).throw(OSError())
        )

        def _timeout(*a, **k):
            raise TimeoutError("timed out")

        monkeypatch.setattr(cli_server, "loopback_urlopen", _timeout)
        with pytest.raises(SystemExit) as exc:
            cli_server._file_delivery_approve()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "busy" in out
        assert "is not running" not in out
