"""Rewrite-fingerprint cache: the settings-fault keep and the artifacts a pass guards.

When the settings read or stat fails, a pass keeps each unchanged agent's overlay instead
of rewriting it without the injected settings servers, and refuses the keep when the
agent's spec, the settings file or the sharing policy changed, or when it cannot vouch for
the overlay, its sidecars or its target binary. This file also pins which deterministic
content stays cacheable, the legacy settings overlay, the fingerprint file's protection,
and the two transient-keep retention windows, which stay closed. The invalidation
contract and the per-agent keep on a failed agent read or overlay write are tested in
``test_mcp_gateway_rewriter_fingerprint.py``.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp_gateway_rewriter_fingerprint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _CMD,
    _bump_mtime,
    _forward_declared_env_on,
    _mk_tree,
    _rewrite,
    _settings_unreadable,
    rewrite_counter_fixture,
)

from kiro_crew import agent_discovery
from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import _FINGERPRINT_NAME, overlay_ready


def test_transient_settings_read_failure_does_not_rewrite_agent_overlays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings read that FAILED is not a settings file that declared
    nothing: the injection set is unknown, not empty, so no agent overlay may
    be written from it.

    A healthy pass injects each poolable settings server INTO every agent
    overlay; the raw entry still merges from the real settings file, but it
    runs per-session and unpooled, without the identity the stub carries.
    Rewriting an agent overlay from an empty injection set would silently
    unpool every globally-declared server for the rest of the gateway's
    lifetime. The pass must refuse to rewrite instead, exactly as the
    per-agent transient read failure does."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)  # healthy pass: global-x injected into every agent
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    for i in range(2):
        healthy = json.loads((overlay_dir / f"agent-{i}.json").read_text())
        assert "global-x" in healthy["mcpServers"]
    before = {p.name: p.read_bytes() for p in overlay_dir.glob("*.json")}
    assert before

    # Fault the settings file itself, which is what forces the rewrite: the
    # fingerprint signs it with _stat_sig (read_bytes), so an unreadable file
    # makes that signature None and the cached early return cannot fire. No
    # agent source is touched -- their signatures must stay comparable, which is
    # what licenses the keep.
    with _settings_unreadable():
        _, target_env = _rewrite(tmp_path)

    # No agent overlay was rewritten at all: byte-identical to the healthy pass.
    for i in range(2):
        kept = json.loads((overlay_dir / f"agent-{i}.json").read_text())
        assert (
            "global-x" in kept["mcpServers"]
        ), "the injected global server must survive a settings read failure"
    assert {p.name: p.read_bytes() for p in overlay_dir.glob("*.json")} == before
    # A kept overlay's stub still needs its target mapping published, or it
    # resolves through the bare-name fallback.
    assert any("GLOBAL_X" in key for key in target_env)
    # Degraded pass stays uncacheable: the next boot retries the read.
    assert not (overlay_dir / _FINGERPRINT_NAME).exists()

    # The fault clears: the next pass rewrites normally and re-caches.
    _rewrite(tmp_path)
    assert (overlay_dir / _FINGERPRINT_NAME).is_file()
    for i in range(2):
        healed = json.loads((overlay_dir / f"agent-{i}.json").read_text())
        assert "global-x" in healed["mcpServers"]


def test_a_changed_agent_spec_is_not_deferred_by_a_settings_read_failure(
    tmp_path: Path,
) -> None:
    """A keep defers everything the overlay encodes, not just the injection
    set, so it is only honest while nothing else has changed. An agent whose own
    spec changed -- an ``autoApprove`` entry removed, a server disabled, a
    server dropped -- must be REWRITTEN even though that costs the injected
    globals for that agent this pass: a revocation is a deliberate instruction
    and cannot wait for the next boot, while the lost injection self-heals."""
    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "global-x" in json.loads(overlay.read_text())["mcpServers"]

    # The operator revokes an approval on the agent's own server.
    (src / "agent-0.json").write_text(
        json.dumps(
            {
                "name": "agent-0",
                "mcpServers": {
                    "srv": {"command": _CMD, "args": ["a0"], "env": {"K": "v"}, "disabled": True}
                },
            }
        )
    )
    with _settings_unreadable():
        _rewrite(tmp_path)

    spec = json.loads(overlay.read_text())
    assert (
        spec["mcpServers"]["srv"].get("disabled") is True
    ), "a revoked/disabled server must not be deferred to the next boot"
    # The cost is declared: the injection could not be established this pass.
    assert "global-x" not in spec["mcpServers"]
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


def test_a_deleted_overlay_is_rebuilt_even_on_a_settings_read_failure(
    tmp_path: Path,
) -> None:
    """ "Inputs unchanged" does not imply "there is an overlay to keep".

    A missing output is itself a reason the cached early return did not fire, so
    this pass can reach the loop with every input signature still matching while
    the overlay file is gone. Adding that agent to the keep set would leave it
    with NO overlay -- unpooling its own servers -- when a rewrite was available.
    The existence check is what separates the two."""
    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)  # healthy pass: fingerprint stored, overlay written
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert overlay.is_file()

    overlay.unlink()  # the output vanishes; every input signature still matches
    with _settings_unreadable():
        _rewrite(tmp_path)

    assert overlay.is_file(), "a vanished overlay must be rebuilt, not withheld"
    servers = json.loads(overlay.read_text())["mcpServers"]
    assert servers["srv"].get("_kirocrew_mcp_gateway_wrapped") is True


def test_settings_read_failure_with_nothing_stubbed_still_rewrites(
    tmp_path: Path,
) -> None:
    """With nothing opted in, a settings read failure changes nothing.

    This pins the OUTCOME rather than a predicate: an empty stub set is not a
    special case in the keep decision, because nothing is wrapped and
    ``_injectable_settings_servers`` returns nothing whatever the file says, so a
    keep and a rewrite would produce identical bytes. What must still hold is
    that an edit to the agent's own spec lands on such a pass."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path, stub_servers=frozenset())
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "late" not in json.loads(overlay.read_text())["mcpServers"]

    # A real source change, so a refusal would be visible as a stale overlay.
    (src / "agent-0.json").write_text(
        json.dumps(
            {
                "name": "agent-0",
                "mcpServers": {"late": {"command": _CMD, "args": ["l"]}},
            }
        )
    )
    with _settings_unreadable():
        _rewrite(tmp_path, stub_servers=frozenset())

    assert "late" in json.loads(overlay.read_text())["mcpServers"]


