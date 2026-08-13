"""Rewrite-fingerprint cache for the MCP overlay rewriter.

``rewrite_agents`` skips the parse/resolve/write pass when a stat-only
fingerprint of every input matches the previous completed run. These tests pin
the two sides of that contract: an unchanged boot is served from cache with a
byte-identical result, and EVERY input that can change the output invalidates
the cache — a missed input would ship a stale overlay silently, which is
strictly worse than the boot cost the cache removes.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.rewriter import (
    _FINGERPRINT_NAME,
    overlay_ready,
    rewrite_agents,
)

# The running interpreter: absolute, exists, and executable on every CI
# platform, so the fixture passes the resolver's absolute-path exec check
# and the ``shutil.which`` bare-name path is never entered.
_CMD = sys.executable


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
# Scope: top-level functions in ``rewriter.py`` only. ``defs`` is built from
# the module body, so a method on a class or a helper in another module is NOT
# scanned — the tripwire covers the in-module helper graph, not everything the
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
        # Baked into every overlay ``command``; fingerprinted as "python".
        ("_build_stub_entry", "sys.executable"),
        # The fingerprint builder reading its own declared inputs.
        ("_rewrite_inputs_fingerprint", "os.environ:PATH"),
        ("_rewrite_inputs_fingerprint", "os.environ:PATHEXT"),
        ("_rewrite_inputs_fingerprint", "sys.executable"),
        # Output-AFFECTING since issue #3495: decides whether an env-declaring
        # server is pooled at all. Read once per pass in rewrite_agents and
        # fingerprinted as "forward_declared_env" (see
        # test_forward_declared_env_change_invalidates).
        ("forward_declared_env_enabled", "config-import:kiro_crew.config.loader"),
    }
)

#: Entry points of the rewrite pass; the scan covers their transitive
#: in-module reference closure (see ``_referenced_defs``).
_REWRITE_PASS_ROOTS = frozenset(
    {"rewrite_agents", "_rewrite_single_spec", "_build_stub_entry"}
)


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


def _ambient_reads(
    func_name: str, fn: ast.AST, config_names: set[str]
) -> set[tuple[str, str]]:
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
            if isinstance(node.slice, ast.Constant) and isinstance(
                node.slice.value, str
            ):
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
    tree = ast.parse(inspect.getsource(rewriter))
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


def _mk_tree(root: Path, *, n_agents: int = 2, with_env: bool = True) -> Path:
    src = root / "agents"
    src.mkdir(parents=True, exist_ok=True)
    settings = root / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "global-x": {"command": _CMD, "args": ["g"], "poolable": True}
                }
            }
        )
    )
    for i in range(n_agents):
        servers: dict[str, Any] = {
            "srv": {"command": _CMD, "args": [f"a{i}"], "poolable": True}
        }
        if with_env:
            servers["srv"]["env"] = {"K": "v"}
        (src / f"agent-{i}.json").write_text(
            json.dumps({"name": f"agent-{i}", "mcpServers": servers})
        )
    return src


@pytest.fixture(autouse=True)
def _forward_declared_env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force declared-env forwarding ON for this module.

    Since issue #3495 (cause B pre-classification) a poolable server that
    declares env while forwarding is OFF is left unwrapped — which would gut
    every ``with_env=True`` fixture here (no stub, no sidecar, no target_env).
    These tests exercise the fingerprint/caching machinery, not the
    classification policy (covered in test_mcp_gateway_rewriter.py), so pin
    the flag ON. ``test_forward_declared_env_change_invalidates`` overrides
    this per-call to prove the flag is itself a fingerprint input.
    """
    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: True)


def _rewrite(root: Path, **overrides: Any) -> tuple[dict[str, int], dict[str, str]]:
    kwargs: dict[str, Any] = dict(
        source_dir=root / "agents",
        overlay_dir=root / "mcp-gateway" / "agents",
        socket_path=root / "gw.sock",
        work_dir=root / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        # Stubbing is opt-in per server name; the fixture's servers must be
        # listed or nothing is wrapped and no sidecar/target_env exists.
        stub_servers=frozenset({"srv", "global-x"}),
        pooling_enabled=True,
    )
    kwargs.update(overrides)
    return rewrite_agents(**kwargs)


