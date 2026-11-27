"""The signed session records live in a directory a backend sandbox cannot write.

``session_key_is_attested`` accepts a signed per-session record, so a record written from
an app backend sandbox must not be enough. The records live in their own directory, which
the sandbox plan seals read-only, and a record placed anywhere else (the writable data-home
root) is not read. The gateway stays the only writer.

Only the plan and the record functions are checked; no sandbox, no process and no real key
run here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
_TOKEN = "stub-token-for-the-test"
_SESSION = "session-key-for-the-test"
_FAKE_KEY = b"k" * 32


@pytest.fixture
def records(app_env, monkeypatch):
    """The record module with a fake MAC key, so no real key is read."""
    from kiro_crew import session_token_sig

    monkeypatch.setattr(session_token_sig, "_load_hmac_key", lambda: _FAKE_KEY)
    return session_token_sig


class TestTheSessionRecordDirectory:
    @_POSIX_ONLY
    def test_the_directory_of_a_record_is_sealed_read_only_in_the_backend_plan(
        self, app_env, records
    ):
        record = records._sig_path(_TOKEN, app_env)
        plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")

        assert str(record.parent) in set(plan.readonly), "a backend can write records"
        assert record.parent != app_env

    @_POSIX_ONLY
    def test_a_shipped_builtin_backend_plan_seals_it_too(self, app_env, records):
        """The directory is sealed for every backend: no owned-leaf carve-out lifts it."""
        record = records._sig_path(_TOKEN, app_env)
        owned = sandbox.app_backend_visible_targets(sandbox.MD_NOTEBOOK_APP_NAME)
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "standard", extra_visible_dirs=owned
        )

        assert str(record.parent) in set(plan.readonly), "a builtin backend can write records"

    def test_a_valid_record_written_outside_the_sealed_directory_is_not_read(
        self, app_env, records
    ):
        """What a backend could write: the data-home root is writable, the directory is not."""
        body = _SESSION
        mac = records._compute_sig(_FAKE_KEY, _TOKEN, body)
        planted = records._sig_path(_TOKEN, app_env)
        (app_env / planted.name).write_text(f"{mac}\n{body}", encoding="utf-8")

        assert records.verify_session_token(_TOKEN) == ""

    def test_the_gateway_publishes_a_record_and_it_verifies(self, app_env, records):
        records.publish_session_token(_TOKEN, _SESSION)

        assert records.verify_session_token(_TOKEN) == _SESSION

    def test_a_stale_record_is_pruned(self, app_env, records):
        from kiro_crew.session_pid_mappings import _prune_stale_session_token_files

        records.publish_session_token(_TOKEN, _SESSION)
        record = records._sig_path(_TOKEN, app_env)
        os.utime(record, (1, 1))

        assert _prune_stale_session_token_files(ttl_secs=0) == 1
        assert not record.exists()

    def test_a_record_names_and_holds_no_token(self, app_env, records):
        """The directory stays readable (in-sandbox MCP servers verify from it), so a read must
        not hand over a token: the name is the token's sha256 and the body is a MAC and a key."""
        records.publish_session_token(_TOKEN, _SESSION)

        (record,) = list(records._sig_path(_TOKEN, app_env).parent.glob("session_token_*.sig"))
        assert record.name == f"session_token_{hashlib.sha256(_TOKEN.encode()).hexdigest()}.sig"
        body = record.read_text(encoding="utf-8")
        assert _TOKEN not in record.name and _TOKEN not in body
        assert body.split("\n") == [records._compute_sig(_FAKE_KEY, _TOKEN, _SESSION), _SESSION]

    @_POSIX_ONLY
    def test_a_symlink_at_the_directory_name_gets_no_record(self, app_env, tmp_path, records):
        """A sandbox can plant the name before the gateway's first seal: never write through it."""
        elsewhere = tmp_path / "attacker-directory"
        elsewhere.mkdir()
        (app_env / "session-tokens").symlink_to(elsewhere, target_is_directory=True)

        records.publish_session_token(_TOKEN, _SESSION)

        assert list(elsewhere.iterdir()) == [], "a record was written outside the sealed directory"
        assert records.verify_session_token(_TOKEN) == ""

    def test_a_file_at_the_directory_name_gets_no_record(self, app_env, records):
        planted = app_env / "session-tokens"
        planted.write_text("planted", encoding="utf-8")

        records.publish_session_token(_TOKEN, _SESSION)

        assert planted.read_text(encoding="utf-8") == "planted"
        assert records.verify_session_token(_TOKEN) == ""


_CHILD = r"""
import errno, json, os, sys

directory = sys.argv[1]
try:
    with open(os.path.join(directory, "forged.sig"), "w", encoding="utf-8") as fh:
        fh.write("forged")
    wrote = "WROTE"
except OSError as exc:
    wrote = errno.errorcode.get(exc.errno, str(exc.errno))
with open(os.path.join(directory, "kept.sig"), encoding="utf-8") as fh:
    read = fh.read()
print("REPORT " + json.dumps({"write": wrote, "read": read}), flush=True)
"""


@_POSIX_ONLY
class TestInTheNamespaceSandbox:
    """The real launcher: a record cannot be created in the directory, one can be read.

    Needs unprivileged user and mount namespaces, which a host without them skips and CI's
    sandbox jobs run.
    """

    pytestmark = [
        pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
        pytest.mark.skipif(not sandbox.userns_available(), reason="no user and mount namespaces"),
    ]

    def test_a_backend_cannot_write_a_record_but_can_read_one(self, tmp_path, monkeypatch, app_env):
        directory = app_env / "session-tokens"
        directory.mkdir()
        (directory / "kept.sig").write_text("record", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        sandbox.reset_backend()
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}

        run = subprocess.run(
            sandbox.namespace_argv([sys.executable, "-c", _CHILD, str(directory)], "standard"),
            cwd=app_env,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        )

        assert run.returncode == 0, run.stdout + run.stderr
        (line,) = [ln for ln in run.stdout.splitlines() if ln.startswith("REPORT ")]
        assert json.loads(line[len("REPORT ") :]) == {"write": "EROFS", "read": "record"}
        assert not (directory / "forged.sig").exists()
