from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from kiro_crew.personal_insights import insights_projection as projection
from kiro_crew.personal_insights.insights_projection import (
    ProjectionExecutionError,
    ProjectionReceiptError,
    ProjectionRunner,
    ProjectionTimeout,
    StagedExecutableError,
    run_projection,
)
from kiro_crew.platform import build_default_context
from kiro_crew.platform.defaults import DefaultUnavailableInsightsProjectionProvider
from kiro_crew.platform.interfaces import (
    InsightsProjectionDescriptor,
    InsightsProjectionUnavailable,
)

RECEIPT_SCHEMA = "agent-session-intelligence.projection-receipt/1.0"
TEMPLATE_SCHEMA = "agent-session-intelligence.projection-descriptor-template/1.0"
FIXED_ARGV = ("asi-projection-probe", "project")


def _neutral(character):
    return "ne-" + character * 64


_RECEIPT = {
    "schema_version": RECEIPT_SCHEMA,
    "event_count": 5,
    "relation_count": 4,
    "group_count": 1,
    "interaction_count": 1,
    "lifecycle_count": 0,
    "multi_turn_count": 0,
    "root_event_count": 1,
    "grouped_event_count": 5,
    "ingest_persisted": True,
    "raw_to_neutral": {
        "raw-owner-1": _neutral("1"),
        "raw-call-1": _neutral("2"),
        "raw-outcome-1": _neutral("3"),
        "raw-call-2": _neutral("4"),
        "raw-outcome-2": _neutral("5"),
    },
}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _script(path, stdout, read_stdin=True, sleep=0):
    lines = ["#!/usr/bin/env bash"]
    if read_stdin:
        lines.append("cat > /dev/null")
    if sleep:
        lines.append(f"sleep {sleep}")
    if stdout is not None:
        lines.append("printf '%s\\n' " + repr(stdout))
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o700)
    return _sha(path)


def _receipt_script(path, receipt=None, **options):
    value = _RECEIPT if receipt is None else receipt
    return _script(path, json.dumps(value, separators=(",", ":")), **options)


def _descriptor(path, digest, **changes):
    values = {
        "schema_version": TEMPLATE_SCHEMA,
        "executable_path": str(path),
        "executable_sha256": digest,
        "argv": FIXED_ARGV,
        "max_stdin_bytes": 4096,
        "max_stdout_bytes": 4096,
        "timeout_seconds": 30,
    }
    values.update(changes)
    return InsightsProjectionDescriptor(**values)


def _run_receipt(tmp_path, receipt):
    path = tmp_path / "probe"
    digest = _receipt_script(path, receipt)
    return run_projection(_descriptor(path, digest), b"{}")


def test_descriptor_is_frozen_and_strict(tmp_path):
    descriptor = _descriptor(tmp_path / "probe", "a" * 64)
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.timeout_seconds = 1  # type: ignore[misc]
    invalid = (
        {"schema_version": "wrong/1.0"},
        {"argv": ("other", "project")},
        {"executable_sha256": "bad"},
        {"max_stdin_bytes": 0},
        {"max_stdout_bytes": True},
        {"timeout_seconds": 0},
    )
    for changes in invalid:
        with pytest.raises(ValueError):
            _descriptor(tmp_path / "probe", "a" * 64, **changes)
    with pytest.raises(ValueError):
        _descriptor(Path("relative"), "a" * 64)
    with pytest.raises(ValueError):
        _descriptor(tmp_path / "x" / ".." / "probe", "a" * 64)


def test_runner_has_no_runtime_override_surface():
    import inspect

    assert tuple(inspect.signature(ProjectionRunner.run).parameters) == (
        "self",
        "stdin_bytes",
    )


