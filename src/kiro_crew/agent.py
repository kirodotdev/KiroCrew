"""KiroCrew kiro-cli agent configuration.

Generates and installs ``kirocrew.json`` into ``~/.kiro/agents/``.

Configuration files (edit these, then ``kirocrew setup --agent-only``):

  ``src/kiro_crew/config/defaults.json``
      Base agent config — tools, model, allowedTools, toolsSettings, etc.

  ``src/kiro_crew/config/prompt.md``
      System prompt.

  ``~/.kiro/crew/agent.json``
      User overrides merged on top of defaults (optional).

  ``~/.kiro/crew/prompt.md``
      User prompt override (optional, takes priority over shipped prompt).

Dynamic fields resolved at install time:
  - ``prompt`` — ``file://`` URI pointing to the prompt file
  - ``mcpServers.kirocrew-cron.command`` — absolute path to ``kirocrew`` binary
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, MutableMapping

from kiro_crew import agent_state, platform_compat
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_files import (
    AGENT_FILENAME,
)
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME as _CONDUCTOR_AGENT_FILENAME
from kiro_crew.agent_files import HEARTBEAT_AGENT_FILENAME as _HEARTBEAT_AGENT_FILENAME
from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME as _KNOWLEDGE_AGENT_FILENAME
from kiro_crew.agent_files import LITE_AGENT_FILENAME as _LITE_AGENT_FILENAME
from kiro_crew.agent_files import (
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_files import (
    PIPELINE_CONDUCTOR_AGENT_FILENAME as _PIPELINE_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    REQUIRED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_files import RESEARCH_AGENT_FILENAME as _RESEARCH_AGENT_FILENAME
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config import config_dir
from kiro_crew.config import config_path as _mc_config_path
from kiro_crew.config.paths import (
    _in_ephemeral_tree,
    _in_linked_git_worktree,
    _under_system_tmp,
    _valid_override_home,
    ambient_agents_dir,
    isolated_agents_dir,
    kiro_agents_dir,
)
from kiro_crew.env import (
    MCP_PATH_HINT,
    dedup_path,
    describe_search_path,
    emit_env,
    mcp_search_path,
    spec_path_key,
)
from kiro_crew.mcp_cleanup import purge_deleted_proxy_from_config
from kiro_crew.mcp_provenance import (
    DERIVED_KEY,
    command_is_ours,
    record_derived,
    recorded_source,
    source_view,
    without_marker,
)
from kiro_crew.mcp_utils import kiro_oauth_wire_entry, mcp_server_alias
from kiro_crew.platform import current_context
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.platform import safe_context_call
from kiro_crew.platform.governance import (
    CU_MCP_SERVER,
    agentcore_posture,
    may_skip_gate_now,
    strip_ungoverned_auto_approve,
)
from kiro_crew.platform.governance_profiles import governance_permits
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import (  # circular import: sel imports config which imports agent
    SecurityEvent,
    sel,
)
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)


def _agentcore_capability_permitted() -> bool:
    """Whether the governance ceiling permits ``capabilities.agentcore``.

    Independent of the CPP adapter. An omitted capability is ungoverned
    (permitted); a transient lookup degrades to False. Used by the
    three-conjunct identity probe (adapter AND this AND known posture).
    """
    return bool(
        safe_context_call(
            lambda: getattr(
                governance_permits(
                    "capabilities.agentcore",
                    "",
                    fail_closed=True,
                    log_warning=False,
                ),
                "permitted",
                False,
            ),
            fallback=False,
            log_message="agentcore governance lookup failed; treating as disabled",
        )
    )


def _agent_identity_enabled() -> bool:
    """Whether the composed agent-identity seam is on.

    True only when the adapter is on AND governance permits
    ``capabilities.agentcore`` AND the ceiling stores a known posture.
    Standalone Default returns False without consulting governance, so
    Gateway/token work stays off. An omitted capability is ungoverned
    (permitted), so the known-posture conjunct is what keeps a forced-on
    adapter off when no row is present. A transient adapter/governance
    error degrades to False (never to enabled) via ``safe_context_call``.
    """
    adapter_on = bool(
        safe_context_call(
            lambda: current_context().agent_identity.enabled(),
            fallback=False,
            log_message="agent_identity.enabled lookup failed; treating as disabled",
        )
    )
    if not adapter_on:
        return False
    if not _agentcore_capability_permitted():
        return False
    return bool(
        safe_context_call(
            lambda: agentcore_posture(current_context().governance) is not None,
            fallback=False,
            log_message="agentcore posture lookup failed; treating as disabled",
        )
    )


def _atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp+rename to prevent read-of-partial-file.

    kiro-cli reads agent configs at spawn and set_mode.  Non-atomic writes
    (truncate-then-write) can deliver empty or partial JSON, crashing the
    ACP process with exit code 1.  rename() is atomic on Linux when source
    and destination are on the same filesystem.

    The rename goes through ``replace_with_retry`` because atomicity is not the
    only way that step fails. On Windows ``os.replace`` raises
    ``PermissionError`` while ANY other handle is open on either path, and a
    just-written temp file is exactly what an indexer or AV scanner opens —
    so a correct atomic write can still lose its payload for reasons unrelated
    to this caller. Here that surfaces as a failed spawn, since these are the
    configs kiro-cli reads. The helper is Windows-only and never sleeps on the
    event loop; ``ensure_agent_materialized`` reaches this from
    ``asyncio.to_thread``, so the retry applies on the path that matters.

    Uses mkstemp for a unique temp file per call so concurrent writers
    to the same path don't clobber each other's temp files.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                mode = 0o644
            platform_compat.fchmod_safe(f.fileno(), mode)
            json.dump(data, f, indent=2)
            f.write("\n")
        replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home" and
# issue #874; dashboard/handlers/usage.py is the reference implementation.
KIRO_AGENTS_DIR: Path | None = None


def kiro_agents_dir_path() -> Path:
    """Kiro agents directory, resolved against the live data home.

    Honors the :data:`KIRO_AGENTS_DIR` override hook when a caller (test/tooling)
    has set it; otherwise resolves live via :func:`kiro_agents_dir`.
    """
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


def missing_required_agent_specs() -> list[str]:
    """Return the :data:`REQUIRED_KIRO_AGENT_FILES` absent from the agents dir.

    A post-install verification, not a duplicate of the install: an empty result
    is the only proof that ``rebuild_agent_config`` actually left usable specs on
    disk. Raising is NOT enough on its own, because two non-raising paths also
    end with no spec written:

    * ``rebuild_agent_config`` mkdirs the agents directory as its first act, so a
      failure anywhere after that leaves a created-but-EMPTY directory — which
      reads as "installed" to anything that only checks the directory.
    * it also RETURNS EARLY when :func:`_decline_shared_agent_home` refuses to
      rewrite a shared agent home. Correct on a machine that already has specs
      (it protects the real install's MCP servers); fatal on one that does not,
      where there is nothing to fall back to.

    Checking the filesystem covers both, plus a spec deleted after install. The
    cost of NOT checking is that the first symptom is kiro-cli answering every
    ``session/set_mode`` with "Mode '<name>' not found" — one failed turn at a
    time, with nothing pointing at the install as the cause.
    """
    if _decline_shared_agent_home(audit=False) is not None:
        # This instance is not allowed to own these specs (a pod, or a gateway
        # booted from a linked git worktree), so their absence is not a defect it
        # can repair. Reporting them would put an unrepairable install behind a
        # full-screen gate whose only remedy declines every time. ``audit=False``
        # keeps this read out of the SEL log -- the audit records write DECISIONS,
        # and a status poll is not one.
        return []
    agents_dir = kiro_agents_dir_path()
    return [name for name in REQUIRED_KIRO_AGENT_FILES if not (agents_dir / name).is_file()]


def present_required_agent_specs() -> list[tuple[str, Path]]:
    """Return the :data:`REQUIRED_KIRO_AGENT_FILES` that DO exist, with paths.

    The counterpart to :func:`missing_required_agent_specs`, for the caller that
    needs to ask a question ABOUT a spec rather than about its absence — currently
    whether kiro-cli accepts it.

    Shares that function's ownership guard on purpose. An instance not allowed to
    own these specs (a pod, or a gateway booted from a linked git worktree) must
    not report on them either: it did not write them, cannot repair them, and its
    verdict would describe another install's files.
    """
    if _decline_shared_agent_home(audit=False) is not None:
        return []
    agents_dir = kiro_agents_dir_path()
    return [
        (name, agents_dir / name)
        for name in REQUIRED_KIRO_AGENT_FILES
        if (agents_dir / name).is_file()
    ]


# AGENT_FILENAME imported from agent_files (single source of truth).
_MAIN_AGENT_NAME = "kirocrew"
# Cheap Claude Code model for KiroCrew's background agents (lite / heartbeat).
# Last-resort fallback for the claude_code (CC) seam ONLY: that backend cannot
# resolve the "auto" sentinel, so an unpinned background role needs a concrete
# cheap model. The kiro-cli path uses the resolved role model (default "auto").
_BACKGROUND_CC_MODEL = "claude-sonnet-4.6"


def _background_agent_model() -> str:
    """Kiro-spec model for background worker agents (lite / heartbeat).

    Resolves ``agent.role_models['background']`` -> ``agent.model`` -> ``"auto"``
    (see :meth:`AgentConfig.resolve_model`). Defaults to ``"auto"`` — which the
    provider resolves server-side against the account's entitlement — so a
    background agent stays usable on every subscription tier unless an operator
    deliberately pins a (cheaper) model. Never raises: a config hiccup falls
    back to ``"auto"``.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().agent.resolve_model("background")
    except Exception:
        logger.debug("background model resolve failed; using 'auto'", exc_info=True)
        return "auto"


def _background_cc_model() -> str:
    """cc_model (claude_code seam) for background agents.

    The CC backend cannot resolve ``"auto"``, so an unpinned background role
    falls back to :data:`_BACKGROUND_CC_MODEL`; an operator's explicit pin is
    honored when it names a concrete model.
    """
    m = _background_agent_model()
    return m if m and m != "auto" else _BACKGROUND_CC_MODEL


_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"
# Well-known Claude Code global MCP config. The core does not read this at
# rebuild/discovery/apply time (OSS is Kiro-only); a companion contributes it as
# a scope via the extra_mcp_scopes() CPP seam. Retained as the canonical path
# constant for that companion and for tests.
_CC_MCP_JSON = Path.home() / ".claude.json"

# Bundled fallback — inside the kiro_crew.config package
_BUNDLED_CFG_DIR = Path(__file__).resolve().parent / "config"


def _project_dir() -> Path | None:
    """Return the project root from KIROCREW_PROJECT_DIR, or None."""
    val = os.environ.get("KIROCREW_PROJECT_DIR")
    if val:
        p = Path(val)
        if p.is_dir():
            return p
    return None


def _shipped_defaults() -> Path:
    """Return defaults.json, preferring project-dir override for development."""
    proj = _project_dir()
    if proj:
        candidate = proj / "agents" / "defaults.json"
        if candidate.is_file():
            return candidate
    return _BUNDLED_CFG_DIR / "defaults.json"


def _shipped_prompt() -> Path:
    """Return prompt.md, preferring project-dir override for development."""
    proj = _project_dir()
    if proj:
        candidate = proj / "agents" / "prompt.md"
        if candidate.is_file():
            return candidate
    return _BUNDLED_CFG_DIR / "prompt.md"


# User overrides. Resolved via lazy accessors (NOT module-level config_dir()
# captures): importing agent.py — which cli.py does transitively at import time
# via cli_doctor — must NOT fire config_dir(), or it would create $KIROCREW_HOME
# before main() reaches its `gateway --seed` guard (whose copytree needs an empty
# target) AND trigger the one-time migration off the single ensure_data_home()
# point. Accessors keep every use lazy; the process-cached config_dir() makes
# repeated calls cheap.
def _user_dir() -> Path:
    return config_dir()


def _user_prompt_path() -> Path:
    return _user_dir() / "prompt.md"


def _user_overrides_path() -> Path:
    return _user_dir() / "agent.json"


# kirocrew binary path — resolved lazily to handle gateway restarts
# where PATH may not include the virtualenv at import time.
_KIROCREW_BIN: str | None = None


def _interpreter_runnable(candidate: Path) -> bool:
    """Return True if *candidate* could actually be exec'd as an interpreter.

    Existence is not enough: a present-but-not-executable interpreter fails at
    exec time (``EACCES`` — "bad interpreter: Permission denied"), so a launcher
    naming one is exactly as dead as a launcher naming a reaped path. Keeping
    this stricter than ``exists()`` is what lets :func:`_bin_is_usable` promise it
    narrows only by provably-dead targets.

    POSIX-only narrowing by construction: Windows has no execute bit and
    ``os.access(f, X_OK)`` is True for any existing file, so this degrades to an
    existence check there rather than validating anything extra.
    """
    return candidate.is_file() and os.access(candidate, os.X_OK)


def _bin_is_usable(path: Path) -> bool:
    """Return True if *path* is a readable launcher whose interpreter still exists.

    Readability alone is not usability. A launcher is a thin wrapper around an
    interpreter living elsewhere, so it OUTLIVES the thing it needs: a reaped work
    directory, a removed ``.venv``, or a pruned bundle leaves an executable file
    that fails at run time with "virtual environment not found". Accepting one
    makes ``ensure_kirocrew_on_path`` publish a machine-wide ``kirocrew`` that is
    broken from the moment it is written — and that function runs on EVERY gateway
    start, so it would keep re-publishing it.

    Two launcher shapes, judged differently because only one of them states the
    answer: a pip console script names its interpreter in the shebang, which stays
    correct for every install layout (a venv, ``python3.12 -m pip install`` into
    ``~/.local/bin``, a distro package), so it is read directly. A shell wrapper's
    shebang names the SHELL, so its interpreter is resolved relative to the
    wrapper instead.

    Nothing is executed, and a launcher naming no interpreter of ours is accepted,
    so this only ever narrows the set by provably-dead targets.
    """
    try:
        with open(path, "rb") as stream:
            head = stream.read(4096)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        # Compiled launcher (pip's Windows .exe, a frozen binary) or a Windows
        # batch shim (`bin\kirocrew.cmd` starts with `@`). The shim DOES name
        # its interpreter (`"%~dp0..\python.exe"`), but we choose not to parse
        # the batch body here; the consumer that spawns it
        # (`_kirocrew_mcp_invocation`) resolves and validates that sibling
        # interpreter itself, mirroring website/electron/main.js.
        return True
    text = head.decode("utf-8", errors="replace")

    shebang = text.splitlines()[0][2:].strip()
    interpreter = shebang.split()[0] if shebang else ""
    # `#!/usr/bin/env python3` names the FINDER, not the interpreter, so it says
    # nothing about a specific path; only an absolute python path is decisive.
    # `is_absolute()` rather than a leading "/" so a native Windows path
    # (`C:\...\python.exe`) is recognised there too -- pip ships a compiled
    # `.exe` launcher on Windows, which returns above, but a shebang script that
    # does reach here must not be judged by a POSIX-only shape.
    candidate = Path(interpreter)
    if candidate.is_absolute() and candidate.name.startswith("python"):
        return _interpreter_runnable(candidate)

    bin_dir = path.parent
    # `<venv>/bin/kirocrew` (already inside the venv) vs `<root>/bin/kirocrew`
    # (the repo launcher and the packaged bundle's wrapper, beside the venv).
    venv_root = bin_dir.parent
    if venv_root.name != ".venv":
        venv_root = venv_root / ".venv"
    checks: list[tuple[str, tuple[Path, ...]]] = [
        (".venv", (venv_root / "bin" / "python", venv_root / "Scripts" / "python.exe")),
        # Packaged PBS bundle: `<root>/bin/python3.12`, beside the launcher. The
        # marker identifies that LAYOUT, not merely a version, and it stays a
        # literal on purpose: widening it to `python3\.\d+` also matches shebangs
        # that name a version while keeping their interpreter somewhere else
        # entirely -- Apollo's `#!/apollo/sbin/envroot $ENVROOT/python3.10/bin/
        # python3.10` is one, and it then gets held to a sibling `python3.10`
        # that was never supposed to exist, so a working launcher is judged dead.
        # Broadening the marker broadens the OBLIGATION it imposes, which is the
        # opposite of what a liveness check should do when it cannot identify the
        # shape. The same literal appears in `packaging/build-desktop.sh`, which
        # builds this layout; unifying the two is its own change.
        ("python3.12", (bin_dir / "python3.12", venv_root / "bin" / "python3.12")),
    ]
    for marker, candidates in checks:
        if marker not in text:
            continue
        if not any(_interpreter_runnable(c) for c in candidates):
            return False
    return True


def _launcher_works(path: Path) -> bool:
    """Return True if *path* is a launcher that would actually run today.

    Combines the two halves of the question asked of any launcher we did not
    write ourselves: the file is present and executable, AND the interpreter it
    delegates to still exists (:func:`_bin_is_usable`). Used to decide whether an
    ``~/.local/bin/kirocrew`` that points somewhere ELSE is a working install's
    launcher — which must be left alone — or a dead one we should replace.

    Deliberately not folded into ``ensure_kirocrew_on_path``'s gate on its OWN
    resolved target: that gate additionally requires an absolute path, and its
    interpreter check already happened inside :func:`_resolve_kirocrew_bin`.
    """
    return path.is_file() and os.access(path, os.X_OK) and _bin_is_usable(path)


def _kirocrew_bin_subpath(root: Path) -> Path:
    """The console-script path under an install ``root`` for this OS.

    A venv exposes its entry points under ``bin/kirocrew`` on POSIX but
    ``Scripts/kirocrew.exe`` on Windows — pip generates a ``.exe`` launcher
    there from the ``console_scripts`` entry point. Resolving the POSIX layout
    on Windows finds nothing, which silently drops the built-in
    ``kirocrew-cron`` / ``kirocrew-core`` MCP servers (``command not found:
    .../bin/kirocrew``). Branch on the platform so both layouts resolve.

    On Windows a relocatable ``bin\\kirocrew.cmd`` shim is preferred over the
    pip-generated ``Scripts\\kirocrew.exe`` when it exists. The desktop bundle
    (``packaging/build-desktop.sh``) ships BOTH: pip drops a console-script
    ``.exe`` in ``Scripts\\``, but distlib embeds the ABSOLUTE interpreter path
    of the machine that built it, so inside a shipped bundle that ``.exe``
    points at a build-agent path that does not exist on the user's machine.
    The ``.cmd`` shim resolves the interpreter via ``%~dp0`` and is the only
    relocatable launcher of the two. The Electron resolver
    (``website/electron/find-bin.js``) ranks them the same way — keep the two
    in sync. Plain pip installs ship no ``bin\\kirocrew.cmd``, so they keep
    resolving ``Scripts\\kirocrew.exe`` via the fallback.
    """
    if platform_compat.IS_WINDOWS:
        cmd_shim = root / "bin" / "kirocrew.cmd"
        if cmd_shim.is_file():
            return cmd_shim
        return root / "Scripts" / "kirocrew.exe"
    return root / "bin" / "kirocrew"


def _resolve_kirocrew_bin() -> str:
    """Resolve the absolute path of the ``kirocrew`` executable.

    Resolution order (first existing + executable wins):

    1. A sibling ``.venv`` entrypoint, for a source-tree install (an editable
       install next to its own venv, e.g. ``project/src/kiro_crew`` plus
       ``project/.venv``). Bounded by the first ``pyvenv.cfg`` walking up, so a
       pip-into-venv install falls through to step 2 instead.
    2. Same install as the current process: walk up from ``kiro_crew.__file__``
       looking for a sibling console script (see
       :func:`_kirocrew_bin_subpath` for the per-OS layout). Covers venv-based
       installs, pip installs, source-tree dev trees, and the desktop app —
       whose bundled interpreter is a python-build-standalone tree exposing a
       launcher at its root, reached by this walk from the bundle's
       ``site-packages``.
    3. The running interpreter's own install prefix (``sys.exec_prefix``). Same
       intent as step 2 — the install this process belongs to — for layouts
       where the console script is not an ancestor-sibling of the package and
       the parent walk therefore cannot reach it.
    4. ``shutil.which('kirocrew')`` — respects PATH order.
    5. Bare ``"kirocrew"`` — last resort, may fail but surfaces the problem
       instead of caching a known-bad absolute path.

    Every candidate is validated with ``is_file()`` and ``os.access(X_OK)``
    before being returned, so stale paths from previous installs are skipped.
    """
    global _KIROCREW_BIN
    if _KIROCREW_BIN:
        return _KIROCREW_BIN

    def _usable(p: str | Path) -> bool:
        sp = str(p)
        # The empty-string guard is this resolver's own concern: its candidates
        # come from config and env, where "" means "unset". Everything after it is
        # the shared predicate, so the two cannot drift apart.
        return bool(sp) and _launcher_works(Path(sp))

    # 1. Prefer the venv entrypoint for source-tree installs (editable
    #    install with a sibling .venv directory, e.g. project/src/kiro_crew
    #    + project/.venv/bin/kirocrew).
    #    NOTE: For pip-into-venv installs where pkg_dir is inside .venv/,
    #    the pyvenv.cfg guard below breaks early and step 2 handles it.
    try:
        # Circular import: kiro_crew.agent is loaded during kiro_crew
        # package initialization, so importing kiro_crew at module level
        # would create a circular dependency. Deferring here resolves
        # after the package is fully loaded.
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            venv_candidate = _kirocrew_bin_subpath(parent / ".venv")
            if _usable(venv_candidate):
                _KIROCREW_BIN = str(venv_candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break
    except Exception:
        logger.debug("kirocrew venv bin check failed", exc_info=True)

    # 2. Walk up from the running package to find the console script
    try:
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            candidate = _kirocrew_bin_subpath(parent)
            if _usable(candidate):
                _KIROCREW_BIN = str(candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break  # reached venv root without finding the binary
    except Exception:
        logger.debug("kirocrew bin walk failed", exc_info=True)

    # 3. The running interpreter's own install prefix.
    #
    #    Step 2 asks "which install does this process belong to?" but answers it
    #    by walking the package's PARENTS, so it only sees a console script that
    #    sits above ``site-packages``. Layouts that put the two in sibling trees
    #    are invisible to it — a prefix-style runtime can have the package at
    #    ``<root>/lib/python3.12/site-packages/kiro_crew`` and the script at
    #    ``<root>/python3.12/bin/kirocrew``, which is not an ancestor of the
    #    package dir at all. The walk then finds nothing and resolution falls
    #    through to PATH, where an unrelated ``kirocrew`` from some earlier
    #    install wins and gets written into ``kirocrew.json`` as the command for
    #    the built-in MCP servers.
    #
    #    ``sys.exec_prefix`` IS the install root for the interpreter actually
    #    running — the venv root inside a venv, the runtime root otherwise — so
    #    handing it to :func:`_kirocrew_bin_subpath` yields the same directory
    #    ``sysconfig.get_path("scripts")`` would, and keeps the per-OS naming
    #    and the Windows ``.cmd``-over-``.exe`` ranking in one place. Derived
    #    from ``sys`` (already imported, and immune to import shadowing) rather
    #    than by importing ``sysconfig`` here: this module is imported during
    #    ``kiro_crew`` package init, which can run with a user project on
    #    ``sys.path``, and a project-local ``sysconfig.py`` would then execute.
    try:
        candidate = _kirocrew_bin_subpath(Path(sys.exec_prefix))
        if _usable(candidate):
            _KIROCREW_BIN = str(candidate)
            return _KIROCREW_BIN
    except Exception:
        logger.debug("kirocrew exec-prefix bin check failed", exc_info=True)

    # 4. PATH lookup (also validated)
    found = shutil.which("kirocrew")
    if found and _usable(found):
        _KIROCREW_BIN = found
        return _KIROCREW_BIN

    # 5. Last resort — don't cache, so a future call can retry
    logger.warning(
        "Could not resolve kirocrew binary to an existing file; "
        "falling back to bare 'kirocrew' (MCP probes may fail)"
    )
    return "kirocrew"


def _managed_mcp_env() -> dict[str, str]:
    """Env every managed KiroCrew MCP server is launched with.

    Pins ``KIROCREW_HOME`` when the gateway is running under an override, because
    a child process does NOT inherit it: the spec's ``env`` is the only channel.
    Without this the gateway and its own stdio shims read DIFFERENT data homes,
    which is silent and self-contradictory rather than merely wrong —
    ``computer_use.json`` is written to the override home by Settings while
    ``mcp_computer`` reads the DEFAULT home, so the panel shows the feature ON
    while the shim publishes an empty ``tools/list`` and the agent truthfully
    reports it has no computer-use tools. The same split would desynchronise the
    cron store and the lessons file.

    Resolved through ``_valid_override_home`` rather than reading the env var
    directly, so an override the loader REFUSES (a filesystem root, ``/usr``) is
    not propagated to children that would then disagree with the gateway in the
    other direction.

    Returns ``{}`` on a default install, which keeps the emitted spec
    byte-for-byte what it is today (``_prune_empty`` drops an empty ``env``).
    """
    override = _valid_override_home()
    return {"KIROCREW_HOME": str(override)} if override else {}


# Declaration discriminator kiro-cli reads for enterprise MCP governance. It is
# NOT a transport: a `registry` entry is a POINTER into the admin's catalog,
# carrying only env/headers/timeout overrides, and its command/url are ignored.
_MCP_REGISTRY_TYPE = "registry"


def _mcp_registry_mode() -> bool:
    """True when the operator has declared this install registry-governed.

    An enterprise Kiro profile with an MCP Registry URL puts the client in
    `registry` access mode, where it resolves each `mcpServers` entry that
    carries ``"type": "registry"`` against the admin's catalog BY THE MAP KEY
    and silently drops every entry that does not. Without the marker the
    managed servers are filtered out before launch and the features they carry
    (`spawn_run`, `cron_add`, `learn_add`, ...) disappear with no local error.

    The mode cannot be auto-detected: the client fetches the toggle and the
    registry URL from GetProfile at startup and persists neither, so nothing on
    disk distinguishes a governed account from an ungoverned one. It is an
    explicit operator declaration, defaulting to false because the filter is
    symmetric — outside registry mode the marked entries are the dropped ones,
    so stamping unconditionally would break every personal install.

    Read through the EFFECTIVE config rather than ``config.json`` alone, because
    ``config.local.json`` deep-merges over it and is where ``kirocrew config set
    --local`` writes. Reading only the base file would ignore an overlay that
    declares the mode, emit no marker, and reproduce the silent drop this whole
    change exists to prevent.
    """
    try:
        # Function-local like the model resolver a few frames up: importing the
        # loader at module scope closes an import cycle through the config plane.
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().agent.mcp_registry_mode is True
    except Exception:
        # A config that cannot be loaded is not a governed declaration. Fall back
        # to the base file so a partially broken overlay still cannot flip the
        # marker on by accident.
        logger.debug("effective config unavailable for registry mode", exc_info=True)
        cfg = _load_json(_mc_config_path()) or {}
        agent_cfg = cfg.get("agent")
        if not isinstance(agent_cfg, dict):
            return False
        return agent_cfg.get("mcp_registry_mode") is True


def _kirocrew_mcp_invocation(subcommand: str) -> tuple[str, list[str]]:
    """Resolve a CWD- and shebang-independent invocation for a built-in
    MCP server (``kirocrew-cron`` / ``kirocrew-core``).

    Prefers a standalone ``kirocrew`` binary when one resolves. Falls back
    to ``<interpreter> -m kiro_crew <subcommand>`` when
    :func:`_resolve_kirocrew_bin` cannot find a usable standalone binary --
    e.g. an install whose launcher is not on the service PATH (the gateway
    running as a systemd user service is the common case): there
    ``_resolve_kirocrew_bin`` returns the bare ``"kirocrew"`` sentinel, the
    command fails to validate, and the server gets dropped from
    ``kirocrew.json`` on every config refresh.

    ``sys.executable`` is the absolute path of the running interpreter, so it
    needs no PATH entry and ignores any broken launcher. ``python -m
    kiro_crew`` dispatches the same CLI as the ``kirocrew`` console script.

    A resolved ``bin\\kirocrew.cmd`` (the Windows bundle's relocatable shim,
    see :func:`_kirocrew_bin_subpath`) is unwrapped to the sibling
    interpreter — ``<root>\\python.exe -P -s -m kiro_crew <sub>`` — instead of
    being emitted verbatim. This mirrors ``website/electron/main.js``, which
    refuses to spawn the shim it resolved (Node's ``spawn()`` rejects
    ``.cmd``/``.bat`` without ``shell:true``, CVE-2024-27980 hardening) and
    substitutes exactly this invocation. Whether kiro-cli's spawner handles a
    batch file is its own implementation detail; emitting the interpreter
    directly removes the question — the shim exists for humans and find-bin
    identity, the process tree runs ``python.exe``. When the sibling
    interpreter is missing (corrupted bundle), fall back to
    ``sys.executable``, which inside the bundle IS that interpreter.
    """
    bin_path = _resolve_kirocrew_bin()
    if bin_path == "kirocrew":  # unresolved sentinel from _resolve_kirocrew_bin
        return sys.executable, ["-m", "kiro_crew", subcommand]
    if bin_path.endswith(".cmd"):
        interpreter = Path(bin_path).parent.parent / "python.exe"
        if _interpreter_runnable(interpreter):
            # ``-P`` (safe path, 3.11+) keeps the spawn CWD off ``sys.path``:
            # kiro-cli spawns managed servers with the user's project as CWD,
            # so with ``-m`` alone a cloned repo carrying a ``kiro_crew/``
            # package would shadow the real one and run unconfined. Safe to
            # pin here because this interpreter is always the bundle's own
            # python-build-standalone 3.12 (packaging/build-desktop.sh); the
            # generic ``sys.executable`` fallbacks below and above stay
            # ``-P``-free because the project still supports Python 3.10,
            # which lacks the flag.
            return str(interpreter), ["-P", "-s", "-m", "kiro_crew", subcommand]
        return sys.executable, ["-m", "kiro_crew", subcommand]
    return bin_path, [subcommand]


def _computer_use_spec_gate() -> bool:
    """Whether ``kirocrew-computer`` belongs in an EMITTED agent spec.

    The shim's own ``enable_state.is_enabled()`` checks (in ``_list_tools`` and
    again in the dispatcher) decide what a RUNNING backend may do; they cannot
    decide whether it runs at all, because they execute inside the process the
    spec already caused kiro-cli to spawn. So a disabled feature still cost a
    full backend process — ~109 MB, per chat process including every
    ``spawn_run`` subagent — and on a platform with no driver it cost that for a
    capability that could not work. This gate is the same decision moved to the
    only place that can act on it: spec emission.

    Two conditions, and the platform one ASKS THE BACKEND rather than naming an
    OS. The driver's own ``status().supported`` is the same seam the Settings panel
    reads, so a platform gaining a driver needs no edit here — which is exactly the
    bug this replaced: a hardcoded ``IS_MACOS`` kept the server out of the spec on
    Windows after the Windows driver shipped, so the tools were advertised in
    ``tools`` while no server was ever spawned and the model was told they did not
    exist.

    **Neither condition loads a native library**, which matters because this gate runs
    on the agent-config rebuild path: ``is_enabled()`` is one small JSON read and
    ``platform_could_be_supported()`` reads only ``platform_compat`` flags, where
    reaching a driver's ``status()`` imports the platform driver and five ``WinDLL``s
    (measured 31ms and 32 modules on Windows) to answer a question the platform flags
    already settle. The keystone is tested first: both must hold, both fail closed, and
    it is the cheaper of the two.

    That makes the support half OPTIMISTIC — it says a driver EXISTS for this OS, not
    that it works on this host. Correct here: this gate's job is to avoid PAYING for a
    backend process on a platform with no driver at all, and a driver that exists but
    will not load is caught by the shim's own in-process checks, which run inside the
    process that would otherwise have done the work.

    Both in-process checks stay as defence in depth. They still cover the case
    this gate structurally cannot — the keystone flipping OFF mid-session, after
    the spec was written and the backend spawned.

    Fails CLOSED, matching the keystone's own posture (``enable_state`` reads a
    missing / unreadable / malformed file as DISABLED): the open position of this
    gate hands out the operator's whole desktop, so an unreadable ceiling must
    never be read generously.
    """
    try:
        # Function-local: ``enable_state`` reaches ``config.loader`` at module
        # scope, and agent.py imports that loader function-locally everywhere
        # else for exactly that reason — a module-scope import here would close
        # an import cycle through the config plane.
        from kiro_crew.computer_use import backend as cu_backend
        from kiro_crew.computer_use import enable_state

        if not enable_state.is_enabled():
            return False
        # The NON-LOADING predicate, not ``status()``: see the docstring above.
        return cu_backend.platform_could_be_supported()
    except Exception:
        logger.debug(
            "computer-use support or keystone unreadable; omitting it from the agent spec",
            exc_info=True,
        )
        return False


def _mcp_spec_gate_open(name: str, spec: dict) -> bool:
    """Whether *spec*'s ``spec_gate`` permits emission RIGHT NOW (absent = open).

    The single place a gate is called. A gate that raises is reported CLOSED, for
    the same fail-closed reason the computer-use gate itself is: emitting the
    entry is what makes kiro-cli spawn the backend, and a keystone we could not
    read is not evidence that the capability is on.
    """
    gate = spec.get("spec_gate")
    if gate is None:
        return True
    try:
        return bool(gate())
    except Exception:
        logger.debug("spec gate for %s raised; treating as closed", name, exc_info=True)
        return False


def _mcp_server_emission_eligible(
    name: str, spec: object, *, gated_off: "frozenset[str] | None" = None
) -> bool:
    """Whether a FRESH spec build would EMIT this MCP server entry.

    THE single definition of "the rebuild re-adds this", and it has exactly two
    disqualifiers, both owned by the entry's own spec:

    * ``opt_in`` — an assignable set, never auto-emitted. ``build_agent_config``
      skips it outright and ``_refresh_dynamic_fields`` keeps an EXISTING grant
      current without ever re-introducing one, so nothing re-adds a grant the
      user removed.
    * a CLOSED ``spec_gate`` — both writers ``pop`` the entry while the gate is
      shut, so the rebuild actively withholds it rather than merely skipping it.

    Both spec writers consult this, and so does the dashboard PUT's merge-on-write
    host set (``handlers/agents.py::_app_or_host_owned``). That co-tenancy is the
    whole point of the helper rather than a convenience: the merge preserves an
    absent managed entry *because* a rebuild would re-add it, so if the two ever
    disagreed the merge would resurrect entries the rebuild withholds — an
    ``opt_in`` grant the user revoked through the only surface that can revoke it,
    or a gate-closed server whose backend the gate exists to keep unspawned.

    *gated_off* is a caller's ONE-PER-REBUILD gate snapshot
    (:func:`_gated_off_servers`); passing it keeps a rebuild's emit path and its
    withhold audit agreeing on one reading, which is why that snapshot exists.
    Omitted (the merge's case, which audits nothing), the gate is read live.

    A spec that is not a mapping at all is reported ELIGIBLE. Only the host can
    produce that shape — the managed map is a module constant and the extras come
    from an edition adapter — the name is host-owned either way, and this keeps
    the merge's pre-existing verdict for it instead of raising ``AttributeError``
    out of a commit unit contracted to leave its targets byte-identical.
    """
    if not isinstance(spec, dict):
        return True
    if spec.get("opt_in"):
        return False
    if gated_off is not None:
        return name not in gated_off
    return _mcp_spec_gate_open(name, spec)


def emission_eligible_mcp_servers() -> frozenset[str]:
    """Every MCP server name a fresh spec build would emit right now.

    Managed servers and the edition's extras under ONE predicate — extras get no
    exemption, so an extra that ever carries ``opt_in`` or a gate is withheld
    here for the same reason a managed one is. Today they carry neither, so this
    is every extra plus the always-emitted managed entries.

    Exported (no leading underscore) because ``handlers/agents.py``'s
    merge-on-write is a legitimate out-of-module consumer: it must preserve
    exactly the set a rebuild would re-add, and computing that itself is what let
    the two drift. Read live rather than cached — a keystone flip between two PUTs
    must change the answer.
    """
    return frozenset(
        name
        for name, spec in (*_MANAGED_MCP_SERVERS.items(), *_extra_mcp_servers().items())
        if _mcp_server_emission_eligible(name, spec)
    )


def _gated_off_servers() -> frozenset[str]:
    """Managed servers whose ``spec_gate`` is CLOSED right now.

    Evaluated ONCE per rebuild and threaded through the emit path and the withhold
    audit, rather than each re-reading the gate. The reads are cheap; agreeing is
    the point. A keystone flip landing between the two would produce a spec and an
    audit trail that contradict each other — the record claiming a server was
    withheld when it was emitted, or staying silent when it was withheld. That
    record is read during incident response, against the config it describes.

    A gate that raises is treated as closed, for the same fail-closed reason the
    computer-use gate itself is — see :func:`_mcp_spec_gate_open`, which is where
    that call now lives so the merge-on-write host set reads the gate the same way.
    """
    return frozenset(
        name for name, spec in _MANAGED_MCP_SERVERS.items() if not _mcp_spec_gate_open(name, spec)
    )


# ---------------------------------------------------------------------------
# Managed MCP servers — single source of truth.
#
# Every server here is dynamically injected into the agent config at install
# time (both fresh and existing configs).  Adding a new managed server =
# one entry here.
#
# An entry may carry a ``spec_gate`` callable: a predicate consulted at spec
# EMISSION time, so a capability that is off (or impossible on this platform)
# costs no backend process rather than merely no tools.  Absent = always
# emitted, which is what the two always-on servers want.
# ---------------------------------------------------------------------------
_MANAGED_MCP_SERVERS: dict[str, dict] = {
    "kirocrew-cron": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-cron")},
    "kirocrew-core": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-core")},
    # Computer use (native desktop GUI automation).  ``spec_gate`` keeps the
    # entry out of the emitted spec unless the platform HAS a supported driver
    # AND the keystone primary enable is on, so kiro-cli never spawns the
    # backend for a feature that is
    # off or unsupported (see _computer_use_spec_gate).  The shim's own empty
    # ``tools/list`` while disabled is retained as defence in depth.
    #
    # DELIBERATELY NO ``autoApprove`` KEY, and none may ever be added: kiro-cli
    # approves an autoApproved MCP tool locally and emits no permission request,
    # so ``hooks.on_tool_call`` — the PreToolUse gate carrying the always-on deny
    # floor, the sensitive-path check and the governance ceiling — is NEVER
    # reached for it. For a tool that can click in an already-authenticated
    # application that would be a complete gate bypass.
    "kirocrew-computer": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-computer"),
        "spec_gate": _computer_use_spec_gate,
    },
    # Dashboard control (sidebar folder tree + which sessions sit in it).
    # ``opt_in``: an ASSIGNABLE SET, not an always-on capability. The two loops
    # that write specs skip it, so the default agent's spec carries neither the
    # entry nor an ``@kirocrew-dashboard`` ref in ``tools`` — and kiro-cli loads a
    # server only when something references it, so a default session spends no
    # context on tools it never uses. An agent that should reorganize the
    # dashboard is granted the set in its own spec, and a refresh keeps that
    # grant's command current without ever re-granting it.
    #
    # No ``autoApprove`` key, for the same reason the computer server has none:
    # an autoApproved MCP tool is approved inside kiro-cli and never reaches
    # ``hooks.on_tool_call``, so the deny floor and governance ceiling would be
    # bypassed for tools that write to the user's session layout.
    "kirocrew-dashboard": {
        "invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-dashboard"),
        "opt_in": True,
    },
}


def _extra_mcp_servers() -> dict[str, dict]:
    """Edition-contributed MCP servers from the active PlatformContext.

    The Default adapter returns ``{}`` so the standalone spec is byte-for-byte
    what it is today; the Amazon companion contributes the internal MCP server
    (and other internal servers).  Entries are already in kiro-cli's ``mcpServers`` shape
    (``{"command", "args", optional "autoApprove", ...}``) — the consumer
    *merges* them into the ``mcpServers`` map rather than restructuring the
    spec, preserving the ``deny_unknown_fields`` invariant.
    """
    # Fail-closed via safe_context_call: a non-standalone host that cannot
    # compose its context re-raises PlatformCompositionError (never silently
    # degrades to the empty OSS server set); any other lookup failure -> none.
    # Annotate the target so safe_context_call's TypeVar binds from here, not
    # from the empty ``fallback={}`` literal (which would infer dict[Never, Never]
    # and clash with extra_mcp_servers()'s dict[str, dict] return).
    extra: dict[str, dict] = safe_context_call(
        lambda: current_context().mcp_tooling.extra_mcp_servers(),
        fallback={},
        log_message="extra_mcp_servers lookup failed; using none",
    )
    return dict(extra) if extra else {}


def _extra_mcp_scope_globals() -> list[Path]:
    """Provider-global MCP config files contributed by the edition (CPP seam).

    Mirrors ``mcp_discovery._extra_scope_sources`` and the ``/api/mcp/apply``
    uninstall path: the rebuild-time merge reads each seam scope's
    ``global_json`` so a companion's provider global (e.g. Claude Code's
    ``~/.claude.json`` → ``ccGlobal``) is merged into the agent config ONLY when
    that edition contributes it. The Default returns ``[]`` so OSS merges the
    Kiro global only — keeping rebuild symmetric with discovery + apply/uninstall
    (a server the dashboard can't see is never re-merged/resurrected). Fails
    closed to no extra scopes.
    """
    scopes: list = safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; rebuild using core scopes only",
    )
    return [s.global_json for s in scopes]