def test_settings_read_failure_still_writes_an_agent_with_no_overlay(
    tmp_path: Path,
) -> None:
    """The refusal only withholds a rewrite where withholding PRESERVES
    something. An agent with no previous overlay has no injected copy to drop,
    so refusing would leave it with no overlay at all -- unpooling its own
    servers too, which is worse than the fault warrants. It must still be
    written."""
    _mk_tree(tmp_path, n_agents=1)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    with _settings_unreadable():
        _rewrite(tmp_path)  # first pass ever: nothing to keep

    written = overlay_dir / "agent-0.json"
    assert written.is_file(), "a cold pass must not be withheld"
    servers = json.loads(written.read_text())["mcpServers"]
    # The agent's OWN server is still wrapped and pooled.
    assert servers["srv"].get("_kirocrew_mcp_gateway_wrapped") is True
    # The injected global is absent -- unknowable this pass -- but its raw entry
    # still merges from the real settings file, so no server is lost.
    assert "global-x" not in servers
    # Still uncacheable: the next boot injects it.
    assert not (overlay_dir / _FINGERPRINT_NAME).exists()


def test_disabling_sharing_is_not_deferred_by_a_settings_read_failure(
    tmp_path: Path,
) -> None:
    """Preserving an injection set must never defer a POLICY change.
    ``_build_stub_entry`` bakes ``--poolable`` into every wrapped entry iff
    sharing was on, so an operator who sets ``mcp_gateway.enabled = false`` and
    hits a transient settings read failure in the same pass would otherwise keep
    shared backends for the whole gateway lifetime. ``pooling_enabled`` is a
    fingerprinted input, so the keep is refused and the overlay rewritten."""
    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)  # healthy pass with sharing ON
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "--poolable" in expand_stub_flags(
        json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
    )

    with _settings_unreadable():
        _rewrite(tmp_path, pooling_enabled=False)  # operator turns sharing OFF

    args = expand_stub_flags(json.loads(overlay.read_text())["mcpServers"]["srv"]["args"])
    assert "--poolable" not in args, "an explicit policy change must not be deferred"


@contextlib.contextmanager
def _settings_text_unreadable() -> "Iterator[None]":
    """Fault ONLY ``read_text`` on settings/mcp.json, leaving ``read_bytes`` live.

    The narrow fault the wide one deliberately avoids, and it is reachable: the
    two are separate syscalls, so a fault can hit one and not the other. The
    fingerprint still SIGNS the file (``_stat_sig`` uses ``read_bytes``) while the
    pass cannot parse it, which is the only state where a settings change is both
    real and known during a settings fault.
    """
    real_read_text = Path.read_text

    def flaky_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "mcp.json" and "settings" in self.parts:
            raise OSError("transient I/O error")
        return real_read_text(self, *args, **kwargs)

    Path.read_text = flaky_text  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.read_text = real_read_text  # type: ignore[method-assign]


