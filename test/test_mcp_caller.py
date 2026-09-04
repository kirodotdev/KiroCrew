"""Unit tests for :mod:`kiro_crew.mcp_caller` — KIROCREW_HOST_PID resolution.

The fork carries only the host-pid env-shortcut tests here (the wider
caller-identity wire-contract suite lives upstream); each test resets the
fork-only process-lifetime ``_FROM_ENV_CACHE`` so a previously-resolved
identity cannot leak between tests.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

import kiro_crew.mcp_caller
from kiro_crew.mcp_caller import (
    _TENANT_NONCE_BYTES,
    TENANT_META_KEY,
    TENANT_SCHEMA_VERSION,
    CallerContext,
    build_caller_meta,
    build_tenant_meta,
    new_tenant_nonce,
    tenant_nonce_from_meta,
)


def test_from_env_uses_host_pid_env_before_walk(tmp_path, monkeypatch) -> None:
    """The sandbox launcher exports KIROCREW_HOST_PID (its own host pid — the
    exact pid the gateway keys session_pid files by). from_env must resolve
    via that env var directly, without depending on the /proc ancestor walk,
    which cannot match when the process's pid view diverges from the host's
    (PID-namespace sandboxing)."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    # File keyed by a pid that is NOT in this test process's real ancestry —
    # only the env var can find it.
    pid_file = tmp_path / "session_pid_987654.txt"
    pid_file.write_text("hostpid-session-789", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "987654"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "hostpid-session-789"
    assert ctx.session_type == "pidfile"


def test_from_env_host_pid_missing_file_falls_back_to_walk(tmp_path, monkeypatch) -> None:
    """A stale/dangling KIROCREW_HOST_PID (no matching file) must not break
    the existing ancestor-walk fallback."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    parent_pid = os.getppid()
    (tmp_path / f"session_pid_{parent_pid}.txt").write_text(
        "walk-session-111", encoding="utf-8"
    )

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "999999"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "walk-session-111"


# --- Per-connection tenant nonce (#5322) ------------------------------------
#
# The nonce exists for the case the caller block cannot serve: a connection the
# gateway cannot NAME. It is a namespace separator, so what these pin is that it
# stays one -- parsed leniently, never promoted to an identity, and never derived
# from anything a stub supplies.


def test_a_tenant_block_carries_the_nonce_and_nothing_else() -> None:
    meta = build_tenant_meta("n0nce")
    assert meta[TENANT_META_KEY]["nonce"] == "n0nce"
    assert meta[TENANT_META_KEY]["schemaVersion"] == TENANT_SCHEMA_VERSION
    assert tenant_nonce_from_meta(meta) == "n0nce"


def test_a_tenant_block_is_not_an_identity() -> None:
    """The separation the whole design rests on.

    If ``from_meta`` accepted a tenant block, an unnamed caller would reach every
    consumer of a session key -- cron ownership, callback routing, audit --
    carrying a value that names no principal while looking resolved.
    """
    assert CallerContext.from_meta(build_tenant_meta("n0nce")) is None


def test_a_caller_block_carries_no_nonce() -> None:
    """And the converse: the two blocks are independent, so a named caller's
    frame does not smuggle a separator the fallback would then append."""
    assert tenant_nonce_from_meta(build_caller_meta(CallerContext(session_key="s"))) == ""


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "not a dict",
        {},
        {TENANT_META_KEY: "not a dict"},
        {TENANT_META_KEY: {}},
        {TENANT_META_KEY: {"nonce": "n"}},  # no schemaVersion
        {TENANT_META_KEY: {"schemaVersion": "1", "nonce": "n"}},  # not an int
        {TENANT_META_KEY: {"schemaVersion": 0, "nonce": "n"}},  # below v1
        {TENANT_META_KEY: {"schemaVersion": 1}},  # no nonce
        {TENANT_META_KEY: {"schemaVersion": 1, "nonce": 7}},  # not a string
    ],
)
def test_a_malformed_tenant_block_reads_as_absent(meta) -> None:
    """Every bad shape degrades to "no separator", never to an exception.

    The consumer's fallback (its own process) is correct in the 1:1 topology, so
    an unparseable block must land there rather than failing the tool call.
    """
    assert tenant_nonce_from_meta(meta) == ""


def test_an_unknown_schema_version_is_read_additively() -> None:
    """Same forward-compatibility rule as the caller block: a v2 gateway talking
    to a v1 backend must still get its nonce across."""
    assert tenant_nonce_from_meta(
        {TENANT_META_KEY: {"schemaVersion": 99, "nonce": "n0nce", "future": 1}}
    ) == "n0nce"


def test_each_minted_nonce_is_distinct_and_unguessable() -> None:
    """Two connections must never collide, and one stub must not be able to
    predict a peer's namespace from its own."""
    minted = {new_tenant_nonce() for _ in range(100)}
    assert len(minted) == 100
    assert all(len(n) == _TENANT_NONCE_BYTES * 2 for n in minted)