def ensure_kirocrew_on_path(
    bin_dir: Path | None = None, *, claim_existing: bool = False
) -> str | None:
    """Ensure a ``kirocrew`` launcher is reachable on the user's PATH.

    The source ``install.sh`` symlinks ``~/.local/bin/kirocrew`` → the venv
    entry point, but install paths that don't run it (notably the packaged
    Electron app) leave no ``kirocrew`` on PATH — breaking the ``kirocrew``
    terminal command. This mirrors that symlink step in Python so it runs from
    ``kirocrew setup``. Best-effort and idempotent:

    * No-op if ``kirocrew`` already resolves on PATH to the same binary.
    * No-op if no concrete binary can be resolved (nothing to point at).
    * No-op if a launcher for a DIFFERENT install is there and still works,
      unless ``claim_existing`` says the user asked for this one by name.
    * Otherwise (re)create ``<bin_dir>/kirocrew`` → the resolved binary.

    Args:
        bin_dir: Target directory for the shim. Defaults to ``~/.local/bin``.
        claim_existing: Take the name over from another install's working
            launcher. ``kirocrew setup`` passes True because the user named this
            install; gateway startup must NOT, since it runs unattended on every
            start and would make the last install to boot win.

    Returns:
        The shim path if one was created/updated, else ``None``.
    """
    # Windows has no ~/.local/bin symlink convention, and creating a symlink
    # there needs Developer Mode or elevation — a normal session raises
    # OSError [WinError 1314] mid-wizard. pip's Scripts\kirocrew.exe console
    # script is already the supported Windows launcher (docs/guides/windows-install.md),
    # so this POSIX install.sh mirror has nothing to do here. Return before any
    # filesystem attempt so `kirocrew setup` never prints a traceback for it.
    if platform_compat.IS_WINDOWS:
        return None

    target = _resolve_kirocrew_bin()
    # Nothing concrete to point at — bare "kirocrew" or a non-executable file.
    if not (os.path.isabs(target) and os.path.isfile(target) and os.access(target, os.X_OK)):
        return None

    # Never aim the user's machine-wide launcher at a linked git worktree. A
    # worktree is ephemeral by construction: `git worktree remove` deletes its
    # `.venv` along with the tree, and the shim is then a dangling symlink, so
    # `kirocrew` stops working EVERYWHERE — not just in the tree that went away.
    # Any process running out of a worktree's venv (a pod gateway, a dev run, a
    # `kirocrew setup` invoked from that tree) resolves its own venv entrypoint
    # here, so without this guard routine worktree work silently hijacks the
    # global command. `instances/token_mint.py` documents the same hazard from
    # the consuming side. Declining leaves whatever already worked in place.
    #
    # `.resolve()` first: the ancestry walk is LEXICAL, and the resolved target
    # is frequently itself a symlink into a worktree (a PATH entry, or the very
    # shim we are about to rewrite). Walking the symlink's own parents would find
    # no `.git` marker and wave the worktree through — reopening this hole.
    if _in_linked_git_worktree(Path(target).resolve()):
        logger.info(
            "Not installing a kirocrew launcher: %s is inside a linked git worktree, "
            "which is ephemeral (removing the worktree would break `kirocrew` "
            "machine-wide). Install from your primary clone, or link it yourself: "
            "ln -sfn <clone>/.venv/bin/kirocrew ~/.local/bin/kirocrew",
            target,
        )
        return None

    # Same hazard from the other direction: an AppImage's runtime mount and a
    # scratch tree under the temp dir are both reaped out from under a launcher
    # that points into them — and this function runs on EVERY gateway start, so
    # it would re-create that dangling link every time. Declining leaves
    # whatever already worked in place; a package install (fixed path under
    # /opt) or a venv install is the shape that can carry a durable launcher.
    if _in_ephemeral_tree(Path(target).resolve()):
        logger.info(
            "Not installing a kirocrew launcher: %s is inside an ephemeral tree (an "
            "AppImage runtime mount, or the system temp directory), which is reaped "
            "out from under the link. Install the deb/rpm package for a durable "
            "`kirocrew` on PATH, or link a persistent install yourself.",
            target,
        )
        return None

    # Already reachable on PATH as the same binary? Then there's nothing to do.
    existing = shutil.which("kirocrew")
    if existing and os.path.realpath(existing) == os.path.realpath(target):
        return None

    # Ownership, checked on PATH before the target path: a working `kirocrew`
    # ANYWHERE on PATH already belongs to some install — a pipx bin dir, a distro
    # package, /usr/local/bin — and writing <bin_dir>/kirocrew would shadow it or
    # be shadowed by it depending on PATH order, which is not a decision an
    # unattended start gets to make. The per-path check further down is still
    # needed and is not redundant with this one: it catches a working launcher
    # sitting AT <bin_dir>/kirocrew while <bin_dir> is not on PATH at all.
    if existing and not claim_existing:
        existing_on_path = Path(os.path.realpath(existing))
        if _launcher_works(existing_on_path):
            logger.info(
                "Leaving `kirocrew` on PATH alone: %s -> %s still works and belongs "
                "to another install. Run `kirocrew setup` from the install you want "
                "on PATH to switch it deliberately.",
                existing,
                existing_on_path,
            )
            return None

    bin_dir = bin_dir or (Path.home() / ".local" / "bin")
    link = bin_dir / "kirocrew"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            existing_target = Path(os.path.realpath(link))
            if os.path.realpath(link) == os.path.realpath(target):
                return None
            # A launcher that still WORKS belongs to another install — typically
            # the cli.sh wheel under ~/.kiro/crew-venv — and taking the name from
            # it is not a repair. This runs on EVERY gateway start, so whichever
            # install booted last would win, and the losing installer's upgrades
            # would then land on a path nothing points at: `kirocrew` keeps
            # working, silently at the wrong version, which is worse than a
            # visible break. The documented Linux pairing (cli.sh for the CLI,
            # deb/rpm for the desktop shell) puts both on one machine by design,
            # so this is the ordinary configuration rather than a corner case.
            #
            # An explicit `kirocrew setup` DOES claim the name: the user named
            # this install. A dangling or otherwise dead launcher is replaced on
            # either path — that vacuum is what this function exists to fill.
            if not claim_existing and _launcher_works(existing_target):
                logger.info(
                    "Leaving the existing kirocrew launcher alone: %s -> %s still "
                    "works and belongs to another install. Run `kirocrew setup` "
                    "from the install you want on PATH to switch it deliberately.",
                    link,
                    existing_target,
                )
                return None
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:
        # A best-effort PATH convenience must never dump a traceback into the
        # interactive setup wizard (which runs without logging.basicConfig, so
        # exc_info would hit Python's lastResort handler and print the stack).
        logger.warning("Could not create kirocrew shim at %s: %s", link, exc)
        return None
    logger.info("Linked kirocrew shim: %s -> %s", link, target)
    return str(link)


# One-time migrations performed automatically on gateway first-run (so the
# desktop app, which never runs `kirocrew setup`, still gets them). Lazy
# accessors (same import-side-effect reason as _user_dir above).
def _migrations_dir() -> Path:
    return _user_dir() / ".migrations"


def _stale_mcp_purge_marker() -> Path:
    return _migrations_dir() / "stale_managed_mcp_purged"


def run_first_run_setup() -> None:
    """Deliver the install-time steps the desktop app needs without a terminal.

    The Electron app only runs ``kirocrew gateway`` — never ``kirocrew
    setup`` — yet several concerns aren't covered by the gateway's agent-config
    rebuild. This is invoked from gateway startup to close that gap:

    * **PATH shim** — ``ensure_kirocrew_on_path()`` is idempotent and only
      writes ``~/.local/bin/kirocrew``, so it runs on every start. It is called
      WITHOUT ``claim_existing`` for exactly that reason: running unattended on
      every start, it must fill an empty or broken slot only, never take the
      command away from another install that still works.
    * **Default-on builtin backfill** — ``defaultEnabled`` is applied only on an
      app's FIRST registration, so a builtin promoted to default-on later never
      reaches installs that already registered it. Runs ONCE, guarded by its own
      marker file, because re-running it would override a user's own disable.
    * **Stale predecessor MCP purge** — ``clean_stale_managed_mcp()`` mutates
      the user's *global* ``~/.kiro/settings/mcp.json``, so it runs ONCE,
      guarded by a marker file, to honor the "KiroCrew owns only the agent
      file" boundary (no global rewrite on subsequent starts).

    Best-effort: never raises — any failure is logged and startup continues.
    """
    # 1. PATH shim — safe and idempotent on every start.
    try:
        shim = ensure_kirocrew_on_path()
        if shim:
            logger.info("First-run: linked kirocrew shim at %s", shim)
    except Exception:
        logger.warning("First-run: shim install failed", exc_info=True)

    # 2. Admission-policy seed — one-time, self-guarded by its OWN marker.  Run
    #    BEFORE the stale-MCP early return below so an EXISTING install (which
    #    already has the stale-MCP marker) still gets seeded on its next start;
    #    otherwise those installs would have no policy file and newly fail closed.
    try:
        from kiro_crew.platform.admission import seed_default_policy  # noqa: PLC0415

        if seed_default_policy():
            logger.info("First-run: seeded default admission policy")
    except Exception:
        logger.warning("First-run: admission policy seed failed", exc_info=True)

    # 3. Default-on builtin backfill — one-shot per app, self-recorded on the
    #    app's own installed.json (no marker file: the flag and the state it
    #    guards must land in one atomic write). Placed BEFORE the stale-MCP early
    #    return for the same reason step 2 is, and here the reason is the whole
    #    point: an EXISTING install already holds the stale-MCP marker, and an
    #    existing install is the ONLY kind this step has anything to do (a fresh
    #    one registers these apps enabled and already flagged).
    try:
        from kiro_crew.apps.manager import (  # noqa: PLC0415
            backfill_default_on_builtins,
        )

        flipped = backfill_default_on_builtins()
        if flipped:
            logger.info("First-run: enabled default-on builtin(s): %s", flipped)
    except Exception:
        logger.warning("First-run: default-on builtin backfill failed", exc_info=True)

    # 4. Stale managed-MCP purge — one-time, marker-guarded.
    stale_marker = _stale_mcp_purge_marker()
    if stale_marker.exists():
        return
    try:
        from kiro_crew.mcp_cleanup import clean_stale_managed_mcp  # noqa: PLC0415

        removed = clean_stale_managed_mcp()
        if removed:
            logger.info("First-run: purged stale managed MCP entries: %s", removed)
        # Mark done even when nothing was removed, so the global mcp.json is
        # never re-read/rewritten on later starts.
        _migrations_dir().mkdir(parents=True, exist_ok=True)
        stale_marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    except Exception:
        logger.warning("First-run: stale MCP purge failed", exc_info=True)


def _prompt_path(mode: str = "") -> Path:
    """Return user prompt if it exists, otherwise shipped prompt.

    When mode="orchestrator", uses the orchestrator prompt.
    The conductor_skill config is independent — it controls agent routing, not the prompt.
    """
    if mode == "orchestrator":
        user_orch = _user_dir() / "prompt-orchestrator.md"
        if user_orch.is_file():
            return user_orch
        proj = _project_dir()
        if proj:
            candidate = proj / "agents" / "prompt-orchestrator.md"
            if candidate.is_file():
                return candidate
        bundled_orch = _BUNDLED_CFG_DIR / "prompt-orchestrator.md"
        if bundled_orch.is_file():
            return bundled_orch

    user_prompt = _user_prompt_path()
    if user_prompt.is_file():
        return user_prompt
    return _shipped_prompt()


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning ``{}`` on any error or non-dict root.

    ``~/.claude.json`` in particular is user-owned and could theoretically
    contain a top-level array after a hand-edit.  Normalizing to an empty
    dict here means every caller can safely do ``_load_json(p).get(key)``
    without an ``isinstance`` check at each call site.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Ignoring invalid %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: top-level JSON is not an object", path)
        return {}
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge *override* into *base* (one level deep for dicts)."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


def _all_skill_paths() -> list[str]:
    """Discover all skill directories (AIM, project, user).

    Returns directories containing SKILL.md files from:
    - ``~/.aim/skills`` and ``~/.aim/packages/*/skills`` (AIM-installed)
    - ``KIROCREW_PROJECT_DIR/skills`` (project-level)
    - ``~/.kiro/crew/skills`` (user-created)
    """
    paths: set[str] = set()
    # AIM skills — only known locations, not broad rglob.
    # TODO(aim-governance follow-up): this hardcoded ``~/.aim`` scan should
    # route through the ``McpToolingProvider.extra_skills()`` CPP seam (as the
    # dashboard skills catalog already does) so the agent-config rebuild and the
    # dashboard read the SAME source. Deferred to its own PR because of the
    # security-sensitive symlink-resolution + sensitive-path gating below.
    # OSS-inert today (no ``~/.aim`` tree on a vanilla install).
    aim_dir = Path.home() / ".aim"
    if aim_dir.is_dir():
        aim_skills = aim_dir / "skills"
        if aim_skills.is_dir():
            paths.add(str(aim_skills))
            # Resolve symlinks in local/ so skill loaders whose glob skips
            # symlinks can still find them: resolve each symlink target and
            # add its parent dir (only if named "skills").
            local_dir = aim_skills / "local"
            if local_dir.is_dir():
                for entry in local_dir.iterdir():
                    if entry.is_symlink():
                        try:
                            target = entry.resolve(strict=True)
                            parent = target.parent
                            if (
                                target.is_dir()
                                and parent.name == "skills"
                                and not is_sensitive_path(str(parent))
                            ):
                                paths.add(str(parent))
                            elif target.is_dir() and is_sensitive_path(str(parent)):
                                logger.debug(
                                    "Skipping sensitive path: %s",
                                    parent,
                                )
                                try:
                                    sel().log_api_access(
                                        caller="system",
                                        operation="skill_path_rejected",
                                        outcome="denied",
                                        source="agent",
                                        resources=str(parent),
                                        error="sensitive_path",
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to emit SEL audit event for sensitive path rejection: %s",
                                        parent,
                                        exc_info=True,
                                    )
                            elif target.is_dir() and parent.name != "skills":
                                # `--local` skill installs always target a
                                # skills/ directory; non-standard layouts are
                                # intentionally skipped for consistency.
                                logger.debug(
                                    "Skipping symlink %s: parent %r is not 'skills'",
                                    entry.name,
                                    parent.name,
                                )
                        except OSError as exc:
                            logger.debug("Skipping unresolvable symlink %s: %s", entry, exc)
        aim_pkgs = aim_dir / "packages"
        if aim_pkgs.is_dir():
            for pkg in aim_pkgs.iterdir():
                if not pkg.is_dir() or pkg.name.startswith("."):
                    continue
                sd = pkg / "skills"
                if sd.is_dir():
                    paths.add(str(sd))
                # Nested variant: ~/.aim/packages/Pkg-1.0/eventId-XXX/skills/
                # Only load from currentEventId to avoid duplicates across snapshots.
                else:
                    manifest = pkg / ".aim" / ".version-manifest.json"
                    current_event = ""
                    if manifest.is_file():
                        try:
                            current_event = json.loads(manifest.read_text(encoding="utf-8")).get(
                                "currentEventId", ""
                            )
                        except (json.JSONDecodeError, OSError):
                            pass
                    for sub in pkg.iterdir():
                        if not sub.is_dir() or sub.name.startswith("."):
                            continue
                        if current_event and sub.name != f"eventId-{current_event}":
                            continue
                        ssd = sub / "skills"
                        if ssd.is_dir():
                            paths.add(str(ssd))
    # Project-level skills (legacy ``<project>/skills/``)
    proj = _project_dir()
    if proj:
        sd = proj / "skills"
        if sd.is_dir():
            paths.add(str(sd))
        # Open-standard workspace location: ``<project>/.kiro/skills/`` —
        # what kiro-cli's native ``skill://`` loader scans.  Adding it here
        # so SkillsLoader sees the same set as kiro-cli does.
        kiro_proj = proj / ".kiro" / "skills"
        if kiro_proj.is_dir() and not is_sensitive_path(str(kiro_proj)):
            paths.add(str(kiro_proj))
    # User-created skills (KiroCrew convention)
    user_skills = config_dir() / "skills"
    if user_skills.is_dir():
        paths.add(str(user_skills))
    # Open-standard global location: ``~/.kiro/skills/`` — canonical home for
    # ``cp -r my-skill ~/.kiro/skills/`` installs and AIM-published skills
    # that follow the spec.  See docs/reference/kiro-cli/skills.md.
    kiro_user = Path.home() / ".kiro" / "skills"
    if kiro_user.is_dir() and not is_sensitive_path(str(kiro_user)):
        paths.add(str(kiro_user))
    return sorted(paths)


# Keep old name as alias for backward compat
_aim_skill_paths = _all_skill_paths


# Allowlist for hook-command paths (config.json is LLM-writable, so this guards
# against indirect command injection). The intent is to reject shell
# metacharacters (; | & $ ` spaces quotes ( ) etc.) — the path is later exec'd as
# an argv element, never through a shell. On Windows an absolute path is
# `D:\Users\...`, so backslash and the drive-letter colon MUST be allowed there or
# EVERY Windows hook path is rejected (autoimport silently loads nothing). `\` and
# `:` are not shell-injection vectors for an argv path, and the is_sensitive_path
# + absolute-path + resolve() checks below still apply. POSIX keeps the original,
# tighter allowlist (no backslash/colon).
if platform_compat.IS_WINDOWS:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-\\:]+$")
else:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-]+$")
_SAFE_MATCHER_RE = re.compile(r"^[a-zA-Z0-9_.*\-]+$")
_MAX_MATCHER_LEN = 200


def _validate_hook_command(command: str, event: str) -> str | None:
    """Validate a user-supplied hook command path.

    Returns the resolved absolute path if safe, or None on failure.
    Since config.json is LLM-writable, this guards against indirect
    command injection.  Uses an allowlist regex for path characters.
    """
    if not _SAFE_PATH_RE.match(command):
        logger.warning("kiro_hooks[%s]: command contains disallowed characters: %r", event, command)
        return None
    if not os.path.isabs(command):
        logger.warning("kiro_hooks[%s]: command must be absolute path, got %r", event, command)
        return None
    resolved = str(Path(command).resolve())
    if not _SAFE_PATH_RE.match(resolved):
        logger.warning(
            "kiro_hooks[%s]: resolved path contains disallowed characters: %r", event, resolved
        )
        return None
    if is_sensitive_path(resolved):
        logger.warning(
            "kiro_hooks[%s]: command points to sensitive path %r, skipping", event, command
        )
        return None
    if not os.path.isfile(resolved):
        logger.warning("kiro_hooks[%s]: command not found: %s", event, command)
        return None
    return resolved


