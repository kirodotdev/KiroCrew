"""Load the authoritative ACP v1 wire vocabulary from the vendored upstream schema.

This module is the *independence seam* for the black-box conformance gate. It
reads the ACP v1 artefacts vendored verbatim from upstream (see
``test/conformance/vendor/acp-v1/VENDOR.md`` — tag ``schema-v1.21.0``, commit
``272bf799f35a258c6a4107a0410ed361e83683d3``) and exposes the closed vocabularies
the wire protocol defines:

* method names (``agentMethods`` / ``clientMethods`` / ``protocolMethods`` from
  ``meta.json``),
* the ``$defs/StopReason`` enum, ``$defs/SessionUpdate`` kinds,
  ``$defs/ContentBlock`` type tags, and ``$defs/PermissionOptionKind`` enum from
  ``schema.json``,
* the negotiated protocol version (``meta.json`` ``version``).

Crucially it imports **nothing** from :mod:`kiro_crew` — the vocabulary comes
from the pinned upstream files, so the conformance oracle built on it can catch a
drift between Kiro Crew's own :mod:`kiro_crew.acp.types` and the real protocol
(the cross-check in ``test_acp_v1_conformance_vendor`` asserts exactly that).

No JSON Schema validator is available offline (``jsonschema`` is absent and an
unpinned dependency is disallowed), so this module extracts the closed
vocabularies rather than running full Draft-2020-12 validation. See VENDOR.md
"Known limitation".
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from typing import Any

VENDOR_DIR = os.path.join(os.path.dirname(__file__), "conformance", "vendor", "acp-v1")

# Immutable upstream pin this vendored copy was taken from. Recorded here so a
# test can assert the tree was not silently re-pointed. Mirrors VENDOR.md.
PINNED_TAG = "schema-v1.21.0"
PINNED_COMMIT = "272bf799f35a258c6a4107a0410ed361e83683d3"

# JSON-RPC 2.0 standard error codes (https://www.jsonrpc.org/specification) plus
# the ACP ``-32800`` "cancelled" extension documented in schema.json's
# CancelRequestNotification. These are the JSON-RPC/ACP standard — NOT Kiro Crew
# constants — so they belong to the independent oracle. -32800 is optional: an
# agent may answer a cancelled request with a valid response instead.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_CANCELLED = -32800
STANDARD_JSONRPC_ERROR_CODES = frozenset(
    {
        JSONRPC_PARSE_ERROR,
        JSONRPC_INVALID_REQUEST,
        JSONRPC_METHOD_NOT_FOUND,
        JSONRPC_INVALID_PARAMS,
        JSONRPC_INTERNAL_ERROR,
    }
)
# The full set an ACP agent may legitimately emit (adds the cancelled extension).
ALLOWED_JSONRPC_ERROR_CODES = STANDARD_JSONRPC_ERROR_CODES | {JSONRPC_CANCELLED}


class VendorError(RuntimeError):
    """The vendored ACP v1 schema is missing, corrupt, or fails its checksum."""


def _path(name: str) -> str:
    return os.path.join(VENDOR_DIR, name)


@functools.lru_cache(maxsize=None)
def _load_json(name: str) -> Any:
    try:
        with open(_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:  # pragma: no cover - vendoring is required
        raise VendorError(
            f"vendored ACP v1 file {name!r} not found under {VENDOR_DIR}; "
            "see test/conformance/vendor/acp-v1/UPDATING.md"
        ) from exc
    except (ValueError, OSError) as exc:  # pragma: no cover
        raise VendorError(f"vendored ACP v1 file {name!r} is unreadable: {exc}") from exc


@functools.lru_cache(maxsize=None)
def meta() -> dict[str, Any]:
    return _load_json("meta.json")


@functools.lru_cache(maxsize=None)
def schema() -> dict[str, Any]:
    return _load_json("schema.json")


def _defs() -> dict[str, Any]:
    doc = schema()
    return doc.get("$defs") or doc.get("definitions") or {}


def _const_members(node: dict[str, Any]) -> list[str]:
    """Collect ``const`` string values from a ``oneOf``/``anyOf`` union node."""
    out: list[str] = []
    for branch in node.get("oneOf", node.get("anyOf", [])):
        if isinstance(branch, dict) and isinstance(branch.get("const"), str):
            out.append(branch["const"])
    return out


def _discriminator_members(node: dict[str, Any], prop: str) -> list[str]:
    """Collect the ``const`` of *prop* across a union node's branches."""
    out: list[str] = []
    for branch in node.get("oneOf", node.get("anyOf", [])):
        if not isinstance(branch, dict):
            continue
        disc = branch.get("properties", {}).get(prop, {})
        if isinstance(disc, dict) and isinstance(disc.get("const"), str):
            out.append(disc["const"])
    return out