def test_a_known_settings_change_refuses_the_keep(tmp_path: Path) -> None:
    """The settings input is skipped only when it cannot be SIGNED, never when it
    is signable and differs.

    ``read_text`` and ``read_bytes`` are separate calls, so the fingerprint can
    sign a NEW settings file on the same pass whose parse failed. Skipping the
    comparison unconditionally would then keep overlays built from the old
    settings -- so a server revoked in settings/mcp.json would stay live, wrapped,
    in every agent overlay for the gateway's lifetime. That is an absorbed
    instruction, not a deferred edit."""
    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "global-x" in json.loads(overlay.read_text())["mcpServers"]

    # The operator revokes the global server, and the parse (not the stat) faults.
    (src.parent / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global-x": {"command": _CMD, "args": ["g"], "disabled": True}}})
    )
    with _settings_text_unreadable():
        _rewrite(tmp_path)

    assert (
        "global-x" not in json.loads(overlay.read_text())["mcpServers"]
    ), "a signable, changed settings file must refuse the keep"


def test_a_tampered_overlay_is_not_served_by_a_keep(tmp_path: Path) -> None:
    """A keep serves an artifact this pass did not write, so it must validate the
    recorded signature exactly as ``_cached_rewrite_result`` does. Otherwise one
    induced transient fault is enough to have an edited overlay served -- its
    stub argv would diverge from the ``target_env`` gatewayd spawns from."""
    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"

    spec = json.loads(overlay.read_text())
    spec["mcpServers"]["srv"]["args"] = ["--tampered"]
    overlay.write_text(json.dumps(spec, indent=2) + "\n")

    with _settings_unreadable():
        _rewrite(tmp_path)

    args = json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
    assert "--tampered" not in args, "an edited overlay must be rewritten, not kept"


def test_an_unvouched_sidecar_blocks_every_keep(tmp_path: Path) -> None:
    """The sidecar set is vouched for once per pass, and a failure withdraws the
    keep for EVERY agent.

    A kept overlay still points ``--env-file`` at those sidecars and the sidecar
    prune is skipped on this pass, so serving a kept overlay beside a sidecar set
    that cannot be validated and re-protected would leave a credential file
    unrepaired. Mirrors the cached path, which refuses wholesale."""
    _mk_tree(tmp_path, n_agents=2)
    _rewrite(tmp_path)
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    sidecars = sorted(env_dir.glob("*.json"))
    assert sidecars

    sidecars[0].write_text(json.dumps({"K": "tampered"}))

    with _settings_unreadable():
        _rewrite(tmp_path)

    # No keep happened, so every overlay was rewritten -- without the injection.
    for i in range(2):
        servers = json.loads((tmp_path / "mcp-gateway" / "agents" / f"agent-{i}.json").read_text())[
            "mcpServers"
        ]
        assert (
            "global-x" not in servers
        ), "an unvouched sidecar set must withdraw the keep for every agent"


def test_a_vanished_target_binary_refuses_the_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept overlay's stub argv embeds the ABSOLUTE path a previous pass
    resolved, and directory contents are which() input no signature can see. So
    the keep re-runs the recorded probes exactly as the cached path does: a target
    binary removed, moved between PATH prefixes, or newly shadowed must refuse the
    keep, or the kept overlay launches a dead path for the gateway's lifetime."""
    bin_dir = tmp_path / "extra-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    exe_name = "kirocrew-test-vanishing-cmd" + (".bat" if os.name == "nt" else "")
    exe = bin_dir / exe_name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)

    src = _mk_tree(tmp_path, n_agents=1, with_env=False)
    spec = json.loads((src / "agent-0.json").read_text())
    spec["mcpServers"]["srv"]["command"] = "kirocrew-test-vanishing-cmd"
    (src / "agent-0.json").write_text(json.dumps(spec))
    _rewrite(tmp_path)

    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    args = expand_stub_flags(json.loads(overlay.read_text())["mcpServers"]["srv"]["args"])
    assert os.path.normcase(args[args.index("--target-command") + 1]) == (
        os.path.normcase(str(exe))
    )
    assert "global-x" in json.loads(overlay.read_text())["mcpServers"]

    exe.unlink()  # the binary goes away; no PATH change, no spec change
    with _settings_unreadable():
        _rewrite(tmp_path)

    # The keep was refused, so the overlay was rewritten -- and the now-dead
    # bare command is left unwrapped rather than pointed at a stale path.
    servers = json.loads(overlay.read_text())["mcpServers"]
    assert "global-x" not in servers, "a vanished target binary must refuse the keep"
    assert "--target-command" not in json.dumps(servers.get("srv", {}))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_keep_retightens_every_artifact_it_serves(tmp_path: Path) -> None:
    """A chmod changes no signature, so a keep must re-assert owner-only
    protection on everything it serves -- the kept overlay, and the env sidecars
    and their directory. Mirrors the cache-hit path's guarantee, for the same
    reason: on Windows the file DACL rather than the directory is what carries
    access, and these files hold the passed-through env of non-poolable servers.

    The fingerprint file is deliberately NOT in the set: this pass unlinks it
    (uncacheable), so re-protecting a file about to be deleted is a no-op."""
    import stat as _stat

    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    artifacts = [overlay_dir / "agent-0.json", *env_dir.glob("*.json")]
    assert len(artifacts) >= 2  # incl. at least one sidecar
    for a in artifacts:
        a.chmod(0o644)
    env_dir.chmod(0o755)

    with _settings_unreadable():
        _rewrite(tmp_path)

    # The keep happened (the injected global survived) AND everything it serves
    # was retightened.
    assert "global-x" in json.loads((overlay_dir / "agent-0.json").read_text())["mcpServers"]
    for a in artifacts:
        assert _stat.S_IMODE(a.stat().st_mode) == 0o600, a
    assert _stat.S_IMODE(env_dir.stat().st_mode) == 0o700


