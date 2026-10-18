"""Tests for on-demand minting of a Connections provider's approval URL.

This file holds grant detection and its audit trail, the rule that no mint coroutine
touches the filesystem on the event loop, the boot-cost and grant-key drift guards, the
HTTP surface with the paths it drives (cancel, expiry, row tokens, and the unlink guards
for symlinked, relative and junction rows) and ``cancel_mint``. The mint flow's session
shape, result matrix, watcher and URL screen are in ``test_connections_mint_flow.py``;
how the ephemeral agent-spec files are recorded and cleaned up is in
``test_connections_mint_spec_files.py``. The fake ACP client and the autouse isolation
the three share are in ``connections_mint_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import connections_mint_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from connections_mint_helpers import (
    _AUTHORIZE,
    _URL,
    _FakeClient,
    _grant_reads_recorded,
    _state_only,
    _write_paired_grant_artifacts,
)
from dashboard_owner_helpers import as_owner

from conftest import make_dir_link, requires_symlinks
from kiro_crew import hooks, mcp_grant
from kiro_crew.connections import mint
from kiro_crew.dashboard.handlers import connections
from kiro_crew.testing.wait import async_wait_until, await_until

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_mint = connections_mint_helpers._isolated_mint
protected_pids = connections_mint_helpers.protected_pids


# ── grant detection ──


def test_a_grant_requires_both_paired_artifacts(tmp_path: Path):
    key = mcp_grant.grant_key(_URL)
    assert mcp_grant.grant_presence(_URL, cache_dir=tmp_path) is False

    (tmp_path / f"{key}.token.json").write_text("{}", encoding="utf-8")
    # A lone token file also matches the single-file SSO naming in this dir.
    assert mcp_grant.grant_presence(_URL, cache_dir=tmp_path) is False

    (tmp_path / f"{key}.registration.json").write_text("{}", encoding="utf-8")
    assert mcp_grant.grant_presence(_URL, cache_dir=tmp_path) is True


def test_grant_artifact_paths_share_the_presence_layout(tmp_path: Path):
    key = mcp_grant.grant_key(_URL)

    assert mcp_grant.grant_artifact_paths(_URL, cache_dir=tmp_path) == (
        tmp_path / f"{key}.token.json",
        tmp_path / f"{key}.registration.json",
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://mcp.example.com/mcp", "https://MCP.Example.com/mcp"),
        ("https://mcp.example.com/mcp", "https://mcp.example.com:443/mcp"),
        ("https://mcp.example.com", "https://mcp.example.com/"),
        ("https://bücher.example/mcp", "https://xn--bcher-kva.example/mcp"),
    ],
)
def test_the_grant_key_normalizes_the_way_the_runtime_does(left: str, right: str):
    assert mcp_grant.grant_key(left) == mcp_grant.grant_key(right)


def test_the_grant_key_keeps_ipv6_origin_brackets():
    expected = hashlib.sha256(b"https://[2001:db8::1]/mcp").hexdigest()

    assert mcp_grant.grant_key("https://[2001:db8::1]/mcp") == expected


def test_the_grant_key_separates_distinct_endpoints():
    assert mcp_grant.grant_key(_URL) != mcp_grant.grant_key("https://mcp.example.com/other")


# ── the grant read stays off the event loop ──
#
# The predicate stats the user's home. That is sub-millisecond locally and
# unbounded on a network-mounted home, and on the loop an unbounded stat takes the
# gateway's heartbeat with it. Both call sites therefore run it in a worker thread.
# ``grant_presence`` is left UNPATCHED in these two -- only wrapped to record the
# thread -- so each asserts the verdict still resolves through the wrapped path
# rather than only that a thread was used.


@pytest.mark.asyncio
async def test_the_reconnect_short_circuit_reads_a_real_grant_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    _write_paired_grant_artifacts(_URL)
    seen: list[int] = []
    monkeypatch.setattr(mcp_grant, "grant_presence", _grant_reads_recorded(seen))

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    # The artifact stat is still what decides whether to VALIDATE at all -- it
    # spawns no process by itself. Validation itself spawns one (and disposes it
    # once the verdict is in), which is a separate, deliberate cost from this stat.
    assert len(_FakeClient.instances) == 1
    assert _FakeClient.instances[-1].shutdowns == 1
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_the_watcher_reads_a_real_grant_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"

    # After the spawn, so the short-circuit above does not consume the grant and
    # the recorder only sees the watcher's own reads -- and the recorder BEFORE
    # the grant becomes visible, so no poll issued after this point can read the
    # grant through the un-instrumented predicate.
    unwrapped = mcp_grant.grant_presence
    seen: list[int] = []
    monkeypatch.setattr(mcp_grant, "grant_presence", _grant_reads_recorded(seen))
    # Ordering guard: the recorder is live while the grant is still invisible.
    # Probes through the ORIGINAL predicate so this thread never enters ``seen``.
    assert not unwrapped(_URL)
    # Barrier for the in-flight tail: ``grant_observed`` freezes the predicate
    # object at ``to_thread`` submission, so a poll submitted before the setattr
    # above can still complete after the write below. The watcher polls
    # sequentially, so once one recorded (necessarily negative) read has landed,
    # no un-instrumented poll remains in flight and the grant may become visible.
    await async_wait_until(
        lambda: bool(seen), describe=lambda: "no recorded watcher poll arrived before the deadline"
    )
    assert seen, "no recorded watcher poll arrived before the deadline"
    _write_paired_grant_artifacts(_URL)

    await await_until(entry["watcher"])

    assert mint._mints["notion"]["state"] == "granted"
    assert seen and threading.get_ident() not in seen


def test_an_unreadable_artifact_is_unknowable_rather_than_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    """The middle answer is the whole point of the tri-state, and it is fragile.

    ``Path.is_file()`` swallows EVERY ``OSError`` from Python 3.14 on and answers
    ``False``, so a permission error or a stalled mount would be indistinguishable
    from "nothing was ever written" -- and the probe would tell the owner of an
    already-authorized server to sign in again. This package declares
    ``requires-python >= 3.10`` with no ceiling, so that version is allowed and the
    derivation cannot rest on ``is_file()``.

    The fake defines ONLY ``stat``: an implementation that reached for ``is_file()``
    would raise ``AttributeError`` here rather than quietly answering ``False``.
    """

    class _Unreadable:
        def stat(self, *a: object, **kw: object) -> object:
            raise PermissionError("EACCES")

    monkeypatch.setattr(
        mcp_grant, "grant_artifact_paths", lambda url, **kw: (_Unreadable(), _Unreadable())
    )

    assert mcp_grant.grant_presence(_URL) is None


# ── the grant-presence stat is SEL-audited ──
#
# The paired artifacts live under ``~/.aws/sso/cache``, a directory
# ``security._SENSITIVE_HOME_DIRS`` classifies. They are stat-ed and never opened,
# so no token material enters the process and there is no content for
# ``hooks.safe_read_file_internal`` to gate -- and the per-provider name is a
# digest, so there is no fixed path to register with it either. What the access
# still owes is a trail, which is what these pin.


def _audit_calls_recorded(monkeypatch: pytest.MonkeyPatch, recorded: bool = True) -> list[Any]:
    """Capture ``(read_id, outcome, thread)`` per audit emission, suppressing SEL."""
    seen: list[Any] = []

    def fake(read_id: str, outcome: str) -> bool:
        seen.append((read_id, outcome, threading.get_ident()))
        return recorded

    monkeypatch.setattr(mcp_grant._hooks, "emit_internal_read_audit", fake)
    return seen


def test_the_grant_presence_read_id_is_registered_for_audit():
    """Registration is what makes the audit real, not the call site.

    ``emit_internal_read_audit`` rejects an unregistered ``read_id`` and returns
    False WITHOUT emitting, so a call site whose id is missing from the registry
    records nothing while looking audited at the point of use.
    """
    assert mcp_grant._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS


@pytest.mark.asyncio
async def test_observing_a_grant_audits_once_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    seen = _audit_calls_recorded(monkeypatch)
    _write_paired_grant_artifacts(_URL)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert [(rid, outcome) for rid, outcome, _ in seen] == [
        (mcp_grant._GRANT_PRESENCE_READ_ID, "success")
    ]
    # Critical SEL events drain the queue on the calling thread.
    assert threading.get_ident() not in [thread for _, _, thread in seen]


@pytest.mark.asyncio
async def test_polling_for_an_absent_grant_never_audits(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    """Otherwise one flow writes a synchronous event every poll for the whole TTL."""
    monkeypatch.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.001)
    seen = _audit_calls_recorded(monkeypatch)

    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    assert entry["state"] == "waiting"
    # Several polls, every one of them negative.
    await asyncio.sleep(0.05)

    assert seen == []
    # Releases the still-waiting watcher, so the poll loop does not outlive the test.
    await mint._dispose_mint(entry)


@pytest.mark.asyncio
async def test_an_unreadable_lookup_audits_as_unreadable(monkeypatch: pytest.MonkeyPatch):
    """ "Could not look" is its own audit outcome, not folded into "missing"."""

    class _Unreadable:
        def stat(self, *a: object, **kw: object) -> object:
            raise PermissionError("EACCES")

    monkeypatch.setattr(
        mcp_grant, "grant_artifact_paths", lambda url, **kw: (_Unreadable(), _Unreadable())
    )
    seen = _audit_calls_recorded(monkeypatch)

    assert await mcp_grant.grant_observed(_URL, audit_absence=True) is None

    assert [(rid, outcome) for rid, outcome, _ in seen] == [
        (mcp_grant._GRANT_PRESENCE_READ_ID, "unreadable")
    ]


@pytest.mark.asyncio
async def test_a_caller_that_acts_on_absence_audits_the_absence(
    monkeypatch: pytest.MonkeyPatch,
):
    """A negative that DRIVES a verdict owes a trail as much as a positive one.

    The mint polls for a grant to appear, so its negatives change nothing and stay
    unaudited. The probe reads once and renders either answer -- an absent pair is
    what makes a row read "Sign-in required" -- so it opts in, and the access is
    recorded whichever way the stat came out.
    """
    seen = _audit_calls_recorded(monkeypatch)

    assert await mcp_grant.grant_observed(_URL, audit_absence=True) is False

    assert [(rid, outcome) for rid, outcome, _ in seen] == [
        (mcp_grant._GRANT_PRESENCE_READ_ID, "missing")
    ]
    # Critical SEL events drain the queue, so this stays off the event loop.
    assert threading.get_ident() not in [thread for _, _, thread in seen]


@pytest.mark.asyncio
async def test_an_unrecordable_audit_does_not_withhold_the_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    """Deliberately fail-OPEN, unlike ``safe_read_file_internal``.

    Nothing sensitive crosses this boundary, so denying on an unrecordable audit
    would only strand a Connect the user actually completed.
    """
    seen = _audit_calls_recorded(monkeypatch, recorded=False)
    _write_paired_grant_artifacts(_URL)

    await mint.start_oauth_mint("notion", _URL)

    assert _state_only(mint.pending_mint_for("notion")) == {"state": "granted"}
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_the_unaudited_warning_names_the_key_not_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """This warning lands in gateway.log, which is not a credential store.

    The lookup is reachable for ANY endpoint a user configured, not just a vetted
    registry one, so the url can carry a credential in its userinfo or query
    string. The sha256 cache key identifies the artifacts consulted without
    carrying anything back out.
    """
    secret_url = "https://user:sup3r-secret@mcp.example.com/mcp?token=abcd1234"
    _audit_calls_recorded(monkeypatch, recorded=False)
    _write_paired_grant_artifacts(secret_url)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_grant"):
        assert await mcp_grant.grant_observed(secret_url) is True

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "proceeding unaudited" in blob, "the fail-open warning must still be emitted"
    assert "sup3r-secret" not in blob
    assert "abcd1234" not in blob
    assert "mcp.example.com" not in blob
    assert mcp_grant.grant_key(secret_url) in blob


# ── every OTHER filesystem touch stays off the event loop too ──
#
# Same reasoning as the grant read above, applied to the rest of the module: the
# agents directory and our own state dir can sit on the same network mount as the
# home, and an unbounded operation on the loop stalls every request. Each test
# WRAPS the real function to record the thread rather than patching it away, so it
# asserts the outcome still resolves through the wrapped path -- a to_thread hop
# that lost its result would pass a thread-only assertion.


def _calls_recorded(fn: Any, seen: list[int]) -> Any:
    """``fn`` itself, plus the id of the thread each call ran on."""

    def recorded(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return fn(*args, **kwargs)

    return recorded


@pytest.mark.asyncio
async def test_the_spec_removal_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    assert spec_path.exists()
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_remove_mint_agent_spec", _calls_recorded(mint._remove_mint_agent_spec, seen)
    )

    await mint._dispose_mint(entry)

    # Off the loop, and the file it was meant to release is really gone.
    assert seen and threading.get_ident() not in seen
    assert not spec_path.exists()


@pytest.mark.asyncio
async def test_a_teardown_cancelled_from_outside_still_releases_the_spec_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, protected_pids: set[int]
):
    # The removal is the last act of a teardown that can be cancelled mid-flight,
    # so it is shielded: moving it to a worker thread must not make a cancellation
    # able to drop it, or a released spec stays on disk as a selectable agent.
    class _HangingShutdown(_FakeClient):
        async def shutdown(self) -> None:
            self.shutdowns += 1
            await asyncio.Event().wait()

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _HangingShutdown)
    await mint.start_oauth_mint("notion", _URL)
    entry = mint._mints["notion"]
    spec_path = Path(entry["spec_path"])
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_remove_mint_agent_spec", _calls_recorded(mint._remove_mint_agent_spec, seen)
    )

    task = asyncio.create_task(mint._dispose_mint(entry))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert not spec_path.exists()
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_the_agents_dir_read_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, _isolated_mint: Path
):
    class _NoChallenge(_FakeClient):
        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)
    # A concurrent uninstall: the real predicate has to actually find the entry
    # gone, so the recorded verdict is the one the flow acts on.
    (_isolated_mint / "kirocrew.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    seen: list[int] = []
    monkeypatch.setattr(
        mint, "_agent_spec_entry_missing", _calls_recorded(mint._agent_spec_entry_missing, seen)
    )

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    assert mint._mints["notion"]["reason"] == "mint_server_absent"


@pytest.mark.asyncio
async def test_the_data_home_resolution_runs_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    # Resolving the data home CREATES it under a KIROCREW_HOME override, so it is a
    # write and not a path join.
    seen: list[int] = []
    monkeypatch.setattr(mint, "data_home", _calls_recorded(mint.data_home, seen))

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    # The resolved value still reaches the client it was resolved for.
    assert _FakeClient.instances[-1].kwargs["work_dir"] == mint.data_home() / "connections" / "mint"
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_the_outcome_audit_runs_off_the_loop(monkeypatch: pytest.MonkeyPatch):
    # Only the APPEND is queued to SEL's writer thread. The first sel() of a
    # process constructs the log first -- trust dir, key validation, a backward
    # scan of the existing log -- on whichever thread calls it, so the audit has
    # to be reached off the loop even though later calls are cheap.
    seen: list[int] = []
    monkeypatch.setattr(mint, "_log_mint_outcome", _calls_recorded(mint._log_mint_outcome, seen))

    await mint.start_oauth_mint("notion", _URL)

    assert seen and threading.get_ident() not in seen
    await mint._dispose_mint(mint._mints["notion"])


# ── the invariant itself: no coroutine here may touch the filesystem ──
#
# Derived from the module's own AST rather than a maintained list, so a NEW helper
# that reaches the filesystem is covered the moment it is called from a coroutine.
# This is what makes the rule a property of the module instead of a fact about the
# call sites that happen to exist today.

# Filesystem primitives, as they appear in a call. Ambiguous string methods
# (``replace``, ``rename``) are deliberately absent: any helper that would use
# them here also stats or writes, so it is already reached through those.
_FS_ATTRS = frozenset(
    {
        "unlink",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "is_file",
        "is_dir",
        "is_symlink",
        "exists",
        "stat",
        "lstat",
        "mkdir",
        "makedirs",
        "rmdir",
        "iterdir",
        "glob",
        "rglob",
        "listdir",
        "scandir",
        "home",
        "_atomic_json_write",
        "write_owner_derived_spec",
        "_load_json",
        "kiro_agents_dir_path",
        # Reaches the filesystem through the SEL singleton (see ``sel`` below):
        # the event is marked critical, so it drains the queue on the calling
        # thread rather than merely enqueueing. Listed so a future DIRECT call
        # from a coroutine fails here instead of silently blocking the loop --
        # ``grant_observed`` passes it to ``asyncio.to_thread`` today.
        "emit_internal_read_audit",
    }
)
# Bare-name calls that reach the filesystem: the builtin, the imported resolvers
# that create the directory they answer for, and the audit singleton -- whose FIRST
# call in a process constructs the log (trust dir, key, backward scan) on the
# caller's thread, even though every call after that only enqueues.
_FS_NAMES = frozenset({"open", "data_home", "sel", "grant_fingerprint"})


def _called_names(node: Any) -> set[str]:
    """Every function NAME this node calls directly.

    A reference that is merely PASSED -- ``asyncio.to_thread(f, x)`` -- is a name
    load and not a call, so it is absent here by construction. That is precisely
    the distinction the invariant rests on.
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                out.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                out.add(sub.func.attr)
    return out


