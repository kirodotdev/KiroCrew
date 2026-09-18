"""Capture what every known harness is LAUNCHED as, for the golden and its writer.

Two callers import this: ``test/test_acp_launch_goldens.py``, which is strictly
read-only against the committed fixture, and
``scripts/update_acp_launch_goldens.py``, which is the only thing that writes it. The
machinery lives here rather than in the test module because a test must not create
files in the repo that outlive the run (AUTOSDE ``no-test-side-effects``), and a
regeneration hook inside a test module is exactly that.

What is captured, per backend id: the argv handed to the process factory, the label
the spawn is logged under, the label stderr is drained under, and the environment
variables the spawn ADDS to the ones it inherited. Those four are the observable
contract of the per-harness spawn arms. A change that moves where they are COMPUTED
must not move what they ARE -- for kiro-cli above all, whose construction path
harness-parity H13 keeps free of work added for an adapter.

The environment is recorded as the DELTA from ``os.environ`` rather than in full, so
the snapshot is a property of the code and not of the machine that ran it. Four
values inside that delta are placeholders for the same reason: the augmented search
path, the interpreter path, pi's per-session gate nonce and the per-session identity
token all vary per host or per run, while the FACT that the harness receives them is
what is pinned.

Every collaborator on the spawn path is stubbed to a fixed answer, including the
resolvers, the sandbox wrapper and each harness's own routing read-back. The point is
the argv and the env, so a real sandbox profile or a real read-back child would add
host dependence and prove nothing this capture asks about. A collaborator that answers
with one of its own arguments is stubbed through :func:`_stub_for`, which reads what to
accept off the live object, so this file holds no copy of a signature it does not own.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.config import paths as config_paths
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

#: The committed fixture. Resolved from this file so both callers agree on it.
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "acp_launch_goldens.json"

#: Fixed resolver answers. Absolute and obviously synthetic so a real host path can
#: never leak into the snapshot.
_KIRO_BIN = "/opt/bin/kiro-cli"
_CLAUDE_ACP_ARGV = ["/opt/bin/node", "/opt/lib/claude-agent-acp/index.js"]
_CODEX_ACP_ARGV = ["/opt/bin/node", "/opt/lib/codex-acp/index.js"]
_PI_ACP_ARGV = ["/opt/bin/node", "/opt/lib/pi-acp/index.js"]
_PI_BIN = "/opt/bin/pi"
_PI_LAUNCHER = "/opt/run/pi-launcher.sh"
_PI_EXTENSION = "/opt/run/kiro_crew_tool_gate.ts"
_OPENCODE_BIN = "/opt/bin/opencode"
_GOOSE_BIN = "/opt/bin/goose"
_DEEPSEEK_BIN = "/opt/bin/dsh"
_SEARCH_PATH = "/opt/bin"
_OPENCODE_CONFIG = '{"permission":"ask"}'

#: Env keys whose VALUE is a property of the host or the run. The key still has to
#: appear -- that a harness receives it at all is the fact being pinned.
VOLATILE_ENV = {
    "PATH": "<augmented-path>",
    "KIROCREW_RUNTIME_PYTHON": "<interpreter>",
    "KIROCREW_PI_GATE_SESSION": "<nonce>",
    # Minted from ``secrets`` on every client, so it can never match a golden twice.
    # That a one-session client's child RECEIVES it is the fact being pinned: it is
    # how a control-plane MCP server on that child resolves its own session.
    STUB_SESSION_TOKEN_ENV: "<session-token>",
}

#: The parent environment every capture runs against, whatever the recording host's
#: own environment happens to be.
#:
#: This is load-bearing, and it is the correction to a real defect rather than
#: tidiness. ``env_added`` is a DELTA, so measuring it against the ambient
#: ``os.environ`` made the answer a property of the recording process: a host that
#: already exported a variable ``_spawn`` also sets saw no difference and recorded no
#: key, while a clean runner recorded one. The variable that actually did this is
#: ``KIROCREW_SPAWNED`` -- Crew sets it on every agent it spawns, so a capture taken
#: from inside an agent could never see ``_spawn`` set it.
#:
#: Pinning the parent makes the delta a property of the CODE. Anything ``_spawn``
#: contributes now appears on every host, including a variable it merely re-asserts.
_FIXED_PARENT_ENV = {
    "PATH": "/opt/bin",
}

#: Keys carried through from the real environment because the interpreter and the OS
#: need them, and ``_spawn`` sets none of them -- so their values stay identical
#: between parent and child and never reach the delta. Windows in particular cannot
#: resolve a home directory or a temp dir without these.
_PASSTHROUGH_ENV_KEYS = (
    "SYSTEMROOT",
    "SystemRoot",
    "SYSTEMDRIVE",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "TEMP",
    "TMP",
    "TMPDIR",
    "COMSPEC",
    "PATHEXT",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
)


def fixed_parent_env() -> dict:
    """The parent environment a capture runs against.

    A small fixed base plus an ALLOWLIST carried through from the host. The
    allowlist is what keeps this usable on Windows; it names only variables
    ``_spawn`` does not touch, so nothing carried through can hide a key the way the
    ambient environment did.
    """
    env = dict(_FIXED_PARENT_ENV)
    for key in _PASSTHROUGH_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def golden_key(backend: str) -> str:
    """The fixture key for *backend*. kiro-cli's id is the empty string."""
    return backend or "kiro"


