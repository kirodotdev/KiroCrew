from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from kiro_crew.personal_insights.insights_projection import ProjectionRunner
from kiro_crew.platform.interfaces import InsightsProjectionDescriptor

DESCRIPTOR_TEMPLATE_SCHEMA: Final[str] = (
    "agent-session-intelligence.projection-descriptor-template/1.0"
)
REQUIRED_TIMEOUT_SECONDS: Final[int] = 30
FIXED_ARGV: Final[tuple[str, str]] = ("asi-projection-probe", "project")
BYTE_BOUND_DERIVATION: Final[str] = (
    "max_stdin_bytes=canonical_fixture_length;"
    "max_stdout_bytes=receipt_fixture_length_plus_newline"
)
STRICT_DESCRIPTOR_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "executable_path",
    "executable_sha256",
    "argv",
    "max_stdin_bytes",
    "max_stdout_bytes",
    "timeout_seconds",
)
_TEMPLATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "argv",
        "max_stdin_bytes",
        "max_stdout_bytes",
        "timeout_seconds",
        "canonical_fixture_sha256",
        "receipt_fixture_sha256",
        "byte_bound_derivation",
    }
)
_EXPECTATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "logical_occurrences",
        "distinct_target_equivalence_ids",
        "maximum_observed_recurrence_per_id",
        "disclosed_undercount",
    }
)


class SmokeError(Exception):
    pass


class P05SmokeInsightsProjectionProvider:
    def __init__(self, value: InsightsProjectionDescriptor) -> None:
        self.value = value

    def descriptor(self) -> InsightsProjectionDescriptor:
        return self.value


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _require_owner_file(path: Path, mode: int | None = None) -> None:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise SmokeError(f"{path.name} is not a regular non-symlink file")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise SmokeError(f"{path.name} is not owner-held")
    if mode is not None and stat.S_IMODE(status.st_mode) != mode:
        raise SmokeError(f"{path.name} mode is not {mode:o}")


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    hasher = hashlib.sha256()
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise SmokeError(f"{path.name} is not a regular file")
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(descriptor)
    return hasher.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SmokeError("JSON object contains duplicate keys")
        result[key] = value
    return result


