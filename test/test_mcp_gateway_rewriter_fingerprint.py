"""Rewrite-fingerprint cache for the MCP overlay rewriter.

``rewrite_agents`` skips the parse/resolve/write pass when a stat-only
fingerprint of every input matches the previous completed run. These tests pin
the two sides of that contract: an unchanged boot is served from cache with a
byte-identical result, and EVERY input that can change the output invalidates
the cache — a missed input would ship a stale overlay silently, which is
strictly worse than the boot cost the cache removes.

The keep on a settings read fault and its refusals, the content that stays cacheable, the
legacy settings overlay, the fingerprint file and the transient-keep retention windows are
tested in ``test_mcp_gateway_rewriter_fingerprint_keep.py``; the tree builder, the rewrite
helper and the fixtures both files use live in ``mcp_gateway_rewriter_fingerprint_helpers.py``.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_gateway_rewriter_fingerprint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _bump_mtime,
    _forward_declared_env_on,
    _mk_tree,
    _rewrite,
    _settings_unreadable,
    rewrite_counter_fixture,
)

from conftest import make_dir_link
from kiro_crew import agent_discovery
from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import _FINGERPRINT_NAME, rewrite_agents


def test_rewrite_agents_signature_is_pinned_to_fingerprint_inputs() -> None:
    """Every rewrite parameter must stay classified as fingerprinted or output-neutral."""
    assert set(inspect.signature(rewrite_agents).parameters) == {
        "source_dir",
        "overlay_dir",
        "socket_path",
        "work_dir",
        "sandbox_mode",
        "approval_mode",
        "stub_servers",
        "pooling_enabled",
        # Fingerprinted through ``LaunchApprovals.digest``.
        "approvals",
    }


# --- Ambient-read tripwire -------------------------------------------------
#
# The signature pin above catches new *parameters*; this scan catches new
# *ambient reads* — env vars, config-loader lookups, home/cwd resolution —
# consulted inside the rewrite pass without a signature change. An ambient
# read that affects the output but is absent from the fingerprint yields
# silently stale overlays, so every read reaching the pass must be a conscious
# decision: either fingerprinted or documented output-neutral.
#
# Scope: top-level functions in ``rewriter.py`` and its ``rewriter_*`` owner
# modules only. ``defs`` is built from their module bodies, so a method on a class
# or a helper in another module is NOT scanned — the tripwire covers the in-module helper graph, not everything the
# pass can transitively touch. A NEW entry point added to the pass must also be
# added to ``_REWRITE_PASS_ROOTS`` to be covered.
#
# Each allowlist entry is (enclosing top-level function, channel). Env reads
# with a literal key carry the variable name (``os.environ:PATH``), so a new
# variable read in an already-listed function still trips. On a failure,
# classify before touching this list:
#   * NEW only            -> a genuinely new read. Output-affecting: add it to
#     ``_rewrite_inputs_fingerprint`` AND an invalidation test in this file.
#     Output-neutral: document why in the fingerprint docstring (see the
#     ``forward_declared_env`` precedent). Then extend this allowlist.
#   * same channel NEW in one function and STALE in another -> a MOVED read
#     (refactor). Re-key the entry to the new function; do NOT change the
#     fingerprint — a redundant key would force a full rewrite for every
#     existing user on upgrade.
#   * STALE only          -> the read is gone; prune the entry.
_AMBIENT_READ_ALLOWLIST = frozenset(
    {
        # The fingerprint builder reading its own declared inputs.
        ("_rewrite_inputs_fingerprint", "os.environ:PATH"),
        ("_rewrite_inputs_fingerprint", "os.environ:PATHEXT"),
        ("_rewrite_inputs_fingerprint", "sys.executable"),
        # Output-AFFECTING: the filtered source view whose values are written
        # into the env sidecar (credential-keyed names removed so an
        # agent-writable spec cannot dereference a secret under a benign key).
        # Deliberately NOT fingerprinted -- encountering a placeholder marks the
        # pass uncacheable instead (see test_env_placeholder_pass_is_never_cached
        # and test_a_spec_without_placeholders_still_caches), so a resolved value
        # is re-resolved on every boot and can never be served stale.
        ("_placeholder_source_env", "os.environ:<dynamic>"),
        # Output-NEUTRAL: distinguishes a credential-filtered refusal from a
        # plain typo purely to pick the log message; the substitution result is
        # the literal ``${VAR}`` either way.
        ("_expand_env_placeholders", "os.environ:<dynamic>"),
        # Output-AFFECTING: decides whether an env-declaring server is pooled at
        # all. Read once per pass in rewrite_agents and
        # fingerprinted as "forward_declared_env" (see
        # test_forward_declared_env_change_invalidates).
        ("forward_declared_env_enabled", "config-import:kiro_crew.config.loader"),
        # Output-AFFECTING: decides which secret-prefixed keys are folded into
        # effective_env_hash and passed on stub argv. Read once per pass in
        # rewrite_agents and fingerprinted as "pool_identity_env" (see
        # test_pool_identity_env_change_invalidates).
        ("pool_identity_env_keys", "config-import:kiro_crew.config.loader"),
    }
)

#: Entry points of the rewrite pass; the scan covers their transitive
#: in-module reference closure (see ``_referenced_defs``).
_REWRITE_PASS_ROOTS = frozenset({"rewrite_agents", "_rewrite_single_spec", "_build_stub_entry"})


def _module_config_names(tree: ast.Module) -> set[str]:
    """Module-level names bound by importing from ``kiro_crew.config*``.

    A module-scope ``from kiro_crew.config.x import Y`` followed by ``Y.load()``
    inside a helper is a config read with no function-local import to detect,
    so the imported names themselves become detection targets.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("kiro_crew.config"):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("kiro_crew.config"):
                    names.add((alias.asname or alias.name).split(".")[0])
    return names