def test_no_coroutine_in_the_mint_module_touches_the_filesystem_directly():
    tree = ast.parse(inspect.getsource(mint))
    sync = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    coros = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    assert sync and coros, "module shape changed; this guard is reading the wrong tree"

    # Which synchronous helpers reach the filesystem, transitively within the
    # module. Fixed point, so a wrapper around a wrapper is still caught.
    touches = {
        name: bool(_called_names(node) & (_FS_ATTRS | _FS_NAMES)) for name, node in sync.items()
    }
    changed = True
    while changed:
        changed = False
        for name, node in sync.items():
            if touches[name]:
                continue
            if any(touches.get(callee) for callee in _called_names(node)):
                touches[name] = changed = True
    fs_helpers = {name for name, hit in touches.items() if hit}
    # The known set, so a helper silently losing its filesystem work (and with it
    # this guard's coverage) is visible rather than a quietly weaker test.
    assert fs_helpers == {
        "_is_reapable_spec",
        "_mint_manifest_path",
        "_read_mint_manifest",
        "_write_mint_manifest",
        "_sweep_mint_specs",
        "_record_mint_spec",
        "_forget_mint_spec",
        "_write_mint_agent_spec",
        "_write_pinned_mint_spec",
        "_remove_mint_agent_spec",
        "_agent_spec_entry_missing",
        "_log_mint_outcome",
    }

    offenders = {
        f"{coro} -> {callee}"
        for coro, node in coros.items()
        for callee in _called_names(node) & (fs_helpers | _FS_ATTRS | _FS_NAMES)
    }
    assert not offenders, (
        "filesystem work on the event loop: "
        + ", ".join(sorted(offenders))
        + " -- route it through asyncio.to_thread"
    )