def _read_json_bytes(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except SmokeError:
        raise
    except (UnicodeDecodeError, ValueError) as error:
        raise SmokeError("JSON payload is invalid") from error
    if not isinstance(value, dict):
        raise SmokeError("JSON payload is not an object")
    return value


def _latest_phase_a_terminal(phase_receipts: Path) -> dict[str, Any]:
    _require_owner_file(phase_receipts, 0o600)
    latest: dict[str, Any] | None = None
    for line in phase_receipts.read_bytes().splitlines():
        if not line.strip():
            continue
        record = _read_json_bytes(line)
        if record.get("phase") == "A" and record.get("status") in {"green", "failed"}:
            latest = record
    if latest is None or latest.get("status") != "green":
        raise SmokeError("latest terminal Phase A receipt is not green")
    return latest


def _named_artifact(record: dict[str, Any], name: str) -> tuple[Path, str]:
    artifacts = record.get("contract_artifacts")
    if not isinstance(artifacts, list):
        raise SmokeError("Phase A receipt lacks contract artifacts")
    matches = [artifact for artifact in artifacts if artifact.get("name") == name]
    if len(matches) != 1:
        raise SmokeError(f"Phase A receipt artifact {name!r} is not unique")
    artifact = matches[0]
    path = artifact.get("path")
    digest = artifact.get("sha256")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise SmokeError(f"Phase A artifact {name!r} path is invalid")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SmokeError(f"Phase A artifact {name!r} digest is invalid")
    return Path(path), digest


def _invoke_describe(executable: Path) -> bytes:
    result = subprocess.run(
        [str(executable), "--describe"],
        capture_output=True,
        timeout=REQUIRED_TIMEOUT_SECONDS,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    if result.returncode != 0 or result.stderr:
        raise SmokeError("projection executable --describe failed")
    return result.stdout.rstrip(b"\n")


def _validate_template(
    template: dict[str, Any], canonical_fixture: bytes, receipt_fixture: bytes
) -> None:
    if set(template) != _TEMPLATE_FIELDS:
        raise SmokeError("descriptor template fields are not exact")
    if template["schema_version"] != DESCRIPTOR_TEMPLATE_SCHEMA:
        raise SmokeError("descriptor template schema version is unexpected")
    if template["argv"] != list(FIXED_ARGV):
        raise SmokeError("descriptor template argv is not the fixed command")
    for field in ("max_stdin_bytes", "max_stdout_bytes", "timeout_seconds"):
        value = template[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SmokeError(f"descriptor template {field} is invalid")
    if template["timeout_seconds"] != REQUIRED_TIMEOUT_SECONDS:
        raise SmokeError("descriptor template timeout is not 30 seconds")
    if template["max_stdin_bytes"] != len(canonical_fixture):
        raise SmokeError("descriptor stdin bound is not fixture-derived")
    if template["max_stdout_bytes"] != len(receipt_fixture) + 1:
        raise SmokeError("descriptor stdout bound is not fixture-derived")
    if template["canonical_fixture_sha256"] != hashlib.sha256(canonical_fixture).hexdigest():
        raise SmokeError("descriptor canonical fixture digest is invalid")
    if template["receipt_fixture_sha256"] != hashlib.sha256(receipt_fixture).hexdigest():
        raise SmokeError("descriptor receipt fixture digest is invalid")
    if template["byte_bound_derivation"] != BYTE_BOUND_DERIVATION:
        raise SmokeError("descriptor byte-bound derivation is invalid")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise SmokeError("descriptor write did not advance")
        offset += written


def prepare(phase_receipts: Path, output: Path) -> dict[str, Any]:
    record = _latest_phase_a_terminal(phase_receipts)
    executable, executable_digest = _named_artifact(record, "projection_executable")
    template_artifact, template_digest = _named_artifact(record, "descriptor_template")
    canonical_path, canonical_digest = _named_artifact(record, "rename_fixture")
    receipt_path, receipt_digest = _named_artifact(record, "projection_receipt_fixture")
    if template_artifact != Path(f"{executable}#--describe"):
        raise SmokeError("descriptor template locator does not match the executable")
    for path, digest in (
        (executable, executable_digest),
        (canonical_path, canonical_digest),
        (receipt_path, receipt_digest),
    ):
        _require_owner_file(path)
        if _sha256_file(path) != digest:
            raise SmokeError(f"{path.name} digest does not match the Phase A receipt")
    canonical_fixture = canonical_path.read_bytes()
    receipt_fixture = receipt_path.read_bytes()
    describe = _invoke_describe(executable)
    if hashlib.sha256(describe).hexdigest() != template_digest:
        raise SmokeError("descriptor template digest does not match the Phase A receipt")
    template = _read_json_bytes(describe)
    _validate_template(template, canonical_fixture, receipt_fixture)
    descriptor = {
        "schema_version": template["schema_version"],
        "executable_path": str(executable),
        "executable_sha256": executable_digest,
        "argv": template["argv"],
        "max_stdin_bytes": template["max_stdin_bytes"],
        "max_stdout_bytes": template["max_stdout_bytes"],
        "timeout_seconds": template["timeout_seconds"],
    }
    if tuple(descriptor) != STRICT_DESCRIPTOR_FIELDS:
        raise SmokeError("descriptor fields are not in the strict order")
    if not output.parent.exists() or _mode(output.parent) != 0o700:
        raise SmokeError("descriptor output parent is not owner-only")
    serialized = json.dumps(
        descriptor, ensure_ascii=True, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")
    try:
        output_descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise SmokeError("descriptor output already exists") from error
    try:
        _write_all(output_descriptor, serialized)
        os.fsync(output_descriptor)
    finally:
        os.close(output_descriptor)
    _require_owner_file(output, 0o600)
    return descriptor


def _load_descriptor(path: Path) -> InsightsProjectionDescriptor:
    value = _read_json_bytes(path.read_bytes())
    if tuple(value) != STRICT_DESCRIPTOR_FIELDS:
        raise SmokeError("projection descriptor fields are not exact")
    try:
        return InsightsProjectionDescriptor(
            schema_version=value["schema_version"],
            executable_path=value["executable_path"],
            executable_sha256=value["executable_sha256"],
            argv=tuple(value["argv"]),
            max_stdin_bytes=value["max_stdin_bytes"],
            max_stdout_bytes=value["max_stdout_bytes"],
            timeout_seconds=value["timeout_seconds"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SmokeError("projection descriptor is invalid") from error


def _validate_expectation(fixture: dict[str, Any]) -> None:
    if set(fixture) != {
        "schema_version",
        "source_id",
        "events",
        "conformance_expectation",
    }:
        raise SmokeError("host fixture fields are not exact")
    events = fixture["events"]
    expectation = fixture["conformance_expectation"]
    if not isinstance(events, list) or not isinstance(expectation, dict):
        raise SmokeError("host fixture conformance expectation is missing")
    if set(expectation) != _EXPECTATION_FIELDS:
        raise SmokeError("host fixture conformance fields are not exact")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in expectation.values()
    ):
        raise SmokeError("host fixture conformance values are invalid")
    calls = [event for event in events if event.get("event_class") == "tool_call"]
    targets = [
        event.get("tool", {}).get("target_equivalence_id")
        for event in calls
        if isinstance(event.get("tool"), dict)
        and event["tool"].get("target_equivalence_id") is not None
    ]
    frequencies = {target: targets.count(target) for target in set(targets)}
    maximum = max(frequencies.values(), default=0)
    observed = {
        "logical_occurrences": len(calls),
        "distinct_target_equivalence_ids": len(frequencies),
        "maximum_observed_recurrence_per_id": maximum,
        "disclosed_undercount": len(calls) - maximum,
    }
    if observed != expectation:
        raise SmokeError("host fixture conformance expectation does not match its events")


def run(descriptor_path: Path, fixture: Path) -> dict[str, Any]:
    descriptor = _load_descriptor(descriptor_path)
    fixture_bytes = fixture.read_bytes()
    if len(fixture_bytes) > descriptor.max_stdin_bytes:
        raise SmokeError("host fixture exceeds descriptor stdin bound")
    fixture_value = _read_json_bytes(fixture_bytes)
    _validate_expectation(fixture_value)
    provider = P05SmokeInsightsProjectionProvider(descriptor)
    receipt = ProjectionRunner(provider.descriptor()).run(fixture_bytes)
    expected_raw_ids = {
        event.get("raw_event_id") for event in fixture_value["events"] if isinstance(event, dict)
    }
    if receipt.event_count != len(fixture_value["events"]):
        raise SmokeError("projection receipt event count differs from the fixture")
    if set(receipt.raw_to_neutral) != expected_raw_ids:
        raise SmokeError("projection receipt map differs from the fixture events")
    return {"receipt": receipt.to_wire(), "receipt_sha256": receipt.receipt_digest()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="p05_projection_smoke")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--phase-receipts", required=True)
    prepare_parser.add_argument("--output", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--descriptor", required=True)
    run_parser.add_argument(
        "--fixture",
        default=str(
            Path(__file__).resolve().parent.parent
            / "test"
            / "personal_insights"
            / "fixtures"
            / "kiro-renamed-target.json"
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(Path(args.phase_receipts), Path(args.output))
    else:
        result = run(Path(args.descriptor), Path(args.fixture))
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