def test_a_swallowed_stat_fault_is_unknown_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_file()`` answers False for a missing file AND for some stat faults,
    and only the errnos in ``pathlib._IGNORED_ERRNOS`` are swallowed that way.

    Measured on CPython 3.12: EACCES, EIO and EPERM RAISE out of the pass (the
    caller logs "rewriter failed -- falling back" and touches nothing), but
    ENOENT, EBADF, ENOTDIR and ELOOP return False. ENOTDIR is reachable without
    the file being gone -- a directory component momentarily replaced, an atomic
    directory swap, a symlink being re-pointed -- and reading it as "absent"
    rewrites every overlay with an empty injection set -- the same unknown-read-as-
    empty fault through the stat path instead of the read path. Only
    ``FileNotFoundError`` may mean absent; every other OSError means unknown."""
    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "global-x" in json.loads(overlay.read_text())["mcpServers"]

    real_stat = Path.stat
    fail = {"on": True}

    def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if fail["on"] and self.name == "mcp.json" and "settings" in self.parts:
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # Confirm first: this errno really is swallowed, so the pass sees "absent".
    assert (src.parent / "settings" / "mcp.json").is_file() is False
    _rewrite(tmp_path)
    fail["on"] = False

    assert (
        "global-x" in json.loads(overlay.read_text())["mcpServers"]
    ), "a swallowed stat fault must read as unknown, not as an absent file"
    # The pass is not cached.
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


@pytest.mark.parametrize(
    "payload, why",
    [
        ("{not json", "bad content"),
        ('["not", "a", "mapping"]', "valid JSON of the wrong shape"),
    ],
    ids=["bad_json", "non_dict"],
)
def test_deterministic_settings_content_is_cacheable_and_injects_nothing(
    tmp_path: Path, payload: str, why: str
) -> None:
    """A settings source that is present but unusable is a deterministic
    CONTENT problem, unlike a transient fault: the injection set is
    established as empty (the agent overlays drop the injected globals) and
    the pass is safe to cache -- fixing the content changes the file's stat
    signature. No settings overlay exists in either state."""
    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert "global-x" in json.loads(overlay.read_text())["mcpServers"]

    (src.parent / "settings" / "mcp.json").write_text(payload)
    _rewrite(tmp_path)

    assert (
        "global-x" not in json.loads(overlay.read_text())["mcpServers"]
    ), f"{why} is deterministic: the empty injection set must be applied"
    # Deterministic, so the pass is cacheable -- the transient arm is not.
    assert (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).is_file()
    assert not (tmp_path / "mcp-gateway" / "settings" / "mcp.json").exists()


def test_malformed_json_source_is_still_cacheable(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """JSONDecodeError is a CONTENT problem: fixing it changes the stat
    signature, so the skip is deterministic and safe to cache."""
    src = _mk_tree(tmp_path)
    (src / "broken.json").write_text("{not json")
    _rewrite(tmp_path)
    assert (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).is_file()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit despite the bad file


def test_sidecar_write_failure_is_not_cached(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed env-sidecar write leaves the overlay without --env-file while
    an older sidecar may still exist at that name, so existence checks cannot
    see the degradation — the pass must simply not be cached."""
    import tempfile as _tempfile

    _mk_tree(tmp_path, with_env=True)
    _rewrite(tmp_path)  # healthy run: sidecars + fingerprint exist
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert fp.is_file()

    # Invalidate, then fail every sidecar write during the forced rewrite.
    spec_path = tmp_path / "agents" / "agent-0.json"
    spec = json.loads(spec_path.read_text())
    spec["mcpServers"]["srv"]["args"] = ["changed-args"]
    spec_path.write_text(json.dumps(spec))

    real_mkstemp = _tempfile.mkstemp
    env_tail = os.path.join("stubs", "env")
    fail = {"on": True}

    def failing(*args: Any, **kwargs: Any) -> Any:
        if fail["on"] and str(kwargs.get("dir", "")).endswith(env_tail):
            raise OSError("disk full")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(_tempfile, "mkstemp", failing)
    _rewrite(tmp_path)
    fail["on"] = False

    assert not fp.exists()  # degraded pass not cached, stale fingerprint removed

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2  # full retry after fault clears
    assert fp.is_file()