# ── boot cost ──


def test_the_handlers_package_does_not_import_the_mint_engine():
    """The gateway imports the handlers package at boot; the mint must not ride along.

    Run in a subprocess because this test module imports the mint directly, so an
    in-process ``sys.modules`` check would always find it.
    """
    probe = (
        "import sys; import kiro_crew.dashboard.handlers;"
        " print('MINT' if 'kiro_crew.connections.mint' in sys.modules else 'CLEAN')"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("CLEAN"), out.stdout


# ── grant-detection drift guard ──
#
# grant_key mirrors an UNDOCUMENTED internal of an external binary (kiro-cli's
# ``compute_key``) and the artifact layout it writes, and that pairing is the
# mint's only consent-completion signal. If a kiro-cli release changes either, the
# watcher never observes the grant, holds the process to the full TTL and reports
# ``expired`` after a consent that actually succeeded -- a silent degradation.
# These literals are recorded, not recomputed from the implementation, so drift
# fails loudly here instead of in the field.

_RECORDED_GRANT_KEYS = {
    "https://mcp.notion.com/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
    "https://mcp.linear.app/mcp": (
        "fb39103c7d2edac291c92d23247e0a7d90470b1b349c07b146aba4ee2c81591f"
    ),
    "https://mcp.atlassian.com/v1/sse": (
        "834761c496c5a564116b2f1c55805d4425c32caee9e86596590d3d6e332a3240"
    ),
    "https://mcp.vercel.com": ("27af4e7d14d9aa7579dff853f8b7033ffbaaf6fb734bf15aec53f68776bb4111"),
    "https://mcp.sentry.dev/mcp": (
        "956f74053c03bea04f650e2341a266b5e3162116bc5c7f74b1f4d5afb4654b72"
    ),
    "https://api.githubcopilot.com/mcp/": (
        "ca602dd499edea084d35bd6584aea648dec2778e67b48e10ab008ae0c1d06284"
    ),
    # Mixed case and an explicit default port, recorded against the SAME key as
    # their canonical spelling: both halves of the normalization are pinned, not
    # just the hash of an already-canonical string.
    "https://MCP.Notion.COM/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
    "https://mcp.notion.com:443/mcp": (
        "1cbd18bf1818c780c9568384b0324fff99a5335bd4a17e102be74712b007e62c"
    ),
}


@pytest.mark.parametrize(("mcp_url", "recorded"), sorted(_RECORDED_GRANT_KEYS.items()))
def test_the_grant_key_matches_its_recorded_value(mcp_url: str, recorded: str):
    assert mcp_grant.grant_key(mcp_url) == recorded


def test_the_grant_key_formula_is_sha256_of_origin_and_path():
    """Independent restatement of the rule, so a rewrite cannot silently redefine it."""
    expected = hashlib.sha256(b"https://mcp.notion.com/mcp").hexdigest()

    assert mcp_grant.grant_key("https://mcp.notion.com/mcp") == expected


def test_the_artifact_layout_assumptions_are_pinned():
    # The directory and the suffix PAIR are as much of the contract as the hash:
    # a layout move breaks detection exactly as silently as a key change.
    assert mcp_grant._KIRO_OAUTH_CACHE_RELATIVE == (".aws", "sso", "cache")
    assert mcp_grant._TOKEN_SUFFIX == ".token.json"
    assert mcp_grant._REGISTRATION_SUFFIX == ".registration.json"
    assert Path("/home/u").joinpath(*mcp_grant._KIRO_OAUTH_CACHE_RELATIVE) == Path(
        "/home/u/.aws/sso/cache"
    )


# ── HTTP surface ──


async def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    app.router.add_get("/api/connections/mint", connections.api_connections_mint_state)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@requires_symlinks
def test_a_row_whose_parent_is_a_symlink_is_never_unlinked(tmp_path, monkeypatch):
    # The leaf is a real file, so a leaf-only symlink check passes it, and the
    # resolved parent equals the agents dir -- yet unlink() re-resolves the link at
    # act time, so the check never governed the path being deleted.
    agents_dir = mint._agent.kiro_agents_dir_path()
    link_dir = tmp_path / "agents-link"
    link_dir.symlink_to(agents_dir, target_is_directory=True)
    victim = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    victim.write_text("{}", encoding="utf-8")

    assert mint._is_reapable_spec(str(link_dir / victim.name)) is False
    # The direct, lexical spelling of the same file stays reapable.
    assert mint._is_reapable_spec(str(victim)) is True


def test_a_relative_row_is_never_unlinked():
    # Relative paths resolve against the process cwd, which is not a property the
    # gateway controls; only the absolute form the writer recorded is reapable.
    assert mint._is_reapable_spec("kirocrew-mint-notion-4242-abcdef01.json") is False


def test_a_junction_at_a_mint_spec_name_is_never_unlinked(tmp_path, monkeypatch):
    # A directory JUNCTION is the only directory link an unprivileged Windows
    # writer can plant, and it answers is_symlink() False while is_dir() True, so
    # a leaf ``is_symlink()`` guard read a junction planted at a mint-spec name as
    # our own spec and unlinked it -- removing a link into a target this module
    # does not own. make_dir_link plants a junction on Windows and a directory
    # symlink on POSIX, so the guard is pinned on every shard. Its name matches
    # the mint shape, so only the link check can refuse it.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours", encoding="utf-8")
    monkeypatch.setattr(mint._agent, "kiro_agents_dir_path", lambda: agents_dir)

    planted = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    make_dir_link(planted, outside)

    assert mint._is_reapable_spec(str(planted)) is False
    # The link and its target are untouched by the refusal.
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "not ours"


@pytest.mark.asyncio
async def test_cancelling_a_mint_still_releases_what_it_holds(monkeypatch):
    # CancelledError is a BaseException, so it bypasses the failure handler while
    # the child process, its listener, the protected PID and the spec are held.
    released: list[dict] = []

    async def _fake_dispose(holdings):
        released.append(dict(holdings))

    class _CancelDuringReady:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url: False)
    # Cancellation lands in ensure_ready, with the spec written and the client
    # already spawned -- the exact window the failure handler cannot see.
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _CancelDuringReady)

    with pytest.raises(asyncio.CancelledError):
        await mint.start_oauth_mint("notion", "https://example.test/mcp")
    # The finally ran and released what the flow had already taken ownership of.
    assert released and released[-1].get("spec_path")
    mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_a_mint_with_no_entry_left_fails_instead_of_reporting_granted(monkeypatch):
    # A concurrent uninstall removed the entry between the Connect writing it and
    # this flow initializing. No entry means no challenge -- which must not read as
    # a granted connection, or the card shows connected with no server behind it
    # and no way back.
    class _NoChallenge:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            return None

        def pop_pending_oauth_requests(self):
            return []

    async def _fake_dispose(holdings):
        return None

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url: False)
    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _NoChallenge)
    monkeypatch.setattr(mint, "_write_mint_agent_spec", lambda slug: ("agent", "/tmp/spec.json"))

    monkeypatch.setattr(mint, "_agent_spec_entry_missing", lambda slug: True)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )
    await mint.start_oauth_mint("notion", "https://example.test/mcp")
    assert mint._mints["notion"]["state"] == "failed"
    assert mint._mints["notion"]["reason"] == "mint_server_absent"
    # A failed outcome audits as an error, like every other failure path.
    assert logged == ["error reason=mint_server_absent"]
    mint._mints.pop("notion", None)

    # With the entry present, the same no-challenge result IS a real grant.
    monkeypatch.setattr(mint, "_agent_spec_entry_missing", lambda slug: False)
    await mint.start_oauth_mint("notion", "https://example.test/mcp")
    assert mint._mints["notion"]["state"] == "granted"
    mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_a_dead_holder_becomes_really_expired_not_just_reported(monkeypatch):
    # The view already refuses to show a dead holder's URL, but the abandon fence
    # only acts on 'expired'. A view-only verdict left the stored row 'waiting', so
    # the entry could never be claimed and the card stuck on needs-attention.
    monkeypatch.setattr(mint, "_mint_holder_alive", lambda entry: False)

    async def _fake_dispose(holdings):
        return None

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    mint._mints["notion"] = {
        "state": "waiting",
        "started": 1.0,
        "token": "tok5",
        "oauth_url": "https://example.test/authorize",
    }
    try:
        await mint.expire_dead_holder("notion")
        assert mint._mints["notion"]["state"] == "expired"
        # The verdict is committed to the row, not merely reported by the view --
        # the card reads a state the stored row actually has.
        assert mint.pending_mint_for("notion")["state"] == "expired"
    finally:
        mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_the_row_is_visible_before_the_post_answers(monkeypatch: pytest.MonkeyPatch):
    # Repro of the visibility window: a terminal row from a PREVIOUS attempt is
    # still in the table when the tab polls. If the POST answers before installing
    # this attempt's row, that poll returns the old terminal row and the card reads
    # it as the verdict on the new attempt, clearing the wait for good.
    displaced_token = "a" * 32
    mint._mints["notion"] = {"state": "expired", "started": 1.0, "token": displaced_token}
    seen: list[dict] = []

    async def _fake_start(slug, url, token=None, prior=None):
        seen.append({"token": token, "prior_state": (prior or {}).get("state")})

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        body = await (await client.post("/api/connections/mint", json={"slug": "notion"})).json()
        # The row a GET would answer with, at the instant the POST returned.
        row = mint.pending_mint_for("notion")
        assert row is not None
        # The POST names the row it just reserved -- NOT the displaced one. Both
        # halves are load-bearing: comparing against an int would pass for free
        # now that tokens are strings.
        assert row["token"] == body["token"]
        assert body["token"] != displaced_token
        assert row["state"] == "minting"
        await asyncio.sleep(0)
        # The displaced terminal row is handed to the flow, not left in the table.
        assert seen and seen[0]["prior_state"] == "expired"
    finally:
        await client.close()
        mint._mints.pop("notion", None)