def _sel_hook_rejected(event: str, command: str, reason: str) -> None:
    """Emit a SEL audit event when a user hook entry is rejected."""
    try:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="config_hooks_merge",
                caller_identity="agent_install",
                agent="kirocrew",
                source="cli",
                operation="kiro_hooks_rejected",
                outcome="rejected",
                # redact-then-truncate on the interpolated value, through the
                # same context-aware shim as the outer call: slicing ``command``
                # raw could cut a credential at the boundary, and slicing after
                # baseline-only redaction would still cut a companion-only token
                # before the companion regexes see it. Context redaction runs
                # over the FULL command first, so no redactor ever sees a
                # boundary-cut fragment.
                resources=redact(f"event={event} command={redact(command)[:200]}"),
                error=reason,
            )
        )
    except Exception:
        logger.debug("SEL audit for rejected hook failed", exc_info=True)


# Kiro Crew-internal hook keys that must NOT appear in generated kiro-cli agent  # brand-ok
# specs (kiro-cli rejects unknown keys). Excluded when deriving _VALID_HOOK_EVENTS
# from bundled defaults below, so an internal key never round-trips as an event.
_INTERNAL_HOOK_KEYS = frozenset(
    {"auto_approve_tools", "auto_deny_tools", "auto_replies", "transforms"}
)

# Valid kiro-cli hook event names — the UNION of the hardcoded baseline (kiro-cli's
# known schema) and any event key present in bundled defaults. Used for generated
# specs and user-input validation; startup repair is ownership-scoped and removes
# only legacy keys Kiro Crew serialized. A new event added to defaults.json is
# automatically accepted without a matching allowlist update (#3362).
_VALID_HOOK_EVENTS = frozenset(
    {"preToolUse", "postToolUse", "userPromptSubmit", "agentSpawn", "stop"}
) | frozenset(
    k
    for k in (_load_json(_BUNDLED_CFG_DIR / "defaults.json") or {}).get("hooks", {})
    if k not in _INTERNAL_HOOK_KEYS
)

# Repair is subtractive against the runtime-only key Kiro Crew is known to have
# serialized into its generated specs. Unknown keys may belong to a newer
# kiro-cli schema or to the user.
_LEGACY_KIROCREW_HOOK_KEYS = frozenset({"auto_approve_tools"})


def _kiro_hooks_only(hooks: dict) -> dict:
    """Return only kiro-cli valid hook keys, stripping everything else.

    Used on the generation path (trusted bundled defaults) and for user-supplied
    config validation. On-disk startup repair is deliberately narrower because
    unknown keys may belong to a newer kiro-cli schema or to the user.
    """
    return {k: v for k, v in hooks.items() if k in _VALID_HOOK_EVENTS}


def _strip_legacy_denied_commands(config: dict) -> None:
    """Remove the retired ``deniedCommands`` / ``autoAllowReadonly`` injection.

    Denied commands are now enforced solely at KiroCrew's hooks.py PreToolUse
    gate; they are no longer injected into the kiro agent spec. But an install
    UPGRADED from a build that DID inject them keeps a stale
    ``toolsSettings.execute_bash/shell.deniedCommands`` (and ``autoAllowReadonly``)
    in its ``kirocrew.json``. kiro-cli would keep enforcing those stale rules
    before the hook gate — so a user who disables a built-in in Settings >
    Security would see it "succeed" yet stay blocked. Strip them on every refresh
    so upgraded installs behave exactly like a fresh one (hooks-gate-only).

    Any OTHER ``toolsSettings`` keys a user authored are preserved, and an
    emptied ``execute_bash``/``shell``/``toolsSettings`` object is removed so no
    empty scaffolding lingers.
    """
    ts = config.get("toolsSettings")
    if not isinstance(ts, dict):
        return
    for tool in ("execute_bash", "shell"):
        entry = ts.get(tool)
        if not isinstance(entry, dict):
            continue
        entry.pop("deniedCommands", None)
        entry.pop("autoAllowReadonly", None)
        if not entry:
            ts.pop(tool, None)
    if not ts:
        config.pop("toolsSettings", None)


_MAX_USER_HOOKS_PER_EVENT = 10
_MAX_TOTAL_USER_HOOKS = 20

# kiro-cli documents hook events in PascalCase (PreToolUse, PostToolUse, ...).
# The agent config stores them in camelCase (preToolUse, ...).  Script headers
# ("# event: PreToolUse") use kiro-cli's PascalCase convention; this map
# normalizes both casings back to the canonical camelCase form.
_HOOK_EVENT_CANONICAL = {
    "pretooluse": "preToolUse",
    "posttooluse": "postToolUse",
    "userpromptsubmit": "userPromptSubmit",
    "agentspawn": "agentSpawn",
    "stop": "stop",
}

# Default hooks directory matches kiro-cli's discovery path.
_DEFAULT_KIRO_HOOKS_DIR = Path.home() / ".kiro" / "hooks"

# Recognize hook event from filename suffix when no "# event:" header is set.
# Ordering matters: check more specific suffixes first.
_FILENAME_EVENT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-post.sh", "postToolUse"),
    ("-prompt.sh", "userPromptSubmit"),
    ("-spawn.sh", "agentSpawn"),
    ("-stop.sh", "stop"),
    ("-pre.sh", "preToolUse"),
)

# Header parsing — only inspect the first few lines so the scan stays O(K).
_HOOK_HEADER_SCAN_LINES = 5
_HOOK_HEADER_RE = re.compile(r"^\s*#\s*(event|matcher)\s*:\s*(\S.*?)\s*$", re.IGNORECASE)


def _parse_hook_script_headers(path: Path) -> tuple[str | None, str | None]:
    """Read the first few lines of a hook script and extract ``# event:`` / ``# matcher:`` directives.

    Returns ``(event_header, matcher_header)``.  Either may be ``None`` if not present.
    Values are returned unparsed; callers normalize/validate them.
    """
    event_header: str | None = None
    matcher_header: str | None = None
    try:
        # Read at most a handful of lines; hook scripts can be large, and we
        # only care about headers immediately after the shebang.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HOOK_HEADER_SCAN_LINES:
                    break
                m = _HOOK_HEADER_RE.match(line)
                if not m:
                    continue
                key = m.group(1).lower()
                val = m.group(2)
                if key == "event" and event_header is None:
                    event_header = val
                elif key == "matcher" and matcher_header is None:
                    matcher_header = val
    except OSError:
        logger.debug("kiro_hooks_autoimport: could not read %s for headers", path, exc_info=True)
    return event_header, matcher_header


def _infer_hook_event(script_path: Path, event_header: str | None) -> str | None:
    """Resolve a script's kiro hook event.

    Precedence:
      1. Explicit ``# event:`` header (normalized to camelCase).  Unknown values
         return ``None`` so the caller can WARN and skip.
      2. Filename suffix convention (``*-post.sh`` -> ``postToolUse`` etc.).
      3. Default: ``preToolUse``.
    """
    if event_header is not None:
        canonical = _HOOK_EVENT_CANONICAL.get(
            event_header.lower().replace("-", "").replace("_", "")
        )
        return canonical  # None if unknown -- caller decides what to do

    name = script_path.name.lower()
    for suffix, event in _FILENAME_EVENT_SUFFIXES:
        if name.endswith(suffix):
            return event
    return "preToolUse"


