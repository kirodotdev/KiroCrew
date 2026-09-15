"""Unit tests for scripts/check_connector_discovery.py.

The discovery validator's whole value is where it draws the line: it rules on
the discovery-unique shapes (request, response, capability rows) and refuses to
rule on the four manifest/run/receipt fields the manifest validator owns. Both
halves need pinning. A rule that silently widens would put a second decision
point on a manifest field -- the single-contract defect the slice exists to
avoid; a rule that silently narrows would let a malformed probe through.

``validate_document`` is pure, so the whole matrix is tested without a
repository or any file I/O.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_connector_discovery.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_connector_discovery", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_connector_discovery"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _good_doc() -> dict:
    return {
        "discovery_request": {
            "provider": "example",
            "account_binding": "binding-1",
            "requested_scope_hint": ["read"],
        },
        "discovery_response": {
            "provider": "example",
            "observed_at": "2026-01-01T00:00:00Z",
            "scope_snapshot": ["read", "write"],
            "capability_rows": [
                {
                    "operation_id": "example.read",
                    "raw_capability_signature": "read",
                    "matches_manifest": True,
                },
                {
                    "operation_id": None,
                    "raw_capability_signature": "vendor.extra",
                    "matches_manifest": False,
                },
            ],
            "version_snapshot": "v3",
        },
    }


class TestWellFormed:
    def test_full_document_passes(self):
        assert gate.validate_document(_good_doc()).ok

    def test_request_only_passes(self):
        assert gate.validate_document({"discovery_request": _good_doc()["discovery_request"]}).ok

    def test_response_only_passes(self):
        assert gate.validate_document({"discovery_response": _good_doc()["discovery_response"]}).ok

    def test_requested_scope_hint_is_optional(self):
        doc = _good_doc()
        del doc["discovery_request"]["requested_scope_hint"]
        assert gate.validate_document(doc).ok


class TestRequestRules:
    @pytest.mark.parametrize("bad", ["", "   ", None, 5, ["p"]])
    def test_provider_must_be_nonempty_string(self, bad):
        doc = {"discovery_request": {"provider": bad, "account_binding": "b"}}
        assert not gate.validate_document(doc).ok

    @pytest.mark.parametrize("bad", ["", "   ", None, 5])
    def test_account_binding_must_be_nonempty_string(self, bad):
        doc = {"discovery_request": {"provider": "p", "account_binding": bad}}
        assert not gate.validate_document(doc).ok

    def test_requested_scope_hint_must_be_array_when_present(self):
        doc = {
            "discovery_request": {
                "provider": "p",
                "account_binding": "b",
                "requested_scope_hint": "read",
            }
        }
        assert not gate.validate_document(doc).ok


class TestResponseRules:
    @pytest.mark.parametrize(
        "missing",
        ["provider", "observed_at", "version_snapshot", "scope_snapshot", "capability_rows"],
    )
    def test_required_fields(self, missing):
        doc = _good_doc()
        del doc["discovery_response"][missing]
        assert not gate.validate_document(doc).ok

    def test_scope_snapshot_must_be_array(self):
        doc = _good_doc()
        doc["discovery_response"]["scope_snapshot"] = "read"
        assert not gate.validate_document(doc).ok

    def test_capability_rows_must_be_array(self):
        doc = _good_doc()
        doc["discovery_response"]["capability_rows"] = {}
        assert not gate.validate_document(doc).ok


class TestCapabilityRow:
    def test_operation_id_null_passes(self):
        doc = _good_doc()
        doc["discovery_response"]["capability_rows"] = [
            {
                "operation_id": None,
                "raw_capability_signature": "sig",
                "matches_manifest": False,
            }
        ]
        assert gate.validate_document(doc).ok

    def test_operation_id_key_absent_fails(self):
        # null is a value; a missing key must not be read as null.
        doc = _good_doc()
        row = doc["discovery_response"]["capability_rows"][0]
        del row["operation_id"]
        assert not gate.validate_document(doc).ok

    @pytest.mark.parametrize("bad", ["", "   ", 5])
    def test_operation_id_bad_string_fails(self, bad):
        doc = _good_doc()
        doc["discovery_response"]["capability_rows"][0]["operation_id"] = bad
        assert not gate.validate_document(doc).ok

    @pytest.mark.parametrize("bad", ["true", "false", 0, 1, None])
    def test_matches_manifest_must_be_real_bool(self, bad):
        doc = _good_doc()
        doc["discovery_response"]["capability_rows"][0]["matches_manifest"] = bad
        assert not gate.validate_document(doc).ok

    @pytest.mark.parametrize("bad", ["", "   ", None, 5])
    def test_raw_capability_signature_must_be_nonempty_string(self, bad):
        doc = _good_doc()
        doc["discovery_response"]["capability_rows"][0]["raw_capability_signature"] = bad
        assert not gate.validate_document(doc).ok


class TestDocumentShape:
    def test_empty_object_fails(self):
        assert not gate.validate_document({}).ok

    @pytest.mark.parametrize("bad", [[], "x", 5, None])
    def test_non_object_fails(self, bad):
        assert not gate.validate_document(bad).ok


class TestSingleContractBoundary:
    """The four manifest fields are not this gate's to rule on.

    A discovery object never carries snapshot_ref / source_status / source_kind /
    schema_version, so their presence or shape must not change this validator's
    verdict either way -- a stray copy on a discovery object is simply ignored
    here, because the manifest validator is their sole enforcer.
    """

    def test_stray_manifest_fields_are_ignored(self):
        doc = _good_doc()
        doc["discovery_response"]["snapshot_ref"] = 12345  # nonsense shape
        doc["discovery_response"]["source_status"] = "not-a-real-value"
        doc["discovery_response"]["source_kind"] = "not-a-real-value"
        doc["discovery_response"]["schema_version"] = {"nested": "junk"}
        # Still valid: the discovery-unique shape is intact and the four fields
        # are outside this gate's contract.
        assert gate.validate_document(doc).ok


class TestSelfTestAndCli:
    def test_builtin_selftest_passes(self):
        assert gate._selftest() == 0

    def test_help_exits_zero(self, capsys):
        assert gate.main(["prog", "--help"]) == 0

    def test_no_args_is_usage_error(self):
        assert gate.main(["prog"]) == 2

    def test_validates_file_ok(self, tmp_path):
        p = tmp_path / "discovery.json"
        p.write_text(json.dumps(_good_doc()), encoding="utf-8")
        assert gate.main(["prog", str(p)]) == 0

    def test_validates_file_fail(self, tmp_path):
        bad = copy.deepcopy(_good_doc())
        bad["discovery_request"]["provider"] = ""
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        assert gate.main(["prog", str(p)]) == 1

    def test_non_json_file_is_usage_error(self, tmp_path):
        p = tmp_path / "not.json"
        p.write_text("this is not json", encoding="utf-8")
        assert gate.main(["prog", str(p)]) == 2

    def test_non_utf8_file_is_usage_error(self, tmp_path):
        # A UnicodeDecodeError (a ValueError subclass) must be caught, not crash:
        # the byte 0xff is invalid UTF-8, so the file cannot be read as a document.
        p = tmp_path / "binary.json"
        p.write_bytes(b"\xff\xfe\x00\x01not utf-8")
        assert gate.main(["prog", str(p)]) == 2

    def test_nul_in_path_is_usage_error(self):
        # open() raises a bare ValueError("embedded null byte") on a NUL in the
        # path argument -- a ValueError that escapes OSError and JSONDecodeError,
        # the same family the manifest validator guards. It must exit 2, not crash.
        assert gate.main(["prog", "/tmp/no\x00such.json"]) == 2