def test_row_tokens_do_not_repeat_across_a_gateway_restart():
    # A counter restarts at the same value with the process, so a tab holding a
    # pre-restart token would match a post-restart row and act on a flow it never
    # started. Each gateway is a fresh interpreter, so that is what this compares --
    # importlib.reload re-executes into the same namespace and would keep any
    # module state alive, hiding exactly the bug under test.
    probe = (
        # Import in the gateway's own order: the handlers package first, then the
        # mint engine it defers. A cold `import kiro_crew.connections.mint` trips a
        # pre-existing cycle in kiro_crew.session_pid that production never hits.
        "import kiro_crew.dashboard.handlers;"
        "from kiro_crew.connections.mint import _new_mint_token as t; print(t())"
    )
    first = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    second = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert first and second and first != second
    # Still unique within one process, which is what the row guards rely on.
    assert len({mint._new_mint_token() for _ in range(64)}) == 64


@pytest.mark.asyncio
async def test_a_stale_token_from_before_a_restart_never_matches_a_live_row():
    # The pre-restart tab's token is a different uuid, so every token-guarded write
    # declines rather than acting on the successor row.
    token, _prior = await mint.reserve_mint_row("notion")
    try:
        stale = "0" * 32
        assert stale != token
        assert mint._mints["notion"]["token"] == token
        # A guard written the way every call site writes it refuses the stale id.
        assert mint._mints["notion"].get("token") != stale
    finally:
        mint._mints.pop("notion", None)