def _autoimport_kiro_hooks(hooks_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Scan ``hooks_dir`` for executable ``*.sh`` files and return a ``kiro_hooks``-shaped dict.

    Each discovered script becomes an entry under its resolved event (camelCase).
    Returns an empty dict if the directory is missing or contains no usable scripts.

    Security parity with the explicit config path:
      * Each script's resolved path goes through ``_validate_hook_command``.
      * ``# matcher:`` headers are validated against ``_SAFE_MATCHER_RE`` / ``_MAX_MATCHER_LEN``.
      * Non-executable files are skipped (INFO log).
      * Sensitive paths are skipped (via ``_validate_hook_command``).

    Final dedup, per-event cap, and total cap are enforced by ``_merge_kiro_hooks``
    which runs on the returned dict.  That keeps explicit config precedence correct:
    callers should invoke ``_merge_kiro_hooks`` with the already-merged ``hooks``
    (bundled + explicit) so auto-imported scripts that duplicate an explicit entry
    are deduped out rather than taking its slot.
    """
    result: dict[str, list[dict[str, str]]] = {}
    try:
        resolved_hooks_dir = hooks_dir.resolve()
    except (OSError, ValueError):
        # OSError: ENAMETOOLONG, ELOOP, EACCES on a path component.
        # ValueError: null bytes (``"\x00"``) reject at Path construction.
        # Emit SEL audit so an auditor sees a distinct "hooks_dir
        # unresolvable" signal — same symmetry principle as the
        # per-entry ``cannot resolve entry`` branch below.
        logger.debug("kiro_hooks_autoimport: cannot resolve %s, skipping", hooks_dir, exc_info=True)
        _sel_hook_rejected("autoimport", str(hooks_dir), "cannot resolve hooks_dir")
        return result
    try:
        entries = sorted(resolved_hooks_dir.iterdir())
    except FileNotFoundError:
        logger.debug("kiro_hooks_autoimport: directory %s does not exist, skipping", hooks_dir)
        return result
    except OSError:
        logger.warning("kiro_hooks_autoimport: cannot read %s, skipping", hooks_dir, exc_info=True)
        # Emit SEL audit so an auditor reconstructing agent-install
        # activity sees a distinct "hooks dir unreadable" signal rather
        # than only the merge-summary ``requested_autoimport=0`` (which
        # looks identical to the no-scripts-configured case).  Same
        # symmetry principle as the per-script rejection branches.
        _sel_hook_rejected("autoimport", str(hooks_dir), "cannot read hooks_dir")
        return result

    loaded = 0
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".sh":
            continue

        # Resolve once up-front and reuse the resolved path for all subsequent
        # checks (stat, validation).  This closes two issues:
        # * TOCTOU: repeated resolve() in _validate_hook_command could race
        #   with an attacker swapping the symlink target between calls.
        # * Symlink escape: entry.is_file() follows symlinks, so a symlink
        #   inside the hooks dir pointing at /tmp/attacker.sh would otherwise
        #   pass (not in _SENSITIVE_HOME_DIRS).  Require the resolved target
        #   to stay under the resolved hooks dir.
        try:
            resolved_entry = entry.resolve()
        except (OSError, ValueError):
            # OSError: typical filesystem failures.  ValueError: filename
            # from ``iterdir()`` carries a null byte or other malformed
            # character that ``Path.resolve()`` rejects.  Without this
            # catch, a maliciously-named file in hooks_dir crashes agent
            # bootstrap.
            logger.warning(
                "kiro_hooks_autoimport: cannot resolve %s, skipping", entry, exc_info=True
            )
            _sel_hook_rejected("autoimport", str(entry), "cannot resolve entry")
            continue
        if (
            resolved_entry != resolved_hooks_dir
            and resolved_hooks_dir not in resolved_entry.parents
        ):
            logger.warning(
                "kiro_hooks_autoimport: %s resolves outside %s (to %s), skipping",
                entry,
                resolved_hooks_dir,
                resolved_entry,
            )
            _sel_hook_rejected("autoimport", str(entry), "resolved path escapes hooks dir")
            continue

        try:
            resolved_entry.stat()  # surface a stat error (broken symlink, perms) as a skip
        except OSError:
            logger.warning("kiro_hooks_autoimport: cannot stat %s, skipping", entry)
            _sel_hook_rejected("autoimport", str(entry), "cannot stat entry")
            continue
        # Executable check is platform-aware: POSIX requires the execute bit (so
        # `chmod -x` disables a hook); Windows has no execute bit, so requiring
        # X_OK there would skip EVERY hook and silently break the whole autoimport
        # — instead a known script extension (.sh/.ps1/.cmd/...) is treated as
        # runnable. See platform_compat.is_executable_file.
        if not platform_compat.is_executable_file(resolved_entry):
            logger.info("kiro_hooks_autoimport: %s is not executable, skipping", entry)
            # Audit parity with the other rejection branches
            # (symlink-escape, cannot-resolve, cannot-stat,
            # failed-validation, unknown-event, invalid-matcher,
            # cannot-read-dir): the non-executable skip is also a
            # permission decision — it determines that a discovered
            # ``.sh`` file will NOT be loaded as a hook — so it must
            # emit a SEL audit event per AUTOSDE.yaml security-controls
            # rule.  Without this call, an auditor reconstructing
            # agent-install activity from SEL would not see scripts
            # that were skipped for lacking the execute bit.
            _sel_hook_rejected("autoimport", str(entry), "not executable")
            continue

        # Defense-in-depth: run the full validation (including
        # is_sensitive_path) BEFORE any file I/O on the script.  The
        # symlink-escape check above already rejects most attacks, but
        # running _validate_hook_command first keeps the "no reads on
        # sensitive paths" invariant intact even if the resolved-path
        # check is ever loosened.  The ``"autoimport"`` event label
        # below is a log tag only - _validate_hook_command uses ``event``
        # solely for log formatting, never as a policy key (e.g. it is
        # never matched against _VALID_HOOK_EVENTS).  The real event is
        # computed from headers after this call succeeds.
        validated_command = _validate_hook_command(str(resolved_entry), "autoimport")
        if validated_command is None:
            # _validate_hook_command already emitted a WARNING with the reason.
            _sel_hook_rejected("autoimport", str(entry), "failed validation")
            continue

        event_header, matcher_header = _parse_hook_script_headers(resolved_entry)
        event = _infer_hook_event(entry, event_header)
        if event is None:
            logger.warning(
                "kiro_hooks_autoimport: %s declares unknown event %r, skipping",
                entry,
                event_header,
            )
            # Match the other three rejection branches in this function
            # (symlink-escape, failed-validation, invalid-matcher): every
            # rejection must emit a SEL audit event per AUTOSDE.yaml's
            # security-controls rule.  Without this call, an auditor
            # reconstructing agent-install activity from SEL would not
            # see scripts that were dropped for declaring unknown event
            # names, which defeats the purpose of the audit trail.
            _sel_hook_rejected("autoimport", str(entry), "unknown event header")
            continue

        entry_dict: dict[str, str] = {"command": validated_command}
        if matcher_header is not None:
            if len(matcher_header) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher_header):
                # An invalid matcher is treated as a validation failure:
                # promoting a tool-scoped hook to unscoped (firing on every
                # tool call) would be a silent privilege expansion.
                logger.warning(
                    "kiro_hooks_autoimport: %s matcher %r is invalid, skipping script",
                    entry,
                    matcher_header,
                )
                _sel_hook_rejected("autoimport", str(entry), "invalid matcher")
                continue
            entry_dict["matcher"] = matcher_header

        result.setdefault(event, []).append(entry_dict)
        loaded += 1

    if loaded:
        logger.info("kiro_hooks_autoimport: loaded %d scripts from %s", loaded, hooks_dir)
    else:
        logger.debug("kiro_hooks_autoimport: no scripts loaded from %s", hooks_dir)
    return result


def _merge_kiro_hooks(hooks: dict, user_hooks: dict) -> dict:
    """Append user-defined kiro_hooks to bundled hooks (per event type).

    Bundled hooks are always first.  User hooks are appended, deduped by
    ``(command, matcher)`` tuple so the same hook doesn't fire twice.
    Malformed entries (missing ``command``) are silently skipped.
    Commands are validated: must be absolute paths to existing files,
    with no shell metacharacters and not in sensitive locations.
    """
    if not isinstance(user_hooks, dict):
        logger.warning("kiro_hooks is not a dict, ignoring")
        return hooks
    merged = dict(hooks)
    total_added = 0
    for event, entries in user_hooks.items():
        if event not in _VALID_HOOK_EVENTS:
            logger.warning("kiro_hooks: unknown event type %r, skipping", event)
            # Audit parity with every other rejection branch in this
            # function: per AUTOSDE.yaml security-controls, rejecting an
            # entire event-bucket is a permission decision that must be
            # SEL-audited.  Use the (invalid) event name as the tag so
            # auditors can correlate with the config input.
            _sel_hook_rejected(str(event), str(entries)[:200], "unknown event type")
            continue
        if not isinstance(entries, list):
            logger.warning("kiro_hooks[%s] is not a list, skipping", event)
            # Same audit-parity rationale: dropping a non-list
            # entries-bucket removes all configured hooks for that
            # event.  SEL must record the decision so auditors can
            # distinguish "0 configured" from "N dropped as non-list".
            _sel_hook_rejected(event, str(entries)[:200], "entries not a list")
            continue
        existing = list(merged.get(event, []))
        existing_keys = {
            (e.get("command"), e.get("matcher")) for e in existing if isinstance(e, dict)
        }
        added = 0
        for entry in entries:
            if added >= _MAX_USER_HOOKS_PER_EVENT:
                logger.warning(
                    "kiro_hooks[%s]: limit of %d reached, ignoring remaining",
                    event,
                    _MAX_USER_HOOKS_PER_EVENT,
                )
                # Audit parity with every other rejection branch in this
                # function (missing command, failed validation, non-string
                # matcher, invalid matcher): hitting the per-event cap is
                # a permission decision - configured hooks are being
                # prevented from loading - and must emit a SEL audit
                # event per AUTOSDE.yaml security-controls.  Without
                # this, an auditor cannot distinguish "user configured 15
                # preToolUse hooks and 5 were cap-dropped" from "user
                # configured 10 and all loaded".
                _sel_hook_rejected(
                    event,
                    (
                        str(entry.get("command", ""))[:200]
                        if isinstance(entry, dict)
                        else str(entry)[:200]
                    ),
                    "per-event limit exceeded",
                )
                break
            if total_added >= _MAX_TOTAL_USER_HOOKS:
                logger.warning(
                    "kiro_hooks: global limit of %d reached, ignoring remaining",
                    _MAX_TOTAL_USER_HOOKS,
                )
                # Same audit-parity rationale as the per-event cap above:
                # hitting the global cap drops remaining hooks across all
                # events, and auditors need a SEL signal to distinguish
                # "25 configured, 5 cap-dropped" from "20 configured, all
                # loaded".
                _sel_hook_rejected(
                    event,
                    (
                        str(entry.get("command", ""))[:200]
                        if isinstance(entry, dict)
                        else str(entry)[:200]
                    ),
                    "global limit exceeded",
                )
                break
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("command"), str)
                or not entry["command"]
            ):
                logger.warning("kiro_hooks[%s]: skipping entry without command", event)
                _sel_hook_rejected(event, str(entry)[:200], "missing or invalid command")
                continue
            resolved = _validate_hook_command(entry["command"], event)
            if resolved is None:
                _sel_hook_rejected(event, entry["command"], "failed validation")
                continue
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                logger.warning("kiro_hooks[%s]: matcher must be a string, skipping", event)
                _sel_hook_rejected(event, entry["command"], "non-string matcher")
                continue
            if isinstance(matcher, str) and (
                len(matcher) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher)
            ):
                logger.warning(
                    "kiro_hooks[%s]: matcher contains disallowed characters or is too long, skipping",
                    event,
                )
                _sel_hook_rejected(event, entry["command"], "invalid matcher")
                continue
            key = (resolved, matcher)
            if key not in existing_keys:
                sanitized = {"command": resolved}
                if isinstance(matcher, str):
                    sanitized["matcher"] = matcher
                existing.append(sanitized)
                existing_keys.add(key)
                added += 1
                total_added += 1
        merged[event] = existing
    return merged


def _apply_user_kiro_hooks(config: dict, mc_cfg: dict) -> None:
    """Merge user-defined kiro_hooks from kirocrew config into *config* (additive).

    Two sources, explicit first then auto-discovered:

      1. ``agent.kiro_hooks`` in ``~/.kiro/crew/config.json`` -- explicit entries
         the user wrote by hand.  Unchanged behavior.
      2. ``agent.kiro_hooks_autoimport`` (default true): scan
         ``agent.kiro_hooks_dir`` (default ``~/.kiro/hooks``) for executable
         ``*.sh`` scripts and merge each as a hook entry.  Event is parsed from
         an optional ``# event:`` header, inferred from a filename suffix, or
         defaults to ``preToolUse``.  Optional ``# matcher:`` header gives the
         same tool-name matcher as explicit entries.

    Autoimport runs in a single merge pass with explicit entries listed first,
    so autoimported scripts that duplicate an explicit entry are deduped out
    (explicit wins) and caps (``_MAX_USER_HOOKS_PER_EVENT`` and
    ``_MAX_TOTAL_USER_HOOKS``) are enforced across both sources combined,
    not per-source.
    """
    agent_cfg = mc_cfg.get("agent") if isinstance(mc_cfg.get("agent"), dict) else {}
    user_hooks = agent_cfg.get("kiro_hooks") if isinstance(agent_cfg, dict) else None
    autoimport_enabled = True
    hooks_dir = _DEFAULT_KIRO_HOOKS_DIR
    if isinstance(agent_cfg, dict):
        if "kiro_hooks_autoimport" in agent_cfg:
            autoimport_enabled = bool(agent_cfg.get("kiro_hooks_autoimport"))
        custom_dir = agent_cfg.get("kiro_hooks_dir")
        if isinstance(custom_dir, str) and custom_dir:
            # config.json is LLM-writable; a malicious override could point
            # hooks_dir at /tmp, a world-writable mount, or ~/Downloads.
            # Require the resolved path to live under the user's HOME and
            # not match a sensitive location.  On any failure, log + SEL
            # audit and fall back to the default (~/.kiro/hooks) rather
            # than turning autoimport off entirely - the safe default is
            # still available.
            requested = Path(os.path.expanduser(custom_dir))
            try:
                resolved = requested.resolve()
                home = Path.home().resolve()
            except (OSError, ValueError):
                # OSError: ENAMETOOLONG, ELOOP (symlink loop), EACCES.
                # ValueError: Path() / resolve() reject strings with null
                # bytes (``"\x00"``) or similar malformed Unicode.  An
                # LLM-writable ``kiro_hooks_dir: "\x00"`` would otherwise
                # propagate ValueError up through install_agent() and
                # crash agent bootstrap (denial of service).
                resolved = None
                home = None
            if (
                resolved is None
                or home is None
                # Strict containment: require ``resolved`` to be *under*
                # HOME, not equal to it.  ``~`` alone would otherwise scan
                # the entire home directory for executable ``*.sh`` files,
                # auto-registering anything a user (or attacker) drops
                # anywhere under ``$HOME``.  ``Path.parents`` of e.g.
                # ``/home/user`` is ``(/, /home)`` and does NOT include
                # ``/home/user`` itself, so a bare ``home not in parents``
                # rejects ``resolved == home``.
                or home not in resolved.parents
                or is_sensitive_path(str(resolved))
            ):
                logger.warning(
                    "kiro_hooks_autoimport: kiro_hooks_dir %r rejected "
                    "(must resolve under %s and not be sensitive), "
                    "falling back to %s",
                    custom_dir,
                    home,
                    _DEFAULT_KIRO_HOOKS_DIR,
                )
                _sel_hook_rejected(
                    "autoimport", str(requested), "kiro_hooks_dir outside HOME or sensitive"
                )
            else:
                # Store the already-resolved path, not the unresolved
                # ``requested``.  Keeping ``requested`` would leave a
                # symlink-swap window: a path component could be swapped
                # between this resolve() and the one inside
                # _autoimport_kiro_hooks, bypassing the HOME containment
                # check we just performed.
                hooks_dir = resolved

    explicit_hooks: dict = user_hooks if isinstance(user_hooks, dict) and user_hooks else {}
    has_explicit = bool(explicit_hooks)
    if not has_explicit and not autoimport_enabled:
        return

    before = sum(len(v) for v in config.get("hooks", {}).values() if isinstance(v, list))

    # Collect both sources up-front and merge in a SINGLE ``_merge_kiro_hooks``
    # pass.  Rationale: ``_merge_kiro_hooks`` initializes ``total_added = 0`` on
    # each call, so invoking it twice would allow the per-call
    # ``_MAX_TOTAL_USER_HOOKS`` cap (20) to apply to each source independently —
    # yielding up to 40 user hooks total instead of the intended 20.  A single
    # pass enforces the per-event cap AND the total cap across the combined
    # set.  Explicit entries are listed first in each event's list so they
    # claim the dedup key before any duplicate from autoimport, preserving the
    # "explicit wins" precedence.
    # Count explicit entries AND audit any non-list buckets as we go.
    # Using a plain loop rather than a generator expression so we can
    # emit WARNING + SEL audit for each dropped event bucket -- dropping
    # a whole event's hooks is a permission decision per AUTOSDE.yaml
    # security-controls, and the caller-side filter must audit it
    # (``_merge_kiro_hooks``'s internal defensive check never fires here
    # because this filter runs first).
    requested_explicit = 0
    for event, entries in explicit_hooks.items():
        if isinstance(entries, list):
            requested_explicit += len(entries)
        else:
            logger.warning("kiro_hooks[%s] is not a list, skipping", event)
            _sel_hook_rejected(str(event), str(entries)[:200], "entries not a list")
    requested_autoimport = 0
    discovered: dict[str, list[dict[str, str]]] = {}
    if autoimport_enabled:
        discovered = _autoimport_kiro_hooks(hooks_dir)
        requested_autoimport = sum(len(v) for v in discovered.values() if isinstance(v, list))

    if requested_explicit == 0 and requested_autoimport == 0:
        # Nothing to merge; keep config["hooks"] untouched (or create empty
        # dict for shape consistency if it wasn't there).
        if "hooks" not in config:
            config["hooks"] = {}
        return

    combined_user_hooks: dict[str, list[dict[str, str]]] = {}
    for src in (explicit_hooks, discovered):
        if not isinstance(src, dict):
            continue
        for event, entries in src.items():
            if not isinstance(entries, list):
                # Already WARN+SEL-audited in the ``requested_explicit``
                # loop above (for explicit_hooks) or filtered out at
                # return-time of ``_autoimport_kiro_hooks`` (discovered
                # never contains non-list values).  Defensive continue.
                continue
            combined_user_hooks.setdefault(event, []).extend(entries)

    config["hooks"] = _merge_kiro_hooks(config.get("hooks", {}), combined_user_hooks)

    after = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
    added = after - before
    try:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="config_hooks_merge",
                caller_identity="agent_install",
                agent="kirocrew",
                source="cli",
                operation="kiro_hooks_merge",
                outcome="completed",
                resources=redact(
                    f"requested_explicit={requested_explicit} "
                    f"requested_autoimport={requested_autoimport} added={added}"
                ),
            )
        )
    except Exception:
        logger.debug("SEL audit for kiro_hooks merge failed", exc_info=True)


def build_agent_config(*, gated_off: "frozenset[str] | None" = None) -> dict:
    """Return the final agent config (shipped defaults + user overrides + dynamic fields).

    Security-critical ``hooks`` always use the bundled config as their base,
    even when a project-dir override is present, so dev overrides cannot
    silently drop the PreToolUse security gate. ``deniedCommands`` are NO
    LONGER injected here — command denial is enforced at KiroCrew's own
    hooks.py PreToolUse gate, not via the kiro agent spec. User-defined
    ``kiro_hooks`` from ``~/.kiro/crew/config.json`` are then additively merged;
    bundled hooks always run first and cannot be removed.

    The assembled ``allowedTools`` list is ceiling-filtered before return (see
    :func:`_apply_allowed_tools_ceiling`), so every spec derived from this
    template starts governed — an installer no longer has to remember the
    filter to avoid shipping a blanket auto-approve for a floor-gated builtin.

    Args:
        gated_off: Managed servers whose ``spec_gate`` is closed. Pass the
            caller's snapshot so one rebuild's emit path and its withhold audit
            agree; omitted, it is evaluated here.
    """
    config = _load_json(_shipped_defaults())
    config = _deep_merge(config, _load_json(_user_overrides_path()))

    # Ensure hooks always come from the bundled config,
    # even if the project-level defaults.json is stale.
    bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot build agent config: hooks missing from bundled defaults")
    # Strip Kiro Crew-internal keys (auto_approve_tools etc.) that kiro-cli  # brand-ok
    # rejects. _VALID_HOOK_EVENTS already unions in every non-internal bundled
    # event key, so this never drops a new event added to bundled defaults (#3362).
    config["hooks"] = _kiro_hooks_only(bundled_hooks)

    # Strip the retired deniedCommands/autoAllowReadonly injection so a config
    # merged from a stale project defaults.json or user override cannot carry it.
    _strip_legacy_denied_commands(config)

    # Merge user-defined kiro_hooks from ~/.kiro/crew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    _apply_user_kiro_hooks(config, mc_cfg)

    # Dynamic fields — always resolved at install time
    config["prompt"] = f"file://{_prompt_path()}"
    mcp = config.setdefault("mcpServers", {})
    registry_mode = _mcp_registry_mode()
    if gated_off is None:
        gated_off = _gated_off_servers()
    for name, spec in _MANAGED_MCP_SERVERS.items():
        if not _mcp_server_emission_eligible(name, spec, gated_off=gated_off):
            # NOT eligible, and the two reasons part company on one point: a
            # closed gate must RETRACT the entry, an opt-in one is merely never
            # introduced.
            if name in gated_off:
                # The gate is the whole point of this branch: emitting the entry is
                # what makes kiro-cli spawn the backend, so a closed gate must not
                # emit one. ``pop`` as well as ``continue`` because the base here is
                # shipped defaults merged with the user override file, and an entry
                # arriving from there would otherwise slip past a platform gate that
                # exists because the capability has no driver on this OS.
                mcp.pop(name, None)
            # An opt-in server is an assignable set: it belongs to the agents whose
            # own spec references it, so a freshly built default spec must not carry
            # it. kiro-cli loads a server only when ``tools`` names it, and the
            # shipped template names only the always-on ones. Left in place rather
            # than popped: an entry already on disk is a grant the user made.
            continue
        if "invocation_fn" in spec:
            cmd, args = spec["invocation_fn"]()
        else:
            cmd = spec.get("command") or spec["command_fn"]()
            args = list(spec["args"])
        entry = {"command": cmd, "args": args}
        # Enterprise registry mode: without this marker the client drops the
        # entry before launch (see _mcp_registry_mode). command/args stay so the
        # spec still describes a runnable server for every other consumer —
        # doctor's handshake probe, the CC sidecar sync, a later un-governed
        # refresh — none of which route through the registry.
        if registry_mode:
            entry["type"] = _MCP_REGISTRY_TYPE
        # Pin the data home so the shim cannot read a DIFFERENT one than the
        # gateway that spawned it (see _managed_mcp_env). Omitted entirely on a
        # default install, so the emitted spec is unchanged there.
        env = _managed_mcp_env()
        if env:
            entry["env"] = env
        if "autoApprove" in spec:
            entry["autoApprove"] = list(spec["autoApprove"])
        mcp[name] = entry

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: standalone
    # contributes {} (unchanged), the Amazon companion adds the internal MCP server etc.
    # Entries are already kiro-spec-shaped, so we only extend the map — no spec
    # restructuring, deny_unknown_fields invariant preserved.
    for name, spec in _extra_mcp_servers().items():
        mcp.setdefault(name, dict(spec))

    # Default-model tracking ("managed" vs frozen) is recorded in the
    # agent_state sidecar by the install path (rebuild_agent_config), never as
    # a kiro-spec key — kiro-cli rejects unknown fields and would drop the whole
    # spec. build_agent_config stays pure (no disk writes) so its many
    # read-only callers don't mutate managed-state as a side effect.

    # Governance ceiling over the assembled ``allowedTools`` — HERE, at the one
    # constructor every derived-spec installer starts from, so the invariant is
    # held by the predicate rather than by each installer's author remembering
    # it (#7401). ``rebuild_agent_config`` keeps its own final pass because its
    # ``_load_existing_config`` path takes entries from an on-disk spec that
    # never comes through here; for that caller this filter is idempotent (the
    # predicate is pure, so filtering twice equals filtering once). A caller
    # that replaces ``allowedTools`` wholesale (``_install_conductor_agent``)
    # is unaffected. The SEL audit inside is best-effort and never raises, so
    # the purity note above still holds for config/managed-state.
    _apply_allowed_tools_ceiling(config, source="build_agent_config")
    return config


def _refresh_dynamic_fields(config: dict, *, gated_off: "frozenset[str] | None" = None) -> None:
    """Update security-critical and dynamic fields in an existing config.

    Called when ``kirocrew.json`` already exists so user customizations are
    preserved while security controls and runtime paths stay current.

    Args:
        gated_off: Managed servers whose ``spec_gate`` is closed. Pass the
            caller's snapshot so one rebuild's emit path and its withhold audit
            agree; omitted, it is evaluated here.
    """
    # Prompt URI — always resolve at install time
    config["prompt"] = f"file://{_prompt_path()}"

    # Managed MCP servers — ensure present and up-to-date.
    # Only refresh command/args; preserve user customizations (e.g. autoApprove).
    mcp = config.setdefault("mcpServers", {})
    registry_mode = _mcp_registry_mode()
    if gated_off is None:
        gated_off = _gated_off_servers()
    for name, spec in _MANAGED_MCP_SERVERS.items():
        eligible = _mcp_server_emission_eligible(name, spec, gated_off=gated_off)
        if not eligible and name in gated_off:
            # RETRACT, not merely skip: an earlier refresh wrote this entry while
            # the gate was open, and leaving it would mean turning the feature
            # off never reclaims the backend process turning it on started.
            #
            # The entry's user-owned fields are NOT preserved. Stashing them
            # would need an agent-writable sidecar, and an ``autoApprove``
            # restored from there is a self-granted auto-approve: kiro-cli
            # approves such a tool locally, so ``hooks.on_tool_call`` never sees
            # the call. An off/on cycle therefore resets a customized entry and
            # the operator re-applies it — losing an approval is the safe
            # direction, granting one from an agent-writable file is not.
            #
            # The server's ``@ref`` in ``tools`` is deliberately left alone. A ref
            # whose server has no ``mcpServers`` entry resolves to nothing and
            # mounts nothing, so withholding the entry is the whole control;
            # removing the ref as well would destroy a grant the user may have
            # narrowed by hand and cannot be reconstructed on re-enable.
            mcp.pop(name, None)
            continue
        is_new = name not in mcp
        # An opt-in server is granted by the spec itself, so a refresh keeps an
        # entry the user put there current but never introduces one: adding it
        # back would re-grant a set on every gateway start. Spelled through the
        # shared eligibility predicate (the gate half is already spent above, so
        # what remains of ineligibility here is exactly ``opt_in``) rather than
        # re-reading the flag, so the rule cannot drift from the emitter's or the
        # dashboard merge's reading of it.
        if is_new and not eligible:
            continue
        if not is_new and spec.get("opt_in") and not isinstance(mcp.get(name), dict):
            # A hand-written entry that is not an object at all. Refreshing it
            # would raise (item assignment on a str), and rewriting it would
            # discard whatever the user meant to say. Leave it untouched and let
            # doctor report it — this pass repairs OUR fields, it does not
            # adjudicate malformed user input.
            #
            # Only for an OPT-IN server, whose entry the user hand-wrote. An
            # always-on entry is ours, nobody hand-writes it, and a malformed one
            # deliberately falls through to raise: the caller catches TypeError
            # and rebuilds from defaults, which is what restores the server.
            # Skipping it here instead would leave it malformed, so validation
            # drops it while its ``@ref`` stays in ``tools`` — every tool on that
            # server silently gone.
            continue
        entry = mcp.setdefault(name, {})
        if "invocation_fn" in spec:
            entry["command"], entry["args"] = spec["invocation_fn"]()
        else:
            entry["command"] = spec.get("command") or spec["command_fn"]()
            entry["args"] = list(spec["args"])
        # Strip any stale remote-transport fields from older builds: these
        # servers are stdio-only, and a leftover ``url`` would otherwise
        # propagate into the CC config and shadow the command. (Root fix for
        # the downstream stdio-force in cc_agent / acp.client.)
        entry.pop("url", None)
        entry.pop("headers", None)
        # Enterprise registry marker — refreshed like command/args rather than
        # preserved like ``autoApprove``, because it tracks the account the
        # gateway is actually signed in to, not a user preference. Removed (not
        # left stale) when the declaration is off, so a host that leaves an
        # enterprise profile stops shipping a marker that would now cause the
        # inverse filter to drop these servers.
        if registry_mode:
            entry["type"] = _MCP_REGISTRY_TYPE
        elif entry.get("type") == _MCP_REGISTRY_TYPE:
            entry.pop("type", None)
        # Data-home pin — refreshed like command/args rather than preserved like
        # ``autoApprove``, because it is OURS, not a user customization: it must
        # track the home the gateway is actually running under. A config written
        # under an override and later refreshed on a default install would
        # otherwise keep pointing the shims at the stale home. Merged into any
        # existing ``env`` so a user's own variables survive, and the key is
        # REMOVED (not left stale) when there is no override.
        pinned = _managed_mcp_env()
        env = dict(entry.get("env") or {})
        env.pop("KIROCREW_HOME", None)
        env.update(pinned)
        if env:
            entry["env"] = env
        else:
            entry.pop("env", None)
        # Seed autoApprove only for genuinely new entries; if the user
        # deliberately removed autoApprove from an existing entry we
        # must not re-add it on every refresh.
        if "autoApprove" in spec and is_new:
            entry["autoApprove"] = list(spec["autoApprove"])

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: only seed a
    # server the user doesn't already have, so user customizations on a refresh
    # are preserved.  Standalone contributes {} (unchanged); Amazon adds
    # the internal MCP server etc.  Already kiro-spec-shaped — no restructuring.
    for name, extra_spec in _extra_mcp_servers().items():
        mcp.setdefault(name, dict(extra_spec))

    # Security: hooks always from bundled config.
    # Hard-fail if bundled defaults are missing — deny-by-default.
    bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
    if bundled is None:
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is missing or unreadable"
        )
    if not isinstance(bundled, dict):
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is not a JSON object"
        )

    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot refresh security fields: hooks missing from bundled defaults")
    config["hooks"] = _kiro_hooks_only(bundled_hooks)

    # Upgrade cleanup: drop the retired deniedCommands/autoAllowReadonly that an
    # older build injected into this existing config, so kiro-cli stops enforcing
    # the stale list ahead of the hooks gate (see _strip_legacy_denied_commands).
    _strip_legacy_denied_commands(config)

    # Merge user-defined kiro_hooks from ~/.kiro/crew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    _apply_user_kiro_hooks(config, mc_cfg)

    # Model migration — replace deprecated model names with current equivalents.
    # Uses the canonical map from chat.py plus legacy pre-4.6 models.
    _model_migration = {
        "claude-opus-4.6-1m": "claude-opus-4.6",
        "claude-sonnet-4.6-1m": "claude-sonnet-4.6",
    }
    cur_model = config.get("model", "")
    if cur_model in _model_migration:
        config["model"] = _model_migration[cur_model]

    # Self-heal: lift any stray KiroCrew bookkeeping keys into the sidecar and
    # strip them from the spec so kiro-cli (deny_unknown_fields) accepts it.
    # This is the steady-state safety net that cleans specs polluted by older
    # builds on the next refresh; the one-time migrate_agent_specs() at startup
    # handles the rest of ~/.kiro/agents/.
    name = config.get("name") or _MAIN_AGENT_NAME
    agent_state.lift_and_strip_bookkeeping(config, name)

    # Imported lazily: config.loader imports this module, so a top-level import
    # would close the cycle. Warm by the time this runs (importing agent pulls
    # config.loader in), so the lookup costs nothing on the caller's thread.
    from kiro_crew.config.loader import DEFAULT_MODEL, normalize_agent_model

    # Default-model tracking: when the model is managed (not an explicit user
    # pick), re-sync it from the shipped defaults.json so a default bump
    # propagates to existing installs. Agents with no sidecar entry are
    # grandfathered and left untouched (never force-changed).
    #
    # The assignment is unconditional for a managed spec, falling back to the
    # inherit sentinel when the template pins nothing: "track the shipped
    # default" and "pin whatever happens to be in the spec already" are not the
    # same state, and only the sentinel makes a managed spec converge on the
    # same value a clean install writes. It is also what lets the global below
    # return to "auto" — leaving the field alone here would strand a concrete
    # model that this propagation itself wrote, and a spec pin outranks the
    # global in resolve_effective_model, so "auto" would be unreachable from the
    # configuration surface. Writing the sentinel rather than deleting the key
    # is equivalent to the resolver (normalize_agent_model collapses "auto" and
    # an absent key to the same "inherit") and keeps the spec shaped like the
    # shipped template.
    if agent_state.get_model_managed(name):
        shipped_model = (_load_json(_shipped_defaults()) or {}).get("model")
        config["model"] = shipped_model or DEFAULT_MODEL

    # config.json agent.model is the user-facing authority (kirocrew config set
    # agent.model). An explicit pick (not the "auto" sentinel) is propagated into
    # the agent file so kiro-cli's --agent startup load matches it; otherwise the
    # stale agent-file model shadows config.json and session/set_model loses the
    # startup race. "auto" defers to managed/shipped resolution above.
    #
    # Read through normalize_agent_model, the resolver's own chokepoint for
    # hand-edited values: it collapses "auto", surrounding whitespace and any
    # non-string to "" (inherit). That keeps this branch's notion of "the global
    # defers" identical to the resolver's, and it is what stops a junk value
    # (` auto `, an int) from reaching a spec kiro-cli validates with
    # deny_unknown_fields — a spec it rejects wholesale, silently falling back to
    # the default agent.
    mc_model = normalize_agent_model((mc_cfg.get("agent") or {}).get("model"))
    if mc_model:
        config["model"] = mc_model

    # Ensure kiro-cli uses agent-level mcpServers exclusively (not global
    # mcp.json).  Existing configs created before this field was added lack
    # it, causing kiro-cli to fall back to the (possibly empty) global file.
    config["includeMcpJson"] = False

    # Seed workspace-relative resources (steering files, AGENTS.md, etc.)
    # only when the user hasn't customized them.  kiro-cli normalizes
    # missing ``resources`` to ``[]`` on read, so existing users created
    # before this field shipped end up with an empty list that prevents
    # ``.kiro/steering/**/*.md`` and friends from auto-loading.  If the user
    # has explicitly listed their own resources, leave them alone.
    bundled_resources = bundled.get("resources")
    if isinstance(bundled_resources, list) and bundled_resources and not config.get("resources"):
        config["resources"] = list(bundled_resources)

    # tools/allowedTools: user-owned and otherwise NOT modified on existing
    # configs.  Narrow exception (ADD-only): ensure the ``tool_search`` built-in
    # grant is present.  kiro-cli only activates MCP Tool Search when the
    # ToolSearch built-in is in the agent's tools list; without the grant, the
    # per-session overlay written for AgentConfig.tool_search (enabled +
    # minPct=0/minTokens=0) is a no-op and full MCP tool specs are sent every
    # turn.  Existing configs created before ``tool_search`` shipped in
    # defaults.json never gain it otherwise, because the tools list is preserved
    # above.  It is a read-only, auto-allowed built-in (permission eval => Allow)
    # so no ``allowedTools`` entry is needed.  Gated on the shipped template
    # actually granting it (so an edition that drops it is respected) and scoped
    # to this single tool; the feature's on/off remains the AgentConfig.tool_search
    # toggle.  Never removes a tool and never reorders the rest.
    tools = config.get("tools")
    if (
        isinstance(tools, list)
        and "tool_search" in (bundled.get("tools") or [])
        and "tool_search" not in tools
    ):
        tools.append("tool_search")


def get_shipped_tools() -> dict[str, list[str]]:
    """Return shipped tool lists. Public API for cross-module use."""
    shipped = _load_json(_shipped_defaults()) or {}
    return {k: shipped.get(k, []) for k in ("tools", "allowedTools")}


def _load_existing_config(
    path: Path, *, gated_off: "frozenset[str] | None" = None
) -> tuple[dict, bool]:
    """Load and refresh an existing kirocrew.json.

    Returns (config, fresh_install).  Falls back to build_agent_config()
    when the file is corrupt or refresh fails.

    *gated_off* is the caller's spec-gate snapshot, forwarded so whichever branch
    runs reads the same decision the caller's audit will report.
    """
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
    if not isinstance(config, dict):
        return build_agent_config(gated_off=gated_off), True
    try:
        _refresh_dynamic_fields(config, gated_off=gated_off)
    except (AttributeError, TypeError, RuntimeError) as exc:
        logger.error("Refresh failed, rebuilding from defaults: %s", exc)
        return build_agent_config(gated_off=gated_off), True
    return config, False


def _norm_mcp_spec(spec: Any) -> Any:
    """Return the comparison form of an ``mcpServers`` spec for dedup.

    Setup / re-installs re-emit the same server across runs with slightly
    different optional-key *shapes*: a bare ``{"command": ...}`` one run, then
    ``"env": {}`` or ``"args": []`` the next. Comparing raw dicts treats those
    as distinct servers, so ``_normalize_mcp_server_keys`` mints an ever-growing
    ``-2``/``-3``... suffix on every build / reinstall / update.
    Dropping empty optional collections makes semantically identical re-merges
    collapse onto the canonical alias. An empty ``env``/``args`` is a launch
    no-op for kiro-cli (missing == empty), so this is also the cleaner spec to
    persist.

    :data:`~kiro_crew.mcp_provenance.DERIVED_KEY` is excluded for the same reason,
    and it is load-bearing: the record is our bookkeeping about which field this
    rebuild computed, not part of how the server launches, so an entry carrying one
    and an otherwise-identical re-merged copy without one ARE the same server.
    Comparing it would make them differ and mint the ever-growing suffix this
    function exists to prevent -- and it would do so asymmetrically, since only the
    population with no other config source is ever recorded.
    """
    if not isinstance(spec, dict):
        return spec
    return {
        k: v for k, v in spec.items() if k != DERIVED_KEY and not (k in ("env", "args") and not v)
    }


def _alias_family_base(key: str) -> str:
    """Strip a collision suffix, yielding the alias its family is named for.

    ``_normalize_mcp_server_keys`` preserves a server whose alias is already held
    by a different spec under the lowest free ``<alias>-<n>``, so that key is the
    only name for a server no source spells. Callers resolving ownership through
    it must still confirm identity: sharing a family means sharing an alias, not
    being the same server.
    """
    base, _, tail = key.rpartition("-")
    return base if base and tail.isdigit() else key


def _connection_tool_aliases_enabled() -> bool:
    """True when the Connections tool-alias pass may write ``toolAliases``.

    Read raw from ``config.json`` (the ``kiro_hooks`` precedent) rather than
    declared on the config dataclass: this is a dark-launch gate that retires
    once the alias behaviour is the only behaviour, and an undeclared key costs
    the schema nothing in the meantime.
    """
    connections = (_load_json(_mc_config_path()) or {}).get("connections")
    if not isinstance(connections, dict):
        return False
    return connections.get("tool_aliases") is True


def _apply_connection_tool_aliases(
    config: dict,
    claimed: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[str, frozenset[tuple[str, str, str]]] | None:
    """Resolve exposed-provider tool-name collisions into ``config['toolAliases']``.

    Without this, two exposed providers that ship the same tool name leave one of
    the two unreachable -- kiro-cli addresses a tool by bare name, so the later
    mount shadows the earlier one silently.

    :mod:`kiro_crew.connections.tool_aliases` owns invariants 1-6, which decide
    WHICH aliases resolve (registry-sourced, collision-only, exposed-and-verified
    providers). The THREE below are this function's, and govern how a resolution
    is written into a spec that a user also edits:

    * **Flag off => byte-identical emission.** The key is neither created nor
      cleared, so a spec built with the gate off is indistinguishable from one
      built before this pass existed. Nothing else here reads ``toolAliases``,
      so leaving a stale key alone cannot mislead a later pass -- and clearing it
      would make "off" a distinct third behaviour instead of a no-op. A no
      collision resolution likewise writes nothing, so the common install (zero
      or one exposed provider) gains no empty object.

    * **The generated subset is read from a PERSISTED record, not inferred from a
      pair's shape.** Cleanup deletes entries out of a file the user also edits, so
      it needs proof of authorship, and no property of the NAME supplies one: a
      ``<slug>_`` prefix test claims a hand-written ``linear_issues``, and
      re-deriving ``<slug>_<tool>`` claims a hand-written ``notion_search`` for a
      provider that declares nothing. So the pass records exactly what it emitted
      and, on the next run, strips only pairs that record claims (whole triple,
      so a user-edited generated alias no longer matches and survives). Merging
      onto the previous output instead is what made "user-authored wins" preserve
      the LAST rebuild's generated refs: the merge is idempotent, so a rename
      survived the mount that justified it going away. Every pair the record does
      not claim is by definition the user's and still wins over the registry
      default. See :mod:`kiro_crew.connections.alias_record` for the generation
      binding that stops the record ever describing a spec it does not match: this
      function OPENS the transaction, so an interrupted rebuild is recoverable from
      whichever side actually reached disk, and the CALLER commits it once the spec
      is durable.

    * **A generated alias never lands on a name already in use.** The destination
      is checked against surviving alias targets, the declared natural names of
      exposed providers, every tool name named in a per-tool ``tools`` ref of ANY
      exposed server (custom servers included), and the builtin names in
      ``tools``; a conflict skips that one alias with a warning. Renaming onto an
      occupied name would recreate the shadowing this pass exists to remove, so it
      fails safe to shadowing rather than to a silent overwrite. A custom server
      mounted WHOLE publishes its names only at runtime and is out of scope by
      construction -- see the module docstring's OUT OF SCOPE note.

    Mutates *config* in place. Idempotent: the resolution is a pure function of
    the exposed provider set, so a rebuild that changes no mounts rewrites the
    same map (or leaves the same absence).

    Args:
        config: The assembled spec. Mutated in place.
        claimed: The triples the record proves THIS pass wrote into the generation
            *config* currently carries, already resolved by the caller against the
            authoritative on-disk map. Empty means nothing is provably ours, so
            every existing pair is treated as the user's and survives.

    Returns:
        ``(fingerprint, emitted)`` for the generation this pass just wrote into
        *config* -- the fingerprint of the resulting ``toolAliases`` map and the
        ``(slug, tool, alias)`` triples it emitted, possibly EMPTY (an empty
        emission is how the pass relinquishes pairs it no longer writes). The
        CALLER owns the transaction: it opens one before the spec write and commits
        it after. ``None`` means the pass did not run -- gate off, no server map, or
        an unreadable registry -- and it has not touched *config*.
    """
    if not _connection_tool_aliases_enabled():
        return None

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None

    tools = config.get("tools")
    tool_refs = tools if isinstance(tools, list) else []

    try:
        # Imported here, not at module scope: kiro_crew.connections.registry
        # validates the committed registry at MODULE level, so a missing or
        # malformed registry.json would raise on `import kiro_crew.agent` --
        # before the guard below can run -- and take down the very module that
        # installs and repairs the agent spec. Deferring the import keeps a data
        # file from breaking the recovery path, and keeps a registry read off
        # agent.py's import cost for every install that never enables this.
        # Guarded by test_importing_agent_does_not_eagerly_load_the_registry.
        from kiro_crew.connections.alias_record import (  # noqa: PLC0415
            emitted_from_alias_map,
            is_recorded_emission,
            spec_fingerprint,
        )
        from kiro_crew.connections.tool_aliases import (  # noqa: PLC0415
            exposed_declared_tools,
            natural_tool_names,
            resolve_tool_aliases,
            statically_visible_tool_names,
        )

        previously_emitted = claimed
        exposed = exposed_declared_tools(servers, tool_refs)
        aliases = resolve_tool_aliases(exposed)
        reserved_natural = natural_tool_names(exposed)
        reserved_visible = statically_visible_tool_names(tool_refs)
    except Exception:  # noqa: BLE001 — a malformed registry must not fail a rebuild
        logger.warning("Skipping Connections tool aliases: registry unavailable", exc_info=True)
        return None

    existing = config.get("toolAliases")
    # A pre-existing non-dict value (hand-edited ``toolAliases: []``) is replaced
    # rather than merged onto: kiro-cli rejects the whole spec over it, so
    # self-healing costs nothing a working config would miss.
    existing_map = existing if isinstance(existing, dict) else None

    # Drop only the pairs the RECORD proves this pass wrote, and keep everything
    # else, whose authorship is unproven and therefore the user's. Then recompute;
    # see the staleness invariant above. The comparison is on the whole triple, so
    # a generated alias the user has since edited no longer matches and stays. A
    # non-string alias is dropped for the same reason a non-dict container is
    # replaced: kiro-cli rejects the entire spec over it, so preserving it would
    # protect a hand-edit by costing the user every tool.
    retained = {
        ref: alias
        for ref, alias in (existing_map or {}).items()
        if isinstance(ref, str)
        and isinstance(alias, str)
        and not is_recorded_emission(previously_emitted, ref, alias)
    }

    # Destination guard: everything a generated alias must not collide with.
    occupied = set(retained.values())
    occupied |= reserved_natural
    occupied |= reserved_visible
    occupied |= {ref for ref in tool_refs if isinstance(ref, str) and not ref.startswith("@")}

    accepted: dict[str, str] = {}
    for ref, alias in aliases.items():
        if ref in retained:
            # Hand-authored override for this exact ref: the user's alias stands
            # and the generated one is not a second entry.
            continue
        if alias in occupied:
            logger.warning(
                "Skipping Connections tool alias %s -> %r: the name is already in use "
                "by another alias or tool, so renaming onto it would shadow that tool",
                ref,
                alias,
            )
            continue
        accepted[ref] = alias
        occupied.add(alias)

    emitted = emitted_from_alias_map(accepted)
    merged = dict(sorted({**accepted, **retained}.items()))

    # The generation this pass is about to write. Nothing generated AND nothing
    # hand-authored surviving means the key goes away entirely rather than being
    # emptied: absent stays absent (gate-off parity), and a key holding only this
    # pass's now-stale output returns the spec to exactly the shape it had before
    # any alias was ever written.
    target = (spec_fingerprint(merged or None), emitted)

    if not merged:
        if existing is not None:
            config.pop("toolAliases", None)
            logger.debug("Cleared Connections tool aliases: no collisions among exposed providers")
    elif merged != existing:
        config["toolAliases"] = merged
        logger.debug(
            "Connections tool aliases written: %s generated, %s retained (%s)",
            len(accepted),
            len(retained),
            ", ".join(f"{ref}->{alias}" for ref, alias in merged.items()),
        )
    return target


def _normalize_mcp_server_keys(config: dict) -> None:
    """Rewrite any slash-containing ``mcpServers`` key to its slash-free alias.

    Mutates ``config`` in place: moves each affected server spec under its
    alias key and rewrites (and de-duplicates) the matching ``@oldkey`` ->
    ``@alias`` reference in ``tools``/``allowedTools``.  Migrates already-broken
    existing configs.  Idempotent: slash-free keys are left untouched and a
    re-merged duplicate collapses onto the canonical alias (no churn).

    Dedup is by *normalized* spec (:func:`_norm_mcp_spec`), so a re-added key
    that differs only by an empty ``env``/``args`` reuses the existing alias
    instead of accumulating a fresh ``-N`` suffix on every build / reinstall /
    update. Convergence: any already-suffixed sibling that is an
    equivalent duplicate is folded back onto the surviving alias (its ``@ref``
    is redirected), so a config already polluted by the pre-fix bug self-heals.

    Collision: if the alias is held by a *genuinely different* spec, the server
    is preserved under the lowest free numeric-suffixed alias (``-2``, ``-3``)
    -- never dropped. Managed servers (slash-free by construction) are skipped
    so their dynamic-field refresh is never disturbed.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return
    managed = set(_MANAGED_MCP_SERVERS)

    def _is_family(key: str, base: str) -> bool:
        """True if ``key`` is ``base`` or a ``base-<n>`` numeric-suffixed sibling."""
        return key == base or (key.startswith(f"{base}-") and key[len(base) + 1 :].isdigit())

    def _rewrite_ref(old_ref: str, new_ref: str) -> None:
        for key in ("tools", "allowedTools"):
            lst = config.get(key)
            if isinstance(lst, list):
                config[key] = list(dict.fromkeys(new_ref if t == old_ref else t for t in lst))

    for old_key in [k for k in servers if "/" in k and k not in managed]:
        spec = _norm_mcp_spec(servers.pop(old_key))
        base = mcp_server_alias(old_key)

        # Reuse an existing home for an equivalent spec — the canonical alias or
        # any already-suffixed sibling — instead of minting a new suffix, so
        # repeated re-merges converge rather than accumulate.
        alias = next(
            (k for k in servers if _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec),
            None,
        )
        if alias is None:
            # Genuinely distinct spec (or nothing here yet): take the canonical
            # alias if free, else the lowest free numeric suffix (never drop a
            # distinct server).
            alias = base
            if alias in servers:
                n = 2
                while f"{alias}-{n}" in servers:
                    n += 1
                alias = f"{alias}-{n}"
        servers[alias] = spec
        _rewrite_ref(f"@{old_key}", f"@{alias}")

        # Converge any OTHER sibling that duplicates the spec we just placed
        # (self-heals configs polluted by the pre-fix bug): drop it and redirect
        # its @ref onto the surviving alias.
        for dup in [
            k
            for k in list(servers)
            if k != alias and _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec
        ]:
            del servers[dup]
            _rewrite_ref(f"@{dup}", f"@{alias}")

        logger.info("Normalized MCP server key %r -> %r (kiro-safe)", old_key, alias)


def migrate_agent_specs() -> int:
    """Strip KiroCrew bookkeeping keys from kiro agent specs into the sidecar.

    kiro-cli validates ``~/.kiro/agents/*.json`` with ``deny_unknown_fields``
    and rejects the entire spec on any unknown field (``model_managed`` /
    ``cc_model``), then silently falls back to the default agent. This lifts
    those values into ``agent_state`` and removes them from each spec so every
    agent loads. Idempotent and cheap (a handful of small JSON files); safe to
    run on every gateway start. Returns the number of spec files cleaned.
    """
    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return 0
    cleaned = 0
    for spec_path in sorted(agents_dir.glob("*.json")):
        # This read is followed by a rewrite, so the hardened reader's
        # sensitive-target refusal is not sufficient on its own: refuse every
        # symlink, escape and sensitive path before reading to prevent copy-out.
        if not _spec_path_is_safe(spec_path, agents_dir):
            continue
        # The hardened reader (size cap, AppleDouble/sensitive-symlink and
        # non-object refusal). This site also WRITES below: a spec the reader
        # refuses is now never rewritten at all, whereas the old read_text
        # path read -- and then rewrote -- whatever the file or link named.
        data = _read_agent_spec(
            spec_path,
            operation="migrate_agent_specs",
            source="unknown",
        )
        if data is None:
            continue
        if "model_managed" not in data and "cc_model" not in data:
            continue
        name = data.get("name") or spec_path.stem
        agent_state.lift_and_strip_bookkeeping(data, name)
        try:
            _atomic_json_write(spec_path, data)
            cleaned += 1
        except OSError as exc:
            logger.warning("Could not rewrite cleaned agent spec %s: %s", spec_path, exc)
    if cleaned:
        logger.info("Cleaned %d kiro agent spec(s) of KiroCrew bookkeeping keys", cleaned)
    return cleaned


def clear_model_pin(config: MutableMapping[str, object], name: str) -> None:
    """Drop *config*'s ``model`` pin and resume tracking the shipped default.

    The in-place half of "return this agent to the default model", shared by
    every caller that offers it, so the dashboard's Agent Templates editor and
    the CLI cannot drift on what clearing a model means (the same reason
    :func:`agent_state.lift_and_strip_bookkeeping` is shared by four writers).
    The caller persists *config* itself.

    Deliberately the ONLY way a spec's ``model`` becomes managed after install:
    ownership cannot be inferred from a spec's value, because a model an older
    build's propagation wrote and one the user typed in by hand are identical on
    disk. So this is driven by an explicit user action -- clearing the model in
    the editor, or ``kirocrew agent reset-model`` -- and never by a heuristic
    running behind the user's back on refresh.

    Ordering is benign in both directions: if the sidecar write lands and the
    caller's spec write does not, the next refresh resolves the still-pinned
    spec to the shipped default, which is what the user asked for; if the spec
    write lands and the sidecar write does not, the pin is gone and the resolver
    falls through to the global.
    """
    config.pop("model", None)
    agent_state.set_model_managed(name, True)


def _read_spec_capped(path: Path) -> dict | None:
    """Parse an agent spec through the hardened, SIZE-CAPPED read gate.

    ``agent_discovery._read_agent_spec`` is what that module documents as the one
    reader for both agent scopes: it reads via ``hooks.safe_read_file_bytes``, so
    a multi-gigabyte "agent config" in a user-writable, tool-shared directory is
    refused at the cap instead of being slurped into memory, and it also rejects
    non-UTF-8 bytes, AppleDouble sidecars and JSON that is not an object.

    A thin wrapper rather than a direct call at each site, so the reason the
    capped reader is used lives in one place.
    """
    return _read_agent_spec(path, operation="agent_spec_lookup", source="unknown")


def _spec_path_is_safe(path: Path, agents_dir: Path) -> bool:
    """True when *path* is a real file inside *agents_dir*, safe to read and rewrite.

    A spec is read and then written back, so a SYMLINK is refused rather than
    followed. Following one would read the target and write a modified copy into
    the agents directory, which launders the contents of a file the reader may
    not otherwise be allowed to open -- a governance-fenced path, for instance --
    into a location that is freely readable. (The rewrite itself does not corrupt
    the target: ``_atomic_json_write`` goes through ``os.replace``, which swaps
    the link rather than writing through it. The copy-out is the problem.)

    Also refuses a resolved path that leaves the agents directory, and any
    sensitive path, which is the same fence this module already applies before
    touching a resolved path elsewhere.
    """
    try:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        if resolved.parent != agents_dir.resolve():
            return False
        if is_sensitive_path(str(resolved)):
            return False
    except OSError:
        return False
    return True


def agent_spec_path(name: str) -> Path | None:
    """Return the user-level kiro spec file for *name*, or ``None`` if absent.

    Prefers ``<agents dir>/<name>.json`` and falls back to a scan for a spec
    whose ``name`` field matches, mirroring how the dashboard's per-agent
    handler resolves an agent to a file (a spec's filename and its ``name`` are
    not required to agree).

    *name* is validated against the shared agent-name grammar BEFORE it reaches
    the path join, so a caller passing a traversal (``../../something``) gets
    ``None`` rather than a path outside the agents directory. The check lives
    here, at the resolver, so every caller inherits it instead of each one
    remembering: this function returns a path that :func:`reset_agent_model`
    then WRITES, and the CLI takes the name from a user-supplied ``--agent``.
    A symlinked or otherwise unsafe candidate is refused for the same reason --
    see :func:`_spec_path_is_safe`.

    A DECLARED ``name`` wins over a matching filename, which is the order the
    other two resolvers already use (``_resolve_named_agent_model`` and the
    dashboard's per-agent handler both test ``data["name"] == agent`` before the
    stem). Preferring the filename would let ``<name>.json`` that declares a
    DIFFERENT agent be selected, and since the caller then writes to it, that
    clears the wrong agent's pin while the requested one stays pinned. The
    filename is accepted only when no spec declares this name -- see below.

    Raises ``ValueError`` when TWO safe specs declare the same name. The runtime
    iterates the directory unordered, so which of them is live is undefined, and
    a writer cannot pick without risking clearing the pin nothing is reading.
    """
    if not _AGENT_NAME_RE.match(name or ""):
        return None
    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return None

    direct = agents_dir / f"{name}.json"
    declared_matches: list[Path] = []
    fallback: Path | None = None
    for spec_path in sorted(agents_dir.glob("*.json")):
        if not _spec_path_is_safe(spec_path, agents_dir):
            continue
        try:
            data = _read_spec_capped(spec_path)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        declared = data.get("name")
        if declared == name:
            declared_matches.append(spec_path)
        elif spec_path == direct:
            # Right filename. Accepted as the fallback even when it declares a
            # DIFFERENT name, because the runtime resolver matches on
            # `data["name"] == agent OR path.stem == agent` -- so with nothing
            # declaring this name, the stem match makes THIS file the live spec,
            # and refusing it would leave a live pin unresettable, which is the
            # bug this change exists to fix. Only used when no declared match is
            # found, and a declared match alongside it is the ambiguity the
            # caller refuses rather than resolves.
            fallback = spec_path
    if len(declared_matches) > 1:
        # Paths are repr'd: a filename in this user-writable, tool-shared
        # directory is untrusted input, and this message is printed to a terminal.
        raise ValueError(
            f"{len(declared_matches)} specs declare the name {name!r}: "
            f"{', '.join(repr(str(p)) for p in declared_matches)}. The runtime iterates the "
            f"directory unordered, so which one is live is undefined -- remove or rename "
            f"one before resetting."
        )
    if declared_matches:
        return declared_matches[0]
    return fallback


def _conflicting_spec_for(name: str, chosen: Path, agents_dir: Path) -> Path | None:
    """Return a DIFFERENT safe spec whose FILENAME also claims *name*.

    The runtime resolver (``KiroCrewConfig._resolve_named_agent_model``) accepts
    EITHER a declared-name match or a filename match -- ``data["name"] == agent
    or path.stem == agent`` -- and iterates ``glob("*.json")``, which is
    unordered. So when ``<name>.json`` declares a different agent AND another
    file declares *name*, which of the two the runtime actually uses is
    UNDEFINED: it is whichever the filesystem yields first.

    A reset cannot pick correctly in that state. Clearing either one can leave
    the live pin in place and strip the model from a spec nothing is reading, so
    the caller refuses instead of guessing.
    """
    direct = agents_dir / f"{name}.json"
    if direct == chosen or not direct.is_file():
        return None
    if not _spec_path_is_safe(direct, agents_dir):
        return None
    return direct


def reset_agent_model(name: str) -> tuple[Path, str]:
    """Clear *name*'s spec model pin on disk; return (spec path, previous model).

    The explicit, narrow counterpart to ``kirocrew setup --clean``, which also
    resumes default-model tracking but regenerates the whole spec and discards
    every user customization with it. Raises ``FileNotFoundError`` when the
    agent has no user-level spec.
    """
    spec_path = agent_spec_path(name)
    if spec_path is None:
        raise FileNotFoundError(f"no kiro agent spec for {name!r} in {kiro_agents_dir_path()}")
    conflict = _conflicting_spec_for(name, spec_path, kiro_agents_dir_path())
    if conflict is not None:
        raise ValueError(
            f"two specs claim {name!r}: {str(spec_path)!r} declares it, and {str(conflict)!r} "
            f"carries the filename. The runtime accepts either, in unordered directory order, "
            f"so which one is live is undefined -- rename or remove one before resetting."
        )
    try:
        data = _read_spec_capped(spec_path)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"could not read agent spec {spec_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FileNotFoundError(f"agent spec {spec_path} is not readable as a JSON object")
    previous = data.get("model") or ""
    clear_model_pin(data, name)
    # Same strip every spec writer runs: kiro-cli validates with
    # deny_unknown_fields and drops the whole agent on an unknown key.
    agent_state.lift_and_strip_bookkeeping(data, name)
    _atomic_json_write(spec_path, data)
    return spec_path, str(previous)


def _decline_shared_agent_home(*, audit: bool = True) -> Path | None:
    """Return the spec path to report, WITHOUT writing, when this instance must
    not own the shared agent home; ``None`` when writing is safe.

    An **ephemeral** KiroCrew instance — one booted from a linked git worktree, or
    one running on its own isolated ``KIROCREW_HOME`` (a pod) — is throwaway by
    construction, but the agent specs it writes are not. ``rebuild_agent_config``
    stamps this instance's own ``.venv`` binary into every managed server's
    ``command`` and its own data home into their ``env``. Written into a spec the
    REAL install also reads, that makes the live gateway's MCP servers run this
    tree's code and read this instance's ``.local_secret`` while still calling the
    live gateway — every managed MCP request 403s (``learn_add``, ``spawn_run``,
    ``cron_*`` all die) — and tearing the instance down leaves those shared specs
    pointing at paths that no longer exist.

    Declining mirrors ``ensure_kirocrew_on_path``'s worktree guard: leave whatever
    already worked in place rather than repointing a shared resource at an
    ephemeral one.

    The predicate is deliberately **"is the target shared, and am I ephemeral"** —
    NOT "am I in a worktree", and NOT "is the target the hard-coded
    ``~/.kiro/agents``". Two bypasses of those narrower forms are closed here:

    * A **pod running from the primary checkout** is not in a linked worktree at
      all, yet ``pod down`` deletes its home and checkout venv, so it still leaves
      the machine-wide specs dangling. Pods therefore declare themselves via
      ``KIROCREW_POD`` (set in ``build_pod_env``) and that counts as ephemeral on
      its own. Note what is deliberately NOT used as the signal: merely *having*
      an isolated ``KIROCREW_HOME``. A CI test gateway (the offline E2E suite boots
      on a tmp data home) and a user who permanently relocated their data home are
      indistinguishable from a pod under that rule, and stopping either from
      writing its specs is a regression, not protection.
    * A globally exported ``KIRO_HOME`` moves the shared directory, so comparing
      against a hard-coded default reads "not the shared one" and waves the write
      straight through. The comparison is therefore against what the AMBIENT
      environment resolves right now (``ambient_agents_dir()``), which is by
      definition the directory every instance under this environment shares.
      Deliberately the override-BLIND resolver, not ``kiro_agents_dir()``: the
      latter follows ``config.paths._agents_dir_override``, so a redirect would
      move both sides of this comparison together, read as "target is the shared
      one", and refuse the write from any ephemeral checkout.

    A target is exempt only when it is **provably private**: either a caller
    redirected the write somewhere the ambient environment would never produce (a
    test's ``tmp_path``), or it is EXACTLY ``isolated_agents_dir(own data home)``
    — the dedicated ``<data home>/kiro/agents`` this instance's teardown owns.

    That second case is the *mechanism* by which a genuinely isolated instance will
    own its specs; it is NOT advice to set ``KIRO_HOME`` today. Nothing in this
    repo sets it (``build_pod_env`` deliberately does not) because it also
    relocates kiro-cli's session storage while KiroCrew still reads the host path
    — see ``kiro_home()``'s scope caveat. The exemption is matched exactly rather
    than by ancestry: "beneath the data home" reads the machine-wide
    ``~/.kiro/agents`` as private the moment the data home is an ancestor of it
    (``KIROCREW_HOME=$HOME`` is enough).
    """
    target = kiro_agents_dir_path().resolve()
    if target != ambient_agents_dir().resolve():
        # A caller pointed the write somewhere of its own choosing; nothing is
        # shared with the ambient install, so there is nothing to protect.
        return None

    own_home = _valid_override_home()
    if own_home is not None and target == isolated_agents_dir(own_home).resolve():
        # The one supported opt-in: the DEDICATED agents dir beneath this
        # instance's own data home, which its teardown owns. Matched exactly, not
        # by ancestry — "anywhere beneath the data home" reads the machine-wide
        # ~/.kiro/agents as private whenever the data home is an ancestor of it
        # (KIROCREW_HOME=$HOME suffices), handing an ephemeral instance the very
        # specs this guard protects. A different KIRO_HOME layout is refused
        # rather than guessed; the warning below names the supported path.
        return None

    # Ephemerality must be POSITIVE evidence that this instance is throwaway.
    # "Has an isolated KIROCREW_HOME" is NOT that: a CI test gateway and a user
    # who permanently relocated their data home both look identical under that
    # rule, and neither should be stopped from writing its own specs (an earlier
    # revision used it and broke the offline E2E gateway, which boots on a tmp data
    # home and then found no agents). A pod needs no arm here: ``build_pod_env``
    # gives it its own ``KIRO_HOME``, so its target is its own dedicated directory
    # and the private-target exemption above already lets it through.
    #
    # A checkout under the system temp directory is the third positive signal:
    # like a linked worktree and a pod, its teardown is a matter of WHEN, not
    # whether — temp trees are reaped by the OS, by CI, and by the automation
    # that cloned them (a per-task scratch clone is created and deleted around a
    # single job). A spec stamped from one names a launcher venv, and possibly a
    # pinned data home, that stop existing when the tree goes; #4781 documents
    # both live failure modes (ENOENT-dead managed servers, and empty-credential
    # ``internal_auth_mismatch`` when the pinned home is recreated empty). This
    # is checked on the CHECKOUT location (``__file__``), not the data home, so
    # the offline E2E harness — which runs the REPO checkout on a temp data
    # home — is unaffected, exactly the regression the note above records.
    #
    # An AppImage's runtime mount is carved back OUT of that arm. It sits under
    # the temp root (``/tmp/.mount_<name>XXXXXX``) and the mount itself is indeed
    # reaped on exit, but the temp signal's premise — a spec outliving the only
    # instance that would have written it — inverts here: a DURABLE install (the
    # ``.AppImage`` file on disk) stands behind the mount and re-runs this on
    # every start, and because the runtime picks a NEW random mount each launch,
    # rewriting the spec per start is the only way its managed servers ever
    # resolve. Declining would freeze the spec on a previous launch's mount path
    # and ENOENT every managed server — manufacturing #4781's own symptom on a
    # shipped channel — and on a fresh install would leave no spec at all
    # (``Mode 'kirocrew' not found``). ``_in_ephemeral_tree`` is the same
    # AppImage-precise predicate the launcher installer uses; the temp-root rule
    # it declines is the one being narrowed here, not adopted.
    #
    # The temp arm also declines only when there is something to preserve. This
    # guard's entire remedy is "use the specs that already worked" — the log line
    # below says exactly that — and with no spec present there are none, so
    # declining does not protect a shared resource, it just leaves the install
    # dead (every turn fails with ``Mode 'kirocrew' not found``). #4781's harm is
    # specifically an OVERWRITE of a working spec, which this still refuses: the
    # spec is present in every reported instance of it. A spec that exists but is
    # already stale stays stale, same as under the worktree arm — repairing it is
    # the durable install's job on its next start, and it rewrites unconditionally.
    # Deliberately scoped to this arm: the worktree and pod arms predate this fix
    # and their populations were chosen on their own grounds, so widening them is
    # a separate decision, not a side effect of adding a third signal.
    checkout = Path(__file__).resolve()
    temp_scratch = (
        _under_system_tmp(checkout)
        and not _in_ephemeral_tree(checkout)
        and (target / AGENT_FILENAME).exists()
    )
    ephemeral = _in_linked_git_worktree(checkout) or temp_scratch
    if not ephemeral:
        # A GRANT over the shared resource, so it is audited like the denial
        # below: every permission decision about the machine-wide agent home is
        # traceable from the audit log alone, matching how ``api_lessons_create``
        # records both its allow and deny branches. Only the shared-home decision
        # is recorded -- the two private-target returns above are not decisions
        # about a shared resource, so auditing them would add volume without
        # adding traceability.
        if audit:
            sel().log_api_access(
                caller="system",
                operation="agent_home_write",
                outcome="allowed",
                source="rebuild_agent_config",
                resources=str(target),
            )
        return None  # an ordinary install writing its own shared home

    if audit:
        logger.warning(
            "Refusing to rewrite the shared agent home %s from an ephemeral instance "
            "(checkout %s, data home %s): it would repoint the real install's MCP "
            "servers at this instance's venv and data home, and break them outright "
            "when it is torn down. This instance will use the existing specs instead. "
            "Deliberately no remedy is suggested here: redirecting the agent home via "
            "KIRO_HOME also relocates kiro-cli's session storage, which Kiro Crew still "
            "reads from the host path -- see kiro_home()'s scope caveat.",
            target,
            Path(__file__).resolve().parents[2],
            own_home or "default",
        )
        # This is a permission decision on a shared, security-relevant resource (the
        # specs carry every managed MCP server's command + env), so it belongs in the
        # audit trail and not only in the log: a silent refusal is indistinguishable
        # from a write that simply did not happen when reconstructing what an
        # ephemeral instance did to the host.
        sel().log_api_access(
            caller="system",
            operation="agent_home_write",
            outcome="denied",
            source="rebuild_agent_config",
            resources=str(target),
            error=(
                f"ephemeral instance (checkout {Path(__file__).resolve().parents[2]}, "
                f"data home {own_home or 'default'}) refused write to shared agent home"
            ),
        )
    return kiro_agents_dir_path() / AGENT_FILENAME


def _strip_ungoverned_auto_approve(servers: dict[str, Any]) -> dict[str, Any]:
    """Local alias so tests can monkeypatch one name (see governance)."""
    return dict(strip_ungoverned_auto_approve(servers))


def _seed_kas_permissions(config: dict[str, Any]) -> None:
    """Give the spec a KAS ``permissions`` block if it has none. Never edit one.

    Two things ride on this field, and the second is the surprising one:

    1. It is how the auto-approve list reaches the KAS backend at all, since
       ``allowedTools`` is a kiro-cli-only field there.
    2. Its mere PRESENCE is what makes KAS load this file. KAS classifies a JSON
       agent profile carrying kiro-cli-only fields and no KAS field as written
       for the other runtime and skips it outright — so without ``permissions``
       the agent is not among the modes KAS advertises, and anything that asks
       for it by name (a resumed session, notably) fails to find it.

    That second point is why an empty policy is still written when nothing
    qualifies for auto-approve: ``{"rules": []}`` says "no tool is
    pre-approved", which is both true and enough to keep the file loadable.
    Dropping the key instead would silently un-register the agent. (The wire
    projection makes the opposite choice and omits the field entirely — there,
    presence buys nothing and absence is the honest report.)

    **Seed, never refresh.** Once the key exists it belongs to whoever edits the
    file, and this function does not touch it again. The obvious alternative —
    recognising Crew's own output by its shape and regenerating that — was
    written first and removed: the shapes overlap (a blanket ``allow`` is exactly
    what a user writes too), so the rule that keeps a derived policy current is
    the same rule that silently overwrites a hand-written one, and losing a
    user's policy is the worse failure. What it costs is staleness: a policy
    written before ``allowedTools`` changed keeps describing the old list. That
    is bounded, because the wire projection derives afresh from ``allowedTools``
    on every session and outranks the file — the block on disk is what applies
    when Crew is NOT injecting an agent.
    """
    if config.get("permissions") is not None:
        return

    # Routed through the agent-sdk boundary: ``drivers.acp`` is the one layer
    # permitted to import ``kiro_crew.acp``, and agent.py's direct-import count
    # is a shrink-only baseline that must not grow. Function-local for the same
    # boot-path reason as every import here — this module has no business
    # dragging the ACP stack onto the gateway boot path just to write one JSON
    # field.
    from kiro_crew.agent_sdk.drivers.acp import (  # noqa: PLC0415 - boot path
        derived_agent_permissions,
    )

    config["permissions"] = derived_agent_permissions(config.get("allowedTools"), AGENT_FILENAME)


def _may_auto_approve(ref: str) -> bool:
    """Whether ``ref`` may go on an auto-approve list, per the governance ceiling.

    One-line delegate on purpose: the decision AND the ceiling resolution both
    live in ``platform.governance`` so the five writers of an ``allowedTools``
    list cannot drift apart. Kept as a named local so it is monkeypatchable in
    tests without reaching into another module's namespace.
    """
    return may_skip_gate_now(ref)


def _apply_allowed_tools_ceiling(config: dict, *, source: str) -> None:
    """Filter ``config["allowedTools"]`` through the governance ceiling, in place.

    ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    every entry on it must be approved by :func:`_may_auto_approve`. This runs
    inside :func:`build_agent_config` so every installer that derives a spec
    from the template inherits the filter — ``_install_research_agent`` shipped
    the template's ``fs_read``/``code``/``glob``/``grep`` grants verbatim
    because the filter lived only in ``rebuild_agent_config``'s final pass,
    which writes only ``kirocrew.json`` (#7401).

    A withheld ref stays MOUNTED (``tools`` is untouched — mounting a tool is
    not auto-approving it); its calls go through the gate, where the
    per-argument rule applies. Non-string entries (a hand-edited config) are
    dropped: they are not valid tool refs and would crash the predicate.

    Withholding a grant is a permission DECISION, so it leaves the same
    ``mcp_auto_approve_withheld`` SEL record every other ``allowedTools``
    writer emits — best-effort, never raising, so an audit failure cannot
    break a build or an install.
    """
    allowed = config.get("allowedTools")
    if not isinstance(allowed, list):
        return
    kept: list[str] = []
    withheld: list[str] = []
    for ref in allowed:
        if not isinstance(ref, str):
            continue
        (kept if _may_auto_approve(ref) else withheld).append(ref)
    config["allowedTools"] = kept
    if withheld:
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source=source,
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    "(governance ceiling); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the build
            logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)


