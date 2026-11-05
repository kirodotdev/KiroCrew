"""A cron's script-path check does not depend on the resolver pool off the event loop.

Both cron call sites hold a path they already resolved, so they ask
``canonical_path_refusal``. Off the loop it answers inline, and a busy or stalled
pool cannot refuse a healthy script. On the loop it still asks the bounded gate.
"""

from __future__ import annotations

import asyncio

import pytest

import kiro_crew.security.paths as security_paths
from kiro_crew import cron_script, mcp_cron_vet
from kiro_crew.security import PathResolutionStalled, is_unverifiable_path_refusal


@pytest.fixture
def stalled_pool(monkeypatch):
    def stall(expanded, worker, **kwargs):
        raise PathResolutionStalled(expanded, expanded)

    monkeypatch.setattr(security_paths, "_run_resolution_bounded", stall)
    monkeypatch.setattr(security_paths, "_home_targets_cache", {})


@pytest.fixture
def cron_job(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_script, "config_dir", lambda: tmp_path)
    crons = tmp_path / "crons"
    crons.mkdir()
    script = crons / "job.py"
    script.write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    return script


def test_resolve_script_path_resolves_while_the_pool_is_stalled(stalled_pool, cron_job):
    path, func = cron_script.resolve_script_path(f"{cron_job}:run")
    assert (path, func) == (str(cron_job.resolve()), "run")


def test_vet_script_file_passes_while_the_pool_is_stalled(stalled_pool, cron_job):
    assert mcp_cron_vet._vet_script_file(str(cron_job)) is None


def test_on_the_event_loop_a_stall_still_refuses(stalled_pool, cron_job):
    async def resolve():
        return cron_script.resolve_script_path(f"{cron_job}:run")

    with pytest.raises(PermissionError) as exc:
        asyncio.run(resolve())
    assert is_unverifiable_path_refusal(str(exc.value))


def test_a_script_under_a_sensitive_dir_is_refused_off_the_loop(
    stalled_pool, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "crons").mkdir()
    script = aws / "crons" / "job.py"
    script.write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(cron_script, "config_dir", lambda: aws)
    with pytest.raises(PermissionError, match="blocked by security policy"):
        cron_script.resolve_script_path(f"{script}:run")
    assert mcp_cron_vet._vet_script_file(str(script)) == (
        "Error: cron script path blocked by security policy "
        "(resolves to a sensitive credential path)"
    )