@pytest.mark.asyncio
async def test_post_returns_the_row_token_it_started(monkeypatch: pytest.MonkeyPatch):
    started: list[tuple[str, str, str]] = []

    async def _fake_start(slug: str, url: str, token: str | None = None, prior=None) -> None:
        started.append((slug, url, str(token or "")))

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        body = await (await client.post("/api/connections/mint", json={"slug": "notion"})).json()
        await asyncio.sleep(0)
        # The response names the row this caller started, so one tab can tell its
        # own terminal state from a sibling tab's.
        assert isinstance(body["token"], str) and len(body["token"]) == 32
        assert started and started[0][2] == body["token"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_carries_the_row_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mint,
        "pending_mint_for",
        lambda slug: {"state": "expired", "reason": "mint_process_gone", "token": "tok77"},
    )
    client = await _client(monkeypatch)
    try:
        body = await (await client.get("/api/connections/mint?slug=notion")).json()
        # Present on the terminal view too -- that is the case a sibling tab would
        # otherwise act on.
        assert body["token"] == "tok77"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_schedules_a_mint_for_a_registry_provider(monkeypatch: pytest.MonkeyPatch):
    started: list[tuple[str, str]] = []

    async def _fake_start(slug: str, url: str, token: str | None = None, prior=None) -> None:
        started.append((slug, url))

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        assert resp.status == 200
        assert (await resp.json())["state"] == "minting"
        await asyncio.sleep(0)
        assert started and started[0][0] == "notion"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "slug",
    ["", "../../etc/passwd", "Notion; rm -rf /", "no-such-provider", "x" * 65],
)
@pytest.mark.asyncio
async def test_post_refuses_anything_that_is_not_a_shipped_provider(
    monkeypatch: pytest.MonkeyPatch, slug: str
):
    spawned: list[str] = []

    async def _fake_start(slug_: str, url: str, token: str | None = None, prior=None) -> None:
        spawned.append(slug_)

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", json={"slug": slug})
        # Registry membership bounds what a caller can make the gateway spawn.
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_provider"
        await asyncio.sleep(0)
        assert spawned == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_a_non_object_body(monkeypatch: pytest.MonkeyPatch):
    client = await _client(monkeypatch)
    try:
        resp = await client.post("/api/connections/mint", data=b"not json")
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_body"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_serves_the_url_once_the_mint_is_waiting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mint,
        "pending_mint_for",
        lambda slug: {"state": "waiting", "oauth_url": _AUTHORIZE},
    )
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {
            "slug": "notion",
            "state": "waiting",
            "oauth_url": _AUTHORIZE,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_distinguishes_no_mint_from_a_mint_that_produced_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    client = await _client(monkeypatch)
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: None)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {"slug": "notion", "state": "idle"}

        monkeypatch.setattr(
            mint,
            "pending_mint_for",
            lambda slug: {"state": "failed", "reason": "mint_timeouterror"},
        )
        resp = await client.get("/api/connections/mint?slug=notion")
        assert await resp.json() == {
            "slug": "notion",
            "state": "failed",
            "reason": "mint_timeouterror",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_never_serves_a_url_outside_the_waiting_state(monkeypatch: pytest.MonkeyPatch):
    await mint.start_oauth_mint("notion", _URL)
    mint._mints["notion"]["state"] = "granted"
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=notion")
        assert "oauth_url" not in await resp.json()
    finally:
        await client.close()
        await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_get_refuses_an_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    client = await _client(monkeypatch)
    try:
        resp = await client.get("/api/connections/mint?slug=no-such-provider")
        assert resp.status == 400
    finally:
        await client.close()


# ── cancel_mint and mint-tier telemetry (N1) ──


@pytest.mark.asyncio
async def test_cancel_mint_disposes_a_waiting_row():
    await mint.start_oauth_mint("notion", _URL)
    assert mint.pending_mint_for("notion") is not None
    client = _FakeClient.instances[-1]

    dropped = await mint.cancel_mint("notion")

    assert dropped is True
    assert mint.pending_mint_for("notion") is None
    # The held kiro-cli process, its listener and its spec are released, not
    # left to the TTL -- that release is the whole point of a real cancel.
    assert client.shutdowns == 1


@pytest.mark.asyncio
async def test_cancel_mint_on_an_empty_table_is_idempotent():
    assert await mint.cancel_mint("notion") is False


@pytest.mark.asyncio
async def test_cancel_mint_is_fenced_by_the_row_token():
    await mint.start_oauth_mint("notion", _URL)
    token = mint._mints["notion"]["token"]
    client = _FakeClient.instances[-1]

    # A stale tab carries a token for a row this flow replaced: refuse to dispose
    # the row that is not theirs. The row must SURVIVE intact -- both the
    # table entry and the process holding the redeemable URL.
    assert await mint.cancel_mint("notion", "not-the-token") is False
    assert mint.pending_mint_for("notion") is not None
    assert mint._mints["notion"]["token"] == token
    assert mint._mints["notion"]["state"] == "waiting"
    assert client.shutdowns == 0  # nothing was torn down
    assert mint._mints["notion"].get("client") is client

    # The row's own token disposes it: entry gone, process released.
    assert await mint.cancel_mint("notion", token) is True
    assert mint.pending_mint_for("notion") is None
    assert "notion" not in mint._mints
    assert client.shutdowns == 1


@pytest.mark.asyncio
async def test_cancel_mint_without_a_token_disposes_the_current_row():
    # A caller that never held a token cannot distinguish rows, so its intent is
    # only "cancel this provider" -- distinct from the fenced path above, and the
    # path the card takes when its POST answered without a token.
    await mint.start_oauth_mint("notion", _URL)
    client = _FakeClient.instances[-1]

    assert await mint.cancel_mint("notion") is True
    assert mint.pending_mint_for("notion") is None
    assert client.shutdowns == 1


@pytest.mark.asyncio
async def test_cancel_mint_records_the_outcome(monkeypatch: pytest.MonkeyPatch):
    details: list[str] = []
    monkeypatch.setattr(
        mint, "_log_mint_outcome", lambda slug, outcome, detail: details.append(detail)
    )
    await mint.start_oauth_mint("notion", _URL)
    details.clear()  # drop the mint's own outcome record; keep only the cancel's

    await mint.cancel_mint("notion")

    assert any("reason=cancelled" in detail for detail in details)


@pytest.mark.asyncio
async def test_a_cold_spawn_records_that_it_minted_a_url(monkeypatch: pytest.MonkeyPatch):
    details: list[str] = []
    monkeypatch.setattr(
        mint, "_log_mint_outcome", lambda slug, outcome, detail: details.append(detail)
    )

    await mint.start_oauth_mint("notion", _URL)  # _FakeClient yields a fresh URL

    assert any("url_minted=True" in detail for detail in details)
    await mint._dispose_mint(mint._mints["notion"])


@pytest.mark.asyncio
async def test_a_reconnect_with_a_validated_grant_records_validated_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    _write_paired_grant_artifacts(_URL)  # kiro-cli already holds a grant
    details: list[str] = []
    monkeypatch.setattr(
        mint, "_log_mint_outcome", lambda slug, outcome, detail: details.append(detail)
    )

    await mint.start_oauth_mint("notion", _URL)

    assert any("reason=validated_grant" in detail for detail in details)