def _ceiling_filtered_spec(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    """An app's MCP spec with a ceiling-governed ``autoApprove`` removed.

    ``autoApprove`` is a SECOND way to reach the same exemption ``allowedTools``
    grants, and a more direct one: kiro-cli approves an autoApproved MCP tool
    locally and emits no permission request, so ``hooks.on_tool_call`` — the deny
    floor, the sensitive-path check, the governance ceiling — never runs for it.
    ``agent.py``'s managed-server block states the rule for our own servers
    ("DELIBERATELY NO autoApprove KEY, and none may ever be added"); this applies
    it to app-contributed ones, which were copied verbatim.

    That verbatim copy meant the grant was declared by an app MANIFEST — content
    that can come from outside this repo — rather than by KiroCrew or the user.
    An app could hand itself a permanent gate exemption by adding three lines to
    its own JSON.

    Only the key is dropped, never the server: the app keeps its tools, they
    simply go through the approval gate, which is where a per-tool ceiling rule is
    actually applied. Unchanged on an ungoverned host, since ``may_skip_gate``
    permits everything when there is no ceiling.
    """
    if "autoApprove" not in spec:
        return spec
    if _may_auto_approve(f"@{mcp_server_alias(ref)}"):
        return spec
    spec.pop("autoApprove", None)
    logger.info(
        "Dropped autoApprove from app MCP server %s: the governance ceiling "
        "constrains it, so its tools go through the approval gate",
        ref,
    )
    # Revoking a gate exemption is a permission DECISION. This fallback drops the
    # grant before the final sanitizer can observe it, so without an event here
    # this would be the one withhold path with no audit trail. Mirror the
    # allowedTools writers' SEL event. Best-effort; never break a rebuild.
    try:
        sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_withheld",
            outcome="ok",
            source="_ceiling_filtered_spec",
            resources=(
                f"@{mcp_server_alias(ref)} autoApprove removed (governance ceiling); "
                "calls go through the approval gate"
            ),
        )
    except Exception:  # noqa: BLE001 — audit must not break the filter
        logger.debug("SEL audit unavailable for app autoApprove strip", exc_info=True)
    return spec


def _collect_app_mcp_servers() -> dict[str, Any]:
    """MCP servers contributed by ENABLED apps, keyed ``{app}:{server}``.

    App MCP servers are registered straight into this agent config rather than
    into the shared ``~/.kiro/settings/mcp.json``, because that file is read by
    everything else sharing ``~/.kiro`` — Kiro IDE and any other kiro-cli agent
    — so an app's private tools would leak into surfaces that never installed
    the app. KiroCrew sessions only ever read the agent config
    (``includeMcpJson`` is pinned False), so writing here is both sufficient and
    properly scoped.

    That makes the app manifests the authoritative source, which this function
    re-derives on every rebuild. Without it a ``clean=True`` rebuild would drop
    every app's servers: clean ignores the existing config, and the entries no
    longer exist in the global file to be re-mirrored from.

    Never raises — a broken app manifest must not stop the agent config from
    being written, or a single bad app would take down every session.
    """
    servers: dict[str, Any] = {}
    try:
        # Imported lazily: kiro_crew.apps imports back into agent/security, so a
        # module-level import here would close a cycle.
        from kiro_crew.apps.bridges import registered_app_mcp_servers
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled, list_apps
    except Exception:  # noqa: BLE001 — apps subsystem unavailable
        return servers

    try:
        apps = list_apps()
    except Exception:  # noqa: BLE001
        return servers

    # The LIVE registered map, not the manifest, is the source of truth for the
    # spec: for a `backend.port:"auto"` app the manifest carries an ILLUSTRATIVE
    # port and the reachable one is only known after the backend starts, at which
    # point reregister_app_mcp_servers writes the resolved URL here. Reading the
    # manifest instead would copy the illustrative (dead) port back over the live
    # one on every rebuild, and kiro-cli dials every server in the config — so the
    # app's tools would fail until the next reregister. The manifest is only the
    # fallback for a stdio/command server (no port to resolve); an HTTP server
    # with no live entry is SKIPPED, mirroring _register_mcp_servers' own refusal
    # to ever write a dead-port URL.
    registered = registered_app_mcp_servers()

    for app in apps:
        name = app.get("name") if isinstance(app, dict) else None
        if not name:
            continue
        try:
            if not is_app_enabled(name):
                continue
            manifest = get_app_manifest(name)
            if not manifest or not manifest.mcpServers:
                continue
            for server_name, spec in manifest.mcpServers.items():
                if not isinstance(spec, dict):
                    continue
                ref = f"{name}:{server_name}"
                live = registered.get(ref)
                if isinstance(live, dict):
                    chosen = dict(live)  # resolved live-port URL / pinned command
                elif spec.get("url"):
                    # An HTTP server's manifest URL is only illustrative when the
                    # GATEWAY launches the backend (backend.entryPoint set): the
                    # port is "auto"-resolved and unknown until the process starts,
                    # so with no live entry we skip rather than write a dead port
                    # (mirroring _register_mcp_servers' refusal to write one).
                    # A SELF-MANAGED HTTP server (no backend.entryPoint — e.g. an
                    # independent companion app on a fixed port) has an
                    # authoritative URL and never gets a live registration, so
                    # preserve the manifest URL instead of dropping the server.
                    if manifest.backend.entryPoint:
                        continue
                    chosen = dict(spec)
                else:
                    chosen = dict(spec)  # stdio/command: nothing to resolve
                servers[ref] = _ceiling_filtered_spec(ref, chosen)
        except Exception:  # noqa: BLE001 — one bad app must not poison the rest
            logger.warning("Skipping MCP servers for app %s (manifest error)", name)
            continue
    return servers