def test_no_settings_overlay_is_ever_written(tmp_path: Path) -> None:
    """A healthy pass over a settings file holding poolable AND non-poolable
    servers must not write
    ``<overlay_dir>/../settings/mcp.json`` — nothing ever read it. The real
    settings file is not modified either, and the stored fingerprint carries
    no ``settings_overlay`` output."""
    src = _mk_tree(tmp_path, n_agents=1)
    real_settings = src.parent / "settings" / "mcp.json"
    spec = json.loads(real_settings.read_text())
    # A non-poolable (HTTP/SSE) entry of the kind the old overlay existed to
    # pass through.
    spec["mcpServers"]["http-y"] = {"url": "https://example.invalid/mcp"}
    real_settings.write_text(json.dumps(spec))
    before_bytes = real_settings.read_bytes()

    _rewrite(tmp_path)

    assert not (tmp_path / "mcp-gateway" / "settings").exists()
    assert real_settings.read_bytes() == before_bytes
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    outputs = json.loads(fp.read_text())["outputs"]
    assert "settings_overlay" not in outputs


def test_legacy_settings_overlay_is_left_untouched(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """A settings overlay left behind by an older release is deliberately
    NOT deleted: it is written owner-only into a 0o700 directory, its content is
    a subset copy of the user's real settings file, and nothing reads it --
    inert, not exposed. An automated deleter
    would itself be an attack surface (real-settings aliasing, foreign files
    under a custom overlay_dir, symlink-redirected parents), so the pass must
    leave the file byte-identical, on the full path and on cache hits alike."""
    _mk_tree(tmp_path, n_agents=1)
    leftover = tmp_path / "mcp-gateway" / "settings" / "mcp.json"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}))
    before_bytes = leftover.read_bytes()

    _rewrite(tmp_path)  # full pass
    assert leftover.read_bytes() == before_bytes

    before = rewrite_counter["n"]
    _rewrite(tmp_path)  # cache hit
    assert rewrite_counter["n"] == before
    assert leftover.read_bytes() == before_bytes


def test_legacy_fingerprint_with_settings_overlay_output_still_cache_hits(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The upgrade-safety invariant: a fingerprint written by an older release
    carries an ``outputs.settings_overlay`` signature. The loader must IGNORE it
    — not reject it — so the first upgraded boot over unchanged inputs is still
    served from cache, and the transient-keep gate keeps comparing inputs
    meaningfully."""
    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    stored = json.loads(fp.read_text())
    stored["outputs"]["settings_overlay"] = [3, 5, "ab"]  # pre-change shape
    fp.write_text(json.dumps(stored, sort_keys=True))

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit; the stale key is inert


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_legacy_overlay_acl_is_retightened_when_vouched(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The ONE guard kept for a leftover legacy overlay: a loosened ACL
    is re-tightened on every pass — cache hits included — when the stored
    fingerprint's recorded ``settings_overlay`` signature vouches for the
    file. An unvouched file is never chmodded, the bytes are never touched,
    and the vouching signature is carried across a fingerprint rewrite while
    the file survives (dropping it would silently end the guard)."""
    import stat as _stat

    from kiro_crew.mcp_gateway.rewriter import _stat_sig

    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    legacy = tmp_path / "mcp-gateway" / "settings" / "mcp.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}")
    legacy.chmod(0o644)

    # Unvouched (fresh fingerprint carries no signature): left alone.
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit
    assert _stat.S_IMODE(legacy.stat().st_mode) == 0o644

    # Vouched (the pre-change release recorded the signature): re-tightened.
    sig = _stat_sig(legacy)
    stored = json.loads(fp.read_text())
    stored["outputs"]["settings_overlay"] = sig
    fp.write_text(json.dumps(stored, sort_keys=True))
    content_before = legacy.read_bytes()
    _rewrite(tmp_path)
    assert _stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert legacy.read_bytes() == content_before  # tightened, never rewritten

    # The vouching signature survives a full-rewrite fingerprint replacement.
    _bump_mtime(src / "agent-0.json")
    _rewrite(tmp_path)  # full rewrite, fingerprint re-stored
    assert json.loads(fp.read_text())["outputs"]["settings_overlay"] == sig


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_fingerprint_file_is_owner_only(tmp_path: Path) -> None:
    """The fingerprint records resolved commands and their args (which can
    legitimately carry credentials in user specs); ratchet the 0600 mode so a
    refactor to a plain write cannot silently loosen it."""
    import stat as _stat

    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert _stat.S_IMODE(fp.stat().st_mode) == 0o600


