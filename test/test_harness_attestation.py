"""The spawn-side Trust_Attestation seam of :mod:`kiro_crew.acp.harness_adapters`.

Every harness — bundled or operator-authored — passes through the same three
steps before a process exists: resolve the descriptor's executable, attest the
resolved candidate (runnable, non-zero-byte, path anchored), and prove the
rendered argv execs the attested bytes. These tests pin each step's verdicts
directly, because the callers that exercise them end to end live a slice away
(the serving layer) and a refusal's TEXT is part of the contract: it is what an
operator reads when a harness will not start.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew.acp.harness_adapters import (
    HarnessExecutableTrustError,
    HarnessSpawnRefused,
    attest_executable,
    checked_spawn_argv,
    resolve_spawn_executable,
)
from kiro_crew.acp.harness_descriptor import HarnessDescriptor


def _executable(tmp_path, name="agy-acp", content="#!/bin/sh\nexit 0\n"):
    """A runnable, non-empty candidate on this platform."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        path = tmp_path / f"{name}.bat"
        path.write_text("@echo off\r\nexit /b 0\r\n")
    else:
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o755)
    return path


def _descriptor(executable: str) -> HarnessDescriptor:
    return HarnessDescriptor(
        id="agy",
        display_name="Agy",
        executable=executable,
        argv=("{executable}", "acp"),
    )


class TestAttestExecutable:
    def test_a_runnable_candidate_returns_a_launch_path_to_its_own_bytes(self, tmp_path):
        """The attested path must point at the vetted file — the exec'd bytes
        and the checked bytes have to be the same file."""
        candidate = _executable(tmp_path)

        launch = attest_executable("agy", str(candidate))

        assert os.path.samefile(launch, candidate)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="on Windows the zero-byte refusal lives in resolution "
        "(_candidate_problem), not the snapshot — covered below via "
        "resolve_spawn_executable",
    )
    def test_a_zero_byte_candidate_is_refused_naming_the_harness_and_the_defect(self, tmp_path):
        """A truncated install must be refused BEFORE exec, and the refusal must
        name the operator's harness — not Kiro CLI, which is the shared
        snapshot's other caller — so the operator debugs the right tool."""
        path = tmp_path / "empty"
        path.touch()
        path.chmod(0o755)

        with pytest.raises(HarnessExecutableTrustError) as excinfo:
            attest_executable("agy", str(path))

        assert "'agy'" in str(excinfo.value)
        assert "zero-byte" in str(excinfo.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bit does not exist on Windows")
    def test_a_non_executable_candidate_is_refused_as_not_executable(self, tmp_path):
        path = tmp_path / "plain"
        path.write_text("data\n")

        with pytest.raises(HarnessExecutableTrustError) as excinfo:
            attest_executable("agy", str(path))

        assert "not an executable file" in str(excinfo.value)


class TestResolveSpawnExecutable:
    def test_resolves_and_attests_an_absolute_declared_executable(self, tmp_path):
        candidate = _executable(tmp_path)

        launch = resolve_spawn_executable(_descriptor(str(candidate)))

        assert os.path.samefile(launch, candidate)

    def test_a_zero_byte_candidate_is_refused_at_resolution_on_every_platform(self, tmp_path):
        """The zero-byte guard is resolution's (``_candidate_problem``), so it
        holds on Windows too — where the attestation snapshot deliberately
        short-circuits and would accept anything."""
        path = tmp_path / ("truncated.bat" if os.name == "nt" else "truncated")
        path.touch()
        if os.name != "nt":
            path.chmod(0o755)

        with pytest.raises(HarnessSpawnRefused) as excinfo:
            resolve_spawn_executable(_descriptor(str(path)))

        assert "zero-byte" in str(excinfo.value)

    def test_an_unresolvable_declaration_is_refused_with_the_resolution_reason(self):
        """A resolution failure surfaces as a spawn refusal that carries the
        listing-side reason verbatim, so the spawn path and the settings surface
        tell the operator the same story."""
        with pytest.raises(HarnessSpawnRefused) as excinfo:
            resolve_spawn_executable(_descriptor("definitely-not-on-path-4242"))

        assert "'agy' cannot start" in str(excinfo.value)
        assert "was not found on PATH" in str(excinfo.value)


class TestCheckedSpawnArgv:
    def test_an_argv_execing_the_attested_path_passes_through_unchanged(self, tmp_path):
        candidate = str(_executable(tmp_path))
        argv = [candidate, "acp", "--flag"]

        assert checked_spawn_argv(_descriptor(candidate), argv, candidate) is argv

    def test_an_empty_argv_is_refused(self, tmp_path):
        candidate = str(_executable(tmp_path))

        with pytest.raises(HarnessSpawnRefused) as excinfo:
            checked_spawn_argv(_descriptor(candidate), [], candidate)

        assert "empty argv" in str(excinfo.value)

    def test_an_argv0_that_is_not_the_attested_path_is_refused_not_rewritten(self, tmp_path):
        """The second half of attestation: a bare name in ``argv[0]`` would be
        re-resolved through PATH by exec, so vetted bytes and run bytes could
        differ. The refusal names both paths and never rewrites."""
        candidate = str(_executable(tmp_path))

        with pytest.raises(HarnessSpawnRefused) as excinfo:
            checked_spawn_argv(_descriptor(candidate), ["agy-acp", "acp"], candidate)

        assert "never checked" in str(excinfo.value)
        assert "'agy-acp'" in str(excinfo.value)