@pytest.fixture()
def rewrite_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count entries into the per-spec rewrite; zero new counts == skipped."""
    calls = {"n": 0}
    real = rewriter._rewrite_single_spec

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(rewriter, "_rewrite_single_spec", spy)
    return calls


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


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

    The flag decides whether an env-declaring server is pooled at all (issue
    #3495 cause B), so serving a cached overlay across a flip would keep a
    server pooled that the new policy declassifies (or vice versa).
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


def test_source_content_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
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


def test_settings_mcp_json_deletion_invalidates_and_prunes_overlay(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    settings_overlay = tmp_path / "mcp-gateway" / "settings" / "mcp.json"
    assert settings_overlay.is_file()
    (tmp_path / "settings" / "mcp.json").unlink()

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before + 2
    assert not settings_overlay.exists()


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


def test_path_env_change_invalidates(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH feeds shutil.which bare-name resolution."""
    _mk_tree(tmp_path)
    _rewrite(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "extra-bin") + os.pathsep + os.environ.get("PATH", ""))
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
                "outputs": {"overlays": ["../escape.json"], "sidecars": [],
                            "settings_overlay": False},
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
    overlay = json.loads(
        (tmp_path / "mcp-gateway" / "agents" / "agent-0.json").read_text()
    )
    args = overlay["mcpServers"]["srv"]["args"]
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
        "kirocrew-test-vanishing-cmd.bat"
        if os.name == "nt"
        else "kirocrew-test-vanishing-cmd"
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
    files, env sidecars, the settings overlay (file + dir), and the
    fingerprint — not just the containing directories (on Windows the file
    DACL is what carries access)."""
    import stat as _stat

    _mk_tree(tmp_path, n_agents=1)
    _rewrite(tmp_path)
    overlay_dir = tmp_path / "mcp-gateway" / "agents"
    settings_overlay = tmp_path / "mcp-gateway" / "settings" / "mcp.json"
    env_dir = tmp_path / "mcp-gateway" / "stubs" / "env"
    artifacts = [
        overlay_dir / "agent-0.json",
        settings_overlay,
        overlay_dir / _FINGERPRINT_NAME,
        *env_dir.glob("*.json"),
    ]
    assert len(artifacts) >= 4  # incl. at least one sidecar
    for a in artifacts:
        a.chmod(0o644)
    settings_overlay.parent.chmod(0o755)

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit (chmod is stat-invisible)
    for a in artifacts:
        assert _stat.S_IMODE(a.stat().st_mode) == 0o600, a
    assert _stat.S_IMODE(settings_overlay.parent.stat().st_mode) == 0o700


def test_transient_source_read_failure_is_not_cached(
    tmp_path: Path, rewrite_counter: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that stats fine but fails to READ must not freeze an incomplete
    output set: readability can return without the stat signature changing."""
    _mk_tree(tmp_path)
    real_read = Path.read_text
    fail = {"on": True}

    def flaky(self: Path, *args: Any, **kwargs: Any) -> str:
        if fail["on"] and self.name == "agent-0.json" and "agents" in self.parts:
            raise OSError("transient I/O error")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
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
    absent (and pruned its overlay) must not be cached, and a fingerprint from
    an earlier healthy run must be removed."""
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
    # The settings overlay is back (the degraded pass had pruned it).
    assert (tmp_path / "mcp-gateway" / "settings" / "mcp.json").is_file()


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


def test_stray_settings_overlay_is_pruned_on_the_skip_path(
    tmp_path: Path, rewrite_counter: dict[str, int]
) -> None:
    """A settings overlay whose deletion failed on a prior full pass must not
    survive cache hits — removed global MCP servers must not stay active."""
    src = _mk_tree(tmp_path, n_agents=1)
    (tmp_path / "settings" / "mcp.json").unlink()
    _bump_mtime(src / "agent-0.json")  # ensure fresh inputs
    _rewrite(tmp_path)  # fingerprint records: no settings overlay

    stray = tmp_path / "mcp-gateway" / "settings" / "mcp.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}")

    before = rewrite_counter["n"]
    _rewrite(tmp_path)
    assert rewrite_counter["n"] == before  # cache hit
    assert not stray.exists()  # and the stray overlay was swept


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

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.rewriter.platform_compat.restrict_to_owner", failing
    )
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
