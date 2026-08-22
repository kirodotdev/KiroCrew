"""Two controls that were re-verified for one of their reasons only."""

from __future__ import annotations

import ast
import inspect
import io
import json
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote

ESC = chr(27)
PAYLOAD = f"{ESC}[2J{ESC}[1;1HRESTORE SUCCEEDED -- nothing was redacted"

HARDENED: dict[str, object] = {
    "block_public_access": {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    },
    "sse": "AES256",
    "versioning": "Enabled",
    "ownership": "BucketOwnerEnforced",
}


class TestTheDownloadReportCannotBeRepainted:
    """The manifest comes from the downloaded bundle, so every value in it is untrusted."""

    def _bundle(self, tmp_path: Path, replacements: object) -> Path:
        (tmp_path / "MANIFEST.json").write_text(
            json.dumps({"redaction": {"redacted": True, "replacements": replacements}}),
            encoding="utf-8",
        )
        return tmp_path

    def _report(self, d: Path) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            snap._report_redacted_bundle(d)
        return buf.getvalue()

    def test_a_crafted_replacement_count_cannot_emit_terminal_controls(
        self, tmp_path: Path
    ) -> None:
        """Filtering non-integers while SUMMING does not make the count safe to PRINT."""
        out = self._report(self._bundle(tmp_path, {"memory.db": PAYLOAD}))
        assert ESC not in out
        assert "RESTORE SUCCEEDED" in out, "the value is still shown, just inert"

    def test_a_crafted_path_cannot_emit_terminal_controls(self, tmp_path: Path) -> None:
        out = self._report(self._bundle(tmp_path, {PAYLOAD: 3}))
        assert ESC not in out

    def test_every_interpolation_in_the_report_is_escaped_or_computed_here(self) -> None:
        """Guards the class rather than the instance: a new raw field fails this."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(snap._report_redacted_bundle)))
        allowed_bare = {"total", "len(reps)"}
        raw: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            src = ast.unparse(node.value)
            if src in allowed_bare:
                continue
            if (
                isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "_safe_name"
            ):
                continue
            raw.append(src)
        assert not raw, f"manifest-derived values printed without escaping: {raw}"


class _ReachedThePut(Exception):
    """Raised by the stubbed AWS call, so 'the preflight allowed it' is observable."""


class TestTheUploadReassertsEveryControl:
    @pytest.fixture()
    def bundle(self, tmp_path: Path) -> Path:
        f = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        f.write_bytes(b"payload")
        return f

    @pytest.fixture()
    def dest(self) -> remote.Destination:
        return remote.Destination(
            bucket="b",
            region="us-west-2",
            account="123456789012",
            created_at="2026-01-01T00:00:00Z",
        )

    @pytest.fixture(autouse=True)
    def _stub_aws(self, monkeypatch) -> None:
        monkeypatch.setattr(remote, "bucket_policy_state", lambda *a, **k: remote.POLICY_ABSENT)

        def _put(*a, **k):
            raise _ReachedThePut()

        monkeypatch.setattr(remote.engine, "run_aws", _put)

    def test_the_upload_refuses_when_a_control_is_gone(
        self, monkeypatch, bundle: Path, dest: remote.Destination
    ) -> None:
        asked: dict[str, bool] = {}

        def _weakened(*a, **k) -> dict[str, object]:
            asked["yes"] = True
            return {**HARDENED, "block_public_access": {}}

        monkeypatch.setattr(remote, "verify_bucket_private", _weakened)

        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, dest, "prof")
        assert asked.get("yes"), "the upload never asked about the other controls"
        assert "no longer reports the protections" in str(e.value)

    def test_an_unreadable_answer_refuses_rather_than_warns(
        self, monkeypatch, bundle: Path, dest: remote.Destination
    ) -> None:
        monkeypatch.setattr(
            remote,
            "verify_bucket_private",
            lambda *a, **k: {
                "block_public_access": {},
                "sse": None,
                "versioning": None,
                "ownership": None,
            },
        )
        with pytest.raises(remote.DestinationError):
            remote.upload(bundle, dest, "prof")

    def test_the_upload_proceeds_when_every_control_holds(
        self, monkeypatch, bundle: Path, dest: remote.Destination
    ) -> None:
        monkeypatch.setattr(remote, "verify_bucket_private", lambda *a, **k: dict(HARDENED))
        # Reaching the stubbed put is the proof that the preflight allowed the upload.
        with pytest.raises(_ReachedThePut):
            remote.upload(bundle, dest, "prof")

    def test_the_upload_and_the_setup_share_one_predicate(self) -> None:
        """Two spellings of 'private' would drift; both must call the same judge."""
        up = inspect.getsource(remote.upload)
        assert "is_fully_private(" in up, "the upload does not re-assert the predicate"
        assert "verify_bucket_private(" in up