def _referenced_defs(fn: ast.AST, defs: dict[str, ast.AST]) -> set[str]:
    """Module functions REFERENCED inside ``fn`` — not just directly called.

    Any ``Name`` or attribute mention counts, so aliasing (``f = helper``) and
    callback passing (``run(helper)``) pull ``helper`` into the closure. This
    deliberately over-approximates (a docstring cannot alias, but a mention in
    code can): the tripwire is fail-closed by design — an over-scanned
    function's reads surface as an explicit allowlist decision, never as a
    silent pass.
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out & set(defs)


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_key(args: list[ast.expr]) -> str:
    if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
        return args[0].value
    return "<dynamic>"


def _ambient_reads(func_name: str, fn: ast.AST, config_names: set[str]) -> set[tuple[str, str]]:
    """Best-effort detectors for the common ambient channels.

    Not a sandbox: a determined read can evade an AST scan. The goal is to
    make the ORDINARY way of adding one (``os.environ``, a config-loader
    import or name, home/cwd resolution) fail a test until the fingerprint
    decision is made consciously.
    """
    hits: set[tuple[str, str]] = set()
    consumed: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                recv = func.value
                if isinstance(recv, ast.Name) and recv.id == "os":
                    if func.attr == "getenv":
                        hits.add((func_name, f"os.environ:{_env_key(node.args)}"))
                    elif func.attr == "getcwd":
                        hits.add((func_name, "os.getcwd"))
                elif func.attr == "get" and _is_os_environ(recv):
                    hits.add((func_name, f"os.environ:{_env_key(node.args)}"))
                    consumed.add(id(recv))
                # Receiver-agnostic on purpose: ``os.path.expanduser(p)``,
                # ``Path(p).expanduser()``, and ``Path.home()`` all resolve
                # the ambient home/cwd regardless of the receiver's AST shape.
                if func.attr in ("expanduser", "expandvars", "home", "cwd"):
                    hits.add((func_name, f".{func.attr}()"))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            key = "<dynamic>"
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                key = node.slice.value
            hits.add((func_name, f"os.environ:{key}"))
            consumed.add(id(node.value))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("kiro_crew.config"):
                hits.add((func_name, f"config-import:{node.module}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("kiro_crew.config"):
                    hits.add((func_name, f"config-import:{alias.name}"))
    # Second pass: reads the call-shaped pass above did not consume — a bare
    # ``os.environ`` (copy/iteration/membership), sys/platform attributes, and
    # any mention of a module-level config-loader name (call OR alias).
    for node in ast.walk(fn):
        if _is_os_environ(node) and id(node) not in consumed:
            hits.add((func_name, "os.environ:<dynamic>"))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base, attr = node.value.id, node.attr
            if base == "sys" and attr in ("executable", "argv", "platform"):
                hits.add((func_name, f"sys.{attr}"))
            elif base == "platform":
                hits.add((func_name, f"platform.{attr}"))
        elif isinstance(node, ast.Name) and node.id in config_names:
            hits.add((func_name, f"config-name:{node.id}"))
    return hits


def test_rewrite_pass_ambient_reads_match_pinned_allowlist() -> None:
    """A new ambient read reaching the rewrite pass must fail until classified.

    Walks the transitive in-module reference closure from the rewrite-pass
    roots and asserts the exact set of detected ambient reads equals the
    pinned allowlist — a NEW read fails (classify: fingerprint it or document
    it output-neutral), a REMOVED read fails (prune the stale entry), and a
    read that MOVED functions in a refactor shows up as one NEW + one STALE
    for the same channel (re-key the entry; no fingerprint change).
    """
    family = (rewriter.__name__, *rewriter._REWRITER_OWNERS)
    tree = ast.parse("".join(inspect.getsource(sys.modules[name]) for name in family))
    defs: dict[str, ast.AST] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing_roots = _REWRITE_PASS_ROOTS - set(defs)
    assert not missing_roots, (
        f"rewrite-pass roots renamed or removed: {sorted(missing_roots)}; "
        "update _REWRITE_PASS_ROOTS to the new entry points"
    )
    config_names = _module_config_names(tree)

    reachable: set[str] = set()
    frontier = set(_REWRITE_PASS_ROOTS)
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        frontier |= _referenced_defs(defs[name], defs) - reachable

    found: set[tuple[str, str]] = set()
    for name in sorted(reachable):
        found |= _ambient_reads(name, defs[name], config_names)

    new = found - _AMBIENT_READ_ALLOWLIST
    stale = _AMBIENT_READ_ALLOWLIST - found
    moved = {ch for _, ch in new} & {ch for _, ch in stale}
    assert found == _AMBIENT_READ_ALLOWLIST, (
        f"ambient reads changed in the rewrite pass.\n"
        f"NEW: {sorted(new)}\n"
        f"STALE: {sorted(stale)}\n"
        f"Same channel in BOTH lists ({sorted(moved) or 'none'}) = a MOVED "
        f"read: re-key the allowlist entry to the new function, do NOT touch "
        f"the fingerprint.\n"
        f"NEW only = a genuinely new read: classify it — output-affecting "
        f"goes into _rewrite_inputs_fingerprint plus an invalidation test; "
        f"output-neutral gets documented in the fingerprint docstring. Then "
        f"extend _AMBIENT_READ_ALLOWLIST.\n"
        f"STALE only = the read is gone: prune the entry."
    )


def test_unchanged_inputs_skip_the_rewrite_and_return_identical_result(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    _mk_tree(tmp_path)
    cold = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 2

    warm = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 2  # no spec re-parsed
    # The caller feeds target_env into GatewaySpec.mcp_target_env — the cached
    # result must be exactly what a full rewrite would have returned.
    assert warm == cold
    assert warm[1]  # non-trivial: target env actually carries entries


def test_forward_declared_env_change_invalidates(
    tmp_path: Path,
    rewrite_counter: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping ``mcp_gateway.forward_declared_env`` regenerates the overlays.

    The flag decides whether an env-declaring server is pooled at all (cause B),
    so serving a cached overlay across a flip would keep a server pooled that the
    new policy declassifies (or vice versa).
    """
    _mk_tree(tmp_path, with_env=True)
    on = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 2
    assert on[0]  # forwarding on: env-declaring servers are wrapped

    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: False)
    off = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 4, "flag flip must not serve the cache"
    # Forwarding off: the env-declaring servers are declassified (unwrapped).
    assert off != on


