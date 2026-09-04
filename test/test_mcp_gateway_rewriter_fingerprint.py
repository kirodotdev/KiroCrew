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
import contextlib
import errno
import inspect
import json
import os
import sys
from collections.abc import Iterator
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
        # Output-AFFECTING since issue #3495: decides whether an env-declaring
        # server is pooled at all. Read once per pass in rewrite_agents and
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


def _mk_tree(
    root: Path,
    *,
    n_agents: int = 2,
    with_env: bool = True,
    env: dict[str, Any] | None = None,
) -> Path:
    src = root / "agents"
    src.mkdir(parents=True, exist_ok=True)
    settings = root / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global-x": {"command": _CMD, "args": ["g"], "poolable": True}}})
    )
    for i in range(n_agents):
        servers: dict[str, Any] = {"srv": {"command": _CMD, "args": [f"a{i}"], "poolable": True}}
        if with_env:
            servers["srv"]["env"] = {"K": "v"} if env is None else dict(env)
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
    # Same reasoning for the identity set: pin it to the default (nothing opted
    # in) so these tests never read the developer's real config, and let
    # ``test_pool_identity_env_change_invalidates`` override it per-call.
    monkeypatch.setattr(rewriter, "pool_identity_env_keys", lambda: frozenset())


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


@contextlib.contextmanager
def _settings_unreadable() -> "Iterator[None]":
    """Make ``settings/mcp.json`` unreadable for the duration, as a real
    transient I/O fault does.

    Both read paths must fail together, and which ones they are is the point.
    ``_rewrite_inputs_fingerprint`` signs the file with ``_stat_sig``, which
    uses ``read_bytes``; the pass itself uses ``read_text``. Faulting only
    ``read_text`` leaves the signature intact, so on otherwise-unchanged inputs
    the cached early return fires and the rewrite loop is never entered -- the
    degraded path under test would not run at all. Faulting both makes the
    signature ``None``, which is what forces the full rewrite on a real fault,
    while leaving every agent source signature comparable.
    """
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def _is_settings(p: Path) -> bool:
        return p.name == "mcp.json" and "settings" in p.parts

    def flaky_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if _is_settings(self):
            raise OSError("transient I/O error")
        return real_read_text(self, *args, **kwargs)

    def flaky_bytes(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if _is_settings(self):
            raise OSError("transient I/O error")
        return real_read_bytes(self, *args, **kwargs)

    Path.read_text = flaky_text  # type: ignore[method-assign]
    Path.read_bytes = flaky_bytes  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.read_text = real_read_text  # type: ignore[method-assign]
        Path.read_bytes = real_read_bytes  # type: ignore[method-assign]


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
    # pre-classification leaves the server UNWRAPPED (issue #3495 cause B).
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
    # into the pool identity, so it is no longer withheld and 'srv' pools too.
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
    writes one (#8111)."""
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
    instead of keying on write success (#5328): a single transient
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
    pass keeps its previous overlay (before #5328 the read-failure
    ``continue`` skipped ``written.add`` and the prune deleted the healthy
    overlay) — AND its env sidecars: the kept overlay's stub argv still
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
    real_read = Path.read_text
    fail = {"on": True}
    victim_src = src / "agent-1.json"

    def flaky(self: Path, *args: Any, **kwargs: Any) -> str:
        if fail["on"] and self == victim_src:
            raise OSError("transient I/O error")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
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
    deleted source, as before #5328."""
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
    write-failure victim keeps its previous one. Guards against 'fixing'
    #5328 by skipping the prune whenever anything failed, which would leak
    overlays for genuinely-deleted agents."""
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


def test_transient_settings_read_failure_does_not_rewrite_agent_overlays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings read that FAILED is not a settings file that declared
    nothing: the injection set is unknown, not empty, so no agent overlay may
    be written from it (#5344).

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
    """The #5344 refusal only withholds a rewrite where withholding PRESERVES
    something. An agent with no previous overlay has no injected copy to drop,
    so refusing would leave it with no overlay at all -- unpooling its own
    servers too, which is worse than the fault warrants and worse than what
    this pass does without the fix. It must still be written."""
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
    assert "--poolable" in json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]

    with _settings_unreadable():
        _rewrite(tmp_path, pooling_enabled=False)  # operator turns sharing OFF

    args = json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
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
    args = json.loads(overlay.read_text())["mcpServers"]["srv"]["args"]
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
    rewrites every overlay with an empty injection set, which is #5344 through
    the stat path instead of the read path. Only ``FileNotFoundError`` may mean
    absent; every other OSError means unknown."""
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
    signature. No settings overlay exists in either state (#8111)."""
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
    """Locks the #8111 removal in: a healthy pass over a settings file holding
    poolable AND non-poolable servers must not write
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
    """A settings overlay left behind by a pre-#8111 release is deliberately
    NOT deleted: it was always written owner-only into a 0o700 directory
    (issue #5285), its content is a subset copy of the user's real settings
    file, and nothing reads it -- inert, not exposed. An automated deleter
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
    """The upgrade-safety invariant of the #8111 removal: a fingerprint
    written by a pre-change release carries an ``outputs.settings_overlay``
    signature. The loader must IGNORE it — not reject it — so the first
    upgraded boot over unchanged inputs is still served from cache, and the
    #5344 transient-keep gate keeps comparing inputs meaningfully."""
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
    """The ONE guard kept for the leftover pre-#8111 overlay: a loosened ACL
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
