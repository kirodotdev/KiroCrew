"""Doubles and fixtures shared by the ``test_voice_reply*.py`` modules.

``_no_argv_prefixers`` is autouse, so every module imports it by name;
``_polly_consented`` is declared ``@pytest.fixture(name=...)`` and imported under
its function name. The subprocess, ``mkstemp`` and sandbox doubles keep every test
off a real TTS engine and the AWS CLI.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────


def _mock_subprocess(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> AsyncMock:
    """Return an AsyncMock shaped like asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _capture_mkstemp(monkeypatch) -> list[str]:
    """Record every path the module allocates via ``tempfile.mkstemp``."""
    allocated: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        allocated.append(path)
        return fd, path

    monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", recording_mkstemp)
    return allocated


# _synthesize_polly() short-circuits to None when the `aws` CLI is absent, so any
# test that exercises the argv build or the subprocess lifecycle must state that
# the CLI is present. It is NOT present on a stock Windows box (nor on a minimal
# Linux CI image), so relying on the ambient host makes those tests silently
# host-dependent rather than deterministic.
_FAKE_AWS_CLI = "aws.exe" if os.name == "nt" else "/usr/bin/aws"


def _patch_aws_on_path(monkeypatch) -> None:
    """Make ``shutil.which`` report the ``aws`` CLI present, others absent."""
    monkeypatch.setattr(
        "kiro_crew.voice_reply.shutil.which",
        lambda name, *a, **k: _FAKE_AWS_CLI if name == "aws" else None,
    )
    # The which stub above is name-sensitive ("aws" only), but the shared
    # deploy-engine resolver would feed it a PATH-hit absolute path.
    # Pin the resolver to the bare name so this fixture keeps meaning exactly
    # "the aws CLI is present" regardless of the host.
    monkeypatch.setattr("kiro_crew.voice_reply.resolve_aws_bin", lambda: "aws")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the module-wide argv pin of test_voice_reply.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _no_argv_prefixers(monkeypatch):
    """Strip the host-dependent argv prefixes for every test in this module.

    Two layers sit between the command these tests build and the
    ``create_subprocess_exec`` they mock, and BOTH prepend to the argv:

    * ``cgroup_scope_argv`` — prepends a launcher on a cgroup-v2 host.
    * ``create_subprocess_limited`` — prepends an RLIMIT shim that re-``exec``s
      in place, so the real argv[0] becomes a python interpreter path.

    Either one displaces argv[0] and makes an assertion about the built command
    host-dependent: green wherever the host offers neither (Windows, an
    unprivileged macOS box) and red on a Linux runner that offers both. Both are
    pinned module-wide rather than per-test so a new test cannot silently inherit
    the same host dependence. A test specifically about resource limits or cgroup
    scoping should patch the real function back.
    """
    monkeypatch.setattr("kiro_crew.voice_reply.cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(
        "kiro_crew.voice_reply.create_subprocess_limited",
        lambda *argv, **kw: asyncio.create_subprocess_exec(*argv, **kw),
    )


def _prepared(cleanup=None, env=None):
    """An async ``sandboxed_spawn_argv_async`` double returning its argv intact.

    The real call returns (argv, scrubbed_env, cleanup_path); these tests assert
    the engine's own argv and the env/cleanup handling around it, so the double
    passes argv through and lets each caller pin the other two.
    """

    async def prepare(cmd, *_a, **_kw):
        return list(cmd), {} if env is None else env, cleanup

    return prepare


def _matching_identity(consent_mod, account: str):
    """An async ``probe_identity`` stand-in that resolves to ``account``."""

    async def _probe(_profile: str, _region: str, *, use_cache: bool = True):
        return consent_mod.Identity(ok=True, account=account)

    return _probe


@pytest.fixture(name="_polly_consented")
def _polly_consented_fixture(tmp_path_factory, monkeypatch):
    """Record operator consent for Polly under the default profile+region.

    ``_synthesize_polly`` now refuses without one, so every test that means to
    exercise the SYNTHESIS path has to consent first. The grant is written into
    a throwaway data home, never the real one. Tests that assert the refusal
    itself deliberately do not use this fixture (see ``test_aws_consent.py``).
    """
    home = tmp_path_factory.mktemp("consent-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    from kiro_crew import aws_consent
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent.record_grant(
        aws_consent.SERVICE_POLLY,
        profile="",
        region="",
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/test",
        granted_at="2026-08-21T00:00:00+00:00",
    )
    # The gate also verifies the LIVE account, which would spawn the AWS CLI.
    # These cases are about synthesis, so return a matching identity instead.
    monkeypatch.setattr(
        aws_consent, "probe_identity", _matching_identity(aws_consent, "111122223333")
    )