def test_pool_identity_env_change_invalidates(
    tmp_path: Path,
    rewrite_counter: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming a key in ``mcp_gateway.pool_identity_env`` regenerates the overlays.

    The list decides which keys are folded into ``effective_env_hash`` and passed
    on stub argv. Serving a cached overlay across an edit is not a cosmetic
    staleness: the cached stub would keep hashing the OLD set while gatewayd
    hashes the new one, so the coherence gate would refuse to forward and the
    setting would appear to do nothing.
    """
    _mk_tree(tmp_path, with_env=True)
    # Declare a rotating-secret-shaped key so the list has something to act on.
    # With nothing opted in this key is withheld from a shared backend, so the
    # pre-classification leaves the server UNWRAPPED (cause B).
    spec_path = tmp_path / "agents" / "agent-0.json"
    spec = json.loads(spec_path.read_text())
    spec["mcpServers"]["srv"]["env"]["OAUTH_TOKEN"] = "t"
    spec_path.write_text(json.dumps(spec))

    before = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 2
    # agent-0 declares 'srv' (env-bearing) plus the injected 'global-x'. Only
    # global-x is wrapped while OAUTH_TOKEN is withheld.
    assert before[0]["agent-0.json"] == 1, "a withheld key must block pooling"

    monkeypatch.setattr(rewriter, "pool_identity_env_keys", lambda: frozenset({"OAUTH_TOKEN"}))
    after = _rewrite(tmp_path)
    assert rewrite_counter["n"] == 4, "an identity-list edit must not serve the cache"
    # Non-vacuous, and the feature's headline behaviour: naming the key folds it
    # into the pool identity, so it stops being withheld and 'srv' pools too.
    assert after[0]["agent-0.json"] == 2
    assert before != after


def test_env_placeholder_pass_is_never_cached(
    tmp_path: Path,
    rewrite_counter: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared ``${env:VAR}`` re-resolves on every boot.

    The resolved value is baked into the sidecar at write time (so the stub and
    gatewayd hash one agreed source), which makes the VARIABLE an input the
    stat-based fingerprint cannot see. Rather than fingerprint the environment,
    the pass is left uncacheable -- otherwise a rotated credential would keep
    serving the OLD value for as long as no file changed.
    """
    monkeypatch.setenv("WH_TOKEN", "first")
    _mk_tree(tmp_path, n_agents=1, env={"TOKEN": "${env:WH_TOKEN}"})
    _rewrite(tmp_path)
    sidecar_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    assert any("first" in p.read_text() for p in sidecar_dir.glob("*.json"))

    # No fingerprint is left behind, so nothing can be served from cache.
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert not fp.exists(), "a placeholder pass must not store a fingerprint"

    before = rewrite_counter["n"]
    monkeypatch.setenv("WH_TOKEN", "second")
    _rewrite(tmp_path)
    assert rewrite_counter["n"] > before, "a changed env value must be re-resolved"
    assert any("second" in p.read_text() for p in sidecar_dir.glob("*.json"))
    assert not any("first" in p.read_text() for p in sidecar_dir.glob("*.json"))


def test_a_spec_without_placeholders_still_caches(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The placeholder opt-out must not disable the cache for everyone else.

    Pins the blast radius of ``env_placeholder_seen``: a declared env with no
    ``${...}`` reference is unaffected and an unchanged boot is still served
    from cache.
    """
    _mk_tree(tmp_path, n_agents=1, env={"K": "literal-value"})
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert fp.is_file(), "a placeholder-free pass must still cache"

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before, "an unchanged boot must serve the cache"


def test_source_content_change_invalidates(tmp_path: Path, rewrite_counter: dict[str, int]) -> None:
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    spec = json.loads((src / "agent-0.json").read_text())
    spec["mcpServers"]["srv"]["args"] = ["changed-and-longer"]
    (src / "agent-0.json").write_text(json.dumps(spec))

    before = rewrite_counter["n"]
    _, target_env = _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    assert any("changed-and-longer" in v for v in target_env.values())


def test_same_size_mtime_bump_invalidates(tmp_path: Path, rewrite_counter: dict[str, int]) -> None:
    """size+mtime_ns together: a same-size write must still invalidate."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    _bump_mtime(src / "agent-0.json")

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_settings_mcp_json_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """settings/mcp.json is an input; omitting it would serve stale overlays
    after a global MCP config edit."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    _bump_mtime(tmp_path / "settings" / "mcp.json")

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_settings_mcp_json_deletion_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """Deleting the global settings file changes the injection set, so the
    cache must invalidate. No settings overlay is involved: the rewriter never
    writes one."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    assert not (tmp_path / "mcp-gateway" / "settings" / "mcp.json").exists()
    (tmp_path / "settings" / "mcp.json").unlink()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


@pytest.mark.parametrize(
    "override",
    [
        {"sandbox_mode": "strict"},
        {"approval_mode": "yolo-ish"},
        {"stub_servers": frozenset({"srv"})},
        {"pooling_enabled": False},
    ],
)
def test_config_parameter_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], override: dict[str, Any]
) -> None:
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path, **override)
    assert rewrite_counter["n"] == before + 2


