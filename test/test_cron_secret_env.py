"""Tests for operator-granted vault secrets on script/command crons.

Covers the grant validator, the code pin, fail-closed resolution, env
injection in both sandboxed runners, and the persistence-layer gate in
CronService (agent jobs refused, pin required, revoke clears).
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronService
from kiro_crew.cron_script import (
    _filter_grant_env,
    _secret_env_precheck,
    compute_secret_env_pin,
    delivery_fingerprint,
    run_command_sandboxed,
    run_script_sandboxed,
    validate_secret_env_grant,
)
from kiro_crew.secrets import SecretVault


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Cron writes require a nameable caller; these tests assume it."""


@pytest.fixture(autouse=True)
def cron_home(monkeypatch, tmp_path):
    """Point cron_script.config_dir at the per-test patched home.

    Same redirect as test_cron_script.py: scripts are written under
    ``<home>/.kirocrew/crons`` and the vault lives beside them, so the
    resolver, the pin computation, and the vault all agree on one root.

    Tests that do NOT patch ``Path.home`` still reach this resolver through
    the grant-pin key (every ``compute_secret_env_pin`` call), so those fall
    back to a per-test tmp dir — never the operator's real ``~/.kirocrew``,
    which this fixture must not create, read, or key. Returns the resolver
    callable so tests can plant script files where the resolver looks.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"

    def _dir() -> Path:
        home = Path.home()
        if home == real_home:
            return fallback
        return home / ".kirocrew"

    monkeypatch.setattr("kiro_crew.cron_script.config_dir", _dir)
    return _dir


def _make_script(tmp_path: Path, body: str, name: str = "job.py") -> Path:
    crons_dir = tmp_path / ".kirocrew" / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    script.write_text(body)
    return script


_GRANTEE_BODY = "def run(ctx): pass\n"
# The approval body every promote test sends: approval is refused unless it
# restates the request the approver saw — the displayed mapping, the request
# timestamp and the sha256 of the reviewed script source — so the fixture's
# defaults (``_request_pending``'s mapping and ts, the default script digest)
# ride along with ``approve_pending``. Tests whose pending request differs
# override the field(s) that differ.
_APPROVE = {
    "approve_pending": True,
    "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
    "expected_ts": 1.0,
    "expected_source_sha256": hashlib.sha256(_GRANTEE_BODY.encode()).hexdigest(),
}


def _grant_script(cron_home, body: str = _GRANTEE_BODY, name: str = "grantee.py") -> str:
    """Plant a script where cron_script's resolver looks; return its spec.

    For tests that do NOT patch ``Path.home`` (endpoint/store/MCP flows):
    grants apply only to script jobs, so every grant fixture needs a real,
    resolvable script file.
    """
    crons_dir = cron_home() / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    # Bytes, not text: the approval digest is over the bytes ON DISK, and a
    # text-mode write would land ``\r\n`` on Windows and break that equality.
    script.write_bytes(body.encode())
    return str(script) + ":run"


class TestValidateSecretEnvGrant:
    def test_valid_grant_passes(self):
        validate_secret_env_grant({"MY_SANDBOX_TOKEN": "slack-sandbox"})

    def test_lowercase_key_rejected(self):
        with pytest.raises(ValueError, match="valid env-var name"):
            validate_secret_env_grant({"my_token": "x"})

    @pytest.mark.parametrize(
        "key",
        [
            "PATH",
            "SLACK_BOT_TOKEN",  # _CRON_ENV_DENY member
            "KIROCREW_INTERNAL_SECRET",
            "KIROCREW_ANYTHING",
            "_KIROCREW_SECRET_FILE",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
        ],
    )
    def test_protected_names_rejected(self, key):
        # A leading-underscore name (_KIROCREW_*) fails the grammar check
        # first; the rest hit the protected-name check. Both refuse.
        with pytest.raises(ValueError, match="env-var name"):
            validate_secret_env_grant({key: "x"})

    @pytest.mark.parametrize(
        "name",
        [
            "",
            " padded ",
            "trailing ",
            "AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG",  # credential-shaped
            "https://evil.example/exfil?d=payload",  # URL-shaped
            "a" * 129,  # over the length cap
        ],
    )
    def test_bad_vault_name_rejected(self, name):
        """The vault-name half of a grant is agent-supplied and echoed to the
        owner view and approval errors — slug grammar keeps that channel from
        carrying credential-shaped or URL-shaped content."""
        with pytest.raises(ValueError, match="vault"):
            validate_secret_env_grant({"MY_TOKEN": name})

    def test_entry_cap_enforced(self):
        grant = {f"KEY_{i}": "v" for i in range(17)}
        with pytest.raises(ValueError, match="max 16"):
            validate_secret_env_grant(grant)


class TestComputeSecretEnvPin:
    def test_command_job_is_refused(self, cron_home):
        """A pin over command TEXT cannot cover helper-file bytes the command
        invokes, so command jobs cannot carry grants at all."""
        with pytest.raises(ValueError, match="script jobs"):
            compute_secret_env_pin("", "echo hi")

    def test_script_pin_tracks_body(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin1 = compute_secret_env_pin(spec, "")
            script.write_text("def run(ctx): return 1\n")
            pin2 = compute_secret_env_pin(spec, "")
        assert pin1 != pin2

    def test_neither_script_nor_command_raises(self):
        with pytest.raises(ValueError, match="script jobs"):
            compute_secret_env_pin("", "")


class TestSecretEnvPrecheck:
    def test_empty_grant_is_noop(self):
        resolved, err = _secret_env_precheck(None, "", command="echo hi")
        assert resolved == {} and err is None
        resolved, err = _secret_env_precheck({}, "", command="echo hi")
        assert resolved == {} and err is None

    def test_missing_pin_fails_closed(self):
        resolved, err = _secret_env_precheck({"T": "x"}, "", command="echo hi")
        assert resolved == {}
        assert err is not None and "re-approve" in err

    def test_changed_body_fails_closed(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"T": "x"})
            script.write_text("def run(ctx): return 1\n")
            body = script.read_bytes()
            resolved, err = _secret_env_precheck({"T": "x"}, pin, script=spec, script_body=body)
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_missing_vault_entry_fails_closed(self, tmp_path):
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"MY_TOKEN": "not-stored"})
            resolved, err = _secret_env_precheck(
                {"MY_TOKEN": "not-stored"}, pin, script=spec, script_body=script.read_bytes()
            )
        assert resolved == {}
        assert err is not None and "does not exist" in err
        # The vault secret NAME must not be echoed (CWE-117 discipline is
        # key-only messages); the env-var key is operator config and may be.
        assert "not-stored" not in err
        assert "MY_TOKEN" in err

    def test_resolves_from_vault(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-123")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"})
            resolved, err = _secret_env_precheck(
                {"MY_SANDBOX_TOKEN": "slack-sandbox"},
                pin,
                script=spec,
                script_body=script.read_bytes(),
            )
        assert err is None
        assert resolved == {"MY_SANDBOX_TOKEN": "xoxb-123"}


class TestRunCommandSandboxedSecretEnv:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_command_grant_fails_closed(self, tmp_path):
        """Grants apply to script jobs only; a command job carrying one means
        the store was edited outside the product — refuse the run entirely."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_command_sandboxed(
                "echo hi",
                timeout=30,
                secret_env={"MY_TOKEN": "slack-sandbox"},
                secret_env_pin="deadbeef" * 8,
            )
        assert result["status"] == "error"
        assert "script jobs" in result["output"]

    def test_no_grant_runs_normally(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_command_sandboxed('printf "%s" "${MY_TOKEN:-unset}"', timeout=30)
        assert result["status"] == "ok", result
        assert result["output"] == "unset"


class TestRunScriptSandboxedSecretEnv:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        # Simulate a live sandbox backend by returning a MODIFIED argv (so
        # the no-backend refusal does not fire) that still executes on every
        # platform: a benign -X flag inserted after the interpreter. A
        # /usr/bin/env prefix would not exist on Windows (WinError 2).
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv",
            lambda argv, **k: ([argv[0], "-X", "utf8", *argv[1:]], None),
        )

    def test_unsandboxed_host_refuses_granted_run(self, tmp_path, monkeypatch):
        """A wrap_argv that hands back the argv unmodified (no-backend host
        with the unsandboxed opt-in) loses the crons-dir hiding — a granted
        run must refuse rather than degrade its approved isolation."""
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pin
            )
        assert result["status"] == "error"
        assert "sandbox" in result["error"]

    def test_granted_run_uses_strict_sandbox_mode(self, tmp_path, monkeypatch):
        """A granted child runs under the STRICT sandbox profile AND an
        isolated interpreter (-I): the pin covers the script's own bytes,
        not code it chooses to load, and -I stops agent-planted
        sitecustomize/usercustomize hooks from running before the launcher
        and capturing the secrets it loads."""
        seen: dict[str, object] = {}

        def recording_wrap(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = list(argv)
            return ([argv[0], "-X", "utf8", *argv[1:]], None)

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", recording_wrap)
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            run_script_sandboxed(spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pin)
        assert seen.get("mode") == "strict"
        assert "-I" in seen.get("argv", [])
        # The mutable trees the pin does not cover are hidden at the sandbox
        # layer: the live crons/ dir AND the script's own parent directory
        # (the verified bytes travel over stdin, so the child never needs the
        # file — a sibling helper rewritten after approval must be
        # unreachable even if the script re-adds the dir to sys.path).
        hidden_dirs = list(seen.get("extra_hidden_dirs") or ())
        assert str(Path(str(script)).resolve().parent) in hidden_dirs

    def test_granted_launcher_lives_in_private_dir_and_strips_it(self, tmp_path, monkeypatch):
        """On Python 3.10 ``-I`` does not imply safe_path, so sys.path[0] is
        still the launcher's own directory. A launcher born in the shared
        temp dir would import an agent-planted ``json.py`` from there before
        reading the secrets payload. The granted launcher therefore lives in
        the private pinned dir AND its prelude strips that directory by value
        before the first stdlib import."""
        seen: dict[str, object] = {}

        def recording_wrap(argv, **kwargs):
            launcher = Path(argv[-1])
            seen["launcher_dir"] = str(launcher.parent)
            seen["prelude"] = launcher.read_text(encoding="utf-8").split("\n", 4)
            return ([argv[0], "-X", "utf8", *argv[1:]], None)

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", recording_wrap)
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pin
            )
        assert result["status"] == "ok", result
        launcher_dir = str(seen["launcher_dir"])
        assert Path(launcher_dir).name.startswith("kirocrew_cron_pin_")
        assert launcher_dir != tempfile.gettempdir()
        prelude = list(seen["prelude"])  # type: ignore[arg-type]
        assert prelude[0] == "import sys"
        # The strip names the launcher dir explicitly and runs before any
        # shadowable import; a positional sys.path[0] strip would drop a
        # stdlib entry on 3.11+, where -I prepends nothing.
        assert prelude[1].startswith("sys.path[:] = [p for p in sys.path if p not in ('', ")
        # The prelude embeds the directory as a Python literal, so match its
        # repr: on Windows the backslashes are escaped in the source text.
        assert repr(launcher_dir) in prelude[1]
        assert prelude[2].startswith("sys.path.insert(0, ")
        assert prelude[3] == "import base64, json, os, types"

    def test_pending_pin_copied_to_active_fields_fails_closed(self, tmp_path):
        """The domain-separation exploit: a store writer copies a valid
        PENDING pin into the active grant fields. It must not verify."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            pending_pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant=grant, domain="pending"
            )
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=pending_pin
            )
        assert result["status"] == "error"
        assert "code changed" in result["error"]

    def test_foreign_job_pin_fails_closed(self, tmp_path):
        """A pin minted for one job must not authorize another job's grant."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            other_pin = compute_secret_env_pin(spec, "", job_id="other-job", grant=grant)
            result = run_script_sandboxed(
                spec, "job1", timeout=120, secret_env=grant, secret_env_pin=other_pin
            )
        assert result["status"] == "error"

    def test_granted_keys_stripped_from_descendant_env(self, tmp_path):
        """The grant authorizes the approved script BODY, never the binaries
        it calls: the launcher seeds ``_GRANTED_ENV_KEYS`` with the granted
        names, so ``_clean_cron_env`` — the env every descendant subprocess
        (notably ``ctx.call_tool``'s MCP server) is built from — strips them,
        while the script's own ``os.environ`` still carries the value."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(
            tmp_path,
            "import os\n"
            "from kiro_crew.cron_script import Done, _clean_cron_env\n"
            "def run(ctx):\n"
            "    if 'MY_SANDBOX_TOKEN' in _clean_cron_env():\n"
            "        raise Done('LEAKED-TO-DESCENDANTS')\n"
            "    if os.environ.get('MY_SANDBOX_TOKEN') != 'xoxb-script':\n"
            "        raise Done('MISSING-FROM-SCRIPT')\n"
            "    raise Done('sealed')\n",
        )
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"}
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )
        assert result["status"] == "done", result
        assert result["message"] == "sealed"

    def test_granted_secret_reaches_script(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(
            tmp_path,
            "import os\n"
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx):\n"
            "    # Compare INSIDE the child: echoing the raw value back would\n"
            "    # be scrubbed from the Done message by the grant-value scrub.\n"
            "    ok = os.environ.get('MY_SANDBOX_TOKEN') == 'xoxb-script'\n"
            "    raise Done('delivered' if ok else 'MISSING')\n",
        )
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"}
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )
        assert result["status"] == "done", result
        assert result["message"] == "delivered"

    def test_body_rewrite_after_grant_fails_closed(self, tmp_path):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        canary = tmp_path / "exfil-canary"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "")
            # The agent-writeable script is swapped after the operator granted.
            script.write_text(
                "import os\n"
                "def run(ctx):\n"
                f"    open({str(canary)!r}, 'w').write(os.environ.get('MY_SANDBOX_TOKEN',''))\n"
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )
        assert result["status"] == "error"
        assert "code changed" in result["error"]
        # Fail closed means the payload never RAN, not merely that it errored.
        assert not canary.exists()


class TestCronServiceSecretEnvGate:
    def _service(self, tmp_path: Path) -> CronService:
        return CronService(base_dir=tmp_path / "crons-store")

    def _script_job(self, svc, cron_home, grant=None):
        spec = _grant_script(cron_home)
        job = svc.add_job("j", "m", every_secs=3600, script=spec)
        pin = compute_secret_env_pin(spec, "", "m", job_id=job.id, grant=grant, domain="active")
        return job, spec, pin

    def test_refused_approval_leaves_active_grant_valid(self, tmp_path, cron_home):
        """A refused grant operation must not invalidate the grant it did not
        replace: minting peeks the next epoch and commits only after the
        store swap, so the existing pin survives a 409."""
        from kiro_crew.cron_script import peek_grant_epoch

        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        # A refused operation minted-but-never-committed: peek alone must not
        # move the epoch the verifier reads.
        peek_grant_epoch(job.id)
        resolved_ok, err_ok = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert err_ok is None or "does not exist" in err_ok  # pin verified; vault may be empty

    def test_revoked_grant_cannot_be_replayed_from_store(self, tmp_path, cron_home):
        """The agent can write the cron store: a saved copy of a formerly
        valid mapping+pin written back after a revoke must not verify —
        the revoke bumped the grant epoch held in the agent-fenced vault."""
        from kiro_crew.cron_script import bump_grant_epoch

        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        # Owner revokes (the endpoint bumps the epoch alongside the clear).
        bump_grant_epoch(job.id)
        svc.update_job(job.id, secret_env={})
        # Agent replays the saved mapping+pin into the writable store.
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        resolved, err = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_rewired_session_key_kills_pin(self, tmp_path, cron_home):
        """The store is agent-writable: rewriting a granted job's
        ``session_key`` (which session receives the run's output) under a
        still-valid pin must fail the run closed — the pin binds delivery
        ownership, so steering an approved script's granted output to
        another session's surface requires re-approval."""
        svc = self._service(tmp_path)
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j", "m", every_secs=3600, script=spec, session_key="dashboard:chat-1-abc"
        )
        grant = {"MY_TOKEN": "slack-sandbox"}
        pin = compute_secret_env_pin(
            spec,
            "",
            "m",
            job_id=job.id,
            grant=grant,
            domain="active",
            delivery=delivery_fingerprint("dashboard:chat-1-abc"),
        )
        body = (cron_home() / "crons" / "grantee.py").read_bytes()
        # Agent rewires delivery to another session: the pin dies.
        resolved, err = _secret_env_precheck(
            grant,
            pin,
            script=spec,
            script_body=body,
            message="m",
            job_id=job.id,
            delivery=delivery_fingerprint("dashboard:chat-9-stolen"),
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_rewired_silent_flag_kills_pin(self, tmp_path, cron_home):
        """``silent`` decides WHETHER a run's Done/Report output is
        auto-delivered at all; it is agent-mutable via cron_update, so the
        pin binds it inside the delivery fingerprint — flipping it under a
        still-valid pin fails the run closed instead of surfacing
        secret-derived output the operator approved as silent."""
        svc = self._service(tmp_path)
        spec = _grant_script(cron_home)
        job = svc.add_job("j", "m", every_secs=3600, script=spec, silent=True)
        grant = {"MY_TOKEN": "slack-sandbox"}
        pin = compute_secret_env_pin(
            spec,
            "",
            "m",
            job_id=job.id,
            grant=grant,
            domain="active",
            delivery=delivery_fingerprint("", True),
        )
        body = (cron_home() / "crons" / "grantee.py").read_bytes()
        # Agent flips silent off: the fingerprint changes, the pin dies.
        resolved, err = _secret_env_precheck(
            grant,
            pin,
            script=spec,
            script_body=body,
            message="m",
            job_id=job.id,
            delivery=delivery_fingerprint("", False),
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_deleted_job_grant_cannot_be_replayed(self, tmp_path, cron_home):
        """Deleting a granted job kills its grant: every removal path bumps
        the grant epoch BEFORE the store swap, so an agent that re-creates
        the job from a saved record (the store is agent-writable) gets a pin
        minted under a dead epoch and the runner refuses it."""
        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        svc.remove_job(job.id, actor="owner", source="test")
        # Agent restores the saved record: same id, same mapping, same pin.
        resolved, err = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_cleared_then_deleted_grant_cannot_be_replayed(self, tmp_path, cron_home):
        """The removal bump must not key on the CURRENT grant fields alone:
        the store is agent-writable, so an agent can clear the fields,
        delete the job, and replay the saved mapping+pin into a re-created
        job. An id with a committed epoch entry bumps on removal regardless
        of what the record shows at delete time."""
        from kiro_crew.cron_script import commit_grant_epoch, peek_grant_epoch

        svc = self._service(tmp_path)
        spec = _grant_script(cron_home)
        job = svc.add_job("j", "m", every_secs=3600, script=spec)
        grant = {"MY_TOKEN": "slack-sandbox"}
        next_epoch = peek_grant_epoch(job.id)
        pin = compute_secret_env_pin(
            spec, "", "m", job_id=job.id, grant=grant, domain="active", epoch=next_epoch
        )
        svc.update_job(job.id, secret_env=grant, secret_env_pin=pin)
        assert commit_grant_epoch(job.id, next_epoch, expected_current=next_epoch - 1)
        # Agent clears the grant fields (store-level write, no bump), then
        # deletes the job — the bypass this test pins closed.
        svc.update_job(job.id, secret_env={})
        svc.remove_job(job.id, actor="owner", source="test")
        # Agent replays the saved record: same id, same mapping, same pin.
        resolved, err = _secret_env_precheck(
            grant,
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_corrupt_epoch_state_fails_closed(self, tmp_path, cron_home):
        """A lost/corrupt epoch counter must never quietly restart at 0 —
        that would revive pins an agent saved under low epochs. Verification
        refuses, and bump raises instead of rewriting a fresh map."""
        from kiro_crew.cron_script import bump_grant_epoch

        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        epochs = cron_home() / ".vault" / ".grant_epochs.json"
        epochs.parent.mkdir(parents=True, exist_ok=True)
        epochs.write_text("{ not json")
        resolved, err = _secret_env_precheck(
            {"MY_TOKEN": "slack-sandbox"},
            pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
        )
        assert resolved == {}
        assert err is not None and "corrupt" in err
        with pytest.raises(ValueError):
            bump_grant_epoch(job.id)

    def test_commit_grant_epoch_cas_refuses_stale(self, tmp_path, cron_home):
        """A commit whose peeked base was overtaken by a concurrent bump is
        refused — re-committing the bumped value would re-validate the very
        pin that bump meant to kill."""
        from kiro_crew.cron_script import bump_grant_epoch, commit_grant_epoch, peek_grant_epoch

        assert commit_grant_epoch("j-cas", peek_grant_epoch("j-cas"), expected_current=0) is True
        bump_grant_epoch("j-cas")  # concurrent revoke/removal -> 2
        assert commit_grant_epoch("j-cas", 2, expected_current=1) is False
        assert commit_grant_epoch("j-cas", 3, expected_current=2) is True

    def test_failed_epoch_bump_aborts_deletion(self, tmp_path, cron_home):
        """An unwritable epoch state must abort the delete: deleting while
        the old epoch is live would leave the saved grant record replayable
        the moment the epoch state heals."""
        svc = self._service(tmp_path)
        job, spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        with patch("kiro_crew.cron_script._write_grant_epochs", side_effect=OSError("read-only")):
            with pytest.raises(OSError):
                svc.remove_job(job.id, actor="owner", source="test")
        assert CronService(base_dir=svc._dir).get_job(job.id) is not None

    def test_failed_epoch_bump_on_run_completion_parks_and_requeues_the_one_shot(
        self, tmp_path, cron_home
    ):
        """A completed delete_after_run at-job whose epoch bump fails must not
        be saved back LIVE: the run path leaves ``enabled`` alone for such a
        job because the delete is what stops it, so a held delete with no
        persisted pause would re-fire the already-run job on every tick until
        the epoch state healed. It must be parked on disk and queued so the
        deferred drain retries the delete and removes it once the bump works."""
        import time

        svc = self._service(tmp_path)
        spec = _grant_script(cron_home)
        job = svc.add_job("once", "m", at_ts=time.time() - 60, script=spec, delete_after_run=True)
        pin = compute_secret_env_pin(
            spec, "", "m", job_id=job.id, grant={"MY_TOKEN": "slack-sandbox"}, domain="active"
        )
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin)
        ran = svc.get_job(job.id)
        assert ran is not None
        ran.last_status = "ok"
        ran.last_run_ts = time.time()

        with patch("kiro_crew.cron_script._write_grant_epochs", side_effect=OSError("read-only")):
            svc._merge_job_result(ran)

        # Still present (the delete was correctly held) ...
        reloaded = CronService(base_dir=svc._dir)
        on_disk = reloaded.get_job(job.id)
        assert on_disk is not None, "a held delete must not drop the grant record"
        # ... but PARKED, so a fresh reader's due-scan cannot re-fire it.
        assert on_disk.enabled is False
        assert not any(
            j.enabled and CronService._is_due(j, time.time()) for j in reloaded._jobs
        ), "a completed one-shot must never be due again while its delete is held"
        # And the retry intent survives for the deferred drain.
        assert job.id in svc._pending_removals

        # Once the epoch store heals, the drain finishes the delete.
        svc._sync()
        assert svc._drain_pending_removals_locked() == [job.id]
        assert CronService(base_dir=svc._dir).get_job(job.id) is None

    def test_epoch_bumps_are_atomic_across_threads(self, tmp_path, cron_home):
        """The epochs guard serializes read-modify-writes: overlapping bumps
        must never collapse into one (a collapsed bump revives revoked pins)."""
        import threading as _threading

        from kiro_crew.cron_script import _grant_epoch, bump_grant_epoch

        def worker() -> None:
            for _ in range(20):
                bump_grant_epoch("j-threads")

        threads = [_threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _grant_epoch("j-threads") == 60

    def test_grant_persists_and_round_trips(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "slack-sandbox"})
        updated = svc.update_job(
            job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin=pin
        )
        assert updated is not None
        assert updated.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert updated.secret_env_pin == pin
        # Round-trip through a fresh service instance (disk reload).
        svc2 = self._service(tmp_path)
        reloaded = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert reloaded.secret_env_pin == pin

    def test_agent_job_refused(self, tmp_path):
        svc = self._service(tmp_path)
        job = svc.add_job("j", "check the queue", every_secs=3600)
        with pytest.raises(ValueError, match="SCRIPT jobs"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"}, secret_env_pin="deadbeef")

    def test_command_job_refused(self, tmp_path):
        """A pin over command TEXT cannot cover helper-file bytes the command
        invokes, so the store refuses grants on command jobs outright."""
        svc = self._service(tmp_path)
        job = svc.add_job("j", "m", every_secs=3600, command="echo hi")
        with pytest.raises(ValueError, match="SCRIPT jobs"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"}, secret_env_pin="deadbeef")

    def test_grant_without_pin_refused(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, _pin = self._script_job(svc, cron_home)
        with pytest.raises(ValueError, match="secret_env_pin"):
            svc.update_job(job.id, secret_env={"MY_TOKEN": "x"})

    def test_protected_name_refused_at_persistence(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, _pin = self._script_job(svc, cron_home)
        with pytest.raises(ValueError, match="protected env-var name"):
            svc.update_job(
                job.id,
                secret_env={"SLACK_BOT_TOKEN": "x"},
                secret_env_pin="deadbeef",
            )

    def test_revoke_clears_pin(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "n"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "n"}, secret_env_pin=pin)
        revoked = svc.update_job(job.id, secret_env={})
        assert revoked is not None
        assert revoked.secret_env == {}
        assert revoked.secret_env_pin == ""

    def test_unrelated_update_leaves_grant_alone(self, tmp_path, cron_home):
        svc = self._service(tmp_path)
        job, _spec, pin = self._script_job(svc, cron_home, grant={"MY_TOKEN": "n"})
        svc.update_job(job.id, secret_env={"MY_TOKEN": "n"}, secret_env_pin=pin)
        updated = svc.update_job(job.id, name="renamed")
        assert updated is not None
        assert updated.secret_env == {"MY_TOKEN": "n"}
        assert updated.secret_env_pin == pin


class TestInjectionDefenseInDepth:
    def test_protected_keys_skipped_at_injection(self):
        """A hand-edited store could hold a protected key; the delivery
        filter drops it on every path (env preamble and stdin payload), so
        product-internal keys can never be named by a grant."""
        kept = _filter_grant_env({"_KIROCREW_SECRET_FILE": "evil", "OK_TOKEN": "fine"})
        assert kept == {"OK_TOKEN": "fine"}

    def test_forged_plain_hash_pin_is_rejected(self, tmp_path):
        """The pin is keyed HMAC: a store writer who recomputes the old plain
        sha256 shape (or any digest without the vault-fenced key) cannot mint
        a pin the runner accepts."""
        import hashlib

        script = _make_script(tmp_path, "def run(ctx): pass\n")
        spec = str(script) + ":run"
        forged = hashlib.sha256(
            b"script\x00" + spec.encode() + b"\x00" + script.read_bytes()
        ).hexdigest()
        with patch("pathlib.Path.home", return_value=tmp_path):
            resolved, err = _secret_env_precheck(
                {"T": "x"}, forged, script=spec, script_body=script.read_bytes()
            )
        assert resolved == {}
        assert err is not None and "code changed" in err

    def test_granted_script_cannot_import_live_crons_sibling(self, tmp_path):
        """The pin covers one approved body; a sibling module in the live,
        agent-writeable crons/ dir must not be importable at fire time."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            script = _make_script(
                tmp_path,
                "import helper\n"
                "from kiro_crew.cron_script import Done\n"
                "def run(ctx):\n"
                "    raise Done(helper.VALUE)\n",
            )
            _make_script(tmp_path, "VALUE = 'sibling ran'\n", name="helper.py")
            spec = str(script) + ":run"
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_TOKEN": "slack-sandbox"}
            )
            with patch(
                "kiro_crew.cron_script.wrap_argv",
                lambda argv, **k: ([argv[0], "-X", "utf8", *argv[1:]], None),
            ):
                SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-1")
                result = run_script_sandboxed(
                    spec,
                    "job1",
                    timeout=120,
                    secret_env={"MY_TOKEN": "slack-sandbox"},
                    secret_env_pin=pin,
                )
        # The sibling import fails (ModuleNotFoundError surfaces as the
        # script error), rather than the unpinned helper running with the
        # secret in env.
        assert result["status"] == "error", result
        assert "helper" in result.get("error", "")


class TestMcpSecretRequest:
    """The agent-reachable half: cron_secret_request writes PENDING only."""

    def _svc_and_job(self, cron_home=None, *, agent: bool = False):
        from kiro_crew.config.loader import config_dir as loader_config_dir

        svc = CronService(base_dir=loader_config_dir())
        spec = "" if agent else _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "m" if agent else "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        return svc, job

    def _vault(self):
        from kiro_crew.config.loader import config_dir as loader_config_dir

        return SecretVault(loader_config_dir())

    def test_request_records_pending_not_active(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert "PENDING" in out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        assert reloaded.secret_env_pending_pin
        # The active grant is untouched — the tool cannot grant.
        assert reloaded.secret_env == {}
        assert reloaded.secret_env_pin == ""

    def test_request_does_not_reveal_vault_name_existence(self, cron_home):
        """No existence probe on the agent surface: a request naming an
        unstored secret is accepted as pending exactly like a stored one, so
        an agent cannot enumerate the owner's vault names by guessing.
        Validation happens on the owner-only approval surfaces."""
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "never-stored"}},
        )
        assert not out.startswith("Error:")
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {"MY_TOKEN": "never-stored"}

    def test_request_refuses_agent_job(self):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(agent=True)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_request_refuses_protected_env_name(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"SLACK_BOT_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_empty_request_withdraws_pending(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        mcp_cron._call_tool(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        out = mcp_cron._call_tool("cron_secret_request", {"job_id": job.id, "secrets": {}})
        assert "Withdrew" in out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {}
        assert reloaded.secret_env_pending_pin == ""

    def test_cron_update_cannot_write_active_grant(self, cron_home):
        """The general-purpose MCP update tool must never carry the grant."""
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        mcp_cron._call_tool(
            "cron_update",
            {
                "job_id": job.id,
                "name": "renamed",
                "secret_env": {"MY_TOKEN": "slack-sandbox"},
                "secret_env_pin": "deadbeef",
            },
        )
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env == {}
        assert reloaded.secret_env_pin == ""


class TestGrantEndpointPendingFlow:
    """The operator half: approve/deny via the dashboard endpoint."""

    @pytest.fixture(autouse=True)
    def _owner_view(self, monkeypatch):
        """These tests exercise grant flow, not identity; grant is owner-only."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    def _app_and_svc(self):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())
        app = web.Application()
        app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc

    def _request_pending(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        pin = compute_secret_env_pin(
            spec,
            "",
            "args",
            job_id=job.id,
            grant={"MY_TOKEN": "slack-sandbox"},
            domain="pending",
            delivery=delivery_fingerprint("dashboard:conftest-slot"),
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=pin,
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.mark.asyncio
    async def test_approve_promotes_pending_to_active(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 200
        # The success echo goes through the owner-view redactor, same as the
        # Schedule listing; an ordinary slug name survives it unchanged.
        assert body["secret_env"] == {"MY_TOKEN": "slack-sandbox"}
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env_pin
        assert job.secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_approval_without_request_snapshot_is_refused(self, cron_home):
        """An approval that omits ``expected_secret_env`` or ``expected_ts``
        attests to nothing about WHICH pending request it blesses — an agent
        could re-issue the request between display and click. Both are
        required, so the omission is refused and nothing is promoted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        digest_only = {k: v for k, v in _APPROVE.items() if k != "expected_secret_env"}
        no_ts = {k: v for k, v in _APPROVE.items() if k != "expected_ts"}
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                for body in (digest_only, no_ts):
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=body)
                    out = await resp.json()
                    assert resp.status == 400, out
                    assert out["code"] == "invalid_secret_env"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_malformed_stored_grant_is_refused_not_a_500(self, cron_home):
        """The store is agent-writable and loaded verbatim: a non-mapping
        planted into ``secret_env`` must not make the owner's approval of a
        valid pending request raise out of ``dict(...)`` into a 500."""
        import json

        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        records = json.loads(svc._path.read_text(encoding="utf-8"))
        jobs = records["jobs"] if isinstance(records, dict) else records
        for rec in jobs:
            if rec["id"] == jid:
                rec["secret_env"] = ["not", "a", "mapping"]
        svc._path.write_text(json.dumps(records), encoding="utf-8")
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "malformed_grant_state"

    @pytest.mark.asyncio
    async def test_unknown_secret_error_does_not_echo_a_credential_shaped_name(self, cron_home):
        """The vault-name half of a grant is agent-authored and the slug grammar
        admits a bare access-key id, so the "unknown name" error must scrub it
        like every other agent string the dashboard echoes."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j", "args", every_secs=3600, script=spec, session_key="dashboard:conftest-slot"
        )
        shaped = "AKIAIOSFODNN7EXAMPLE"  # credential-shaped, slug-valid, not in the vault
        pin = compute_secret_env_pin(
            spec,
            "",
            "args",
            job_id=job.id,
            grant={"MY_TOKEN": shaped},
            domain="pending",
            delivery=delivery_fingerprint("dashboard:conftest-slot"),
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": shaped},
            secret_env_pending_pin=pin,
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{job.id}/secrets",
                    json={**_APPROVE, "expected_secret_env": {"MY_TOKEN": shaped}},
                )
                text = await resp.text()
        assert resp.status == 400
        assert '"unknown_secret"' in text
        assert shaped not in text

    @pytest.mark.asyncio
    async def test_approve_racing_bump_conflicts_and_kills_pin(self, cron_home):
        """An epoch bump landing between the approval's store swap and its
        commit (a racing revoke or job removal) must refuse the CAS commit,
        return 409, and leave the just-swapped pin DEAD — never re-commit
        the value the concurrent bump minted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron_script import _secret_env_precheck, bump_grant_epoch

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)

        real_update = svc.update_job_async

        async def raced(*args: object, **kwargs: object):
            result = await real_update(*args, **kwargs)
            bump_grant_epoch(jid)  # the concurrent removal/revoke wins the gap
            return result

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch.object(svc, "update_job_async", raced),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "grant_conflict"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env_pin
        resolved, err = _secret_env_precheck(
            job.secret_env,
            job.secret_env_pin,
            script=job.script,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message=job.message,
            job_id=jid,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    @pytest.mark.asyncio
    async def test_approve_epoch_commit_failure_keeps_pending(self, cron_home):
        """An unwritable epoch store must not strand the job: the compensating
        write restores BOTH the prior active grant (its pin is still valid —
        the failed commit never advanced the epoch) AND the consumed pending
        request, so the owner re-approves once storage heals — never a dead
        just-swapped pin standing in for the grant that worked before."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        pending_pin_before = CronService(base_dir=svc._dir).get_job(jid).secret_env_pending_pin
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_commit_failed"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        # The restore carries the original request pin, so the re-approval's
        # code-drift check still runs against what the agent actually asked.
        assert job.secret_env_pending_pin == pending_pin_before
        # FULL compensation: the prior active grant (none here) is restored —
        # the dead just-swapped pin does not survive as the active grant.
        assert job.secret_env == {}
        assert job.secret_env_pin == ""

    @pytest.mark.asyncio
    async def test_epoch_commit_failure_restores_prior_active_grant(self, cron_home):
        """Active grant A + pending B: a failed epoch commit must put A back —
        its pin is still valid (the epoch never advanced), so replacing it
        with B's dead pin would destroy a working grant for nothing."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("other-secret", "xoxb-2")
        jid = self._request_pending(svc, cron_home)
        # Plant prior active grant A directly in the store.
        svc.update_job(jid, secret_env={"OLD_TOKEN": "other-secret"}, secret_env_pin="pin-A")
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 503
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"OLD_TOKEN": "other-secret"}
        assert job.secret_env_pin == "pin-A"
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_consumes_pending_atomically(self, cron_home):
        """The promoting write and the pending-consumption are ONE store
        write: a window between them would let a concurrent deny (or second
        approval) pass its compare-and-swap against the already-promoted
        snapshot and misreport success against the new grant."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        calls: list[dict] = []
        real_update = svc.update_job_async

        async def recording_update(job_id, **kwargs):
            calls.append(dict(kwargs))
            return await real_update(job_id, **kwargs)

        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            with patch.object(svc, "update_job_async", side_effect=recording_update):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 200
        pending_writes = [c for c in calls if "secret_env_pending" in c]
        assert len(pending_writes) == 1
        assert "secret_env" in pending_writes[0]
        assert pending_writes[0]["secret_env_pending"] == {}
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_revoke_epoch_bump_failure_is_structured_503(self, cron_home):
        """Corrupt or unwritable epoch state must not crash a revoke into a
        500, and must NOT clear the grant either: without the bump a saved
        copy of the mapping+pin would verify again once the state heals, so
        the handler answers a structured 503 and leaves the store as-is
        (runs already refuse under unhealthy epoch state — fail closed)."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin="somepin")
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.bump_grant_epoch",
                side_effect=ValueError("grant-epoch state corrupt (non-integer epoch)"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{job.id}/secrets", json={"secret_env": {}})
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_bump_failed"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_refused_when_audit_unwritable(self, cron_home):
        """AUDIT-OR-DENY: a grant must never exist unaudited. An SEL store
        that cannot take the pre-mutation intent record refuses the approval
        with a structured 503 and NOTHING mutated — pending intact, no
        active grant."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            sel_mock.return_value.log_api_access.side_effect = OSError("read-only fs")
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "audit_unavailable"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_decision_audits_are_critical_writes(self, cron_home):
        """The default SEL path enqueues and swallows a filesystem failure, so
        the audit-or-deny refusal above only exists if the intent record is a
        ``critical`` (synchronous, re-raising) write — for approve, deny and
        revoke alike."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            log = sel_mock.return_value.log_api_access
            async with TestClient(TestServer(app)) as client:
                await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                jid2 = self._request_pending(svc, cron_home)
                await client.put(f"/api/crons/{jid2}/secrets", json=_APPROVE)
                await client.put(f"/api/crons/{jid2}/secrets", json={"secret_env": {}})
        # Only the pre-mutation INTENT records are gated; the approve path also
        # emits a best-effort terminal "allowed" event, which stays non-critical.
        ops = {
            c.kwargs["operation"]: c.kwargs.get("critical")
            for c in log.call_args_list
            if c.kwargs.get("outcome") == "invoked"
        }
        assert ops["cron.secret_request_denied"] is True
        assert ops["cron.secret_request_approved"] is True
        assert ops["cron.secret_revoke"] is True

    @pytest.mark.asyncio
    async def test_deny_and_revoke_refused_when_audit_unwritable(self, cron_home):
        """Privilege-reducing decisions take the same audit-or-deny gate: an
        unwritable SEL refuses the denial (request stays pending) and the
        revocation (grant stays active), each with 503 audit_unavailable."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        svc.update_job(jid, secret_env={"OLD": "jira-token"}, secret_env_pin="pin")
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            sel_mock.return_value.log_api_access.side_effect = OSError("read-only fs")
            async with TestClient(TestServer(app)) as client:
                deny = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                deny_body = await deny.json()
                revoke = await client.put(f"/api/crons/{jid}/secrets", json={"secret_env": {}})
                revoke_body = await revoke.json()
        assert (deny.status, deny_body["code"]) == (503, "audit_unavailable")
        assert (revoke.status, revoke_body["code"]) == (503, "audit_unavailable")
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env == {"OLD": "jira-token"}

    @pytest.mark.asyncio
    async def test_unreadable_store_is_structured_on_approve_and_deny(self, cron_home):
        """An EACCES/EIO store must answer both decisions with the structured
        non-retryable 409 (cron_store_unreadable), never a 500."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron import CronStoreUnreadable

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)

        async def unreadable(job_id, **kwargs):
            raise CronStoreUnreadable("crons.json unreadable: EACCES")

        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            with patch.object(svc, "update_job_async", side_effect=unreadable):
                async with TestClient(TestServer(app)) as client:
                    for decision in (_APPROVE, {"deny_pending": True}):
                        resp = await client.put(f"/api/crons/{jid}/secrets", json=decision)
                        body = await resp.json()
                        assert resp.status == 409, (decision, body)
                        assert body["code"] == "cron_store_unreadable"
                        assert body["retryable"] is False

    def test_owner_view_grant_map_is_redacted(self):
        """The store is agent-writable and the read path loads grant dicts
        verbatim, so the owner-view serializer must scan keys AND values —
        a mapping planted directly in crons.json with credential- or
        exfil-URL-shaped content must not reach the dashboard raw."""
        from kiro_crew.dashboard.handlers.cron import _redacted_grant_map

        out = _redacted_grant_map({"AWS_KEY": "https://evil.example/exfil?d=AKIAIOSFODNN7EXAMPLE"})
        assert out is not None
        serialized = str(out)
        assert "AKIAIOSFODNN7EXAMPLE" not in serialized
        assert _redacted_grant_map({}) is None
        # A benign product-written mapping passes through unchanged.
        assert _redacted_grant_map({"MY_TOKEN": "slack-sandbox"}) == {"MY_TOKEN": "slack-sandbox"}
        # Malformed agent-planted shapes serialize as None, never a 500.
        assert _redacted_grant_map([]) is None  # type: ignore[arg-type]
        assert _redacted_grant_map("junk") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_epoch_commit_and_compensation_double_fault_is_loud(self, cron_home):
        """When the epoch commit fails AND every compensation attempt fails
        too (store went bad between the writes), the handler must answer a
        structured 503 that says the prior grant was NOT restored — never
        pretend the compensation landed or crash with a 500."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron import CronStoreUnreadable

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        real_update = svc.update_job_async
        calls = {"n": 0}

        async def first_write_only(job_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_update(job_id, **kwargs)
            raise CronStoreUnreadable("crons.json unreadable: EIO")

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
            patch.object(svc, "update_job_async", side_effect=first_write_only),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_commit_failed"
        assert "restore failed" in body["error"]

    @pytest.mark.asyncio
    async def test_approve_refuses_when_code_changed_after_request(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        # A pending request whose pin was taken over DIFFERENT code than the
        # job now carries — the shape a post-request script rewrite produces.
        spec = _grant_script(cron_home, body="def run(ctx): return 'current'\n")
        other_spec = _grant_script(
            cron_home, body="def run(ctx): return 'requested'\n", name="other.py"
        )
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                other_spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{job.id}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "code_changed"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None and reloaded.secret_env == {}

    @pytest.mark.asyncio
    async def test_deny_with_stale_expected_refuses(self, cron_home):
        """A denial restating request A must not clear a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        job_spec = str(cron_home() / "crons" / "grantee.py") + ":run"
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "other-secret"},
            secret_env_pending_pin=compute_secret_env_pin(
                job_spec,
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "other-secret"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env_pending == {"MY_TOKEN": "other-secret"}

    @pytest.mark.asyncio
    async def test_deny_clears_pending(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json={"deny_pending": True})
        assert resp.status == 200
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {}
        assert job.secret_env == {}

    @pytest.mark.asyncio
    async def test_compensation_never_resurrects_concurrent_revoke(self, cron_home):
        """Epoch commit fails AND a concurrent revoke cleared the grant in
        the gap: the compensating restore must yield to the revocation — the
        active-field compare-and-swap sees the fields no longer hold the
        just-promoted grant and skips, never writing grant A back over what
        the operator withdrew."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("other-secret", "xoxb-2")
        jid = self._request_pending(svc, cron_home)
        svc.update_job(jid, secret_env={"OLD_TOKEN": "other-secret"}, secret_env_pin="pin-A")

        def failing_commit(job_id, next_epoch, expected_current):
            # Simulate the concurrent revoke landing in the exact gap: the
            # store clears the active fields BEFORE the handler's
            # compensation runs, then the commit itself fails.
            svc.update_job(jid, secret_env={}, secret_env_pin="")
            raise OSError("no space left on device")

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=failing_commit,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 503
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        # The revocation stands: neither grant A nor the dead promoted grant
        # may be resurrected by the compensation.
        assert job.secret_env == {}
        assert job.secret_env_pin == ""

    @pytest.mark.asyncio
    async def test_approve_without_source_digest_is_refused(self, cron_home):
        """An approval that does not restate the reviewed source digest is a
        400: it attests to nothing about the code, so it cannot promote."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                for bad in (
                    {"approve_pending": True},
                    {"approve_pending": True, "expected_source_sha256": ""},
                    {"approve_pending": True, "expected_source_sha256": "ABC"},
                    {"approve_pending": True, "expected_source_sha256": 42},
                ):
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=bad)
                    body = await resp.json()
                    assert resp.status == 400, (bad, body)
                    assert body["code"] == "invalid_secret_env"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_of_reissued_request_over_unseen_source_is_refused(self, cron_home):
        """The reissue TOCTOU: the operator loaded source A, the agent rewrote
        the script to B and re-issued the request (pin now over B), and the
        dashboard's request banner refreshed while its source view did not.
        The approval restates B's mapping and timestamp — both request-level
        checks pass and the pin verifies against B — but its source digest is
        A's, so the promotion must refuse rather than bless code nobody read."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        seen_sha = _APPROVE["expected_source_sha256"]  # digest of revision A
        # Agent rewrites the script (revision B) and re-issues the request.
        spec = _grant_script(cron_home, body="def run(ctx): return 'B'\n")
        job = svc.get_job(jid)
        assert job is not None
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
                delivery=delivery_fingerprint("dashboard:conftest-slot"),
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "approve_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                        "expected_ts": 2.0,
                        "expected_source_sha256": seen_sha,
                    },
                )
                body = await resp.json()
                assert resp.status == 409, body
                assert body["code"] == "stale_source"
                # Restating B's digest — what a re-loaded source view shows —
                # is the one approval that promotes.
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "approve_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                        "expected_ts": 2.0,
                        "expected_source_sha256": hashlib.sha256(
                            b"def run(ctx): return 'B'\n"
                        ).hexdigest(),
                    },
                )
                assert resp.status == 200, await resp.json()
        reloaded = CronService(base_dir=svc._dir).get_job(jid)
        assert reloaded is not None and reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_refuses_source_the_display_cannot_show_verbatim(self, cron_home):
        """A body whose dashboard rendering differs from its raw bytes (the
        redactor masked a credential-shaped span here; undecodable bytes are
        the other case) is not approvable even with the CORRECT raw digest:
        the operator could never have read the masked span, so an agent could
        hide executable content behind it."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        body = 'KEY = "AKIAIOSFODNN7EXAMPLE"\ndef run(ctx): pass\n'
        spec = _grant_script(cron_home, body=body)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
                delivery=delivery_fingerprint("dashboard:conftest-slot"),
            ),
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{job.id}/secrets",
                    json={
                        **_APPROVE,
                        "expected_source_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    },
                )
                out = await resp.json()
        assert resp.status == 409, out
        assert out["code"] == "source_not_reviewable"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None and reloaded.secret_env == {}


class TestGrantEndpointBoundaries:
    """Identity boundaries of the owner-only grant endpoint."""

    def _apps(self, *, internal_auth: bool):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())

        @web.middleware
        async def _mark_internal(request, handler):
            if internal_auth:
                request["internal_auth"] = True
            return await handler(request)

        state = SimpleNamespace(
            crons=svc,
            push_refresh=lambda *a, **k: None,
            _slots={"chat-1-abc": object()},
        )
        app = web.Application(middlewares=[_mark_internal])
        app["state"] = state
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc, state

    def _pending_job(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:chat-1-abc",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.fixture(autouse=True)
    def _owner_view(self, monkeypatch):
        """Default to owner view; the non-owner test overrides it."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.mark.asyncio
    async def test_direct_grant_is_refused(self, cron_home):
        """Direct grants are removed: nothing binds what the owner saw to
        what gets pinned, so the request->approve flow is the only mint
        path. The endpoint keeps revoke and pending decisions only."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={"secret_env": {"MY_TOKEN": "slack-sandbox"}},
                )
                body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "direct_grant_removed"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_replaced_pending_request_is_not_promoted_by_stale_approval(self, cron_home):
        """The agent can overwrite a pending request at any moment; an
        approval that restates request A must not promote a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc, _state = self._apps(internal_auth=False)
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("jira-token", "fake-2")
        jid = self._pending_job(svc, cron_home)  # pending: MY_TOKEN <- slack-sandbox
        # Agent replaces the request AFTER the operator's view rendered.
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "jira-token"},
            secret_env_pending_pin=compute_secret_env_pin(
                str(cron_home() / "crons" / "grantee.py") + ":run",
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "jira-token"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        **_APPROVE,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_token_cannot_grant(self, monkeypatch, cron_home):
        """A dashboard token minted for an allowed Slack user (not the owner)
        must not approve grants — the vault boundary is owner-only."""
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 403
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_grant_endpoint_refuses_machine_credential(self, cron_home):
        """/api/crons is a prefix internal path, so the grant route IS
        reachable with X-Internal-Secret — the handler must refuse it."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=True)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "operator_only"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}


class TestGrantBodySizeCap:
    """What the approval signs must be what the owner reviewed: the grantable
    body cap may never exceed the dashboard preview ceiling, or an agent
    could hide executable tail bytes behind the preview truncation."""

    def test_grant_cap_never_exceeds_preview_cap(self):
        from kiro_crew.cron_script import _MAX_GRANTED_SCRIPT_BYTES
        from kiro_crew.dashboard.handlers.cron import _SCRIPT_SOURCE_MAX_BYTES

        assert _MAX_GRANTED_SCRIPT_BYTES <= _SCRIPT_SOURCE_MAX_BYTES

    def test_oversized_script_refuses_grant(self, tmp_path):
        """A script past the cap fails the grant path closed (no pin is
        minted for bytes the owner could not have fully previewed)."""
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-1")
        big = "# pad\n" * 50_000  # ~300 KiB, over the 256 KiB cap
        script = _make_script(tmp_path, "def run(ctx): pass\n" + big)
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(ValueError):
                compute_secret_env_pin(spec, "", job_id="job1", grant={"MY_TOKEN": "slack-sandbox"})


class TestGrantValueScrubbing:
    """A granted secret VALUE must never surface in child diagnostics: the
    launcher's status JSON carries str(e) from the script's own exception and
    bypasses the stderr redaction path, so both routes get the value scrub."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv",
            lambda argv, **k: ([argv[0], "-X", "utf8", *argv[1:]], None),
        )

    def _run(self, tmp_path, body):
        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-leakyvalue")
        script = _make_script(tmp_path, body)
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_TOKEN": "slack-sandbox"}
            )
            return run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_TOKEN": "slack-sandbox"},
                secret_env_pin=pin,
            )

    def test_exception_embedding_secret_is_scrubbed(self, tmp_path):
        """`raise Exception(token)` flows through the launcher's status JSON —
        the value must be replaced before the result reaches gateway logs."""
        result = self._run(
            tmp_path,
            "import os\n"
            "def run(ctx):\n"
            "    raise RuntimeError('auth failed for ' + os.environ['MY_SANDBOX_TOKEN'])\n",
        )
        assert result["status"] == "error"
        assert "xoxb-leakyvalue" not in result["error"]
        assert "[redacted-grant-value]" in result["error"]

    def test_stderr_traceback_embedding_secret_is_scrubbed(self, tmp_path):
        """A hard-crash traceback on stderr (non-zero exit, empty stdout) is
        the other diagnostic route — the value scrub covers it too."""
        result = self._run(
            tmp_path,
            "import os, sys\n"
            "def run(ctx):\n"
            "    sys.stderr.write('boom ' + os.environ['MY_SANDBOX_TOKEN'])\n"
            "    os._exit(3)\n",
        )
        assert result["status"] == "error"
        assert "xoxb-leakyvalue" not in result["error"]

    def test_short_secret_is_scrubbed_too(self, tmp_path):
        """A 1-3 char vault value is still a secret: no length threshold may
        exempt it from the diagnostic scrub."""
        SecretVault(tmp_path / ".kirocrew").set_sync("pin-secret", "7q1")
        script = _make_script(
            tmp_path,
            "import os\n"
            "def run(ctx):\n"
            "    raise RuntimeError('bad pin ' + os.environ['MY_SANDBOX_PIN'] + ' rejected')\n",
        )
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_PIN": "pin-secret"}
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_PIN": "pin-secret"},
                secret_env_pin=pin,
            )
        assert result["status"] == "error"
        assert "7q1" not in result["error"]
        assert "[redacted-grant-value]" in result["error"]

    def test_status_token_is_not_scrubbed_when_a_vault_value_equals_it(self, tmp_path):
        """The launcher's ``status`` is a closed vocabulary the caller matches
        against literals; a vault value that equals one of those tokens must
        not rewrite a successful outcome into a failed one. The diagnostic
        fields still take the scrub."""
        SecretVault(tmp_path / ".kirocrew").set_sync("word-secret", "done")
        script = _make_script(
            tmp_path,
            "import os\n"
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx):\n"
            "    raise Done('all ' + os.environ['MY_SANDBOX_WORD'] + ' here')\n",
        )
        spec = str(script) + ":run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(
                spec, "", job_id="job1", grant={"MY_SANDBOX_WORD": "word-secret"}
            )
            result = run_script_sandboxed(
                spec,
                "job1",
                timeout=120,
                secret_env={"MY_SANDBOX_WORD": "word-secret"},
                secret_env_pin=pin,
            )
        assert result["status"] == "done"
        assert result["message"] == "all [redacted-grant-value] here"