def test_request_model_home_and_environment_cannot_override(tmp_path, monkeypatch):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    runner = ProjectionRunner(descriptor)
    for name in (
        "PROJECTION_EXECUTABLE",
        "PROJECTION_ARGV",
        "PROJECTION_MODEL_OUTPUT",
        "PROJECTION_HOME_CONFIG",
    ):
        monkeypatch.setenv(name, "/untrusted/value")
    assert runner.run(b"{}").event_count == 5
    for key in ("request", "model_output", "home_config"):
        with pytest.raises(TypeError):
            runner.run(b"{}", **{key: "/untrusted/value"})


def test_public_default_and_context_are_unavailable():
    with pytest.raises(InsightsProjectionUnavailable):
        DefaultUnavailableInsightsProjectionProvider().descriptor()
    context = build_default_context(profile="standalone", cfg=cast(Any, None))
    with pytest.raises(InsightsProjectionUnavailable):
        context.insights_projection.descriptor()


class P05TestInsightsProjectionProvider:
    def __init__(self, descriptor):
        self._descriptor = descriptor

    def descriptor(self):
        return self._descriptor


def test_test_provider_composes_through_dataclasses_replace(tmp_path):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    context = dataclasses.replace(
        build_default_context(profile="standalone", cfg=cast(Any, None)),
        insights_projection=P05TestInsightsProjectionProvider(descriptor),
    )
    assert context.insights_projection.descriptor() is descriptor


def test_digest_type_symlink_and_mode_fail_closed(tmp_path):
    path = tmp_path / "probe"
    _receipt_script(path)
    with pytest.raises(StagedExecutableError):
        run_projection(_descriptor(path, "f" * 64), b"{}")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(StagedExecutableError):
        run_projection(_descriptor(directory, "a" * 64), b"{}")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        _descriptor(link, _sha(path))
    path.chmod(0o720)
    with pytest.raises(StagedExecutableError):
        run_projection(_descriptor(path, _sha(path)), b"{}")


def test_owner_mismatch_rejected(tmp_path, monkeypatch):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    monkeypatch.setattr(projection.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(StagedExecutableError):
        ProjectionRunner(descriptor).run(b"{}")


def test_copy_uses_open_descriptor_after_path_swap(tmp_path, monkeypatch):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    original_lseek = projection.os.lseek
    swapped = {"done": False}

    def swap_after_digest(file_descriptor, offset, whence):
        result = original_lseek(file_descriptor, offset, whence)
        if not swapped["done"] and offset == 0 and whence == os.SEEK_SET:
            swapped["done"] = True
            path.rename(tmp_path / "verified-inode")
            _script(path, "tampered")
        return result

    monkeypatch.setattr(projection.os, "lseek", swap_after_digest)
    assert ProjectionRunner(descriptor).run(b"{}").event_count == 5
    assert swapped["done"] is True


def test_in_place_mutation_after_hash_is_caught_by_staged_digest(tmp_path, monkeypatch):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    original_lseek = projection.os.lseek
    mutated = {"done": False}

    def mutate_after_digest(file_descriptor, offset, whence):
        result = original_lseek(file_descriptor, offset, whence)
        if not mutated["done"] and offset == 0 and whence == os.SEEK_SET:
            mutated["done"] = True
            path.write_text("tampered")
        return result

    monkeypatch.setattr(projection.os, "lseek", mutate_after_digest)
    with pytest.raises(StagedExecutableError):
        ProjectionRunner(descriptor).run(b"{}")


def test_staging_directory_and_file_are_owner_only(tmp_path, monkeypatch):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path))
    observed = {}
    original = projection._bounded_run

    def inspect_stage(staged, current_descriptor, payload):
        observed["directory"] = stat.S_IMODE(staged.parent.stat().st_mode)
        observed["file"] = stat.S_IMODE(staged.stat().st_mode)
        return original(staged, current_descriptor, payload)

    monkeypatch.setattr(projection, "_bounded_run", inspect_stage)
    ProjectionRunner(descriptor).run(b"{}")
    assert observed == {"directory": 0o700, "file": 0o700}