def test_socket_and_work_dir_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """socket_path and work_dir are in the stub argv and the PoolKey."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path, socket_path=tmp_path / "other.sock")
    assert rewrite_counter["n"] == before + 2
    _rewrite(tmp_path, socket_path=tmp_path / "other.sock", work_dir=tmp_path / "wd2")
    assert rewrite_counter["n"] == before + 4


def test_pre_casing_schema_forces_regeneration(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema 5 overlays can embed the PATHEXT spelling and must be rebuilt."""
    _mk_tree(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(rewriter, "_FINGERPRINT_SCHEMA", 5)
        _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_pre_cmd_safe_schema_forces_regeneration(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema 6 overlays can carry a spaced interpreter command that cmd.exe
    quote-stripping destroys; an upgrade must rebuild them, not serve them."""
    _mk_tree(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(rewriter, "_FINGERPRINT_SCHEMA", 6)
        _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_pre_governed_passthrough_schema_forces_regeneration(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema 7 overlay can hold a stub for a server that must not be pooled.

    A registry-governed or muted entry passes through unwrapped, and its inputs
    are identical either way -- so the stat fingerprint alone sees no change and
    a kept overlay keeps a stub whose name ``injection_server_names`` collects.
    That stub launches the server at session level: for a marked entry, one whose
    launch belongs to the administrator's catalog; for a muted one, the server the
    user silenced. The schema is what rejects such an overlay on upgrade.
    """
    _mk_tree(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(rewriter, "_FINGERPRINT_SCHEMA", 7)
        _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_a_kept_overlay_from_before_the_vendor_denylist_drops_the_vendor_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema 10 overlay can hold a stub for a credential vendor.

    Before the vendors joined ``UNPOOLABLE_SERVERS`` an operator could list one
    in ``stub_servers`` and the rewriter wrapped it. The denylist is not a
    fingerprint input, so with every input unchanged a kept overlay would keep
    that stub, and its shared backend would run outside every chat sandbox. The
    schema bump is what rejects such an overlay on the next boot.
    """
    from mcp_gateway_rewriter_fingerprint_helpers import _CMD

    from kiro_crew.mcp_gateway import rewriter_spec
    from kiro_crew.mcp_gateway.rewriter_launch import _WRAPPER_MARKER

    src = tmp_path / "agents"
    src.mkdir()
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (src / "agent-0.json").write_text(
        json.dumps({"name": "agent-0", "mcpServers": {"creds-agent": {"command": _CMD}}})
    )
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"

    def vendor_entry() -> dict[str, Any]:
        return json.loads(overlay.read_text())["mcpServers"]["creds-agent"]

    stub_servers = frozenset({"creds-agent"})
    with monkeypatch.context() as patch:
        # The pass as it ran before this change: schema 10, empty denylist.
        patch.setattr(rewriter, "_FINGERPRINT_SCHEMA", 10)
        patch.setattr(rewriter_spec, "UNPOOLABLE_SERVERS", frozenset())
        _rewrite(tmp_path, stub_servers=stub_servers)
    assert vendor_entry().get(_WRAPPER_MARKER) is True  # pooled before

    # Same inputs, current schema and denylist: the overlay must be rebuilt.
    _rewrite(tmp_path, stub_servers=stub_servers)
    assert _WRAPPER_MARKER not in vendor_entry()
    assert vendor_entry()["command"] == _CMD


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows executable casing")
@pytest.mark.parametrize("transient_settings_fault", [False, True])
@pytest.mark.parametrize("rename_command", [False, True])
def test_windows_casing_is_revalidated_on_cached_and_kept_overlays(
    tmp_path: Path,
    rewrite_counter: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    transient_settings_fault: bool,
    rename_command: bool,
) -> None:
    """A case-only binary rename changes dispatch even when which stays identical."""
    real_bin_dir = tmp_path / "Real Bin"
    real_bin_dir.mkdir()
    bin_dir = tmp_path / "Linked Bin"
    make_dir_link(bin_dir, real_bin_dir)
    filename = "casing-mcp.exe"
    exe = real_bin_dir / filename
    exe.touch()
    expected = bin_dir / filename
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".EXE")
    src = _mk_tree(tmp_path, n_agents=1, with_env=False)
    spec_path = src / "agent-0.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["mcpServers"]["srv"]["command"] = "casing-mcp"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    first = _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    written = json.loads(overlay.read_text(encoding="utf-8"))
    flags = expand_stub_flags(written["mcpServers"]["srv"]["args"])
    assert flags[flags.index("--target-command") + 1] == str(expected)
    before = rewrite_counter["n"]
    if rename_command:
        filename = "CASING-mcp.exe"
        exe = exe.rename(exe.with_name(filename))
        expected = bin_dir / filename
    with _settings_unreadable() if transient_settings_fault else contextlib.nullcontext():
        second = _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + int(rename_command)
    if not rename_command:
        # A transient keep reports no rewrites, but still publishes its targets.
        assert second[1] == first[1]
    written = json.loads(overlay.read_text(encoding="utf-8"))
    flags = expand_stub_flags(written["mcpServers"]["srv"]["args"])
    assert flags[flags.index("--target-command") + 1] == str(expected)


def test_effective_user_site_policy_change_invalidates(
    tmp_path: Path,
    rewrite_counter: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mk_tree(tmp_path, n_agents=1, with_env=False)
    monkeypatch.setattr(rewriter.platform_compat, "is_bundled_interpreter", lambda: False)
    monkeypatch.setattr(rewriter.platform_compat.site, "ENABLE_USER_SITE", True)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    first_args = json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
    assert first_args[:3] == ["-P", "-m", rewriter._STUB_MODULE]

    before = rewrite_counter["n"]
    monkeypatch.setattr(rewriter.platform_compat.site, "ENABLE_USER_SITE", False)
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 1, "a user-site policy change must not serve cache"
    second_args = json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
    assert second_args[:4] == ["-s", "-P", "-m", rewriter._STUB_MODULE]


def test_path_env_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH feeds shutil.which bare-name resolution."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    monkeypatch.setenv(
        "PATH", str(tmp_path / "extra-bin") + os.pathsep + os.environ.get("PATH", "")
    )
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_package_version_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded package must regenerate overlays written by older logic."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    monkeypatch.setattr(rewriter, "__version__", "999.0.0-test")
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2


def test_deleted_agent_spec_invalidates_and_prunes(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-1.json"
    assert overlay.is_file()
    (src / "agent-1.json").unlink()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 1  # one agent left
    assert not overlay.exists()


def test_transient_overlay_write_failure_keeps_the_previous_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale-overlay prune keeps a TRANSIENT failure's previous overlay
    instead of keying on write success: a single transient
    overlay-write failure must leave that agent's previous, healthy overlay
    on disk — stale-but-working beats no overlay at all — while the other
    agents still rewrite."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    victim = tmp_path / "mcp-gateway" / "agents" / "agent-1.json"
    survivor = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    assert victim.is_file()
    before_bytes = victim.read_bytes()

    # Change agent-1's CONTENT (not just mtime): a successful write would now
    # produce different bytes, so surviving-with-old-bytes proves both that
    # the write failed and that the prune spared the file.
    spec_path = src / "agent-1.json"
    spec = json.loads(spec_path.read_text())
    spec["mcpServers"]["srv"]["args"] = ["changed-args"]
    spec_path.write_text(json.dumps(spec))

    real_write = rewriter.atomic_write
    fail = {"on": True}

    def flaky(target: Path, *args: Any, **kwargs: Any) -> None:
        if fail["on"] and Path(target).name == "agent-1.json":
            raise OSError("disk full")
        real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter, "atomic_write", flaky)
    _rewrite(tmp_path)
    fail["on"] = False

    assert victim.is_file()  # healthy overlay survived the failed pass
    assert victim.read_bytes() == before_bytes  # ...byte-identical, not rewritten
    assert survivor.is_file()  # the unaffected agent still rewrote
    # Degraded pass not cached: the next boot retries the write.
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert not fp.exists()

    # Fault cleared: the retry rewrites agent-1 and the stale bytes go away.
    _rewrite(tmp_path)
    assert victim.read_bytes() != before_bytes
    assert fp.is_file()


def test_transient_agent_read_failure_keeps_the_previous_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same keying, other transient arm: a source spec that fails to READ this
    pass keeps its previous overlay (a read-failure ``continue`` that skips
    ``written.add`` lets the prune delete the healthy overlay) — AND its env
    sidecars: the kept overlay's stub argv still
    points ``--env-file`` at them, and a read-failure pass cannot enumerate
    the victim's sidecar names, so the sidecar prune is skipped for the whole
    (already-uncacheable) pass. Pruning them would spawn the kept overlay's
    backends credential-less for the rest of the gateway's lifetime."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    victim = tmp_path / "mcp-gateway" / "agents" / "agent-1.json"
    assert victim.is_file()
    before_bytes = victim.read_bytes()
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    sidecars_before = set(env_dir.glob("*.json"))
    assert sidecars_before  # _mk_tree declares env, so sidecars exist

    _bump_mtime(src / "agent-1.json")  # invalidate so the next call rewrites
    # Specs are read through ``agent_discovery._read_spec_bytes``, whose
    # unreadable file surfaces as ``OSError``; the strict reader lets that
    # propagate as the transient ``OSError`` handled here.
    real_read = agent_discovery._read_spec_bytes
    fail = {"on": True}
    victim_src = (src / "agent-1.json").resolve()

    def flaky(real: Path) -> bytes:
        if fail["on"] and Path(real) == victim_src:
            raise OSError(errno.EACCES, "agent spec could not be read", str(real))
        return real_read(real)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", flaky)
    _rewrite(tmp_path)
    fail["on"] = False

    assert victim.is_file()  # overlay survived the unreadable-source pass
    assert victim.read_bytes() == before_bytes
    # Every sidecar survived too — overlay/sidecar coherence is kept.
    assert set(env_dir.glob("*.json")) == sidecars_before
    # Degraded pass not cached: the next boot retries the read.
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


def test_malformed_source_still_prunes_its_overlay(
    tmp_path: Path,
) -> None:
    """Deterministic bad content (JSONDecodeError / non-dict) is NOT a
    transient keep: the pass is cacheable and the cached-path prune keys on
    the stored outputs, so sparing the overlay here would let two boots over
    identical inputs behave differently. Bad content prunes exactly like a
    deleted source."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-1.json"
    assert overlay.is_file()

    (src / "agent-1.json").write_text("{not json")
    _rewrite(tmp_path)
    assert not overlay.exists()  # deterministic skip -> pruned, and cacheable
    assert (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).is_file()


def test_write_failure_does_not_suppress_the_prune_for_deleted_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prune keep-set is per-source, never a pass-wide failure switch: in
    ONE degraded pass, a deleted source still loses its overlay while the
    write-failure victim keeps its previous one. Guards against skipping the
    prune whenever anything failed, which would leak overlays for
    genuinely-deleted agents."""
    src = _mk_tree(tmp_path, n_agents=3)
    _rewrite(tmp_path)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    assert (overlay_dir / "agent-0.json").is_file()
    assert (overlay_dir / "agent-1.json").is_file()
    assert (overlay_dir / "agent-2.json").is_file()

    (src / "agent-0.json").unlink()  # genuinely deleted -> must be pruned
    real_write = rewriter.atomic_write

    def flaky(target: Path, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "agent-1.json":
            raise OSError("disk full")
        real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter, "atomic_write", flaky)
    _rewrite(tmp_path)

    assert not (overlay_dir / "agent-0.json").exists()  # prune still ran
    assert (overlay_dir / "agent-1.json").is_file()  # victim kept its overlay
    assert (overlay_dir / "agent-2.json").is_file()  # healthy agent rewrote


def test_write_failure_pass_keeps_the_kept_overlays_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that kept a previous overlay must not prune that overlay's env
    sidecars. With a server RENAME (srv -> srv2) in the failing agent's spec,
    ``written_sidecars`` holds only the new name — pruning the old ``srv``
    sidecar would leave the kept overlay's ``--env-file`` dangling and spawn
    that backend credential-less until the next boot. The sidecar prune is
    therefore skipped on any transient-keep pass (already uncacheable, so a
    healthy boot re-sweeps)."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    sidecars_before = set(env_dir.glob("*.json"))
    assert sidecars_before

    # Rename agent-1's server so the new pass enumerates a DIFFERENT sidecar
    # name, then fail agent-1's overlay write so the old overlay is kept.
    spec_path = src / "agent-1.json"
    spec = json.loads(spec_path.read_text())
    spec["mcpServers"]["srv2"] = spec["mcpServers"].pop("srv")
    spec_path.write_text(json.dumps(spec))

    real_write = rewriter.atomic_write

    def flaky(target: Path, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "agent-1.json":
            raise OSError("disk full")
        real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter, "atomic_write", flaky)
    _rewrite(tmp_path)

    # The old (agent-1, srv) sidecar — still referenced by the kept overlay's
    # --env-file — survived the pass.
    assert sidecars_before <= set(env_dir.glob("*.json"))
    # Degraded pass not cached: the next boot retries and re-sweeps.
    assert not (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).exists()


def test_kept_overlay_target_mappings_stay_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept overlay's stub must keep resolving to its OWN backend command.
    The stub's pool key hashes the OLD args, so if the pass only published
    ``target_env`` from freshly-rewritten specs, the retained stub would miss
    its args-hashed entry and fall back to the bare server-name key — which,
    with two agents declaring the same server name, is ANOTHER agent's
    command. The pass therefore harvests every kept overlay's wrapped entries
    into ``target_env`` after the prune (mirroring the cached path)."""
    src = _mk_tree(tmp_path)
    _, healthy_env = _rewrite(tmp_path)
    # The fixture gives agent-0/agent-1 the same server name with different
    # args, so each contributes its own args-hashed key.
    hashed_before = {k for k in healthy_env if "__" in k}
    assert len(hashed_before) >= 2

    # Change agent-1's args and fail its overlay write: its overlay (old
    # args) is kept, and the fresh pass publishes only the NEW args' hash.
    spec_path = src / "agent-1.json"
    spec = json.loads(spec_path.read_text())
    spec["mcpServers"]["srv"]["args"] = ["changed-args"]
    spec_path.write_text(json.dumps(spec))

    real_write = rewriter.atomic_write

    def flaky(target: Path, *args: Any, **kwargs: Any) -> None:
        if Path(target).name == "agent-1.json":
            raise OSError("disk full")
        real_write(target, *args, **kwargs)

    monkeypatch.setattr(rewriter, "atomic_write", flaky)
    _, degraded_env = _rewrite(tmp_path)

    # Every hash-keyed mapping the healthy pass published is still present:
    # the kept overlay's old-args hash (what its live stub actually sends)
    # resolves to its own command, not the bare-key fallback.
    assert hashed_before <= set(degraded_env)


def test_missing_overlay_file_forces_full_rewrite(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """A manually deleted overlay must be regenerated, not skipped over."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    overlay.unlink()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    assert overlay.is_file()


def test_missing_env_sidecar_forces_full_rewrite(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    _mk_tree(tmp_path, with_env=True)
    _rewrite(tmp_path)
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    sidecars = list(env_dir.glob("*.json"))
    assert sidecars
    sidecars[0].unlink()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    assert list(env_dir.glob("*.json"))


@pytest.mark.parametrize(
    "content",
    [
        "not json at all {{{",
        json.dumps("a string, not an object"),
        json.dumps({"inputs": {}, "outputs": {}}),  # missing results/target_env
        json.dumps(
            {
                "inputs": {},
                "outputs": {"overlays": ["../escape.json"], "sidecars": []},
                "results": {},
                "target_env": {},
            }
        ),  # path traversal in a recorded name
    ],
)
def test_bad_fingerprint_means_full_rewrite_not_a_match(
    tmp_path: Path, rewrite_counter: dict[str, int], content: str
) -> None:
    """Unreadable/malformed must mean 'rewrite', never 'match' — and never raise."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert fp.is_file()
    fp.write_text(content)

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    # and the full rewrite healed the fingerprint
    assert json.loads(fp.read_text())["inputs"]


def test_traversal_name_in_matching_fingerprint_is_rejected(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """Defense in depth: recorded output names are joined onto the overlay
    dirs, so a tampered fingerprint whose INPUTS still match must be refused
    on its names, not probed outside the tree."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    data = json.loads(fp.read_text())
    data["outputs"]["overlays"]["../escape.json"] = [1, 1]
    fp.write_text(json.dumps(data))

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2  # full rewrite, not a match


def test_skip_path_still_prunes_stray_overlay_and_sidecar(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The prune pass must not live inside the skipped branch."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    stray_overlay = tmp_path / "mcp-gateway" / "agents" / "stray.json"
    stray_overlay.write_text("{}")
    stray_sidecar = tmp_path / "mcp-gateway" / "stubs" / "env" / "stray.json"
    stray_sidecar.write_text("{}")

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit
    assert not stray_overlay.exists()
    assert not stray_sidecar.exists()


def test_unresolved_bare_command_is_reprobed_and_install_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """which() failure depends on filesystem state the stat fingerprint cannot
    see. An unchanged failure is cacheable (the re-probe agrees), but the
    binary APPEARING at an already-listed PATH dir — no PATH string change, no
    spec change — must invalidate via the recorded-probe comparison."""
    bin_dir = tmp_path / "extra-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    src = _mk_tree(tmp_path, n_agents=1, with_env=False)
    spec = json.loads((src / "agent-0.json").read_text())
    spec["mcpServers"]["srv"]["command"] = "kirocrew-test-definitely-missing-cmd"
    (src / "agent-0.json").write_text(json.dumps(spec))

    _rewrite(tmp_path)
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert fp.exists()  # unresolved probes are recorded, not cache-disabling

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # still unresolved -> cache hit

    # Install the binary WITHOUT touching PATH or the spec. On Windows,
    # shutil.which resolves bare names only through PATHEXT, so the file
    # needs a .bat suffix there; the spec keeps declaring the bare name.
    exe_name = (
        "kirocrew-test-definitely-missing-cmd.bat"
        if os.name == "nt"
        else "kirocrew-test-definitely-missing-cmd"
    )
    exe = bin_dir / exe_name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)

    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 1  # probe disagreed -> full rewrite
    overlay = json.loads((tmp_path / "mcp-gateway" / "agents" / "agent-0.json").read_text())
    args = expand_stub_flags(overlay["mcpServers"]["srv"]["args"])
    i = args.index("--target-command")
    # normcase: which() may report a differently-cased/slashed spelling on
    # Windows than pathlib's str().
    assert os.path.normcase(args[i + 1]) == os.path.normcase(str(exe))


def test_resolved_binary_removal_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: a bare command that RESOLVED is later removed
    while PATH and specs are unchanged — the cache must not keep serving the
    dead absolute path (the pre-fix every-boot rewrite would have healed it)."""
    bin_dir = tmp_path / "extra-bin"
    bin_dir.mkdir()
    # .bat on Windows: shutil.which resolves bare names only through PATHEXT.
    exe_name = (
        "kirocrew-test-vanishing-cmd.bat" if os.name == "nt" else "kirocrew-test-vanishing-cmd"
    )
    exe = bin_dir / exe_name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    src = _mk_tree(tmp_path, n_agents=1, with_env=False)
    spec = json.loads((src / "agent-0.json").read_text())
    spec["mcpServers"]["srv"]["command"] = "kirocrew-test-vanishing-cmd"
    (src / "agent-0.json").write_text(json.dumps(spec))

    _rewrite(tmp_path)
    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # resolved and unchanged -> cache hit

    exe.unlink()  # binary removed; PATH string and specs unchanged

    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 1  # probe disagreed -> full rewrite


def test_edited_overlay_content_forces_full_rewrite(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """Outputs are validated by size+mtime_ns, not mere existence: a
    hand-edited overlay would diverge from the cached target_env (the stub's
    PoolKey would hash the edited command while gatewayd spawns the recorded
    one), so it must be regenerated."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    overlay = tmp_path / "mcp-gateway" / "agents" / "agent-0.json"
    data = json.loads(overlay.read_text())
    overlay.write_text(json.dumps(data, indent=4))  # same JSON, different bytes

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    assert json.loads(overlay.read_text()) == data  # regenerated canonical form


def test_same_size_same_mtime_content_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """The content digest is load-bearing: a same-size write whose mtime is
    restored to the recorded tick (coarse-timestamp collision, or a tool that
    preserves times) must still invalidate — e.g. an autoApprove entry
    swapped for an equal-length one changes the permission surface."""
    src = _mk_tree(tmp_path, n_agents=1)
    spec_path = src / "agent-0.json"
    original = spec_path.read_text()
    _rewrite(tmp_path)
    st = spec_path.stat()

    # Same byte length, different bytes; then force the ORIGINAL mtime back.
    assert "agent-0" in original
    spec_path.write_text(original.replace("agent-0", "tnega-0"))
    os.utime(spec_path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert spec_path.stat().st_size == st.st_size
    assert spec_path.stat().st_mtime_ns == st.st_mtime_ns

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 1  # digest mismatch -> full rewrite


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_cache_hit_retightens_all_artifact_permissions(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """A chmod changes no content signature, so the cache-hit path must
    re-assert owner-only permissions on EVERY served artifact — overlay
    files, env sidecars, and the fingerprint — not just the containing
    directories (on Windows the file DACL is what carries access)."""
    import stat as _stat

    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    artifacts = [
        overlay_dir / "agent-0.json",
        overlay_dir / _FINGERPRINT_NAME,
        *env_dir.glob("*.json"),
    ]
    assert len(artifacts) >= 3  # incl. at least one sidecar
    for a in artifacts:
        a.chmod(0o644)

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit (chmod is stat-invisible)
    for a in artifacts:
        assert _stat.S_IMODE(a.stat().st_mode) == 0o600, a


def test_transient_source_read_failure_is_not_cached(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that stats fine but fails to READ must not freeze an incomplete
    output set: readability can return without the stat signature changing."""
    _mk_tree(tmp_path)
    # Agent specs are read through ``agent_discovery._read_spec_bytes``; an
    # ``OSError`` from it is the transient read failure the rewriter keeps the
    # previous overlay for.
    real_read = agent_discovery._read_spec_bytes
    fail = {"on": True}

    def flaky(real: Path) -> bytes:
        p = Path(real)
        if fail["on"] and p.name == "agent-0.json" and "agents" in p.parts:
            raise OSError(errno.EACCES, "agent spec could not be read", str(real))
        return real_read(real)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", flaky)
    _rewrite(tmp_path)
    fail["on"] = False

    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert not fp.exists()  # degraded pass not cached

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2  # full retry
    assert (tmp_path / "mcp-gateway" / "agents" / "agent-0.json").is_file()
    assert fp.is_file()  # healthy pass cached again


def test_transient_settings_read_failure_is_not_cached(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings/mcp.json read site has the same transient-failure rule as
    the agent-spec site: a pass that treated an existing settings file as
    absent must not be cached, and a fingerprint from an earlier healthy run
    must be removed."""
    src = _mk_tree(tmp_path)
    _rewrite(tmp_path)  # healthy run: fingerprint exists
    fp = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
    assert fp.is_file()

    _bump_mtime(src / "agent-0.json")  # invalidate so the next call rewrites
    real_read = Path.read_text
    fail = {"on": True}

    def flaky(self: Path, *args: Any, **kwargs: Any) -> str:
        if fail["on"] and self.name == "mcp.json" and "settings" in self.parts:
            raise OSError("transient I/O error")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    _rewrite(tmp_path)
    fail["on"] = False

    assert not fp.exists()  # degraded pass not cached, stale fingerprint gone

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2  # full retry after fault clears
    assert fp.is_file()
