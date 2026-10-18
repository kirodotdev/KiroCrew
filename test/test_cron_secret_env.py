"""Tests for operator-granted vault secrets on script/command crons.

Covers the grant validator, the code pin, fail-closed resolution, env
injection in both sandboxed runners, and the persistence-layer gate in
CronService (agent jobs refused, pin required, revoke clears).

This file also holds the defense-in-depth checks at injection, the agent's MCP side
(``cron_secret_request`` records or withdraws a pending request; neither it nor
``cron_update`` writes an active grant), the cap on the script body a grant can pin and
the scrub of granted values from run diagnostics. The owner-only dashboard endpoint that
approves, denies and revokes a grant is pinned in
``test_cron_secret_env_grant_endpoint.py``; the autouse fixtures and the ``_grant_script``
helper both files use live in ``cron_secret_env_helpers.py``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cron_secret_env_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _grant_script,
    cron_home_fixture,
)

from kiro_crew.cron import CronService, CronStoreBusy
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


def _make_script(tmp_path: Path, body: str, name: str = "job.py") -> Path:
    crons_dir = tmp_path / ".kirocrew" / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    script.write_text(body)
    return script


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

    def test_rethreading_via_update_job_kills_pin(self, tmp_path, cron_home):
        """``thread_ts`` is mutable after creation, so re-threading a granted
        job moves its delivery fingerprint and the next run fails CLOSED
        pending re-approval. Driven through ``update_job`` rather than by handing the
        precheck a different literal, so the store write and the fingerprint
        the runner recomputes from it are both exercised."""
        svc = self._service(tmp_path)
        spec = _grant_script(cron_home)
        job = svc.add_job("j", "m", every_secs=3600, script=spec, thread_ts="1776298241.408339")
        grant = {"MY_TOKEN": "slack-sandbox"}
        pin = compute_secret_env_pin(
            spec,
            "",
            "m",
            job_id=job.id,
            grant=grant,
            domain="active",
            delivery=delivery_fingerprint("", False, "", "1776298241.408339"),
        )
        svc.update_job(job.id, secret_env=grant, secret_env_pin=pin)
        # Agent re-threads the job: the persisted fingerprint moves with it.
        svc.update_job(job.id, thread_ts="1776298241.999999")
        stored = CronService(base_dir=tmp_path / "crons-store").list_jobs()[0]
        assert stored.thread_ts == "1776298241.999999"
        resolved, err = _secret_env_precheck(
            stored.secret_env,
            stored.secret_env_pin,
            script=spec,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message="m",
            job_id=job.id,
            delivery=delivery_fingerprint(
                stored.session_key, stored.silent, stored.channel or "", stored.thread_ts or ""
            ),
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
        out = mcp_cron._call_tool_locally(
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
        out = mcp_cron._call_tool_locally(
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
        out = mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_request_refuses_protected_env_name(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        out = mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"SLACK_BOT_TOKEN": "slack-sandbox"}},
        )
        assert out.startswith("Error:")

    def test_empty_request_withdraws_pending(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        self._vault().set_sync("slack-sandbox", "xoxb-1")
        mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        out = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": job.id, "secrets": {}})
        assert "Withdrew" in out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {}
        assert reloaded.secret_env_pending_pin == ""

    def _pending_request(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        out = mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}},
        )
        assert "PENDING" in out, out
        return svc, job

    @pytest.mark.parametrize(
        "args",
        # What a client that drops empty objects delivers for ``secrets: {}`` beside
        # the flag is the flag alone; a client that keeps both sends both.
        [{"withdraw": True}, {"withdraw": True, "secrets": {}}],
    )
    def test_withdraw_withdraws_the_pending_request(self, cron_home, args):
        from kiro_crew import mcp_cron

        svc, job = self._pending_request(cron_home)

        out = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": job.id, **args})

        assert out == "Withdrew the pending secret request."
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {}
        assert reloaded.secret_env_pending_pin == ""

    def test_a_mapping_beside_withdraw_false_is_still_recorded_pending(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)

        out = mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}, "withdraw": False},
        )

        assert "PENDING" in out, out
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    def test_withdraw_beside_a_mapping_is_refused_and_keeps_the_request(self, cron_home):
        from kiro_crew import mcp_cron

        svc, job = self._pending_request(cron_home)
        before = CronService(base_dir=svc._dir).get_job(job.id)
        assert before is not None

        out = mcp_cron._call_tool_locally(
            "cron_secret_request",
            {"job_id": job.id, "secrets": {"OTHER_TOKEN": "slack-sandbox"}, "withdraw": True},
        )

        assert out.startswith("Error: withdraw:"), out
        after = CronService(base_dir=svc._dir).get_job(job.id)
        assert after is not None
        assert after.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        assert after.secret_env_pending_pin == before.secret_env_pending_pin

    @pytest.mark.parametrize("args", [{}, {"withdraw": False}, {"secrets": None}])
    def test_neither_secrets_nor_withdraw_is_refused(self, cron_home, args):
        from kiro_crew import mcp_cron

        svc, job = self._pending_request(cron_home)

        out = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": job.id, **args})

        assert out.startswith("Error: secrets: required"), out
        after = CronService(base_dir=svc._dir).get_job(job.id)
        assert after is not None and after.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    # ``withdraw: true`` stands in for ``secrets: {}`` where a client drops the empty
    # object, so each way an empty request can end must read the same in both.
    @pytest.mark.parametrize("args", [{"secrets": {}}, {"withdraw": True}])
    def test_an_empty_request_for_a_job_out_of_reach_changes_nothing(
        self, cron_home, monkeypatch, args
    ):
        from kiro_crew import mcp_cron

        svc, job = self._pending_request(cron_home)

        unknown = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": "0123abcd", **args})
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-other")
        foreign = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": job.id, **args})

        assert unknown == mcp_cron._not_found("0123abcd")
        assert foreign == mcp_cron._not_found(job.id)
        after = CronService(base_dir=svc._dir).get_job(job.id)
        assert after is not None and after.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.parametrize("args", [{"secrets": {}}, {"withdraw": True}])
    def test_an_empty_request_on_a_busy_store_says_so_and_keeps_the_request(
        self, cron_home, monkeypatch, args
    ):
        from kiro_crew import mcp_cron

        svc, job = self._pending_request(cron_home)

        def busy(*_args, **_kwargs):
            raise CronStoreBusy("store busy")

        monkeypatch.setattr(CronService, "update_job", busy)

        out = mcp_cron._call_tool_locally("cron_secret_request", {"job_id": job.id, **args})

        assert out == "Error: cron store busy, please retry"
        after = CronService(base_dir=svc._dir).get_job(job.id)
        assert after is not None and after.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    def test_cron_update_cannot_write_active_grant(self, cron_home):
        """The general-purpose MCP update tool must never carry the grant."""
        from kiro_crew import mcp_cron

        svc, job = self._svc_and_job(cron_home)
        mcp_cron._call_tool_locally(
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