# ── authoritative vocabularies (from the vendored upstream files) ──


@functools.lru_cache(maxsize=None)
def protocol_version() -> int:
    version = meta().get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise VendorError(f"meta.json 'version' is not an int: {version!r}")
    return version


def _method_set(key: str) -> frozenset[str]:
    table = meta().get(key)
    if not isinstance(table, dict):
        raise VendorError(f"meta.json {key!r} is not an object")
    return frozenset(str(v) for v in table.values())


@functools.lru_cache(maxsize=None)
def agent_methods() -> frozenset[str]:
    return _method_set("agentMethods")


@functools.lru_cache(maxsize=None)
def client_methods() -> frozenset[str]:
    return _method_set("clientMethods")


@functools.lru_cache(maxsize=None)
def protocol_methods() -> frozenset[str]:
    return _method_set("protocolMethods")


@functools.lru_cache(maxsize=None)
def all_methods() -> frozenset[str]:
    return agent_methods() | client_methods() | protocol_methods()


def _def_members(def_name: str, extract) -> frozenset[str]:
    node = _defs().get(def_name)
    if not isinstance(node, dict):
        raise VendorError(f"schema.json $defs/{def_name} missing or not an object")
    members = extract(node)
    if not members:
        raise VendorError(f"schema.json $defs/{def_name} yielded no members")
    return frozenset(members)


@functools.lru_cache(maxsize=None)
def stop_reasons() -> frozenset[str]:
    return _def_members("StopReason", _const_members)


@functools.lru_cache(maxsize=None)
def session_update_kinds() -> frozenset[str]:
    return _def_members("SessionUpdate", lambda n: _discriminator_members(n, "sessionUpdate"))


@functools.lru_cache(maxsize=None)
def content_block_types() -> frozenset[str]:
    return _def_members("ContentBlock", lambda n: _discriminator_members(n, "type"))


@functools.lru_cache(maxsize=None)
def permission_option_kinds() -> frozenset[str]:
    return _def_members("PermissionOptionKind", _const_members)


@functools.lru_cache(maxsize=None)
def config_option_categories() -> frozenset[str]:
    """The reserved ``SessionConfigOptionCategory`` const values (mode/model/...).

    The category union also has a free-form ``other`` string branch (no const),
    which ``_const_members`` skips — so this returns only the reserved, spec-named
    categories, which is exactly what a cross-check against Kiro Crew's emitted
    ``"model"`` category needs.
    """
    return _def_members("SessionConfigOptionCategory", _const_members)


# ── provenance verification ──


def verify_provenance() -> dict[str, Any]:
    """Verify the vendored files against SHA256SUMS + basic schema invariants.

    Raises :class:`VendorError` on any mismatch. Returns a small summary dict
    (dialect, version, checked-file count) for a test to assert against.
    """
    sums_path = _path("SHA256SUMS")
    try:
        with open(sums_path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError as exc:
        raise VendorError(f"SHA256SUMS unreadable: {exc}") from exc
    if not lines:
        raise VendorError("SHA256SUMS is empty")
    checked = 0
    for line in lines:
        expected, _, name = line.partition("  ")
        name = name.strip()
        if not expected or not name:
            raise VendorError(f"malformed SHA256SUMS line: {line!r}")
        try:
            with open(_path(name), "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
        except OSError as exc:
            raise VendorError(f"vendored file {name!r} missing for checksum") from exc
        if actual != expected:
            raise VendorError(f"checksum mismatch for {name!r}: expected {expected}, got {actual}")
        checked += 1

    dialect = schema().get("$schema", "")
    if "2020-12" not in dialect:
        raise VendorError(f"schema.json is not Draft 2020-12: {dialect!r}")
    if protocol_version() != 1:
        raise VendorError(f"unexpected ACP wire version: {protocol_version()}")
    return {
        "tag": PINNED_TAG,
        "commit": PINNED_COMMIT,
        "dialect": dialect,
        "version": protocol_version(),
        "checked_files": checked,
    }
