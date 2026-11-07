"""Optional project-owned evidence check before either keep path mutates the clone."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.sandbox import run_limited

from . import ledger as L

if TYPE_CHECKING:
    from .contracts import Proposal
    from .driver import Driver

MAX_RESPONSE_BYTES = 65536


class AdmissionRefused(RuntimeError):
    """The project checker did not admit this exact request."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate admission response field")
        result[key] = value
    return result


@dataclass(frozen=True)
class CandidateAdmission:
    """A trusted, operator-configured argv; never a shell or candidate-owned script."""

    argv: tuple[str, ...]

    @classmethod
    def from_argv(cls, value: object) -> CandidateAdmission | None:
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in value)
            or not Path(value[0]).is_absolute()
        ):
            raise ValueError("candidateAdmissionCommand requires argv with an absolute executable")
        return cls(tuple(value))

    def check(self, driver: Driver, winner: Proposal) -> None:
        request = {
            "schema_version": "candidate-admission-request-v1",
            "request_id": uuid.uuid4().hex,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "base_sha": driver.head_sha(),
            "branch": driver.branch,
            "candidate_id": winner.cand_id,
            "patch_sha256": hashlib.sha256(winner.diff.encode("utf-8")).hexdigest(),
            "profile_id": driver.profile.id,
            "track": winner.candidate.kind,
            "metric": driver.profile.ruler.primary_name,
            "direction": driver._metric_direction(),
        }
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request_hash = hashlib.sha256(raw).hexdigest()
        try:
            roots = (driver.clone.resolve(), winner.worktree.resolve())
            if any(
                Path(arg).is_absolute()
                and any(Path(arg).resolve().is_relative_to(root) for root in roots)
                for arg in self.argv
            ):
                raise ValueError("checker inputs must be outside candidate-owned trees")
            # Keep arbitrary checker output out of memory and application logs.
            with tempfile.TemporaryFile() as output:
                process = run_limited(
                    self.argv,
                    input=raw,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    cwd=Path(self.argv[0]).parent,
                    timeout=60,
                )
                output.seek(0)
                response_bytes = output.read(MAX_RESPONSE_BYTES + 1)
            if process.returncode != 0 or len(response_bytes) > MAX_RESPONSE_BYTES:
                raise ValueError("checker failed or response too large")
            response = json.loads(response_bytes, object_pairs_hook=_unique_object)
            if (
                not isinstance(response, dict)
                or set(response)
                != {"schema_version", "request_sha256", "status", "assessment_id", "reason"}
                or response["schema_version"] != "candidate-admission-result-v1"
                or response["request_sha256"] != request_hash
                or response["status"] not in {"admitted", "rejected"}
                or not isinstance(response["reason"], str)
                or re.fullmatch(r"[a-z_]{1,64}", response["reason"]) is None
            ):
                raise ValueError("invalid admission response")
            if response["status"] != "admitted":
                raise AdmissionRefused(
                    f"candidate admission refused ({response['reason']}); request={request_hash}"
                )
            if (
                not isinstance(response["assessment_id"], str)
                or re.fullmatch(r"[a-f0-9]{64}", response["assessment_id"]) is None
            ):
                raise ValueError("admission requires an assessment identity")
            if (
                driver.head_sha() != request["base_sha"]
                or hashlib.sha256(winner.diff.encode("utf-8")).hexdigest()
                != request["patch_sha256"]
            ):
                raise ValueError("candidate changed during admission")
        except AdmissionRefused:
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            raise AdmissionRefused(
                f"candidate admission unavailable; request={request_hash}"
            ) from exc


def admit_candidate(driver: Driver, winner: Proposal) -> bool:
    """Refusal is a verification failure, never an archived keep or provisional commit."""
    check = getattr(driver, "candidate_admission", None)
    if check is None:
        return True
    try:
        check.check(driver, winner)
    except Exception as exc:  # A checker failure cannot enable the keep path.
        note = str(exc) if isinstance(exc, AdmissionRefused) else "candidate admission unavailable"
        driver.stats.not_kept += 1
        driver._record(winner, L.STATUS_FAILED_VERIFY, note)
        driver.log.warning("%s", note)
        return False
    return True