class _Recorder:
    """What one ``_spawn`` handed to the factory, the logger and the drainer."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.spawn_label = ""
        self.stderr_label = ""


def _stub_for(real: Any, answer: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
    """A stub for *real* that accepts exactly the arguments *real* accepts.

    What the stub accepts is DERIVED from ``real``'s own signature, by binding each
    call against it, so the stub tracks the thing it stubs: a keyword the spawn path
    starts handing over is accepted here the moment ``real`` declares it, and one
    ``real`` does not declare raises ``TypeError`` here exactly as it would there.

    That derivation is the point. A parameter list typed out by hand is a second copy
    of somebody else's signature, and it goes stale silently the moment the first copy
    grows an argument -- a capture that stubs eight collaborators would hold eight such
    copies. Widening to ``**kwargs`` is worse than a stale copy: it accepts anything,
    so the drift stops being visible at all and this file stops measuring the argument
    it claims to.

    ``answer`` receives the bound arguments by name and returns what the stub returns,
    which lets a stub answer with one of the call's own values.
    """
    signature = inspect.signature(real)

    def _stub(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return answer(bound.arguments)

    return _stub


def _async_stub_for(real: Any, answer: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
    """:func:`_stub_for` for a collaborator the spawn path awaits."""
    inner = _stub_for(real, answer)

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        return inner(*args, **kwargs)

    return _stub


#: Collaborators the capture answers with one of the call's OWN arguments, and which
#: argument each answers with. Every one of them reads the host otherwise -- the two
#: env resolvers and the pod home remap read config and the real environment, the pod
#: bundle wrap resolves a bundled runtime, and the cgroup wrap asks the OS for a scope
#: -- while the argv and the env they are handed are the answers this file records. So
#: each hands its own argument straight back.
#:
#: Keyed by attribute name so the stub is built from the live object: see
#: :func:`_stub_for` for why the accepted arguments are derived rather than typed out.
_PASSTHROUGH_STUBS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "scrub_agent_subprocess_env": lambda call: call["env"],
    "_resolve_spawn_env": lambda call: call["env"],
    "_apply_pod_home_remap": lambda call: call["env"],
    "cgroup_scope_argv": lambda call: call["argv"],
    "apply_pod_bundle_spawn": lambda call: (call["argv"], False),
}

#: The same, for the collaborator the spawn path awaits. It answers with the argv it
#: was handed and no cleanup handle, so the sandbox wrap contributes nothing to the
#: argv this file records.
_ASYNC_PASSTHROUGH_STUBS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "wrap_argv_async": lambda call: (list(call["argv"]), None),
}


def _stub_common(stack: list, rec: _Recorder, tmp_path: Path) -> None:
    """Patch every collaborator that is not the answer under test."""
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = None
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.stdin = MagicMock()

    async def _factory(*argv: str, **kwargs: Any):
        rec.argv = list(argv)
        rec.env = dict(kwargs.get("env") or {})
        return proc

    async def _drain(_stream, *, label: str):
        rec.stderr_label = label

    def _finish(_process, _pid, *, label: str) -> bool:
        rec.spawn_label = label
        return True

    # The pass-through collaborators, each accepting what the live object accepts.
    stack.extend(
        patch.object(client_mod, name, side_effect=_stub_for(getattr(client_mod, name), answer))
        for name, answer in _PASSTHROUGH_STUBS.items()
    )
    stack.extend(
        patch.object(
            client_mod, name, side_effect=_async_stub_for(getattr(client_mod, name), answer)
        )
        for name, answer in _ASYNC_PASSTHROUGH_STUBS.items()
    )

    stack.extend(
        [
            patch.object(client_mod, "create_subprocess_limited", side_effect=_factory),
            patch.object(client_mod, "finish_suspended_spawn", side_effect=_finish),
            patch.object(AcpClient, "_drain_stderr", side_effect=_drain),
            patch.object(AcpClient, "_prepare_spawn_workspace", return_value=None),
            patch.object(AcpClient, "_resolve_session_mcp_servers", return_value=[]),
            patch.object(client_mod, "browser_session_env", return_value={}),
            patch.object(client_mod, "browser_socket_env", return_value={}),
            patch.object(client_mod, "inject_xdist_auto_cap", return_value=None),
            patch.object(client_mod, "_get_child_pids", return_value=[]),
            patch.object(client_mod.agent_scratch, "allocate_scratch", return_value=None),
            patch.object(client_mod, "_run_preflight_bounded", new=AsyncMock(return_value=())),
            patch("kiro_crew.session._track_pid", return_value=None),
            patch("kiro_crew.session._track_session_pid", return_value=None),
            patch.object(
                client_mod, "assert_voice_runtime_outside_agent_workspace", return_value=None
            ),
            patch.object(
                client_mod,
                "bind_voice_safe_agent_workspace_async",
                new=AsyncMock(return_value=(str(tmp_path), None)),
            ),
            # kiro-cli's own pre-spawn gates. Each reads disk or the agents tree;
            # the argv they guard is what this capture records, not their verdicts.
            patch.object(
                client_mod, "_resolve_kiro_bin_for_spawn", new=AsyncMock(return_value=_KIRO_BIN)
            ),
            patch.object(client_mod, "ensure_agent_materialized", return_value=None),
            patch.object(client_mod, "require_fresh_derived_spec", return_value=None),
            patch.object(client_mod, "require_fork_governance", return_value=None),
            patch.object(client_mod, "delegated_workspace_exposes_agents_dir", return_value=None),
            # The adapter resolvers.
            patch.object(
                client_mod,
                "_resolve_claude_acp_bin",
                return_value=(_CLAUDE_ACP_ARGV, _SEARCH_PATH),
            ),
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(_CODEX_ACP_ARGV, _SEARCH_PATH)
            ),
            patch.object(
                client_mod, "_resolve_pi_acp_bin", return_value=(_PI_ACP_ARGV, _SEARCH_PATH)
            ),
            patch.object(client_mod, "_resolve_pi_bin", return_value=(_PI_BIN, _SEARCH_PATH)),
            patch.object(client_mod, "_resolve_claude_code_executable", return_value=""),
            patch.object(AcpClient, "_write_claude_local_settings", return_value=None),
            patch.object(client_mod, "_seal_pi_gate_extension", return_value=_PI_EXTENSION),
            patch.object(client_mod, "_ensure_pi_gate_launcher", return_value=_PI_LAUNCHER),
            patch.object(AcpClient, "_verify_pi_gate", return_value=("", "")),
            patch.object(AcpClient, "_verify_opencode_routing", return_value=("", "")),
            patch.object(AcpClient, "_opencode_routing_config", return_value=_OPENCODE_CONFIG),
            patch.object(client_mod, "_unlink_readback_launcher", return_value=None),
            # The DEFAULT data home, pinned to this run's temp dir. Required rather
            # than incidental: the parent environment above carries no
            # ``KIROCREW_HOME``, so anything on the spawn path that resolves
            # ``config_dir()`` falls through to the operator's real ``~/.kiro/crew``
            # and CREATES it (``conftest._refuse_a_resolved_real_default_home`` fails
            # the run for exactly that). Pinning the resolver rather than exporting the
            # variable keeps the capture host-independent, which is the property the
            # fixed parent exists to give it.
            patch.object(config_paths, "_resolve_default_home", lambda: tmp_path / "default-home"),
            patch.object(
                client_mod,
                "_resolve_self_served_bin",
                side_effect=lambda backend: {
                    ACP_BACKEND_OPENCODE: (_OPENCODE_BIN, _SEARCH_PATH),
                    ACP_BACKEND_GOOSE: (_GOOSE_BIN, _SEARCH_PATH),
                    ACP_BACKEND_DEEPSEEK: (_DEEPSEEK_BIN, _SEARCH_PATH),
                }[backend],
            ),
        ]
    )


#: The module-level resolver caches a capture disturbs. Each is resolved once per
#: process behind an ``_UNRESOLVED`` sentinel, so a capture has to clear them to make
#: every backend resolve afresh -- and has to put them back, because the stubbed
#: resolvers WRITE synthetic paths into them during the spawn.
_ADAPTER_CACHE_NAMES = (
    "_claude_acp_argv_cache",
    "_codex_acp_argv_cache",
    "_pi_acp_argv_cache",
    "_pi_bin_cache",
)


def snapshot_bin_caches() -> dict[str, Any]:
    """The resolver caches as they stand, for :func:`restore_bin_caches`.

    The self-served mapping is copied rather than referenced: it is the same dict
    object the spawn path mutates, so holding the reference would snapshot nothing.
    """
    saved: dict[str, Any] = {name: getattr(client_mod, name) for name in _ADAPTER_CACHE_NAMES}
    saved["_self_served_bin_caches"] = dict(client_mod._self_served_bin_caches)
    return saved


def restore_bin_caches(saved: dict[str, Any]) -> None:
    """Put every resolver cache back exactly as :func:`snapshot_bin_caches` found it.

    This is not tidiness. A capture stubs the resolvers and then drives the real
    spawn, which writes the stub's synthetic path into the process-wide cache. Left
    there, a later test in the same worker that reads a cache it did not seed would
    see ``/opt/bin/...`` and pass or fail on this file's fiction. The mapping is
    updated in place, so a caller holding the same dict object sees the restore.
    """
    for name in _ADAPTER_CACHE_NAMES:
        setattr(client_mod, name, saved[name])
    client_mod._self_served_bin_caches.clear()
    client_mod._self_served_bin_caches.update(saved["_self_served_bin_caches"])


def _reset_bin_caches() -> None:
    """Drop the module-level resolver caches so each backend resolves afresh."""
    unresolved = client_mod._UNRESOLVED
    for name in _ADAPTER_CACHE_NAMES:
        setattr(client_mod, name, unresolved)
    client_mod._self_served_bin_caches.clear()


def capture(backend: str, tmp_path: Path) -> dict[str, Any]:
    """Drive ``_spawn`` for *backend* and return its four launch answers.

    ``tmp_path`` is the work dir the client is built against; nothing is written
    inside the repository.
    """
    rec = _Recorder()
    # The parent the delta is measured against, and the one the spawn actually runs
    # under: both are this fixed environment, so ``env_added`` is what _spawn
    # contributes rather than what this host happened not to have already.
    parent_env = fixed_parent_env()
    # Snapshot BEFORE the reset, restore in ``finally``: the reset clears the caches
    # and the stubbed resolvers then fill them with this file's synthetic paths, so
    # without the restore a later test in the same worker reads that fiction.
    saved_caches = snapshot_bin_caches()
    _reset_bin_caches()
    stack: list = [patch.dict(os.environ, parent_env, clear=True)]
    _stub_common(stack, rec, tmp_path)
    entered: list = []
    try:
        for ctx in stack:
            entered.append(ctx.__enter__())
        client = AcpClient(
            work_dir=tmp_path / "workspace",
            session_key="golden-session",
            acp_backend=backend,
            model="auto",
        )
        asyncio.run(client._spawn())
    finally:
        for ctx in reversed(stack):
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # pragma: no cover - teardown must not mask a failure
                pass
        restore_bin_caches(saved_caches)
    added = {
        key: VOLATILE_ENV.get(key, value)
        for key, value in sorted(rec.env.items())
        if parent_env.get(key) != value
    }
    return {
        "argv": rec.argv,
        "spawn_label": rec.spawn_label,
        "stderr_label": rec.stderr_label,
        "env_added": added,
    }


def capture_all(tmp_path: Path) -> dict[str, Any]:
    """Every known backend's launch answers, keyed as the fixture keys them."""
    return {
        golden_key(backend): capture(backend, tmp_path / golden_key(backend))
        for backend in sorted(ACP_BACKENDS_KNOWN)
    }


def render(snapshot: dict[str, Any]) -> str:
    """The fixture's on-disk text. One spelling, so a rewrite is a content diff."""
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def read_golden() -> dict[str, Any]:
    """The committed fixture."""
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