def test_oversized_input_and_output_are_typed_failures(tmp_path):
    path = tmp_path / "probe"
    descriptor = _descriptor(path, _receipt_script(path), max_stdin_bytes=2)
    with pytest.raises(ProjectionExecutionError):
        ProjectionRunner(descriptor).run(b"too large")
    descriptor = _descriptor(path, _sha(path), max_stdout_bytes=10)
    with pytest.raises(ProjectionExecutionError):
        ProjectionRunner(descriptor).run(b"{}")


def test_blocked_stdin_and_wall_clock_timeout_are_terminated(tmp_path):
    path = tmp_path / "probe"
    digest = _script(path, None, read_stdin=False, sleep=30)
    descriptor = _descriptor(
        path,
        digest,
        max_stdin_bytes=2_000_000,
        timeout_seconds=1,
    )
    started = time.monotonic()
    with pytest.raises(ProjectionTimeout):
        ProjectionRunner(descriptor).run(b"x" * 1_000_000)
    assert time.monotonic() - started < 5


def test_valid_receipt_returns_deterministic_digest(tmp_path):
    first = _run_receipt(tmp_path, _RECEIPT)
    second = _run_receipt(tmp_path, _RECEIPT)
    assert first.event_count == 5
    assert first.receipt_digest() == second.receipt_digest()
    assert len(first.receipt_digest()) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="text"),
        lambda value: value.update(event_count=True),
        lambda value: value.update(event_count=-1),
        lambda value: value.update(grouped_event_count=4),
        lambda value: value.update(group_count=2),
        lambda value: value.update(ingest_persisted=False),
        lambda value: value.update(raw_to_neutral={"raw": _neutral("a")}),
        lambda value: value.update(
            raw_to_neutral={
                "raw-1": "ne-short",
                "raw-2": _neutral("2"),
                "raw-3": _neutral("3"),
                "raw-4": _neutral("4"),
                "raw-5": _neutral("5"),
            }
        ),
        lambda value: value.update(
            raw_to_neutral={
                "raw-1": _neutral("1"),
                "raw-2": _neutral("1"),
                "raw-3": _neutral("3"),
                "raw-4": _neutral("4"),
                "raw-5": _neutral("5"),
            }
        ),
        lambda value: value.update(
            raw_to_neutral={
                "source text": _neutral("1"),
                "raw-2": _neutral("2"),
                "raw-3": _neutral("3"),
                "raw-4": _neutral("4"),
                "raw-5": _neutral("5"),
            }
        ),
    ],
)
def test_invalid_receipts_are_rejected(tmp_path, mutation):
    value = json.loads(json.dumps(_RECEIPT))
    mutation(value)
    with pytest.raises(ProjectionReceiptError):
        _run_receipt(tmp_path, value)


def test_malformed_and_duplicate_key_receipts_are_rejected(tmp_path):
    path = tmp_path / "probe"
    digest = _script(path, "not-json")
    with pytest.raises(ProjectionReceiptError):
        ProjectionRunner(_descriptor(path, digest)).run(b"{}")
    duplicate = json.dumps(_RECEIPT, separators=(",", ":")).replace(
        '"event_count":5', '"event_count":5,"event_count":5'
    )
    digest = _script(path, duplicate)
    with pytest.raises(ProjectionReceiptError):
        ProjectionRunner(_descriptor(path, digest)).run(b"{}")