def test_lockdown_failure_falls_through_to_full_rewrite(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache-hit lockdown is fail-loud on every platform: a foreign-owned
    or otherwise unprotectable artifact must never be served from cache — the
    cache hit aborts and the full rewrite re-creates the artifact through its
    protect-before-content writers."""
    from kiro_crew import platform_compat as _pc

    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)

    real = _pc.restrict_to_owner
    fail = {"on": True}

    def failing(path: Any) -> None:
        if fail["on"] and str(path).endswith("agent-0.json"):
            raise OSError("operation not permitted (foreign owner)")
        real(path)

    monkeypatch.setattr("kiro_crew.mcp_gateway.rewriter.platform_compat.restrict_to_owner", failing)
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    fail["on"] = False
    assert rewrite_counter["n"] == before + 1  # cache refused -> full rewrite


def test_fingerprint_carries_no_command_material(tmp_path: Path) -> None:
    """Security ratchet: the fingerprint must never store target_env/results —
    the cache-hit path reconstructs them from the validated overlays, so
    tampering with the fingerprint can at worst skip a rewrite, never make
    gatewayd spawn a command that is not already in the overlay files."""
    _mk_tree(tmp_path, n_agents=1)
    _, target_env = _rewrite(tmp_path)
    assert target_env  # the run did produce command material...
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    data = json.loads(fp.read_text())
    assert set(data.keys()) == {"inputs", "outputs", "which"}
    # ...and none of it is in the fingerprint.
    assert "KIROCREW_MCP_TARGET" not in fp.read_text()


def test_fingerprint_file_survives_prune_and_does_not_fake_overlay_ready(
    tmp_path: Path,
) -> None:
    """The fingerprint must be invisible to the *.json plumbing: pathlib's
    glob('*.json') matches dotfiles, so a .json-suffixed name would be pruned
    as stale and would make an empty overlay dir report ready."""
    assert not _FINGERPRINT_NAME.endswith(".json")
    src = _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    fp = overlay_dir / _FINGERPRINT_NAME
    assert fp.is_file()

    # Full rewrite (input change) must not prune the fingerprint.
    _bump_mtime(src / "agent-0.json")
    _rewrite(tmp_path)
    assert fp.is_file()

    # With no overlays, the fingerprint alone must not make the dir "ready".
    for p in overlay_dir.glob("*.json"):
        p.unlink()
    assert not overlay_ready(overlay_dir)


# --- Bounded transient-keep retention windows --------------------------
#
# The prune keep-set spares a transient victim's previous overlay, which
# means a pass can SERVE artifacts it did not write. Two windows that opens,
# both bounded to one pass but both avoidable:
#
#   1. the env sidecar published at a fixed name during stub construction,
#      while the overlay referencing it lands later -- a kept old-args overlay
#      paired with a new-generation env;
#   2. a source deleted between the listing and the read, reported as
#      FileNotFoundError and treated as a transient fault, so the deleted
#      agent's overlay survived one more pass.


def _sidecar_temps(env_dir: Path) -> list[Path]:
    """Staged, not-yet-committed sidecars: mkstemp names start with a dot."""
    if not env_dir.is_dir():
        return []
    return sorted(p for p in env_dir.iterdir() if p.name.startswith("."))


def test_a_spec_deleted_between_listing_and_read_prunes_its_overlay_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished source is a DELETE, not a transient fault.

    The listing globs the spec, the user removes it, and the read raises
    FileNotFoundError. The generic OSError arm treats that as transient and
    keeps the deleted agent's overlay for one more pass -- the agent is gone
    but its MCP servers stay injectable. Confirmed-absent prunes in the SAME
    pass, exactly as a source already gone at listing time does.

    The pass writes no fingerprint, and that half is load-bearing: the input
    signatures were taken while the spec still existed, so caching would let the
    same file, restored with the same size and mtime, match a fingerprint whose
    outputs lack its overlay -- the agent's servers gone with nothing left to
    invalidate. Pinned by restoring the source byte- and stat-identical
    and requiring the overlay back.
    """
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-1.json"
    survivor = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert overlay.is_file()
    spec_path = src / "agent-1.json"
    spec_bytes = spec_path.read_bytes()
    spec_stat = spec_path.stat()

    _bump_mtime(src / "agent-1.json")  # invalidate so the pass runs
    real_read = agent_discovery.read_agent_spec_strict

    def racing(path: Path, **kwargs: Any) -> Any:
        if path.name == "agent-1.json":
            path.unlink()  # lost the race between the glob and the read
        return real_read(path, **kwargs)

    with monkeypatch.context() as patch:
        # context(), never undo(): this fixture instance is shared with the
        # module's autouse pins and the conftest host isolation, so undoing it
        # would let the next _rewrite read the developer's real config.
        patch.setattr(agent_discovery, "read_agent_spec_strict", racing)
        _rewrite(tmp_path)

    assert not overlay.exists(), "the deleted agent's overlay survived the pass"
    assert survivor.is_file()  # the unaffected agent still rewrote
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert not fp.exists(), "a pass whose signatures predate the deletion must not cache"

    # Restored byte- and stat-identical: only an uncached pass can rebuild it.
    spec_path.write_bytes(spec_bytes)
    os.utime(spec_path, ns=(spec_stat.st_atime_ns, spec_stat.st_mtime_ns))
    _rewrite(tmp_path)
    assert overlay.is_file(), "a restored source with identical stats stayed pruned"
    assert fp.is_file()  # and the healthy pass caches again


def test_a_failed_overlay_write_leaves_the_previous_generations_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept overlay must never pair with the next generation's env.

    agent-1 changes BOTH its args and its env, then its overlay write fails.
    The kept overlay carries the old args and points --env-file at a fixed
    sidecar name: publishing the new env there would launch old args against
    new credentials for the pass window. The staged write is discarded instead.
    Per-agent, not pass-wide: agent-0's overlay landed, so its env commits.
    """
    src = _mk_tree(tmp_path, env={"K": "old"})
    _rewrite(tmp_path)
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    victim_sidecar = env_dir / rewriter.env_sidecar_name("agent-1", "srv")
    healthy_sidecar = env_dir / rewriter.env_sidecar_name("agent-0", "srv")
    assert json.loads(victim_sidecar.read_text())["K"] == "old"
    assert json.loads(healthy_sidecar.read_text())["K"] == "old"

    for i in (0, 1):
        spec_path = src / f"agent-{i}.json"
        spec = json.loads(spec_path.read_text())
        spec["mcpServers"]["srv"]["args"] = [f"a{i}-changed"]
        spec["mcpServers"]["srv"]["env"] = {"K": "new"}
        spec_path.write_text(json.dumps(spec))

    real_write = rewriter.atomic_write

    def flaky(target: Path, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "agent-1.json":
            raise OSError("disk full")
        real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter, "atomic_write", flaky)
    _rewrite(tmp_path)

    kept = json.loads((tmp_path / "mcp-gateway" / "agents" / "agent-1.json").read_text())
    kept_flags = expand_stub_flags(kept["mcpServers"]["srv"]["args"])
    assert "--env-file" in kept_flags, "premise: the kept overlay names a sidecar"
    kept_env_file = Path(kept_flags[kept_flags.index("--env-file") + 1])
    assert kept_env_file == victim_sidecar, "the kept overlay moved off its own sidecar"
    assert (
        json.loads(victim_sidecar.read_text())["K"] == "old"
    ), "the kept overlay's old args now launch against the new generation's env"
    assert (
        json.loads(healthy_sidecar.read_text())["K"] == "new"
    ), "a sibling's successful overlay must still commit its own sidecar"
    assert _sidecar_temps(env_dir) == [], "a discarded sidecar left its temp file behind"
    # Degraded pass not cached: the next boot retries and both sides converge.
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


def test_a_staged_sidecar_is_owner_only_and_complete_before_it_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protection precedes content, and deferring the commit does not move it.

    Two measurements at two seams: the descriptor is locked down while the file
    is still zero bytes (no secret existed in a readable file at any point), and
    at the moment the OVERLAY is written the sidecar is still staged -- already
    0o600, already holding this generation's env, not yet at its published name.
    The staged snapshot is also what proves the commit is deferred at all.
    """
    src = _mk_tree(tmp_path, env={"SECRET_TOKEN": "s3cr3t"})
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"

    sizes_at_lockdown: list[int] = []
    real_fchmod = rewriter.platform_compat.fchmod_safe

    def measuring(fd: int, mode: int) -> Any:
        sizes_at_lockdown.append(os.fstat(fd).st_size)
        return real_fchmod(fd, mode)

    staged_seen: list[tuple[int, dict[str, Any]]] = []
    real_write = rewriter.atomic_write

    def inspecting(target: Path, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "agent-0.json":
            for tmp in _sidecar_temps(env_dir):
                staged_seen.append((tmp.stat().st_mode & 0o777, json.loads(tmp.read_text())))
        return real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter.platform_compat, "fchmod_safe", measuring)
    monkeypatch.setattr(rewriter, "atomic_write", inspecting)
    _rewrite(tmp_path)

    assert sizes_at_lockdown, "premise: the sidecar writer locked a descriptor down"
    assert all(
        s == 0 for s in sizes_at_lockdown
    ), f"a secret byte was written before the lockdown: {sizes_at_lockdown}"
    assert staged_seen, "the sidecar was committed before its overlay was written"
    for mode, content in staged_seen:
        assert content == {"SECRET_TOKEN": "s3cr3t"}
        if rewriter.platform_compat.IS_POSIX:
            assert mode == 0o600, f"staged sidecar readable by others: {oct(mode)}"
    # Committed by the end of the pass, at its published name, nothing left over.
    assert (env_dir / rewriter.env_sidecar_name("agent-0", "srv")).is_file()
    assert _sidecar_temps(env_dir) == []
    assert (src / "agent-0.json").is_file()


def test_the_deferred_commit_keeps_the_pass_cacheable(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The fingerprint must sign the COMMITTED sidecar names.

    The cache signature hashes every recorded sidecar; if it signed a staged
    temp (or a name whose file is not there yet) every boot would rewrite and
    the cache-skip path would be dead. So: signatures point at real published
    files, and a second pass over unchanged inputs is served from cache.
    """
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    fp = json.loads((tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).read_text())
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    recorded = fp["outputs"]["sidecars"]
    assert recorded, "premise: the fixture declares env, so sidecars were recorded"
    for name, sig in recorded.items():
        assert not name.startswith("."), f"a staged temp name was recorded: {name}"
        assert (env_dir / name).is_file(), f"recorded sidecar missing: {name}"
        assert sig

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before, "unchanged inputs stopped being cacheable"


def test_a_failed_sidecar_commit_publishes_no_other_generations_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar that cannot be renamed into place is REMOVED, not left stale.

    The overlay lands first, so its stub argv already names the sidecar. If the
    rename then fails (a same-directory ENOSPC/EIO, a Windows sharing
    violation), a file left at the published name would hand the new command
    another generation's credentials. Both readers treat a MISSING sidecar as
    "declares no env", which is the degradation a failed staging write already
    produces, and the pass is uncacheable so the next one republishes.
    """
    src = _mk_tree(tmp_path, env={"K": "old"})
    _rewrite(tmp_path)
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    victim_sidecar = env_dir / rewriter.env_sidecar_name("agent-1", "srv")
    healthy_sidecar = env_dir / rewriter.env_sidecar_name("agent-0", "srv")
    assert json.loads(victim_sidecar.read_text())["K"] == "old"

    for i in (0, 1):
        spec_path = src / f"agent-{i}.json"
        spec = json.loads(spec_path.read_text())
        spec["mcpServers"]["srv"]["args"] = [f"a{i}-changed"]
        spec["mcpServers"]["srv"]["env"] = {"K": "new"}
        spec_path.write_text(json.dumps(spec))

    real_replace = os.replace

    def flaky(source: Any, dest: Any, **kwargs: Any) -> None:
        # Narrow and delegating: this is the module-global os.replace, which
        # atomic_write and pytest's own teardown also call.
        if Path(dest) == victim_sidecar:
            raise OSError("no space left on device")
        return real_replace(source, dest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rewriter.os, "replace", flaky)
        _rewrite(tmp_path)

    published = json.loads((tmp_path / "mcp-gateway" / "agents" / "agent-1.json").read_text())
    flags = expand_stub_flags(published["mcpServers"]["srv"]["args"])
    assert "--env-file" in flags, "premise: the published overlay names a sidecar"
    assert not victim_sidecar.exists(), (
        "a stale sidecar under a freshly published overlay pairs the new command "
        "with another generation's credentials"
    )
    assert json.loads(healthy_sidecar.read_text())["K"] == "new"  # sibling unaffected
    assert _sidecar_temps(env_dir) == []
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


def test_a_raise_between_staging_and_commit_leaves_no_credential_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged sidecars are reclaimed on every exit from an agent's iteration.

    A staged name starts with a dot and pathlib's ``*.json`` glob skips
    dotfiles, so the sidecar prune can never reclaim one: a raise between the
    staging and the commit would leave credential files on disk for good.
    """
    _mk_tree(tmp_path, env={"SECRET_TOKEN": "s3cr3t"})
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"

    def exploding(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("env harvest fails between staging and commit")

    with monkeypatch.context() as patch:
        patch.setattr(rewriter, "_collect_target_env", exploding)
        with pytest.raises(RuntimeError):
            _rewrite(tmp_path)

    assert _sidecar_temps(env_dir) == [], "a staged credential temp outlived the pass"
    assert list(env_dir.glob("*.json")) == []  # nothing published either
