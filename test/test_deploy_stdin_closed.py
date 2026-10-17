"""The deploy-web AWS CLI runner closes stdin unless a body descriptor is passed.

GPT BLOCKING (engine.py runner): the CLI resolves credentials through its own
provider chain, which can spawn a ``credential_process`` child. That child would
inherit whatever this process's stdin happens to be, so an inherited handle is a
path for one subprocess's input to reach another. Every call must therefore get a
CLOSED stdin (``DEVNULL``) unless the caller explicitly passes a body descriptor
(paired with ``--body /dev/stdin``), which is the only way a body reaches the CLI.
"""

import subprocess
from types import SimpleNamespace

from kiro_crew.deploy import engine


def _capture_run_limited(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run_limited(argv, **kwargs):
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(engine, "wrap_argv", lambda argv, **kw: (list(argv), None))
    monkeypatch.setattr(engine, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(engine, "run_limited", fake_run_limited)
    return seen


def test_stdin_is_devnull_when_no_body_descriptor(monkeypatch):
    seen = _capture_run_limited(monkeypatch)
    engine.run_aws(["s3api", "list-buckets"], "prof")
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


def test_stdin_is_the_body_descriptor_when_passed(monkeypatch):
    seen = _capture_run_limited(monkeypatch)
    engine.run_aws(["s3api", "put-object"], "prof", stdin_fd=7)
    # The caller's descriptor becomes the child's stdin, never DEVNULL.
    assert seen["kwargs"]["stdin"] == 7