def _load_smoke():
    path = Path(__file__).resolve().parents[2] / "scripts" / "p05_projection_smoke.py"
    spec = importlib.util.spec_from_file_location("p05_projection_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _smoke_fixture_bytes():
    return (Path(__file__).resolve().parent / "fixtures" / "kiro-renamed-target.json").read_bytes()


def _smoke_inputs(tmp_path):
    smoke = _load_smoke()
    canonical = _smoke_fixture_bytes()
    receipt = json.dumps(_RECEIPT, separators=(",", ":")).encode("utf-8")
    executable = tmp_path / "probe"
    describe = {
        "schema_version": TEMPLATE_SCHEMA,
        "argv": list(FIXED_ARGV),
        "max_stdin_bytes": len(canonical),
        "max_stdout_bytes": len(receipt) + 1,
        "timeout_seconds": 30,
        "canonical_fixture_sha256": hashlib.sha256(canonical).hexdigest(),
        "receipt_fixture_sha256": hashlib.sha256(receipt).hexdigest(),
        "byte_bound_derivation": smoke.BYTE_BOUND_DERIVATION,
    }
    describe_bytes = json.dumps(describe, separators=(",", ":")).encode("utf-8")
    body = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--describe" ]; then\n'
        f"printf '%s\\n' {repr(describe_bytes.decode())}\n"
        "exit 0\n"
        "fi\n"
        "cat > /dev/null\n"
        f"printf '%s\\n' {repr(receipt.decode())}\n"
    )
    executable.write_text(body)
    executable.chmod(0o700)
    canonical_path = tmp_path / "canonical.json"
    canonical_path.write_bytes(canonical)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(receipt)
    receipts = tmp_path / "phase-receipts.jsonl"
    record = {
        "phase": "A",
        "status": "green",
        "contract_artifacts": [
            {
                "name": "projection_executable",
                "path": str(executable),
                "sha256": _sha(executable),
            },
            {
                "name": "descriptor_template",
                "path": str(executable) + "#--describe",
                "sha256": hashlib.sha256(describe_bytes).hexdigest(),
            },
            {
                "name": "rename_fixture",
                "path": str(canonical_path),
                "sha256": _sha(canonical_path),
            },
            {
                "name": "projection_receipt_fixture",
                "path": str(receipt_path),
                "sha256": _sha(receipt_path),
            },
        ],
    }
    receipts.write_text(json.dumps(record) + "\n")
    receipts.chmod(0o600)
    tmp_path.chmod(0o700)
    descriptor_path = tmp_path / "descriptor.json"
    fixture_path = tmp_path / "host-fixture.json"
    fixture_path.write_bytes(canonical)
    return smoke, receipts, descriptor_path, fixture_path


def test_smoke_prepare_and_run_use_production_runner(tmp_path, monkeypatch):
    smoke, receipts, descriptor_path, fixture_path = _smoke_inputs(tmp_path)
    descriptor = smoke.prepare(receipts, descriptor_path)
    assert tuple(descriptor) == smoke.STRICT_DESCRIPTOR_FIELDS
    assert stat.S_IMODE(descriptor_path.stat().st_mode) == 0o600
    called = {"count": 0}
    original = smoke.ProjectionRunner.run

    def counted(self, payload):
        called["count"] += 1
        return original(self, payload)

    monkeypatch.setattr(smoke.ProjectionRunner, "run", counted)
    result = smoke.run(descriptor_path, fixture_path)
    assert called["count"] == 1
    assert result["receipt"]["event_count"] == 5
    assert len(result["receipt_sha256"]) == 64
    with pytest.raises(smoke.SmokeError):
        smoke.prepare(receipts, descriptor_path)


def test_smoke_rejects_latest_failed_phase_a(tmp_path):
    smoke, receipts, descriptor_path, _ = _smoke_inputs(tmp_path)
    with receipts.open("a") as stream:
        stream.write(json.dumps({"phase": "A", "status": "failed"}) + "\n")
    with pytest.raises(smoke.SmokeError):
        smoke.prepare(receipts, descriptor_path)


def test_smoke_rejects_tampered_fixture_and_expectation(tmp_path):
    smoke, receipts, descriptor_path, fixture_path = _smoke_inputs(tmp_path)
    smoke.prepare(receipts, descriptor_path)
    value = json.loads(fixture_path.read_text())
    value["conformance_expectation"]["logical_occurrences"] = 3
    fixture_path.write_text(json.dumps(value, separators=(",", ":")))
    with pytest.raises(smoke.SmokeError):
        smoke.run(descriptor_path, fixture_path)