def _durable_tool_aliases(path: Path) -> tuple[bool, object]:
    """Read the ``toolAliases`` generation the spec ON DISK carries right now.

    The single reader behind both the pre-write reconcile and the ownership
    transition, so the value the record is fingerprinted against and the value
    written into the spec can never come from two different reads.

    Returns:
        ``(existed, aliases)`` -- *existed* is False when there is no readable spec
        (so there is no durable generation at all, which is NOT the same as a spec
        holding an invalid one); *aliases* is the raw value the spec carries, which
        :func:`_set_tool_aliases` and
        :func:`~kiro_crew.connections.alias_record.spec_fingerprint` both read as the
        absent generation when it is not a usable map.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (False, None)
    try:
        on_disk = json.loads(raw)
    except ValueError:
        return (True, None)
    return (True, on_disk.get("toolAliases") if isinstance(on_disk, dict) else None)


def _set_tool_aliases(config: dict, aliases: object) -> None:
    """Put *aliases* into *config*, removing the key when there is no usable map."""
    if isinstance(aliases, dict):
        config["toolAliases"] = aliases
    else:
        config.pop("toolAliases", None)


def _reconcile_tool_aliases_from_disk(path: Path, config: dict) -> bool:
    """Align ``config['toolAliases']`` with the generation the spec ON DISK carries.

    ``config`` is assembled from a spec read taken BEFORE the write lock, so its
    alias map can be a stale generation by the time the write happens. Two
    overlapping rebuilds serialize their spec writes but not that read: the second
    would otherwise write its pre-lock snapshot back, resurrecting aliases the
    first had removed, and would fingerprint a generation that is no longer there.

    So the map is re-read here, inside the critical section that writes it, and
    that value is what the alias pass resolves against (alias_record invariant 7).
    It runs whether or not the alias pass will: a gate-off or fail-closed rebuild
    would write the stale snapshot just the same. A CLEAN rebuild is the one
    exemption and the caller makes it -- clean regenerates from defaults, so
    importing the old spec's map would defeat the reset (see the call site).

    A MISSING spec is not an invalid one. With no file there is no durable
    generation to reconcile against, so the assembled map stands: a first install
    whose ``agent.json`` carries a hand-written ``toolAliases`` would otherwise
    have it erased before it was ever written. Only a spec that EXISTS decides the
    map -- a dict is imported, and an absent or non-dict value clears the key,
    because kiro-cli rejects the whole spec over a non-dict ``toolAliases`` and
    re-importing one would carry the broken file forward and cost the user every
    tool. Dropping it here repairs it even when the alias pass never runs.

    Uses the file being written rather than a fixed path, so it is correct on the
    canonical spec (where the caller holds the lock) and on any other spec the
    rebuild targets.

    Returns:
        True when a spec existed on disk and therefore decided the map; False when
        there was none and *config* was left exactly as assembled.
    """
    existed, aliases = _durable_tool_aliases(path)
    if existed:
        _set_tool_aliases(config, aliases)
    return existed


#: Generation of the ceiling the on-disk ``allowedTools`` was last derived under. Compared
#: for equality only, per ``governance_generation``'s contract.
_projected_ceiling_generation: int | None = None


def prime_ceiling_projection() -> None:
    """Record the ceiling generation boot projected the agent config under.

    Called once, before the central-distribution poller starts. Seeding here rather than on
    :func:`reproject_for_ceiling_change`'s first call is the difference between "nothing has
    changed since boot" and "nothing has changed since the first poll" — and the first poll
    can install a new ceiling, so the latter would record that generation and skip the very
    rebuild it needed.
    """
    global _projected_ceiling_generation
    from kiro_crew.platform.context import governance_generation

    _projected_ceiling_generation = governance_generation()


def reproject_for_ceiling_change() -> None:
    """Re-derive the on-disk ``allowedTools`` when the governance ceiling has moved.

    ``allowedTools`` is kiro-cli's blanket auto-approve list, and it is **materialised**: the
    five writers of it consult the ceiling when they write, and kiro-cli then reads the FILE.
    So a ceiling that comes to deny a tool mid-flight does not narrow a list already on disk,
    and every session started afterwards would keep auto-approving what the fleet now
    forbids — the tool short-circuits inside the harness and never reaches Kiro Crew's own
    PreToolUse gate.

    Registered as a post-install hook on the central-distribution refresher, alongside the
    tailnet revocation and for the same reason: before a live refresh existed the ceiling
    only changed at boot, and boot projects the config anyway.

    **Bounded to an actual change.** Hooks run on every confirming poll, so an unconditional
    rebuild would rewrite a file kiro-cli watches every refresh interval, for nothing. The
    baseline is seeded by :func:`prime_ceiling_projection` BEFORE the poller starts, not on
    the first call: the first poll can itself install a new ceiling, and a first-call baseline
    would record that generation and skip the very rebuild it needed.

    **The memo advances only after a successful rebuild.** A failure raises through the hook
    runner, which logs it and moves on — and if the generation had already been marked
    synchronised, the retry the next poll would otherwise give us is lost, leaving forbidden
    auto-approvals on disk for the process lifetime.

    An unseeded baseline rebuilds once on the first call rather than skipping, which is the
    safe direction: a redundant rewrite costs a file write, a skipped one costs the tighten.

    What this cannot do is narrow a session ALREADY negotiated: kiro-cli holds the grants it
    was given, and no policy mechanism reaches into a running one. That limit is the same
    shape as an already-running process keeping its own sandbox, and a restart is its only
    answer — which is why removing live refresh would not close it either.
    """
    global _projected_ceiling_generation
    from kiro_crew.platform.context import governance_generation

    generation = governance_generation()
    if _projected_ceiling_generation == generation:
        return
    logger.info("the governance ceiling moved; re-deriving the agent config's auto-approvals")
    rebuild_agent_config()
    _projected_ceiling_generation = generation


def rebuild_agent_config(*, clean: bool = False) -> Path:
    """Rebuild and write the merged kirocrew.json to ~/.kiro/agents/.

    This is the single authoritative function for producing the agent config.
    It reads all source files, merges with correct priority, resolves commands,
    and injects fresh AIM skill paths.

    Merge priority (highest wins):
      1. ~/.kiro/crew/mcp.json (agent-specific overrides)
      2. ~/.kiro/settings/mcp.json (kiro global, fills gaps)
      3. Existing kirocrew.json (preserves user customizations)
      4. Bundled defaults (security, managed servers)

    --skill-paths are always resolved fresh from AIM manifests regardless
    of what any source file contains.

    When the config already exists and *clean* is False, the existing file
    is used as the base so that **all** user customizations are preserved.
    Only security-critical ``hooks`` and dynamic fields (``prompt`` URI,
    kirocrew MCP server commands) are refreshed from defaults.

    Args:
        clean: If True, ignore existing config and regenerate from defaults.
    """
    declined = _decline_shared_agent_home()
    if declined is not None:
        return declined

    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = kiro_agents_dir_path() / AGENT_FILENAME

    # One-time (idempotent) self-heal: strip KiroCrew bookkeeping keys from
    # every kiro agent spec into the sidecar so kiro-cli accepts them all.
    migrate_agent_specs()

    # Managed MCP sync happens after config is fully built (see below).

    # One spec-gate snapshot for the whole rebuild, so the emit path below and the
    # withhold audit near the end describe the SAME decision (see
    # _gated_off_servers).
    gated_off = _gated_off_servers()

    if not clean and path.exists():
        # Existing config — preserve user customizations, only refresh
        # security-critical and dynamic fields.
        config, fresh_install = _load_existing_config(path, gated_off=gated_off)
    else:
        config = build_agent_config(gated_off=gated_off)
        fresh_install = True

    # Seed default-model tracking for a fresh/clean build. A clean regen always
    # resumes tracking the shipped default; a first-time install seeds tracking
    # only when the sidecar has no prior (possibly frozen) choice to preserve.
    main_name = config.get("name") or _MAIN_AGENT_NAME
    if fresh_install and (clean or agent_state.get_model_managed(main_name) is None):
        agent_state.set_model_managed(main_name, True)

    # Merge shared MCP servers from ~/.kiro/settings/mcp.json (Kiro user-level
    # config) FIRST.  KiroCrew is kiro-first (ACP/kiro-cli only), so Kiro
    # global OUTRANKS the Claude Code global on collisions — setdefault makes
    # the first writer win.  Skip managed servers — their command/args are set
    # by _refresh_dynamic_fields() and must not be overwritten by stale global
    # entries.  Write-through is never done here (KiroCrew reads globals but
    # never mutates them).
    #
    # NOTE: this reverses the prior "CC global wins over Kiro global" rule.
    # See docs/architecture/mcp.md. CC global is kept only as a gap-filler so
    # the Claude Code provider can be re-enabled later without rework; it must
    # not shadow a Kiro-global entry.
    managed_names = set(_MANAGED_MCP_SERVERS)

    # App-contributed MCP servers go in FIRST so an app's namespaced entry
    # outranks any same-named leftover in the shared global file (every loop
    # below uses setdefault, so whatever lands here wins). Re-derived from the
    # enabled apps' manifests on every rebuild, which is what lets a clean
    # rebuild keep them — see _collect_app_mcp_servers for why apps don't write
    # the global file at all.
    #
    # ASSIGNMENT, not setdefault, for the app's own key. The manifests are the
    # authoritative source and this re-derives them, so `setdefault` kept
    # whatever the PREVIOUS rebuild wrote: a spec whose `autoApprove` this pass
    # had just stripped (the ceiling now governs that server) lost to the stale
    # grant, the tightening never reached an existing config, and those tools
    # kept skipping the PreToolUse gate.
    for _app_srv, _app_spec in _collect_app_mcp_servers().items():
        if _app_srv not in managed_names:
            config.setdefault("mcpServers", {})[_app_srv] = _app_spec
            # EXPOSE it: kiro-cli connects entries declared in `mcpServers`, but
            # an unreferenced server contributes no tools to the agent. `tools`
            # is the unconditional exposure list (the final
            # dedup below removes any duplicate); auto-approve stays governed —
            # the spec's `autoApprove` was already ceiling-filtered in
            # _collect_app_mcp_servers, and the final allowedTools pass covers the
            # @ref if it ever lands there.
            config.setdefault("tools", []).append(f"@{_app_srv}")

    shared_mcp = _load_json(_KIRO_MCP_JSON).get("mcpServers", {})
    for name, spec in shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy so config never aliases the source dict — a later update()
            # (kirocrew merge) must not mutate shared_mcp, which is reused as a
            # fallback candidate during command validation below. The copy also
            # drops our authorship marker: it records who wrote the entry in a
            # SHARED file and has no meaning in a spec we render ourselves, so
            # keeping it would put a key in front of the runtime that says nothing
            # to it.
            config.setdefault("mcpServers", {}).setdefault(name, without_marker(spec))

    # Merge shared MCP servers from edition-contributed provider globals (CPP
    # seam) — now LOWER priority than Kiro global; setdefault is a no-op when
    # Kiro already populated the same key, so these only fill gaps. In OSS the
    # seam is empty, so NO provider global (e.g. ~/.claude.json) is merged —
    # keeping rebuild symmetric with discovery + apply/uninstall so a server the
    # dashboard can't see is never re-merged into sessions. A companion
    # contributes its Claude Code scope here and manages it end-to-end.
    # ``extra_shared_mcp`` accumulates the raw per-scope entries (first scope
    # wins) for the fallback-candidate lookup and shared-server tools sync below
    # (replaces the old single ``cc_shared_mcp``).
    extra_shared_mcp: dict[str, dict] = {}
    for scope_global in _extra_mcp_scope_globals():
        scope_shared_mcp = _load_json(scope_global).get("mcpServers", {})
        for name, spec in scope_shared_mcp.items():
            if not isinstance(spec, dict):
                continue
            extra_shared_mcp.setdefault(name, spec)
            if name not in managed_names:
                # Copy (see note above) so the source dict stays pristine for
                # the fallback-candidate lookup.
                config.setdefault("mcpServers", {}).setdefault(name, without_marker(spec))

    # ~/.kiro/crew/mcp.json overrides kiro mcp.json for the kirocrew agent —
    # kirocrew-specific config wins in a tie.
    # Uses update() to merge into existing specs, preserving user-set fields
    # like autoApprove while letting kirocrew's command/args/env win.
    # Skip managed servers for the same reason as above.
    kirocrew_mcp = _load_json(_user_dir() / "mcp.json").get("mcpServers", {})
    for name, spec in kirocrew_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            mcps = config.setdefault("mcpServers", {})
            if name in mcps and isinstance(mcps[name], dict):
                # mcps[name] is a private copy (globals were copied in above),
                # so update() does not mutate any source dict.
                mcps[name].update(spec)
            else:
                mcps[name] = dict(spec)

    # Resolve MCP commands to absolute paths and validate.
    #
    # Resolution-aware fallback: a server can be defined in several sources
    # with different commands.  If the merged winner's command does not
    # resolve (e.g. a bare command whose binary isn't on the rebuild PATH —
    # the classic internal-MCP-server shadowing case), fall back to the SAME server's
    # spec from the other sources before dropping it, in priority order
    # (kirocrew > kiro-global > provider-global).  This prevents one source's
    # unresolvable command from killing a server another source can resolve.
    def _resolve_command(cmd: str, env: dict | None) -> tuple[str | None, str]:
        """Resolve an MCP command to an absolute path, plus the path searched.

        Returns ``(resolved_or_None, search_path)``. The second element is what
        lets the drop warning name the directories actually consulted; it is ""
        when no PATH search happened (empty command, or an absolute command
        accepted directly).

        Accepts an absolute path directly when the file exists and is
        executable — shutil.which can fail inside user-namespace sandboxes
        even when the file is fine.

        Searches the server's own env.PATH first, then the contributed MCP
        directories, then the same augmented PATH the MCP probe uses — all via
        :func:`mcp_search_path`, so resolution, the probe and the rewriter all
        agree. A divergence would let a server probe healthy
        on the dashboard while being silently dropped from the generated agent
        config ("command not found: kirocrew"). The value EMITTED into the spec
        is :func:`spec_env_path` instead, which omits the contributed
        directories: an emitted PATH is persisted and read back as an authored
        entry, so a contributed directory written there could never be removed
        again. augmented_path
        covers ~/.aim/mcp-servers and ~/.toolbox/bin and appends the running
        interpreter's console-scripts dir
        (venv ``Scripts\\`` on Windows, ``bin/`` on POSIX) as a last-resort
        fallback for pip-generated wrappers like ``kirocrew``.
        """
        if not cmd:
            return None, ""
        if os.path.isabs(cmd) and os.path.isfile(cmd) and os.access(cmd, os.X_OK):
            return cmd, ""
        # Case-insensitive PATH key: a Windows-authored spec says "Path", and
        # resolving against a DIFFERENT path than the emitted spec carries would
        # reopen the probe/session split from the other side.
        _env = env or {}
        _key = spec_path_key(_env)
        _declared = _env.get(_key, "") if _key else ""
        _search = mcp_search_path(_declared if isinstance(_declared, str) else "")
        # A command carrying a directory component is not PATH-searched:
        # ``shutil.which`` returns before it reads ``path=`` when
        # ``os.path.dirname(cmd)`` is truthy, checking exactly the one location
        # the command names. Reporting ``_search`` for it would send the reader
        # to audit directories that were never consulted, which is the opposite
        # of the not-installed/installed-elsewhere distinction this path draws --
        # so return "" as the searched path even though the lookup still runs.
        if os.path.dirname(cmd):
            return shutil.which(cmd, path=_search), ""
        # The search path is returned, not recomputed by the caller: a candidate
        # that declares its own ``env.PATH`` is searched against a DIFFERENT path
        # than one that does not, so a caller reporting ``mcp_search_path("")``
        # would name directories that were never searched.
        return shutil.which(cmd, path=_search), _search

    valid_servers: dict[str, Any] = {}
    # The store is keyed by its own RAW name, but ``name`` below iterates the
    # config, whose slashed keys a previous pass rewrote to their alias
    # (``_normalize_mcp_server_keys``). Looking the store up by the raw key alone
    # would miss the owner of an aliased entry and fall through to "unmanaged",
    # preserving the previously-rendered wire hints -- so an edit that cleared them
    # would answer 200 and never take effect. Alias-keyed for that reason, and the
    # mapping skips a malformed value for the same reason the merge does.
    #
    # A LIST per alias, not one entry: the mapping is many-to-one, so two store
    # names can share an alias. Keeping only the last would strip the other server
    # of its owner entirely, and the identity check below would then read it as
    # unmanaged rather than simply looking at the next candidate.
    _store_by_alias: dict[str, list[dict]] = {}
    for _n, _s in kirocrew_mcp.items():
        if isinstance(_s, dict):
            _store_by_alias.setdefault(mcp_server_alias(_n), []).append(_s)
    _cfg_servers: dict[str, Any] = config.get("mcpServers", {})
    # One spelling of the scope chain, in priority order, for BOTH consumers below:
    # the live-value probe that keeps a rebuild-authored field re-derivable, and the
    # resolution candidate list. The probe's correctness is "this is the value the
    # chain would have resolved", so two separate spellings could drift apart.
    _scopes: tuple[tuple[str, dict], ...] = (
        ("kirocrew", kirocrew_mcp),
        ("kiro-global", shared_mcp),
        ("provider-global", extra_shared_mcp),
    )
    for name, spec in _cfg_servers.items():
        if not isinstance(spec, dict):
            continue
        # This file is BOTH this function's output and, here, one of its inputs: the
        # entry is read back so a field the user set and we never model survives. One
        # field below is ours, not the user's -- the resolved absolute ``command`` --
        # and reading our own computed value back as if it were authored is what made
        # it permanent: ``_resolve_command`` takes an absolute path without searching,
        # so no later change to how commands resolve could rebind one stored once.
        #
        # The record applies ONLY to a server no other source declares -- the one
        # whose sole persisted home is this file, and which therefore has nothing to
        # lose a conflict to. For a scope-owned server, choosing between the record
        # and the live declaration correctly means selecting a per-field source AFTER
        # resolution (the merge picks a winner by which command resolves, then adopts
        # that winner's args/env as a unit), which is a merge-precedence change rather
        # than a provenance one; it is tracked separately. Excluding that population
        # leaves it behaving exactly as it does today.
        #
        # The test is deliberately CONSERVATIVE, and compares by alias rather than by
        # raw key. A scope keys entries by their own raw name while ``name`` here is
        # the config's, whose slash-containing spellings an earlier pass rewrote to
        # aliases (see the ``_store_by_alias`` note above), so a raw-key probe would
        # miss the owner of an aliased entry and wrongly read it as having no other
        # home. Over-matching only declines to apply the record -- today's behavior,
        # and safe. Under-matching would let the record shadow a live declaration.
        #
        # A scope owns the COMMAND only when it actually supplies one. A dict alone is
        # not enough: a same-named URL-only entry, or an empty one, declares nothing
        # about ``command``, so treating it as a competing source would strip the
        # record off an agent-only stdio server and strand its stale path forever.
        # A non-dict value supplies nothing either, and the candidate chain below
        # skips it for the same reason (``isinstance(alt, dict)``).
        #
        # This stays a yes/no ownership question -- does any other source declare a
        # command? -- and never a choice BETWEEN two declared values. Choosing would
        # need the after-resolution ordering this PR is scoped out of.
        _alias_here = mcp_server_alias(name)
        _scope_owned = any(
            any(
                mcp_server_alias(k) == _alias_here
                and isinstance(v, dict)
                and isinstance(v.get("command"), str)
                and v["command"]
                for k, v in _scope.items()
            )
            for _label, _scope in _scopes
        )
        # Captured BEFORE the view rewrites anything: what the entry carried on the
        # way in, and the record's own pair. Together they decide what may be
        # recorded on the way out -- see the emit site below.
        _owned_in = command_is_ours(spec) if not _scope_owned else False
        _pre_cmd = spec.get("command")
        _pair = recorded_source(spec)
        # PRESERVED, never stripped. A scope-owned server's record is not acted on --
        # no restoration, see above -- but destroying it would be a decision in its
        # own right, and the wrong one: a scope command that does not resolve, or a
        # scope entry that later goes away, would leave the server agent-only again
        # with its only re-derivation source deleted, so a relocated binary could
        # never rebind. Keeping it inert costs nothing, because the ownership guard
        # re-checks the emitted value before anything is ever restored from it, so a
        # record that has gone stale in the meantime is simply not used.
        #
        # Deciding this by which candidate WINS resolution would be the other way to
        # rule out a non-resolving scope command, and that is the after-resolution
        # per-field source selection this change is deliberately scoped out of; it
        # belongs with the agent-config ownership work. Preserving is the part that
        # needs no ordering at all.
        _keep_record: tuple[str, str] | None = _pair if _scope_owned else None
        if not _scope_owned:
            _viewed = source_view(spec)
            if not _owned_in or _pair is None:
                # Nothing of ours to restore; the view only strips the key.
                spec = _viewed
            elif _resolve_command(_pair[0], _viewed.get("env"))[0]:
                # The source still resolves, so re-derive from it: that is the whole
                # point, and it is what rebinds a moved binary.
                spec = _viewed
            else:
                # It does NOT resolve, and for this population the emitted config is
                # the entry's only copy -- so re-deriving would drop the server from
                # the map, the file would be rewritten without it, and the next
                # rebuild would have nothing to read. A stale-but-working command
                # beats a deleted server, so keep what we emitted.
                #
                # The record is re-recorded VERBATIM below rather than refreshed from
                # the value we kept: re-deriving must stay possible once whatever
                # broke the source clears, and recording the emitted path as its own
                # source would retire the record's only useful fact.
                _keep_record = _pair
                spec = {k: v for k, v in spec.items() if k != DERIVED_KEY}
        # Remote Streamable HTTP servers — preserved as-is except for the OAuth
        # hints, which are renamed to the fields kiro-cli actually deserializes.
        # This is the one boundary where the internal spelling (``scopes`` /
        # ``clientId``, what mcp.json and the UI use) becomes the wire spelling,
        # so every source file keeps one shape and only the emitted spec changes.
        #
        # The dashboard store's own entry answers both ownership and source. A
        # usable dict means the store owns this name and states its hints (in
        # either spelling -- the scope-toggle preservation rule copies a global
        # spec in verbatim, so a store entry can legitimately hold wire form).
        # Anything else -- absent, or a malformed value the merge above skipped
        # and which therefore supplied nothing -- means we own nothing here, and
        # the entry's own wire values are the only copy of configuration written
        # in a file we do not control.
        if spec.get("url"):
            # An entry with no store owner is unmanaged: its own wire values are
            # the only copy of configuration written in a file we do not control,
            # so they are preserved verbatim. That includes a server defined only
            # in the agent config itself (``kiro-cli mcp add --agent kirocrew``, a
            # hand-edit) -- the rebuild merges onto that file, so clearing its
            # hints here would destroy the only copy. Narrowing a grant is the
            # editor's job, where the change is explicit and reversible.
            # A malformed store value contributes nothing -- and "nothing"
            # includes no veto over the alias lookup, so it cannot shadow a
            # usable slashed owner that aliases onto this name. It still does not
            # confer ownership: a name with no usable entry anywhere stays
            # unmanaged, because the fallback yields ``None`` too.
            #
            # ``mcp_server_alias`` is many-to-one, so an alias match is NOT an
            # identity match: an unrelated user-owned name can collide with a
            # managed one. A binding that GRANTS -- these hints are credentials
            # and requested access -- therefore also demands transport identity,
            # or one server's grant lands on another's. (The disabled guard below
            # is the opposite direction and stays name-only on purpose: see there.)
            _store_entry = kirocrew_mcp.get(name)
            if not isinstance(_store_entry, dict):
                _url = spec.get("url")
                _candidates = [
                    c
                    for c in _store_by_alias.get(mcp_server_alias(name), ())
                    if c.get("url") == _url
                ]
                # Nothing in a name says whether normalization minted it or a user
                # typed it, and a url is not an identity when two owners share one.
                # So the collision family is searched only with corroboration that
                # a mint was actually forced -- the plain alias is held by a
                # DIFFERENT transport -- and only when exactly one owner answers.
                # An ambiguous or uncorroborated family leaves the entry unmanaged,
                # because preserving a grant costs less than moving one.
                if not _candidates:
                    _base = _alias_family_base(name)
                    _held = _cfg_servers.get(_base)
                    if _base != name and isinstance(_held, dict) and _held.get("url") != _url:
                        _candidates = [
                            c for c in _store_by_alias.get(_base, ()) if c.get("url") == _url
                        ]
                _store_entry = _candidates[0] if len(_candidates) == 1 else None
            valid_servers[name] = kiro_oauth_wire_entry(spec, store_entry=_store_entry, server=name)
            continue
        # Build candidate specs in priority order: the merged winner first,
        # then the same server from each source as a resolution fallback.
        candidates: list[tuple[str, dict]] = [("winner", spec)]
        for label, src in _scopes:
            alt = src.get(name)
            if isinstance(alt, dict) and alt is not spec:
                candidates.append((label, alt))

        resolved: str | None = None
        chosen: dict = spec
        tried: list[str] = []
        searched: list[str] = []
        had_any_command = False
        for label, cand in candidates:
            cmd = cand.get("command", "")
            if cmd:
                had_any_command = True
            r, cand_search = _resolve_command(cmd, cand.get("env"))
            if cand_search:
                searched.append(cand_search)
            tried.append(f"{label}={cmd or '<none>'}{' -> ok' if r else ''}")
            if r:
                resolved = r
                chosen = cand
                break

        if resolved:
            # Start from the merged winner so user-set NON-command fields
            # (autoApprove, disabled, ...) are preserved.  When we fall back to
            # a *different* source, adopt that source's command/args/env as a
            # unit (args belong with their command) — drop the winner's stale
            # args/env so we never pair one source's command with another's
            # args.
            merged = dict(spec)
            merged["command"] = resolved
            if chosen is not spec:
                merged.pop("args", None)
                merged.pop("env", None)
                if "args" in chosen:
                    merged["args"] = chosen["args"]
                if "env" in chosen:
                    merged["env"] = chosen["env"]
            # A declared env.PATH replaces the child's PATH rather than
            # extending it, so emit the full effective one via the shared
            # normalization point (see emit_env / spec_env_path). emit_env
            # returns a fresh dict: ``dict(spec)`` is shallow, so the env dict
            # here is still the source config's own and must not be mutated
            # through.
            spec_env = merged.get("env")
            if isinstance(spec_env, dict):
                merged["env"] = emit_env(spec_env)
            # Record only for a server with no other source, and only a field this
            # pass may honestly claim: one the record already proved ours on the way
            # in, or one whose emitted value DIFFERS from what the entry carried, so
            # we computed it.
            #
            # The excluded case is a value we merely passed through -- a hand edit,
            # or an already-absolute declaration nothing was derived from. It survives
            # this rebuild either way, but a record written over it would read as
            # proof on the NEXT pass, which is how a claim we never earned turns into
            # a value we overwrite. Unrecorded means it stays the user's.
            #
            # Read from the candidate that WON: on a fallback the command came from
            # ``chosen``, so recording ``spec``'s would name a source this entry was
            # not derived from. ``None`` records "the source carried no such field".
            _derived: tuple[str, str] | None = _keep_record
            if _keep_record is None and not _scope_owned and (_owned_in or resolved != _pre_cmd):
                _cmd_source = chosen.get("command")
                # Non-empty by construction -- ``resolved`` is truthy, and it came
                # from resolving THIS candidate's command -- but assert it in the
                # type rather than in a comment: a record whose source is blank is
                # unreadable on the way back, so writing one would silently disable
                # the fix instead of failing here.
                if isinstance(_cmd_source, str) and _cmd_source:
                    _derived = (_cmd_source, resolved)
            valid_servers[name] = record_derived(merged, _derived)
        elif not had_any_command:
            # No candidate defined a command at all — distinct from a command
            # that was defined but couldn't be resolved.
            logger.warning("Dropping MCP server %r: no command", name)
        else:
            # The searched directories belong in the WARNING, not only at DEBUG:
            # a default-level reader is exactly who needs to tell "installed
            # somewhere this path does not cover" from "not installed at all".
            # Built from the paths the candidates were ACTUALLY searched against
            # and deduped -- a candidate declaring its own env.PATH is searched
            # against a different path, so recomputing one here would name
            # directories that were never consulted. The candidate list stays at
            # DEBUG: that is about which spec won, not about why none resolved.
            if searched:
                logger.warning(
                    "Dropping MCP server %r: command not found: %s — %s; %s",
                    name,
                    spec.get("command", ""),
                    describe_search_path(dedup_path(os.pathsep.join(searched))),
                    MCP_PATH_HINT,
                )
            else:
                # No candidate was PATH-searched (e.g. every command carries a
                # directory component, which shutil.which looks up directly).
                # ``describe_search_path("")`` would render "searched no
                # directories (empty PATH)" and blame a PATH that was never
                # consulted, so omit the clause instead.
                logger.warning(
                    "Dropping MCP server %r: command not found: %s; %s",
                    name,
                    spec.get("command", ""),
                    MCP_PATH_HINT,
                )
            logger.debug("MCP %r resolution failed; tried %s", name, "; ".join(tried))
    config["mcpServers"] = valid_servers

    # Rewrite slash-containing server keys to kiro-safe aliases (also migrates
    # already-broken configs); runs after merges so global-only servers and
    # their stale @refs are normalized too. See mcp_server_alias.
    _normalize_mcp_server_keys(config)

    # Drop any server whose argv invokes the deleted mcp-playwright-proxy
    # subcommand.  Runs on EVERY rebuild because the entry can be
    # re-injected from ~/.kiro/crew/mcp.json by the merges above.  The
    # first-run marker-guarded purge (clean_stale_managed_mcp) covers the
    # GLOBAL ~/.kiro/settings/mcp.json, which is a different file and a
    # different ownership boundary; this covers the assembled agent config.
    purge_deleted_proxy_from_config(config)

    # Sync shared (user-installed) servers to tools/allowedTools.
    # These are explicitly installed by the user via `aim mcp install` or
    # manual mcp.json edits — unlike managed servers, they should always
    # be registered regardless of fresh/existing config state.
    #
    # ``kirocrew_mcp`` is in this chain for the same reason: it holds every entry
    # the user added through the dashboard, including Connections providers. It
    # was omitted originally, and because ``tools`` is a CLOSED allowlist (no
    # wildcard) the result was silent and total — kiro-cli mounted a connected
    # provider and exposed none of its tools, so a fully consented Notion
    # connection still answered "I don't have a Notion integration". The entry
    # reached ``mcpServers`` (via the merges above) but never ``tools``.
    _shared_added: list[str] = []
    _shared_removed: list[str] = []
    _shared_not_auto: list[str] = []
    # ``disabled`` is TIGHTEST-WINS across scopes, because the scopes disagree by
    # design: ``POST /api/mcp/toggle enabled:false`` writes ``disabled: true``
    # into the kiro global ONLY, so a same-named dashboard-store entry legitimately
    # carries no such key -- and this chain visits the store LAST. Judging each
    # spec in isolation would let that final entry undo the earlier removal,
    # clear the flag off the emitted spec, and re-add the ref to BOTH lists.
    # ``allowedTools`` is the one path that never reaches the PreToolUse gate, so
    # the operator's disable would be silently void for every tool on that server.
    #
    # Both sides are keyed by the ALIAS, not the raw key, because that is the
    # identity the emitted ref carries and the mapping is many-to-one: a slashed
    # global key and a slash-free store key are different dict keys that mount the
    # same ``@ref``. Comparing raw keys would let the alias-spelled entry look
    # like a different server and re-add the ref the disable just removed.
    #
    # Unlike the OAuth-hint binding above, this match deliberately does NOT also
    # demand transport identity. The two run in opposite directions: over-matching
    # here only over-disables -- an availability cost, no privilege gained --
    # while under-matching would let an operator's disable be missed on the one
    # path (``allowedTools``) that never reaches the PreToolUse gate. Denying is
    # allowed to be loose; granting is not.
    _disabled_anywhere = {
        mcp_server_alias(srv)
        for scope in (extra_shared_mcp, shared_mcp, kirocrew_mcp)
        for srv, srv_spec in scope.items()
        if isinstance(srv_spec, dict) and srv_spec.get("disabled")
    }
    # A server the probe has failed N consecutive times is COUNTED and surfaced,
    # but not unmounted here. The unmount has no safe lever in this file: the
    # generated agent config is simultaneously the mount decision and the only
    # home for agent-only configuration, so dropping an entry destroys whatever
    # lives only there and stamping ``disabled`` makes ``list_servers`` delete the
    # server's own row. See the follow-up issue linked from
    # docs/system-specs/features/mcp-probe-quarantine.md.
    for name, spec in itertools.chain(
        extra_shared_mcp.items(), shared_mcp.items(), kirocrew_mcp.items()
    ):
        if not isinstance(spec, dict) or name in managed_names:
            continue
        alias = mcp_server_alias(name)
        ref = f"@{alias}"
        if spec.get("disabled") or alias in _disabled_anywhere:
            for key in ("tools", "allowedTools"):
                lst = config.get(key)
                if lst is not None and ref in lst:
                    lst.remove(ref)
                    if ref not in _shared_removed:
                        _shared_removed.append(ref)
        elif alias in valid_servers:
            valid_servers[alias].pop("disabled", None)
            # `tools` is what MOUNTS the server; `allowedTools` additionally
            # auto-approves it — and auto-approve is the one path that never
            # reaches the PreToolUse gate. So a server the enterprise ceiling has
            # an opinion about is mounted but NOT auto-approved: its calls go
            # through the gate, which applies the per-tool rule with the real
            # arguments. Without this the ceiling was un-enforceable for every
            # user-installed MCP server on the primary agent — the same bypass
            # that was closed for app agents, at the second of the two places
            # that write such a list. One predicate serves both.
            keys = ("tools", "allowedTools") if _may_auto_approve(ref) else ("tools",)
            for key in keys:
                if ref not in config.get(key, []):
                    config.setdefault(key, []).append(ref)
                    if ref not in _shared_added:
                        _shared_added.append(ref)
            if "allowedTools" not in keys:
                lst = config.get("allowedTools")
                if lst is not None and ref in lst:
                    # A grant written before the ceiling arrived must not survive it.
                    lst.remove(ref)
                if ref not in _shared_not_auto:
                    _shared_not_auto.append(ref)
    if _shared_added:
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_added)} added to tools/allowedTools (shared)",
        )
    if _shared_not_auto:
        # Its own SEL record: "mounted but not auto-approved" is a governance
        # outcome an operator has to be able to see, and it is invisible in the
        # added/removed pair (the ref still shows as added, to `tools`).
        sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_withheld",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{', '.join(_shared_not_auto)} mounted without auto-approve "
                f"(governance ceiling); calls go through the approval gate"
            ),
        )
    if _shared_removed:
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_removed)} removed from tools/allowedTools (disabled)",
        )

    # On fresh installs, ensure managed MCP tools are in tools (but NOT
    # allowedTools — new MCPs may have destructive tools; user opts in).
    # On existing configs, don't touch tools/allowedTools — user controls those.
    if fresh_install:
        added_refs: list[str] = []
        # Managed servers + edition-contributed servers both get their @ref
        # registered so their tools are callable. Edition servers are injected
        # into config['mcpServers'] via _extra_mcp_servers() above; their @ref
        # must also be added to config['tools'], otherwise kiro-cli exposes the
        # server but not its tools. The public edition contributes none, so this
        # is a no-op there.
        _register_names = list(_MANAGED_MCP_SERVERS) + [
            n for n in _extra_mcp_servers() if n not in _MANAGED_MCP_SERVERS
        ]
        for mcp_name in _register_names:
            ref = f"@{mcp_name}"
            if mcp_name in valid_servers and ref not in config.get("tools", []):
                config.setdefault("tools", []).append(ref)
                added_refs.append(ref)
        if added_refs:
            sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="install_agent",
                resources=f"{', '.join(added_refs)} added to tools (fresh install)",
            )

    # Narrow ADD-only exception on EXISTING configs, mirroring the
    # ``tool_search`` precedent in _refresh_dynamic_fields: ensure the
    # computer-use @ref is in ``tools``.
    #
    # Without this, an UPGRADING install never gains the ref — the fresh-install
    # branch above is the only place it is added — so ``kirocrew-computer`` is
    # registered in ``mcpServers`` but kiro-cli exposes none of its tools, and the
    # feature silently does nothing for every pre-existing user. (Unlike a
    # third-party MCP, the user cannot have "opted out" of a ref that never
    # existed on their install.)
    #
    # DELIBERATELY tools-only, never ``allowedTools``: that list is kiro-cli's
    # blanket auto-approve, and an auto-approved MCP tool is approved locally by
    # kiro-cli — it emits no permission request, so ``hooks.on_tool_call`` (the
    # deny floor + governance ceiling + approval clamp) is never reached for it.
    # Granting it here would delete the PreToolUse plane for a tool that can click
    # and type into an already-authenticated application.
    #
    # Gated on the shipped template actually granting the ref (so an edition that
    # drops computer use is respected) and on the server having resolved, and
    # scoped to this ONE server so no other managed ref is re-added behind the
    # user's back. The primary enable still lives in the keystone file, so a config
    # that gains the ref is not a feature that turns itself on: the shim answers an
    # empty tools/list until the user opts in from Settings.
    if not fresh_install and CU_MCP_SERVER in valid_servers:
        cu_ref = f"@{CU_MCP_SERVER}"
        shipped_tools = get_shipped_tools().get("tools", [])
        existing_tools = config.get("tools")
        if (
            isinstance(existing_tools, list)
            and cu_ref in shipped_tools
            and cu_ref not in existing_tools
        ):
            existing_tools.append(cu_ref)
            sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="install_agent",
                resources=f"{cu_ref} added to tools (existing config upgrade)",
            )

    # Audit the DECISION, not a config delta. Nothing in the spec changes shape
    # when a gate closes — the ``@ref`` stays exactly where the template put it
    # and only the ``mcpServers`` entry is withheld — so there is no delta to
    # observe, and a reader of the audit trail would otherwise have no record
    # that a shipped server was deliberately not emitted. Derived from the gate
    # plus the shipped template so the fresh and existing paths record the same
    # fact.
    _withheld = sorted(
        f"@{name}" for name in gated_off if f"@{name}" in get_shipped_tools().get("tools", [])
    )
    if _withheld:
        sel().log_api_access(
            caller="system",
            operation="mcp_server_withheld",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{', '.join(_withheld)} withheld from mcpServers (unsupported "
                f"platform or capability disabled); its tools ref is retained and "
                f"resolves to nothing"
            ),
        )

    # Final dedup (preserves order).
    for key in ("tools", "allowedTools"):
        config[key] = list(dict.fromkeys(config.get(key, [])))

    # LAST governance pass over the auto-approve LIST itself. The writers above
    # apply the ceiling to entries THEY add, but a builtin auto-approve (fs_read,
    # execute_bash, …) arrives straight from the agent TEMPLATE into
    # `allowedTools` and no writer ever re-touches it — so a `filesystem.read`
    # ceiling would leave `fs_read` on the blanket auto-approve list and kiro-cli
    # would approve every read WITHOUT reaching the PreToolUse gate that carries
    # the ceiling. Filter the whole assembled list through the one predicate: a
    # governed builtin (or `@server`) loses its blanket grant and its calls go
    # through the gate, where the per-argument rule actually applies; anything the
    # ceiling is silent about is kept (the predicate returns True), and an
    # ungoverned host keeps everything. `tools` is deliberately left intact —
    # mounting a tool is not auto-approving it.
    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        kept: list[str] = []
        withheld: list[str] = []
        for ref in allowed:
            if not isinstance(ref, str):
                # A malformed non-string entry (e.g. a hand-edited config with
                # `allowedTools: [1]`) would crash may_skip_gate's
                # ref.startswith() and fault the whole rebuild. It is not a valid
                # tool ref, so drop it entirely rather than keep or audit it.
                continue
            (kept if _may_auto_approve(ref) else withheld).append(ref)
        config["allowedTools"] = kept
        if withheld:
            # Withholding a grant is a permission DECISION, and this final pass is
            # the ONLY place a builtin that arrived straight from the shipped
            # template (fs_read, code, …) loses its blanket auto-approve. The
            # per-writer paths already emit this SEL event for the grants they
            # touch; a silent drop here would leave an operator no record of why a
            # template tool now prompts. Same operation name, so it lands in one
            # feed. Auditing must never fail the rebuild.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="rebuild_agent_config",
                    resources=(
                        f"{', '.join(withheld)} mounted without auto-approve "
                        "(governance ceiling); calls go through the approval gate"
                    ),
                )
            except Exception:  # noqa: BLE001 — the audit must not break a rebuild
                logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)

    # kirocrew.json has TWO independent-locked writers: this regenerating one and
    # the app-MCP registration path (bridges._register_mcp_servers), which does a
    # read-modify-write of the SAME file under bridges._mcp_lock. We snapshotted
    # the app servers via registered_app_mcp_servers() far above, so a register
    # that lands BETWEEN that snapshot and this write would be silently dropped by
    # our full-file regeneration — "settings or MCP entries silently overwritten".
    # Hold that same lock across a final re-read+merge of the app-namespaced
    # servers so the two writers serialize and neither loses the other's entries.
    # (Only for kirocrew.json — every other agent file this may write has a single
    # writer.) The re-read uses the UNLOCKED reader because we already hold the lock.
    from kiro_crew.apps.bridges import (
        _mcp_json_path,
        _mcp_lock,
        _read_mcp_json_unlocked,
    )

    def _finalize_and_write() -> None:
        servers_map = config.get("mcpServers")
        # Runs here, at the single funnel every write path goes through, and AFTER
        # the passes that mutate `allowedTools` (managed/shared MCP sync) — a policy
        # seeded before them would describe a list that no longer exists.
        _seed_kas_permissions(config)
        if isinstance(servers_map, dict):
            # LAST governance pass over the assembled server map. `autoApprove` can
            # arrive from an app manifest, a per-agent policy, a managed spec or an
            # imported config; filtering here, on the final map, covers every source.
            config["mcpServers"] = _strip_ungoverned_auto_approve(servers_map)
        # THE OWNERSHIP TRANSITION. Everything from here to the commit is one
        # critical section, and it runs for EVERY write that changes the alias map --
        # not only when the alias pass runs. A claim that outlives the generation it
        # describes is the one state that can strip a name the user has since
        # hand-written, and a clean or gate-off rebuild changes the map just as a
        # generated pass does.
        #
        # Imported inside a try: `kiro_crew.connections.alias_record` is a submodule,
        # so importing it executes `kiro_crew.connections.__init__`, which eagerly
        # loads and VALIDATES registry.json at import time (`_PROVIDERS =
        # _load_registry()`). A registry that is corrupt, unreadable or newly invalid
        # therefore raises HERE -- before the fail-closed alias guard below can catch
        # it -- and would abort the whole rebuild, taking the agent spec down over an
        # OPTIONAL feature. The aliases are optional; the spec is not. So on an import
        # failure this still reconciles the on-disk map (a local, import-free helper)
        # and writes the spec, and only the ownership pass is skipped.
        try:
            from kiro_crew.connections.alias_record import (  # noqa: PLC0415
                AliasGeneration,
                begin_transaction,
                commit_transaction,
                load_claimed,
                spec_fingerprint,
            )
        except Exception:  # noqa: BLE001 — an optional feature must not fail the spec
            logger.warning(
                "Skipping Connections tool aliases: the alias ownership module could "
                "not be imported (a broken connections registry does this). The agent "
                "spec is written normally and the aliases already on disk are kept.",
                exc_info=True,
            )
            # Same reconciliation the normal path does, and for the same reason: the
            # assembled map is a PRE-LOCK snapshot, so writing it back would resurrect
            # aliases a concurrent rebuild removed. Clean is exempt (it regenerates
            # from defaults), exactly as at the call below. No ownership transition is
            # opened: with no record module there is no claim to retire, and leaving
            # the record untouched is invariant 4's safe reading -- the pairs on disk
            # are treated as the user's and survive.
            if not clean:
                _reconcile_tool_aliases_from_disk(path, config)
            _atomic_json_write(path, config)
            return

        # `durable` is the generation really on disk, read once inside this section:
        # `config` carries a PRE-LOCK alias snapshot, so an overlapping rebuild would
        # otherwise write its stale copy back, resurrect aliases this one removed,
        # and fingerprint a generation that is no longer there. It is also the
        # transaction's `previous` candidate, which is what makes a lost spec write
        # recoverable.
        durable_existed, durable_aliases = _durable_tool_aliases(path)
        previous_fingerprint = spec_fingerprint(durable_aliases if durable_existed else None)
        previous_claim = load_claimed(previous_fingerprint)
        # A CLEAN rebuild regenerates from defaults, so the old spec's map is NOT
        # imported -- importing it would make `toolAliases` the one key that survives
        # the reset the user asked for. It still takes part in the transition above:
        # `durable` is snapshotted as the previous generation so the stale claim is
        # retired rather than left describing a map that is being replaced.
        if not clean:
            _reconcile_tool_aliases_from_disk(path, config)
        alias_generation = _apply_connection_tool_aliases(config, previous_claim)
        if alias_generation is None:
            # The pass stood down (gate off, no server map, unreadable registry). The
            # map can still have changed -- a clean rebuild drops it outright -- and
            # then the old claim describes a generation that will not exist, so the
            # transition must still happen with an EMPTY emission to retire it. When
            # the map is unchanged the record is left exactly as it is: rewriting it
            # empty there would forget a real emission and strand those aliases.
            target_fingerprint = spec_fingerprint(config.get("toolAliases"))
            if target_fingerprint == previous_fingerprint:
                _atomic_json_write(path, config)
                return
            alias_generation = (target_fingerprint, frozenset())

        target = AliasGeneration(*alias_generation)
        # Opened BEFORE the spec write, so a lost spec write still has a recoverable
        # previous generation (state-table rows 2/3) and a lost commit still resolves
        # to this emission (rows 4/5). Failing to open it FAILS CLOSED on the aliases
        # alone: the map is restored to the durable generation the surviving record
        # still describes, and the rest of the spec is written normally -- an
        # unwritable sidecar must not take down agent-spec repair.
        try:
            begin_transaction(AliasGeneration(previous_fingerprint, previous_claim), target)
        except OSError:
            logger.warning(
                "Skipping Connections tool aliases: the ownership transaction could not "
                "be opened, so the spec's aliases are left at the generation the record "
                "still describes rather than advanced past it.",
                exc_info=True,
            )
            _set_tool_aliases(config, durable_aliases if durable_existed else None)
            _atomic_json_write(path, config)
            return

        _atomic_json_write(path, config)
        # The spec carrying those aliases is durable, so the open transaction can be
        # committed -- inside whatever lock guarded that write, so the two land as one
        # unit. Committing outside it would let two rebuilds serialize their spec
        # writes and still commit in the opposite order, leaving a record that
        # describes the OTHER pass's spec. A commit failure PROPAGATES on purpose
        # (alias_record invariant 6): it is recoverable rather than harmful -- the
        # pending record's target fingerprint already matches the map now on disk, so
        # the next pass resolves to exactly this emission (row 5) instead of
        # abandoning it -- but an unwritable data home is still reported when it
        # happens.
        commit_transaction(target)

    try:
        is_kirocrew_json = path.resolve() == _mcp_json_path().resolve()
    except OSError:
        is_kirocrew_json = False
    if is_kirocrew_json:
        with _mcp_lock():
            on_disk = _read_mcp_json_unlocked().get("mcpServers", {})
            if isinstance(on_disk, dict):
                servers = config.setdefault("mcpServers", {})
                # on_disk was written under THIS lock by the app register/deregister
                # path. It is authoritative for a concurrent PORT change (same key,
                # new URL) and for a concurrent REGISTER (a key our snapshot missed),
                # so we overwrite/add from it below. But absence from on_disk is NOT
                # by itself proof that an app server should be dropped: a clean
                # rebuild (or a missing/empty config) starts with an empty on_disk,
                # yet every ENABLED app's manifest-derived servers must still be
                # written — dropping them here made an enabled stdio app's tools
                # vanish. So drop an app server ONLY when its app is confirmed no
                # longer enabled (a concurrent deregister), which is what actually
                # resurrects a dead entry; keep it otherwise.
                try:
                    from kiro_crew.apps.manager import is_app_enabled

                    def _app_of_key_enabled(_key: str) -> bool:
                        try:
                            return bool(is_app_enabled(_key.split(":", 1)[0]))
                        except Exception:  # noqa: BLE001 — cannot verify → fail closed
                            # A malformed installed.json makes enablement
                            # unverifiable. Keeping the entry would leave a
                            # deregistered/unknown app's MCP tools callable with no
                            # way to confirm they should be — so drop it. It is
                            # re-derived from the manifest on the next clean rebuild.
                            return False

                except Exception:  # noqa: BLE001 — apps subsystem unavailable

                    def _app_of_key_enabled(_key: str) -> bool:
                        # If the apps subsystem itself will not import, no app can
                        # be confirmed enabled — drop app-scoped entries rather than
                        # retain unverifiable tools.
                        return False

                on_disk_app = {_k for _k in on_disk if ":" in _k and _k not in managed_names}
                for _k in [k for k in servers if ":" in k and k not in managed_names]:
                    if not _app_of_key_enabled(_k):
                        del servers[_k]
                for _k, _v in on_disk.items():
                    # ALWAYS assign, not add-if-missing: on_disk is authoritative
                    # for app servers, so a concurrent re-registration on a new
                    # port (same key, new URL) must OVERWRITE our stale snapshot —
                    # otherwise the dead pre-rebuild URL is persisted.
                    if _k in on_disk_app:
                        servers[_k] = _v
            _finalize_and_write()
    else:
        _finalize_and_write()
    logger.info("Installed agent config: %s", path)

    # Install KiroCrew AIM capabilities package (includes kirocrew-lite)
    _install_aim_capabilities()

    # Install kirocrew-knowledge agent (used by Knowledge Library LLMPool)
    try:
        _install_knowledge_agent()
    except Exception:
        logger.debug("kirocrew-knowledge agent install failed", exc_info=True)

    # Install kirocrew-research agent (used by the Research Lab campaign loop)
    try:
        _install_research_agent()
    except Exception:
        logger.debug("kirocrew-research agent install failed", exc_info=True)

    # Install kirocrew-heartbeat agent (used by HeartbeatService for unattended polling)
    try:
        _install_heartbeat_agent()
    except Exception:
        logger.debug("kirocrew-heartbeat agent install failed", exc_info=True)

    # Install kirocrew-conductor agent (goal decomposition + session-control dispatch)
    try:
        _install_conductor_agent()
    except Exception:
        logger.debug("kirocrew-conductor agent install failed", exc_info=True)

    # Install kirocrew-pipeline-conductor agent (repository pipeline fleet supervision)
    try:
        _install_pipeline_conductor_agent()
    except Exception:
        logger.debug("kirocrew-pipeline-conductor agent install failed", exc_info=True)

    # Bidirectional sync: ensure packages installed for one provider
    # are also available for the other (agents↔plugins, skills).
    sync_aim_packages()

    # Security: sanitize invalid hook keys in agent configs
    repair_agent_configs()

    return path


# Backward-compat alias — callers may still use the old name.
install_agent = rebuild_agent_config


def ensure_agent_materialized(agent: str | None) -> bool:
    """Self-heal: guarantee the managed default agent config exists on disk.

    kiro-cli discovers its selectable *modes* at process startup by scanning
    ``~/.kiro/agents/*.json``. A session that spawns ``kiro chat --agent <name>``
    and then issues ``session/set_mode {modeId: <name>}`` therefore needs the
    backing file present BEFORE spawn, or kiro-cli answers
    ``-32603 "Mode '<name>' not found"`` on every turn (the crash this closes).
    Normally ``kirocrew setup --agent-only`` writes it, but a source checkout /
    dev launch that skips setup leaves it absent — this makes the runtime
    self-sufficient regardless.

    Only the managed default (``AGENT_FILENAME`` → ``kirocrew.json``) is
    regenerable here, via :func:`rebuild_agent_config`. App/custom agents are
    owned by their own subsystems, so a missing one is reported (``False``) and
    left to the caller's graceful set_mode fallback rather than being guessed at.

    Returns ``True`` when the managed default file is present (already, or after
    a regenerate); ``False`` when *agent* is non-managed or regeneration failed.
    Best-effort — never raises, so it can sit on the spawn hot path.
    """
    try:
        managed = Path(AGENT_FILENAME).stem
        if not agent or agent != managed:
            return False
        agent_file = kiro_agents_dir_path() / AGENT_FILENAME
        if agent_file.exists():
            return True
        logger.warning(
            "Managed agent config %s missing — regenerating before spawn "
            "(self-heal for kiro-cli 'Mode not found')",
            agent_file,
        )
        rebuild_agent_config()
        return agent_file.exists()
    except Exception:
        logger.warning("ensure_agent_materialized failed for agent %r", agent, exc_info=True)
        return False


def _install_aim_capabilities() -> None:
    """Write a bare ``kirocrew-lite`` agent config.

    Symbol preserved for callers (``rebuild_agent_config``).  The previous
    AIM-package install path is omitted on public installs (AIM is an
    Amazon-internal package manager); the generic ``kirocrew-lite`` fallback
    config — used by the claude_code provider for cheap background work — is
    still written.
    """
    _install_lite_agent_fallback()


def _install_lite_agent_fallback() -> None:
    """Write a bare kirocrew-lite config (cheap background agent)."""
    lite_path = kiro_agents_dir_path() / _LITE_AGENT_FILENAME
    lite_config = {
        "name": "kirocrew-lite",
        "model": _background_agent_model(),
        "tools": [],
        "mcpServers": {},
        "prompt": "",
    }
    _atomic_json_write(lite_path, lite_config)
    # Cheap model for the claude_code (CC) provider. kiro-cli resolves the lite
    # model from `model` via --agent; the CC backend can't, so the provider
    # factory reads this cc_model for the lite agent. The kiro spec above uses
    # the resolved background role model (default "auto", entitlement-safe on
    # every tier); the CC seam needs a concrete model, so it falls back to the
    # cheap default when the role is unpinned. Stored in the sidecar (kiro spec
    # stays schema-clean).
    agent_state.set_cc_model("kirocrew-lite", _background_cc_model())


_KNOWLEDGE_SYSTEM_PROMPT = (
    "You are a knowledge extraction specialist for KiroCrew's Knowledge Library. "
    "Your job is to analyze documents and extract structured information.\n\n"
    "You ALWAYS output valid JSON. No markdown, no explanation — just the JSON object.\n\n"
    "Be precise with entity names — use canonical forms (e.g., 'DynamoDB' not 'dynamo' or 'DDB').\n"
    "Only extract entities explicitly mentioned in the text, do not infer.\n"
    "Relations must reference entities that appear in your entities list."
)


def _install_knowledge_agent() -> None:
    """Generate and install the kirocrew-knowledge agent config.

    This agent is used by the Knowledge Library's LLMPool for document
    extraction. By default it uses the user's configured agent.model (so
    extraction runs on the same model as chat). If the user sets
    knowledge.extraction_model explicitly, that model is used instead —
    allowing a cheaper model for extraction without changing the chat default.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    path = kiro_agents_dir_path() / _KNOWLEDGE_AGENT_FILENAME

    # Resolve model: knowledge.extraction_model > agent.model > "auto"
    try:
        cfg = KiroCrewConfig.load()
        model = cfg.knowledge.extraction_model.strip()
        if not model:
            # Use the user's default model (same as chat).
            model = cfg.agent.model or "auto"
    except Exception:
        model = "auto"

    config: dict[str, object] = {
        "name": "kirocrew-knowledge",
        "description": (
            "Dedicated agent for knowledge extraction, categorization, " "and summarization."
        ),
        "model": model,
        "includeMcpJson": False,
        "prompt": _KNOWLEDGE_SYSTEM_PROMPT,
        "mcpServers": {},
        "tools": [],
    }

    _atomic_json_write(path, config)
    logger.info("Installed knowledge agent config: %s (model=%s)", path, model)


_RESEARCH_SYSTEM_PROMPT = """# KiroCrew Research Worker

You are `kirocrew-research`, an autonomous research worker. You run ONE research
cycle per turn inside an autonudge loop, then end your turn — the next cycle fires
automatically. The Research Lab app drives you; the nudge names the campaign and dir.

## Per-cycle protocol (strict order)
1. Status check (first action): read `<dir>/status.json`. If status is not
   `running`, stop and end the turn.
2. Brief: read `<dir>/brief.md` for the question, sub-questions, and allowed sources.
3. Guidance: if `<dir>/guidance.txt` exists, read it, incorporate it, then delete it.
4. Orient (compact): skim only the one-line `summary`/`key_insight` of existing
   `findings/cycle_*.json` and the `## Research State` section of `FINDINGS.md` —
   NOT the full findings. Note what's answered, what's weak, and which leads are open.
   RECOVERY: if the dir looks emptier than the conversation implies (e.g. you
   recall completing a cycle but no matching `cycle_*.json` is on disk), a prior
   cycle's write was dropped mid-turn (connection loss / gateway restart). Re-derive
   that lost finding from context and write it to disk THIS cycle under the correct
   `cycle_NNN.json` name — do NOT invent a new naming scheme to "save" the work.
5. Decide direction: choose the single highest-value next step toward the question —
   a sub-question, a follow-up a prior finding surfaced, or shoring up weak evidence.
   Steer toward closing the goal; don't just walk the list.
6. Investigate that one step using one source/tool.
7. Record: write `findings/cycle_NNN.json` where **NNN = the count of existing
   `findings/cycle_*.json` files, zero-padded to 3 digits** (first cycle ->
   `cycle_000.json`, next -> `cycle_001.json`, ...). NEVER reuse or overwrite an
   existing cycle file. The filename pattern is a HARD contract: the Research Lab
   counts findings and detects completion by matching `cycle_NNN.json` ONLY. A
   finding written under any other name (e.g. a descriptive `01-topic.md`) is
   INVISIBLE — the campaign will show 0 findings and appear stalled even though
   your work is on disk. When in doubt, match `cycle_NNN.json` exactly. Keys:
   `cycle` (= NNN), `summary, sources_checked, sources_empty, new_findings_count,
   evidence_strength, key_insight, sub_question`; append the cycle to `FINDINGS.md`
   with citations; then rewrite its short `## Research State` (open questions,
   leads, dead-ends, weak spots) for the next cycle.
8. End the turn.

## Evidence strength
- `strong`: corroborated by 2+ independent sources
- `moderate`: a single source
- `weak`: inferred/speculative, no direct source

## Rules
- Be honest about `new_findings_count` (0 if nothing new this cycle).
- Never fabricate sources or findings; cite everything with a URL or path.
- Sources: use `web_search`/`web_fetch` for the public web. The local codebase
  (`grep`/`code`/`fs_read`) and the user's Knowledge Library are first-class
  sources too — search them when the question touches the user's own projects
  or saved documents.
- One cycle = one step. The compact summaries are your memory — do not re-read
  full prior findings.
- If brief.md lists sub-questions, they are the AUTHORITATIVE checklist — answer
  each; do NOT generate your own initial set. If brief.md lists none, derive
  sub-questions yourself from the question and scope. Use FIRST PRINCIPLES to steer
  which open sub-question (or weak-evidence gap) to pursue each cycle. When a
  finding surfaces a genuinely new high-value angle not in the checklist, you MAY
  append it as an emergent sub-question and pursue it (note it in FINDINGS.md
  `## Research State`).
- Follow brief.md's questions directive: when allowed, you MAY pause with ONE
  high-leverage clarification question — write {"question": ..., "why": ...} to
  questions.json and end the turn — when the goal or scope is genuinely ambiguous
  in a way that would materially change your research direction. Keep the bar high:
  proceed on a best-reasoned assumption (and record it) for anything minor or that
  you can resolve yourself.
- If `brief.md` defines a **Definition of Done**, verify against it each cycle using
  your tools (run tests, review code, run the eval) and record
  `verification: {passed: bool, detail: "..."}` in the finding. The campaign
  auto-completes when `passed` is true.
- On the final cycle (`cycle == max_cycles - 1`), write an executive summary +
  recommendation at the TOP of `FINDINGS.md` instead of new research.
"""


def _install_research_agent() -> None:
    """Generate and install the kirocrew-research agent config.

    Derives from the kirocrew agent (MCP servers, security, tools) but swaps in a
    lean research-worker prompt + identity. Used by the Research Lab app's
    autonudge loop to run one research cycle per turn.
    """
    config = build_agent_config()
    config["name"] = "kirocrew-research"
    config["description"] = (
        "Autonomous research worker — runs one research cycle per turn "
        "in a Research Lab campaign loop."
    )
    config["prompt"] = _RESEARCH_SYSTEM_PROMPT
    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = kiro_agents_dir_path() / _RESEARCH_AGENT_FILENAME
    _atomic_json_write(path, config)
    logger.info("Installed research agent config: %s", path)


_CONDUCTOR_SYSTEM_PROMPT = """# Kiro Crew Conductor

You are `kirocrew-conductor`. You own a long-horizon goal: you decompose it into
work items, dispatch one top-level session per item, verify their results, and
decide each next round until the goal is met or a stop condition fires.

**You never do a work item's work yourself.** A file to write, a build to run, a
fix to make — each one is a work item for a child session. You have no
file-writing tool, and a work item never goes to `spawn_run`,
`spawn_sub_agents`, `workflow_run` or `task_run`: it goes to a session you can
dispatch, verify and report on.

**Acceptance is the evaluator's verdict, never your reading of a child's
transcript.** Shell access exists to run the `goal-conductor` skill's two
bundled scripts: `scripts/accept_eval.py` for acceptance verdicts,
`scripts/ledger_entry.py` for the ledger's item-entry format.

**Patrol with `monitor_start`, never with `wait`.** Arm it with the full cycle
instructions AND the exit condition, then end the turn; call `autonudge_stop`
when you stop. A reply saying *requested* is success — do not retry it. If
arming is refused outright, say no loop is running and drive that one round
with `wait`.

Your tools:

- Child sessions — `session_create`, `session_send`, `session_read_message`,
  `session_stop`, `list_sessions`.
- Keeping the goal's sessions together — `chat_folder_tree`,
  `chat_folder_create`.
- State that outlives a round — `session_ledger_read`, `session_ledger_record`.
- Patrol — `monitor_start`, `monitor_update`, `autonudge_stop`, `wait`.
- Capacity, before standing up several sessions at once — `resource_status`.
- Talking to the person — `ask_question` puts a decision that is not yours to
  make to them as a card, after which you END your turn and their answer
  arrives as the next message; `send_message` / `send_notification` to report.
- Naming the right skill in a seed message — `skill_search`, `skill_fetch`.
- Reading — `fs_read`, `web_fetch`.
- `tool_search` loads a tool that is not in your list yet.

The `goal-conductor` skill carries the operating procedure — the work-item
tests, the dispatch steps, the patrol cycle, the stop conditions. Read it
before acting on a goal. The user can message you at any time: apply goal
changes at the round boundary, except a message that invalidates an in-flight
item, which you handle immediately.

{{VERBOSITY_BLOCK}}
"""


#: The dashboard verbs the conductor may call WITHOUT an approval prompt, named one
#: by one rather than as the whole ``@kirocrew-dashboard`` server.
#:
#: THE INVARIANT, so a later reader extends this by rule and not by taste. A
#: granted verb must satisfy BOTH halves:
#:
#: 1. It may CREATE something new or READ. It may never MUTATE user-visible
#:    workspace state that already exists and is not the conductor's own — a
#:    session's contents or liveness, or the arrangement the person made of their
#:    sessions and folders.
#: 2. Its worst case, called in a loop, must be BOUNDED BY THE SERVER — and
#:    bounded so the resource stays reachable by everyone else.
#:
#: The conductor ingests untrusted text by design — its charter's worked example is
#: "resolve this repo's open issues", and it holds ``web_fetch`` for exactly that —
#: so every granted verb is reachable by content it read, with no human in the loop
#: on a nudge-driven patrol cycle. Per-call approval was the only thing
#: rate-limiting a granted verb, and ``allowedTools`` has no argument or rate
#: matching to replace it, so the bound cannot live in this list: it has to live in
#: the endpoint. Half 2 is not a restatement of half 1 — an unbounded create is how
#: a create does damage without mutating anything.
#:
#: Half 1 names user-visible workspace state deliberately, rather than "any
#: pre-existing resource", because a create ALWAYS writes some shared bookkeeping —
#: the slot table, the folder index, the session-pulse counter below — and a literal
#: reading would forbid every create and decide nothing. What it protects is state
#: the person arranged and would have to reconstruct by hand. Creation is otherwise
#: recoverable clutter; mutation of what the user arranged is not.
#:
#: Both granted creation verbs earn half 2 from a server ceiling, and BOTH ceilings
#: were added by this change — neither verb was safe to auto-approve as the code
#: stood:
#:
#: * ``chat_folder_create`` had no bound at all, so a loop grew durable on-disk
#:   state without limit. Now ``MAX_CHAT_FOLDERS``, tested under the folder lock.
#: * ``session_create`` had a GLOBAL ceiling (``MAX_LIVE_SLOTS``) but no
#:   distribution: one caller could hold all 500, and every later create — the
#:   person opening a chat tab included — got the 429. A bounded resource that one
#:   caller can exhaust is not bounded from anybody else's point of view. Now
#:   ``MAX_SLOTS_PER_CREATOR`` bounds what a single caller holds, leaving 450 slots
#:   reachable no matter what the conductor does.
#:
#: Every verb this server exposes, against that rule:
#:
#: * ``chat_folder_tree`` — READ of the caller's visible tree. GRANTED.
#: * ``chat_folder_create`` — creates a NEW folder, and
#:   ``_refuse_tree_shaping_if_unverifiable`` refuses an unverifiable caller and
#:   keeps an app agent out of the person's own folders. Touches nothing that
#:   already existed, and bounded by ``MAX_CHAT_FOLDERS``. GRANTED.
#: * ``session_create`` — creates a NEW session in the caller's workspace, bounded
#:   both globally (``MAX_LIVE_SLOTS``, 429 on breach) and per caller
#:   (``MAX_SLOTS_PER_CREATOR``), and visible in the sidebar. GRANTED.
#:   One known side effect, recorded because it is the closest thing to an
#:   exception here: ``create_session`` mints its slot with
#:   ``origin=SlotOrigin.USER`` (it is a first-class user-owned session, which is
#:   what keeps it correctly private), and ``get_or_create_slot`` increments the
#:   session-pulse counter on exactly that origin — so conductor-created sessions
#:   count toward the feedback survey's "10 genuine user chats" window. That is a
#:   pre-existing conflation in the counter, not something this grant introduces:
#:   the counter uses the ownership tag as a proxy for "a person started a chat",
#:   and it miscounts for every caller of the session-control create verb. Filed
#:   as issue #6139 rather than point-fixed here, because the correct fix is a
#:   fail-open/fail-closed decision about which call sites opt in, inside the
#:   session-pulse surface. Consequence if it drifts: a survey prompt appears
#:   earlier than the product intended. No workspace state is altered.
#: * ``session_read_message`` — read-only, and the verb the patrol loop actually
#:   needs on a cycle with nobody at the keyboard. GRANTED.
#: * ``chat_folder_move_session`` — WITHHELD. It writes another session's
#:   ``folder_id``: the PATCH goes to ``/api/chat/slots/<target>/folder`` where the
#:   target is the session named in the ARGUMENTS, and the strictly-resolved
#:   caller key is only the authority header. ``mcp_dashboard`` calls it "the one
#:   tool here that writes to a session OTHER than the caller's". Auto-approving it
#:   would let ingested content silently refile or unfile any persistent
#:   same-workspace session, losing filing the user did by hand.
#: * ``chat_folder_move`` — WITHHELD. Reparents an existing folder tree, and no
#:   conductor step needs it.
#: * ``session_send`` — WITHHELD. Runs text as another session's user-role turn
#:   under that target's own grants. The server-side gates bound WHICH target is
#:   reachable; nothing bounds WHAT is sent.
#: * ``session_stop`` — WITHHELD. Ends another session's in-flight turn and
#:   DISCARDS its work (``stop_target``: "A stop cancels cooperatively", and the
#:   cancelled turn's work is gone either way — the retry de-duplication that
#:   keeps a re-sent stop from ALSO discarding the queue does not make the verb
#:   non-destructive).
#:
#: Every withheld verb stays MOUNTED (``@kirocrew-dashboard`` is still in
#: ``tools``) — it just passes through ``hooks.on_tool_call`` like any ungranted
#: tool. The cost is an approval when a round files a session, seeds a child, or
#: stops one; all three happen right after a human approved the plan, while the
#: unattended patrol cycle needs none of them. Issue #6118 (a ``folder`` argument
#: on ``session_create``) would remove the filing call altogether.
_CONDUCTOR_DASHBOARD_GRANTS: tuple[str, ...] = (
    "@kirocrew-dashboard/chat_folder_tree",
    "@kirocrew-dashboard/chat_folder_create",
    "@kirocrew-dashboard/session_create",
    "@kirocrew-dashboard/session_read_message",
)


def _install_conductor_agent() -> None:
    """Generate and install the kirocrew-conductor agent config.

    Derives from the kirocrew agent (resolved MCP invocations, security hooks)
    but narrows to the conductor's charter: session control + core tools +
    shell for the bundled skill scripts (acceptance evaluator + ledger entry
    codec), and **no tool that can write a
    file** — not ``fs_write``, and not ``code`` either, which governance classes
    under ``filesystem.write`` because it writes files and can shell out. That
    is what makes "never does a work item's work itself" a property of the spec
    rather than of the prompt. The ``kirocrew-dashboard`` server is the opt-in
    per-agent set (folder + session-control tools); this installer granting it IS
    the explicit per-agent assignment that set requires — it is deliberately
    absent from the default agent's spec.

    ``@kirocrew-dashboard`` is MOUNTED whole but auto-approved only verb by verb,
    via ``_CONDUCTOR_DASHBOARD_GRANTS`` (see its comment for the per-verb
    reasoning). Both backends honour a per-tool reference, so the narrowing is
    real rather than cosmetic: kiro-cli's ``is_tool_in_allowlist`` checks
    ``@server`` and then ``@server/<tool>``, and ``allowed_tools_to_permissions``
    maps the same entry to an exact KAS ``server/tool`` resource match.

    The line the split follows is stated as an invariant on that tuple, not as a
    taste call: a granted verb may CREATE or READ, never MUTATE something that
    already exists and is not the conductor's own. Reads and creates are granted
    because the patrol loop is nudge-driven and must not block on an approval
    nobody is there to give. ``session_stop`` (discards a peer's in-flight turn),
    ``session_send`` (runs text as a peer's turn) and ``chat_folder_move_session``
    (writes a peer session's ``folder_id``) are withheld, because the conductor
    ingests untrusted content by design and the server-side gates bound which
    target is reachable, not what is done to it.

    ``execute_bash`` is withheld for a different reason that is worth keeping
    distinct: ``allowedTools`` is name-scoped with no argument matching, so
    trusting the two bundled scripts cannot be told apart from trusting arbitrary
    shell. There is no per-argument form of that grant the way there is a per-tool
    form of the MCP one.

    The operating procedure ships as the ``goal-conductor`` builtin skill, NOT
    ``conductor``: that skill name is owned by the generated delegation skill
    (``conductor_skill.generate_conductor_skill``), and two existing code paths
    delete ``<skills>/conductor/SKILL.md`` when ``agent.conductor_skill`` is
    false — the default. Sharing the name would let ``kirocrew setup`` erase the
    packaged skill on a stock install, and quarantine the user's delegation
    skill when the flag is on.
    """
    config = build_agent_config()
    config["name"] = "kirocrew-conductor"
    config["description"] = (
        "Owns a long-horizon goal: decomposes it into work items, stands up "
        "a top-level session per item, patrols their state, and decides each "
        "next round. Never does the work itself."
    )
    config["prompt"] = _CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        # ``web_fetch`` serves the charter's own worked example (reading an issue
        # list during triage). Deliberately NOT mounted: ``web_search`` (nothing
        # names it), ``grep``/``glob`` (``fs_read`` covers every read the charter
        # describes), and above all ``code`` — governance classes it under
        # ``filesystem.write`` because it "writes files AND can shell out", so
        # mounting it would make this spec's whole no-write property false.
        # An unused grant is surface the charter cannot account for.
        "web_fetch",
        "session",
        "report",
        # Load-bearing, not decoration: with MCP Tool Search active the
        # session-control specs are deferred, so the conductor cannot reach
        # ``session_create`` / ``chat_folder_*`` / ``monitor_start`` at all until
        # it loads them by id. Named in the prompt's tool inventory for that
        # reason, and auto-approved below so the load itself never prompts.
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
    ]
    # ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so
    # every grant is filtered through the governance ceiling first — the same
    # predicate ``rebuild_agent_config`` applies to the primary spec's assembled
    # list, and the entry point ``may_skip_gate_now`` exists precisely so a new
    # writer cannot re-open the bypass by restating a literal. A governed ref
    # stays MOUNTED (it is still in ``tools``); it just prompts, and the gate
    # then applies the ceiling's per-tool rule with the real arguments.
    granted: list[str] = []
    withheld: list[str] = []
    # ``tool_search`` is granted on the same rule as the dashboard verbs below:
    # it only READS a tool spec into context — it cannot act, touch workspace
    # state, or reach the machine — and it is bounded by the mounted catalog.
    # Withholding it made the ONE call that unblocks every deferred
    # session-control tool prompt first, so an unattended patrol cycle stalled
    # on the load rather than on the work. ``execute_bash`` stays withheld for
    # the reason recorded above it: ``allowedTools`` has no argument matching,
    # so trusting the two bundled scripts cannot be told apart from trusting
    # arbitrary shell.
    for ref in (
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        *_CONDUCTOR_DASHBOARD_GRANTS,
    ):
        (granted if _may_auto_approve(ref) else withheld).append(ref)
    config["allowedTools"] = granted
    if withheld:
        # Withholding a grant is a permission DECISION, and every other writer of
        # an ``allowedTools`` list emits this same event for it — see
        # ``strip_ungoverned_auto_approve``, whose comment names a silent pop as
        # the one withhold path with no audit trail. Filtering silently here would
        # make this installer exactly that path: on a governed host a ref loses its
        # grant and the operator has no record of why the conductor now prompts.
        # Same operation name so it lands in one feed, and the audit must never
        # break the install.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="_install_conductor_agent",
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    "(governance ceiling); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    narrowed: dict = {}
    if core_entry:
        narrowed["kirocrew-core"] = core_entry
    dash_cmd, dash_args = _kirocrew_mcp_invocation("mcp-dashboard")
    dash_entry: dict[str, Any] = {"command": dash_cmd, "args": dash_args}
    # Same managed-server metadata `build_agent_config` stamps on every entry it
    # emits, and the reason this entry needs it spelled out is that it is the one
    # server hand-built here rather than inherited: without `"type": "registry"`
    # a registry-mode client silently DROPS the entry, so the conductor's
    # session-control tools never launch and its whole dispatch/patrol purpose is
    # dead with no local error; without the `KIROCREW_HOME` pin the shim reads the
    # DEFAULT data home while the gateway runs under an override, so session
    # control would act on a different session store than the one it reports on.
    # Both helpers return empty on a default install, so the emitted spec is
    # unchanged there.
    if _mcp_registry_mode():
        dash_entry["type"] = _MCP_REGISTRY_TYPE
    dash_env = _managed_mcp_env()
    if dash_env:
        dash_entry["env"] = dash_env
    narrowed["kirocrew-dashboard"] = dash_entry
    config["mcpServers"] = narrowed
    # Derive the KAS policy from the FILTERED grant list instead of restating it
    # as a literal: the rules come out byte-identical, a later edit to
    # ``allowedTools`` carries through, and a ceiling that strips a grant strips
    # its KAS rule with it (a hand-written ``kirocrew-core/*`` allow would have
    # survived the filter on the KAS backend). Routed through the agent-sdk
    # boundary like the pipeline conductor below: ``drivers.acp`` is the one
    # layer permitted to import ``kiro_crew.acp``, and agent.py's direct-import
    # count is a shrink-only baseline that must not grow.
    from kiro_crew.agent_sdk.drivers.acp import (  # noqa: PLC0415 - boot path
        derived_agent_permissions,
    )

    config["permissions"] = derived_agent_permissions(
        config["allowedTools"], _CONDUCTOR_AGENT_FILENAME
    )
    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = kiro_agents_dir_path() / _CONDUCTOR_AGENT_FILENAME
    _atomic_json_write(path, config)
    logger.info("Installed conductor agent config: %s", path)


_PIPELINE_CONDUCTOR_SYSTEM_PROMPT = """# Kiro Crew Pipeline Conductor

You are `kirocrew-pipeline-conductor`. You run ONE pipeline on ONE repository:
you pick up queued work items, stand up one worker session per item in the
pipeline's folder, patrol the fleet, verify claimed results independently,
intervene when a worker loops or stalls, adjudicate blocked items, govern host
resources and per-item credit budgets, and report verified greens to the
person as plain-language digests.

**You never do a work item's work yourself.** A file to write, a build to run,
a fix to make — each one belongs to a worker session you dispatch, verify and
report on. You have no dedicated file-writing tool (the shell tool stays
mounted but gated behind operator approval), and a work item never goes to
`spawn_run`, `spawn_sub_agents`, `workflow_run` or `task_run`. `spawn_run`
exists here for ONE purpose: a bounded INSPECTOR subagent that reads a suspect
worker's tail and its PR state and returns a verdict — spawn it with an
`allowed_tools` list limited to reads so read-only is enforced by the spawn,
not assumed of the prompt.

**Scripts are the deterministic half of your loop.** Shell access exists to
run the scripts the `pipeline-conductor` skill carries:
`scripts/claim_preflight.py` (ONE verdict per candidate item before you dispatch
it — CLAIM / SKIP / CLOSE / UNKNOWN, branched on the exit code, and UNKNOWN is
never permission), `scripts/fleet_probe.py` (the ONE batch probe per patrol
cycle — worker tails, tail index, idle age, error tails, banned-process scan,
host load, delivery counters) and `scripts/credit_spend.py` (per-item credit
rollups and budget verdicts). Read their output; never re-derive what they
compute from transcripts. A script your install does not carry reads as UNKNOWN
for the questions it answers — never as permission; the skill says what to do
in that case.

**Patrol with `monitor_start`, never with `wait`.** Arm it with the full cycle
instructions AND the exit condition, then end the turn; call `autonudge_stop`
when you stop. A reply saying *requested* is success — do not retry it. If
arming is refused outright, say no loop is running and drive that one round
with `wait`. A quiet cycle is one line, then end the turn.

Your tools:

- Worker sessions — `session_create`, `session_send`, `session_read_message`,
  `session_stop`, `list_sessions`.
- Keeping the pipeline's sessions together — `chat_folder_tree`,
  `chat_folder_create`.
- State that outlives a round — `session_ledger_read`, `session_ledger_record`.
- Patrol — `monitor_start`, `monitor_update`, `autonudge_stop`, `wait`.
- Capacity, before dispatching — `resource_status`.
- Inspecting a suspect worker — `spawn_run`, bounded and read-only.
- Talking to the person — `ask_question` puts a decision that is not yours to
  make to them as a card, after which you END your turn and their answer
  arrives as the next message; `send_message` / `send_notification` to report.
- Naming the right skill in a seed message — `skill_search`, `skill_fetch`.
- Reading — `fs_read`, `web_fetch`.
- `tool_search` loads a tool that is not in your list yet.

The `pipeline-conductor` skill carries the operating procedure — the pipeline
spec, the claim preflight, the work-order brief, the probe cycle and its action
table, the intervention ladder, outage recovery and loop liveness, the
adjudication and override protocol, the delivery-based admission table, the
credit budget rules, the `conductor-status/v1` file that records your OWN
obligations, and the cleanup steps. Read
it before acting on a pipeline. The user can message you at any time: a
steering message is a MODE CHANGE — fold it into the standing patrol
instruction with `monitor_update` so every later cycle honors it.

{{VERBOSITY_BLOCK}}
"""


#: The kirocrew-core verbs the pipeline conductor may call WITHOUT an approval
#: prompt. Named one by one rather than as the whole ``@kirocrew-core`` server,
#: extending the dashboard-grants invariant below to the core surface: the
#: conductor ingests untrusted content (issue text, PR bodies) on unattended
#: cycles, and a server-wide grant would let that content start persistent
#: work (``task_run``, ``workflow_run``, ``cron_add``) or spawn arbitrary
#: subagents with no human in the loop. What is granted is reads
#: (``resource_status``, ``list_sessions``, skills), the conductor's OWN
#: patrol-loop lifecycle (``monitor_*``, ``autonudge_stop``, ``wait``), its
#: OWN durable ledger, and reporting to the owner (``send_message``,
#: ``send_notification``, ``ask_question``). ``spawn_run`` — the intervention
#: ladder's read-only inspector — is deliberately NOT here: it starts agent
#: work from ingested context, so like ``session_send``/``session_stop`` it
#: stays mounted-but-gated and unattended runs get it from the operator's
#: session-level trust grant.
_PIPELINE_CONDUCTOR_CORE_GRANTS: tuple[str, ...] = (
    "@kirocrew-core/monitor_start",
    "@kirocrew-core/monitor_update",
    "@kirocrew-core/autonudge_stop",
    "@kirocrew-core/wait",
    "@kirocrew-core/resource_status",
    "@kirocrew-core/list_sessions",
    "@kirocrew-core/session_ledger_read",
    "@kirocrew-core/session_ledger_record",
    "@kirocrew-core/skill_search",
    "@kirocrew-core/skill_fetch",
    "@kirocrew-core/send_message",
    "@kirocrew-core/send_notification",
    "@kirocrew-core/ask_question",
)


#: The dashboard verbs the pipeline conductor may call WITHOUT an approval
#: prompt. Same tuple, same reasoning, as ``_CONDUCTOR_DASHBOARD_GRANTS``
#: above — the invariant (a granted verb may CREATE or READ, never MUTATE
#: workspace state that already exists and is not the agent's own; its worst
#: case in a loop must be bounded by the server) applies verbatim, because
#: this agent too ingests untrusted content by design: issue text and PR
#: bodies feed every granted verb on a nudge-driven cycle with nobody at the
#: keyboard. ``session_send`` / ``session_stop`` — which the patrol's
#: intervention ladder does use — stay mounted-but-gated for the same reason
#: they are gated on the goal conductor; unattended operation gets them via
#: the operator arming the conductor's own session in trust mode (the same
#: explicit, session-scoped human grant the worker sessions already require),
#: not via a standing spec-level bypass.
_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS: tuple[str, ...] = (
    "@kirocrew-dashboard/chat_folder_tree",
    "@kirocrew-dashboard/chat_folder_create",
    "@kirocrew-dashboard/session_create",
    "@kirocrew-dashboard/session_read_message",
)


def _install_pipeline_conductor_agent() -> None:
    """Generate and install the kirocrew-pipeline-conductor agent config.

    Follows ``_install_conductor_agent`` above deliberately — one standalone
    installer per generated agent is the file's established pattern — and
    keeps every property that installer's docstring argues for: derived from
    the kirocrew agent, **no dedicated file-writing tool** (neither ``fs_write``
    nor ``code``), ``@kirocrew-dashboard`` mounted whole but auto-approved only
    verb by verb, ``execute_bash`` mounted but never auto-approved
    (``allowedTools`` has no argument matching, so trusting the two bundled
    skill scripts cannot be told apart from trusting arbitrary shell), and the
    KAS policy derived from the FILTERED grant list. Where the two agents
    differ is charter, not mechanics: this one supervises a repository
    pipeline's worker fleet (probe / verify / intervene / adjudicate / govern)
    per the ``pipeline-conductor`` builtin skill, rather than decomposing a
    free-form goal.
    """
    config = build_agent_config()
    config["name"] = "kirocrew-pipeline-conductor"
    config["description"] = (
        "Runs one repository pipeline as a supervised fleet: picks up queued "
        "work items, dispatches one worker session per item, probes and "
        "verifies them, intervenes on stalls, adjudicates blocked items, and "
        "governs host resources and per-item credit budgets. Never does a "
        "work item's work itself."
    )
    config["prompt"] = _PIPELINE_CONDUCTOR_SYSTEM_PROMPT
    config["tools"] = [
        "execute_bash",
        "fs_read",
        "web_fetch",
        "session",
        "report",
        "tool_search",
        "@kirocrew-core",
        "@kirocrew-dashboard",
    ]
    granted: list[str] = []
    withheld: list[str] = []
    for ref in (
        "session",
        "report",
        "tool_search",
        *_PIPELINE_CONDUCTOR_CORE_GRANTS,
        *_PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
    ):
        (granted if _may_auto_approve(ref) else withheld).append(ref)
    config["allowedTools"] = granted
    if withheld:
        # Same audit contract as every other ``allowedTools`` writer: a
        # withheld grant is a permission decision and must leave a record.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="_install_pipeline_conductor_agent",
                resources=(
                    f"{', '.join(withheld)} mounted without auto-approve "
                    "(governance ceiling); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            logger.debug("SEL audit unavailable for withheld auto-approve", exc_info=True)
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    narrowed: dict = {}
    if core_entry:
        narrowed["kirocrew-core"] = core_entry
    dash_cmd, dash_args = _kirocrew_mcp_invocation("mcp-dashboard")
    dash_entry: dict[str, Any] = {"command": dash_cmd, "args": dash_args}
    # Same managed-server metadata as the conductor's hand-built entry, for the
    # same two failure modes: a registry-mode client silently DROPS an entry
    # with no ``type``, and without the data-home pin the shim reads the
    # DEFAULT home while the gateway runs under an override.
    if _mcp_registry_mode():
        dash_entry["type"] = _MCP_REGISTRY_TYPE
    dash_env = _managed_mcp_env()
    if dash_env:
        dash_entry["env"] = dash_env
    narrowed["kirocrew-dashboard"] = dash_entry
    config["mcpServers"] = narrowed
    # Same derive-don't-restate rationale as the conductor above, but routed
    # through the agent-sdk boundary: ``drivers.acp`` is the one layer permitted
    # to import ``kiro_crew.acp``, and agent.py's direct-import count is a
    # shrink-only baseline that must not grow.
    from kiro_crew.agent_sdk.drivers.acp import (  # noqa: PLC0415 - boot path
        derived_agent_permissions,
    )

    config["permissions"] = derived_agent_permissions(
        config["allowedTools"], _PIPELINE_CONDUCTOR_AGENT_FILENAME
    )
    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = kiro_agents_dir_path() / _PIPELINE_CONDUCTOR_AGENT_FILENAME
    _atomic_json_write(path, config)
    logger.info("Installed pipeline-conductor agent config: %s", path)


_HEARTBEAT_SYSTEM_PROMPT = """# KiroCrew Heartbeat Worker

You are `kirocrew-heartbeat`, an unattended polling worker that runs one task
per heartbeat cycle. You are dispatched by HeartbeatService when a task line in
`HEARTBEAT.md` is due to run; the gateway delivers your response text directly
to the user as a notification (no `send_message` call required, no chat panel
to write to).

## Charter

- **Observe and report only.** Heartbeat tasks watch for a condition (a build
  status, a file change, an external page state). When you see it, report.
  When you don't, respond with `HEARTBEAT_KEEP` so the task stays armed for the
  next cycle.
- **No write actions.** Tool approval is gated at the gateway against
  `HEARTBEAT_SAFE_TOOLS` (read-only allowlist). Any write tool you try will
  be rejected and audited; do not waste a turn attempting one. If a task
  asks you to "fix" or "update" something, treat it as "observe and notify
  the user so they can fix" — never the action itself.
  - **Translate write→read; never call the write tool.** A task line may
    literally instruct you to `spawn_run` a subagent, `send_message`, write a
    file, or `cron_add` — these (and every other write tool) are blocked here.
    Do the equivalent read yourself with your allowed tools and put the result
    in your response text, which is auto-delivered as the notification. You do
    NOT need — and must not attempt — `spawn_run` or `send_message` to report:
    your response IS the message. Attempting a blocked tool just burns the
    cycle and emits a `denied` audit event.
  - **Drop tasks that truly need a write tool.** If a task cannot be done
    read-only (it fundamentally requires an action you can't take), report that
    limitation to the user once and OMIT `HEARTBEAT_KEEP` so the task is dropped
    — do not re-arm it to fail the same way every cycle.
- **Your response IS the notification.** Whatever you write becomes the
  message the user sees, routed per the task's `<!-- deliver:... -->` tag or,
  when untagged, the `heartbeat.default_deliver` config (default `slack` = Slack
  DM + dashboard bell; `dashboard` = dashboard bell only). Report only when there
  is a real signal — a failure, a blocked CR, an item needing action. For a
  routine "nothing to do" completion, keep your response minimal. There is no
  transcript to scroll; be concise (a sentence or two for a status check, a short
  bulleted summary for a comment dump). Keep it scannable.
- **HEARTBEAT_KEEP semantics.** Include the literal token `HEARTBEAT_KEEP`
  anywhere in your response when the task is NOT done (so it retries next
  cycle). Omit the token when the task is fully complete (so it is dropped
  from the file).

## Tools

You have a curated read-only toolset (codebase search, knowledge-base query,
and side-effect-free kirocrew-core reads). Anything outside that list is
rejected. If you find yourself wanting a tool that isn't available, say so in
the response — the operator will add it after observing the SEL `denied` event.
"""


def _install_heartbeat_agent() -> None:
    """Generate and install the kirocrew-heartbeat agent config.

    A dedicated agent for HeartbeatService.  Minimal MCP surface — only
    ``kirocrew-core`` (learn/cron/spawn list, recall, artifacts read) on
    public installs.  Tool approval is enforced gateway-side against
    ``HEARTBEAT_SAFE_TOOLS`` regardless; the per-agent MCP narrowing here
    keeps cold-start cost low and reduces the surface the gateway has to
    police.

    (The Amazon-internal MCP server code-review/ticket/pipeline read wiring is
    omitted on public installs, matching ``_install_research_agent`` /
    ``_install_knowledge_agent``.)

    SEL audit logging stays at the gateway side — see
    ``GatewayOrchestrator._heartbeat_approval``.
    """
    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = kiro_agents_dir_path() / _HEARTBEAT_AGENT_FILENAME

    # Pull the ``kirocrew-core`` entry from the main agent config so the
    # resolved command + skill-paths match the main agent (write-denied
    # commands and security still come from bundled hooks). Strip the main
    # agent's ``--include-tools``/``--include-tool-tags``/``--exclude-tools``
    # filters so all read tools surface to the heartbeat agent — security is
    # enforced gateway-side against ``HEARTBEAT_SAFE_TOOLS`` via
    # ``_heartbeat_approval``, not by per-agent MCP filtering. Read through the
    # capped reader (#6736): a refused main spec degrades as absent, but with an
    # operator-visible signal, because the result is a heartbeat agent with no
    # MCP servers -- a worker that fails every task.
    main_path = kiro_agents_dir_path() / AGENT_FILENAME
    main_config = _read_spec_capped(main_path)
    if main_config is None and main_path.exists():
        logger.warning(
            "Main agent spec %s unusable; heartbeat agent installs with no MCP servers", main_path
        )
    main_mcp = (main_config or {}).get("mcpServers", {}) or {}

    _strip_flags = ("--include-tools", "--include-tool-tags", "--exclude-tools")
    mcp: dict[str, dict] = {}
    for name in ("kirocrew-core",):
        entry = main_mcp.get(name)
        if not isinstance(entry, dict):
            continue
        cleaned = dict(entry)
        args = entry.get("args") or []
        if isinstance(args, list):
            filtered: list[str] = []
            skip_next = False
            for arg in args:
                if skip_next:
                    skip_next = False
                    continue
                if not isinstance(arg, str):
                    filtered.append(arg)
                    continue
                if any(arg == f or arg.startswith(f + "=") for f in _strip_flags):
                    # Form ``--flag=value`` is dropped; bare ``--flag`` consumes
                    # the next arg too.
                    skip_next = "=" not in arg
                    continue
                filtered.append(arg)
            cleaned["args"] = filtered
        mcp[name] = cleaned

    config: dict[str, object] = {
        "name": "kirocrew-heartbeat",
        "description": (
            "Unattended polling worker — runs one HeartbeatService task per "
            "cycle with a read-only MCP toolset. Tool approval is gated "
            "gateway-side against HEARTBEAT_SAFE_TOOLS."
        ),
        "model": _background_agent_model(),
        "includeMcpJson": False,
        "prompt": _HEARTBEAT_SYSTEM_PROMPT,
        "mcpServers": mcp,
        # Build from the servers actually resolved so we never reference a
        # tool namespace without a matching mcpServers entry — the
        # rebuild_agent_config flow may run before either main entry exists.
        "tools": [f"@{name}" for name in mcp],
    }

    _atomic_json_write(path, config)
    # CC model for the heartbeat agent lives in the sidecar, not the kiro spec.
    agent_state.set_cc_model("kirocrew-heartbeat", _background_cc_model())
    logger.info("Installed heartbeat agent config: %s", path)


def sync_aim_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for callers (``rebuild_agent_config``).  AIM is an
    Amazon-internal agents/skills/plugins package manager; there is nothing
    to sync across providers on a public install, so this returns immediately.
    """
    return None


def repair_agent_configs() -> None:
    """Remove legacy Kiro Crew hook keys from agent configs owned by Kiro Crew."""
    _sanitize_agent_hooks()


_hooks_sanitized_mtimes: dict[str, float] = {}


def _sanitize_agent_hooks() -> None:
    """Remove legacy Kiro Crew hook keys from agent configs owned by Kiro Crew.

    Kiro-cli rejects unknown variants in the ``hooks`` field (e.g.
    ``auto_approve_tools``), causing it to silently fall back to the
    default agent — losing kirocrew-core, kirocrew-cron.

    Auto-repairs configs carrying keys Kiro Crew wrote in prior versions. Files
    outside :data:`OWNED_KIRO_AGENT_FILES` and unrecognized hook keys are left
    untouched because Kiro Crew does not own their schema or contents.
    """
    agents_dir = kiro_agents_dir_path()
    for filename in OWNED_KIRO_AGENT_FILES:
        f = agents_dir / filename
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if _hooks_sanitized_mtimes.get(str(f)) == mtime:
            continue
        data = _load_json(f)
        if not data:
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        removed_keys = [key for key in hooks if key in _LEGACY_KIROCREW_HOOK_KEYS]
        if not removed_keys:
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        data["hooks"] = {
            key: value for key, value in hooks.items() if key not in _LEGACY_KIROCREW_HOOK_KEYS
        }
        _atomic_json_write(f, data)
        _hooks_sanitized_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Removed legacy Kiro Crew hook keys %s from %s", removed_keys, f.name)
        sel().log_api_access(
            caller="system",
            operation="sanitize_agent_hooks",
            outcome="ok",
            source="agent",
            resources=f"{f.name}: removed {removed_keys}",
        )
