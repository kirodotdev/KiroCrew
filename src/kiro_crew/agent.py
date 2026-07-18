"""KiroCrew kiro-cli agent configuration.

Generates and installs ``kirocrew.json`` into ``~/.kiro/agents/``.

Configuration files (edit these, then ``kirocrew setup --agent-only``):

  ``src/kiro_crew/config/defaults.json``
      Base agent config — tools, model, allowedTools, toolsSettings, etc.

  ``src/kiro_crew/config/prompt.md``
      System prompt.

  ``~/.kirocrew/agent.json``
      User overrides merged on top of defaults (optional).

  ``~/.kirocrew/prompt.md``
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
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import agent_state, platform_compat
from kiro_crew.aim_agents import installed_kiro_packages_missing_from_cc
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import config_path as _mc_config_path
from kiro_crew.env import augmented_path
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform import current_context
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.platform import safe_context_call
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import (  # circular import: sel imports config which imports agent
    SecurityEvent,
    sel,
)

logger = logging.getLogger(__name__)


def _atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp+rename to prevent read-of-partial-file.

    kiro-cli reads agent configs at spawn and set_mode.  Non-atomic writes
    (truncate-then-write) can deliver empty or partial JSON, crashing the
    ACP process with exit code 1.  rename() is atomic on Linux when source
    and destination are on the same filesystem.

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
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"
AGENT_FILENAME = "kirocrew.json"
_MAIN_AGENT_NAME = "kirocrew"
# Cheap Claude Code model for KiroCrew's background agents (lite / heartbeat).
# Stored in the agent_state sidecar, never in the kiro spec (deny_unknown_fields).
_BACKGROUND_CC_MODEL = "claude-sonnet-4.6"
_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"
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


# User overrides
_USER_DIR = Path.home() / ".kirocrew"
_USER_PROMPT = _USER_DIR / "prompt.md"
_USER_OVERRIDES = _USER_DIR / "agent.json"

# kirocrew binary path — resolved lazily to handle gateway restarts
# where PATH may not include the virtualenv at import time.
_KIROCREW_BIN: str | None = None


def _bin_is_usable(path: Path) -> bool:
    """Return True if *path* is a readable file.

    Symbol preserved for callers; the previous Amazon-specific Apollo/Brazil
    wrapper-script rejection logic is a no-op on a public install (those
    binaries are absent), so any readable executable is accepted.
    """
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _kirocrew_bin_subpath(root: Path) -> Path:
    """The console-script path under an install ``root`` for this OS.

    A venv exposes its entry points under ``bin/kirocrew`` on POSIX but
    ``Scripts/kirocrew.exe`` on Windows — pip generates a ``.exe`` launcher
    there from the ``console_scripts`` entry point. Resolving the POSIX layout
    on Windows finds nothing, which silently drops the built-in
    ``kirocrew-cron`` / ``kirocrew-core`` MCP servers (``command not found:
    .../bin/kirocrew``). Branch on the platform so both layouts resolve.
    """
    if platform_compat.IS_WINDOWS:
        return root / "Scripts" / "kirocrew.exe"
    return root / "bin" / "kirocrew"


def _resolve_kirocrew_bin() -> str:
    """Resolve the absolute path of the ``kirocrew`` executable.

    Resolution order (first existing + executable wins):

    0. Frozen/PyInstaller app (the shipped desktop app): ``sys.executable``
       *is* the kirocrew CLI — e.g. ``.../kirocrew-backend`` — which accepts
       the ``mcp-core`` / ``mcp-cron`` subcommands. The bundle has no
       ``bin/kirocrew`` and nothing named ``kirocrew`` on PATH, so this is the
       only reliable handle; without it kirocrew-core/kirocrew-cron are dropped.
    1. Same install as the current process: walk up from ``kiro_crew.__file__``
       looking for a ``bin/kirocrew`` sibling. Covers venv-based installs and
       source-tree dev trees.
    2. ``shutil.which('kirocrew')`` — respects PATH order.
    3. Bare ``"kirocrew"`` — last resort, may fail but surfaces the problem
       instead of caching a known-bad absolute path.

    Every candidate is validated with ``is_file()`` and ``os.access(X_OK)``
    before being returned, so stale paths from previous installs are skipped.
    """
    global _KIROCREW_BIN
    if _KIROCREW_BIN:
        return _KIROCREW_BIN

    def _usable(p: str | Path) -> bool:
        sp = str(p)
        if not (sp and os.path.isfile(sp) and os.access(sp, os.X_OK)):
            return False
        return _bin_is_usable(Path(sp))

    # Frozen/PyInstaller app (shipped desktop app): ``sys.executable`` is the
    # bundled ``kirocrew-backend`` binary, which *is* the kirocrew CLI and
    # accepts the ``mcp-core`` / ``mcp-cron`` subcommands. The bundle ships no
    # ``bin/kirocrew`` and nothing named ``kirocrew`` on PATH, so this is the
    # only reliable handle — without it kirocrew-core / kirocrew-cron (and
    # therefore spawn_run / cron_add / learn_add …) get dropped.
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if _usable(exe):
            _KIROCREW_BIN = exe
            return _KIROCREW_BIN

    # 0. Prefer the venv entrypoint for source-tree installs (editable
    #    install with a sibling .venv directory, e.g. project/src/kiro_crew
    #    + project/.venv/bin/kirocrew).
    #    NOTE: For pip-into-venv installs where pkg_dir is inside .venv/,
    #    the pyvenv.cfg guard below breaks early and step 1 handles it.
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

    # 1. Walk up from the running package to find bin/kirocrew
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

    # 2. PATH lookup (also validated)
    found = shutil.which("kirocrew")
    if found and _usable(found):
        _KIROCREW_BIN = found
        return _KIROCREW_BIN

    # 3. Last resort — don't cache, so a future call can retry
    logger.warning(
        "Could not resolve kirocrew binary to an existing file; "
        "falling back to bare 'kirocrew' (MCP probes may fail)"
    )
    return "kirocrew"


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
    """
    bin_path = _resolve_kirocrew_bin()
    if bin_path == "kirocrew":  # unresolved sentinel from _resolve_kirocrew_bin
        return sys.executable, ["-m", "kiro_crew", subcommand]
    return bin_path, [subcommand]


# ---------------------------------------------------------------------------
# Managed MCP servers — single source of truth.
#
# Every server here is dynamically injected into the agent config at install
# time (both fresh and existing configs).  Adding a new managed server =
# one entry here.
# ---------------------------------------------------------------------------
_MANAGED_MCP_SERVERS: dict[str, dict] = {
    "kirocrew-cron": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-cron")},
    "kirocrew-core": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-core")},
}


def _extra_mcp_servers() -> dict[str, dict]:
    """Edition-contributed MCP servers from the active PlatformContext.

    The Default adapter returns ``{}`` so the standalone spec is byte-for-byte
    what it is today; the Amazon companion contributes builder-mcp (and other
    internal servers).  Entries are already in kiro-cli's ``mcpServers`` shape
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


def ensure_kirocrew_on_path(bin_dir: Path | None = None) -> str | None:
    """Ensure a ``kirocrew`` launcher is reachable on the user's PATH.

    The source ``install.sh`` symlinks ``~/.local/bin/kirocrew`` → the venv
    entry point, but install paths that don't run it (notably the packaged
    Electron app) leave no ``kirocrew`` on PATH — breaking the ``kirocrew``
    terminal command. This mirrors that symlink step in Python so it runs from
    ``kirocrew setup``. Best-effort and idempotent:

    * No-op if ``kirocrew`` already resolves on PATH to the same binary.
    * No-op if no concrete binary can be resolved (nothing to point at).
    * Otherwise (re)create ``<bin_dir>/kirocrew`` → the resolved binary.

    Args:
        bin_dir: Target directory for the shim. Defaults to ``~/.local/bin``.

    Returns:
        The shim path if one was created/updated, else ``None``.
    """
    target = _resolve_kirocrew_bin()
    # Nothing concrete to point at — bare "kirocrew" or a non-executable file.
    if not (os.path.isabs(target) and os.path.isfile(target) and os.access(target, os.X_OK)):
        return None

    # Already reachable on PATH as the same binary? Then there's nothing to do.
    existing = shutil.which("kirocrew")
    if existing and os.path.realpath(existing) == os.path.realpath(target):
        return None

    bin_dir = bin_dir or (Path.home() / ".local" / "bin")
    link = bin_dir / "kirocrew"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if os.path.realpath(link) == os.path.realpath(target):
                return None
            link.unlink()
        link.symlink_to(target)
    except OSError:
        logger.warning("Could not create kirocrew shim at %s", link, exc_info=True)
        return None
    logger.info("Linked kirocrew shim: %s -> %s", link, target)
    return str(link)


# One-time migrations performed automatically on gateway first-run (so the
# desktop app, which never runs `kirocrew setup`, still gets them).
_MIGRATIONS_DIR = _USER_DIR / ".migrations"
_STALE_MCP_PURGE_MARKER = _MIGRATIONS_DIR / "stale_managed_mcp_purged"


def run_first_run_setup() -> None:
    """Deliver the install-time steps the desktop app needs without a terminal.

    The Electron app only runs ``kirocrew gateway`` — never ``kirocrew
    setup`` — yet two concerns aren't covered by the gateway's agent-config
    rebuild. This is invoked from gateway startup to close that gap:

    * **PATH shim** — ``ensure_kirocrew_on_path()`` is idempotent and only
      writes ``~/.local/bin/kirocrew``, so it runs on every start.
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

    # 3. Stale managed-MCP purge — one-time, marker-guarded.
    if _STALE_MCP_PURGE_MARKER.exists():
        return
    try:
        from kiro_crew.mcp_cleanup import clean_stale_managed_mcp  # noqa: PLC0415

        removed = clean_stale_managed_mcp()
        if removed:
            logger.info("First-run: purged stale managed MCP entries: %s", removed)
        # Mark done even when nothing was removed, so the global mcp.json is
        # never re-read/rewritten on later starts.
        _MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        _STALE_MCP_PURGE_MARKER.write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
        )
    except Exception:
        logger.warning("First-run: stale MCP purge failed", exc_info=True)


def _prompt_path(mode: str = "") -> Path:
    """Return user prompt if it exists, otherwise shipped prompt.

    When mode="orchestrator", uses the orchestrator prompt.
    The conductor_skill config is independent — it controls agent routing, not the prompt.
    """
    if mode == "orchestrator":
        user_orch = _USER_DIR / "prompt-orchestrator.md"
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

    if _USER_PROMPT.is_file():
        return _USER_PROMPT
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
    - ``~/.kirocrew/skills`` (user-created)
    """
    paths: set[str] = set()
    # AIM skills — only known locations, not broad rglob
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
    user_skills = Path.home() / ".kirocrew" / "skills"
    if user_skills.is_dir():
        paths.add(str(user_skills))
    # Open-standard global location: ``~/.kiro/skills/`` — canonical home for
    # ``cp -r my-skill ~/.kiro/skills/`` installs and AIM-published skills
    # that follow the spec.  See docs/kiro-cli/skills.md.
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
                resources=redact(f"event={event} command={command[:200]}"),
                error=reason,
            )
        )
    except Exception:
        logger.debug("SEL audit for rejected hook failed", exc_info=True)


_VALID_HOOK_EVENTS = frozenset(
    {"preToolUse", "postToolUse", "userPromptSubmit", "agentSpawn", "stop"}
)


def _kiro_hooks_only(hooks: dict) -> dict:
    """Return only kiro-cli valid hook keys, stripping KiroCrew-internal ones."""
    return {k: v for k, v in hooks.items() if k in _VALID_HOOK_EVENTS}


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

      1. ``agent.kiro_hooks`` in ``~/.kirocrew/config.json`` -- explicit entries
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


def build_agent_config() -> dict:
    """Return the final agent config (shipped defaults + user overrides + dynamic fields).

    Security-critical fields (``deniedCommands``, ``hooks``) always use the
    bundled config as their base, even when a project-dir override is present.
    This prevents dev overrides from silently dropping security controls.
    User-defined ``kiro_hooks`` from ``~/.kirocrew/config.json`` are then
    additively merged; bundled hooks always run first and cannot be removed.
    """
    config = _load_json(_shipped_defaults())
    config = _deep_merge(config, _load_json(_USER_OVERRIDES))

    # Ensure deniedCommands and hooks always come from the bundled config,
    # even if the project-level defaults.json is stale.
    bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
    bundled_dc = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if bundled_dc:
        config.setdefault("toolsSettings", {}).setdefault("execute_bash", {})[
            "deniedCommands"
        ] = bundled_dc
    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot build agent config: hooks missing from bundled defaults")
    config["hooks"] = _kiro_hooks_only(bundled_hooks)

    # Merge user-defined kiro_hooks from ~/.kirocrew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    _apply_user_kiro_hooks(config, mc_cfg)

    # Dynamic fields — always resolved at install time
    config["prompt"] = f"file://{_prompt_path()}"
    mcp = config.setdefault("mcpServers", {})
    for name, spec in _MANAGED_MCP_SERVERS.items():
        if "invocation_fn" in spec:
            cmd, args = spec["invocation_fn"]()
        else:
            cmd = spec.get("command") or spec["command_fn"]()
            args = list(spec["args"])
        entry = {"command": cmd, "args": args}
        if "autoApprove" in spec:
            entry["autoApprove"] = list(spec["autoApprove"])
        mcp[name] = entry

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: standalone
    # contributes {} (unchanged), the Amazon companion adds builder-mcp etc.
    # Entries are already kiro-spec-shaped, so we only extend the map — no spec
    # restructuring, deny_unknown_fields invariant preserved.
    for name, spec in _extra_mcp_servers().items():
        mcp.setdefault(name, dict(spec))

    # Default-model tracking ("managed" vs frozen) is recorded in the
    # agent_state sidecar by the install path (rebuild_agent_config), never as
    # a kiro-spec key — kiro-cli rejects unknown fields and would drop the whole
    # spec. build_agent_config stays pure (no disk writes) so its many
    # read-only callers don't mutate managed-state as a side effect.
    return config


def _refresh_dynamic_fields(config: dict) -> None:
    """Update security-critical and dynamic fields in an existing config.

    Called when ``kirocrew.json`` already exists so user customizations are
    preserved while security controls and runtime paths stay current.
    """
    # Prompt URI — always resolve at install time
    config["prompt"] = f"file://{_prompt_path()}"

    # Managed MCP servers — ensure present and up-to-date.
    # Only refresh command/args; preserve user customizations (e.g. autoApprove).
    mcp = config.setdefault("mcpServers", {})
    for name, spec in _MANAGED_MCP_SERVERS.items():
        is_new = name not in mcp
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
        # Seed autoApprove only for genuinely new entries; if the user
        # deliberately removed autoApprove from an existing entry we
        # must not re-add it on every refresh.
        if "autoApprove" in spec and is_new:
            entry["autoApprove"] = list(spec["autoApprove"])

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: only seed a
    # server the user doesn't already have, so user customizations on a refresh
    # are preserved.  Standalone contributes {} (unchanged); Amazon adds
    # builder-mcp etc.  Already kiro-spec-shaped — no restructuring.
    for name, extra_spec in _extra_mcp_servers().items():
        mcp.setdefault(name, dict(extra_spec))

    # Security: deniedCommands and hooks always from bundled config.
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

    bundled_dc = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if not bundled_dc:
        raise RuntimeError(
            "Cannot refresh security fields: deniedCommands missing from bundled defaults"
        )
    config.setdefault("toolsSettings", {}).setdefault("execute_bash", {})[
        "deniedCommands"
    ] = bundled_dc

    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot refresh security fields: hooks missing from bundled defaults")
    config["hooks"] = _kiro_hooks_only(bundled_hooks)

    # Merge user-defined kiro_hooks from ~/.kirocrew/config.json (additive).
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
    if "model_managed" in config:
        if agent_state.get_model_managed(name) is None:
            agent_state.set_model_managed(name, bool(config["model_managed"]))
        del config["model_managed"]
    if "cc_model" in config:
        if agent_state.get_cc_model(name) is None and config["cc_model"]:
            agent_state.set_cc_model(name, str(config["cc_model"]))
        del config["cc_model"]

    # Default-model tracking: when the model is managed (not an explicit user
    # pick), re-sync it from the shipped defaults.json so a default bump
    # propagates to existing installs. Agents with no sidecar entry are
    # grandfathered and left untouched (never force-changed).
    if agent_state.get_model_managed(name):
        shipped_model = (_load_json(_shipped_defaults()) or {}).get("model")
        if shipped_model:
            config["model"] = shipped_model

    # config.json agent.model is the user-facing authority (kirocrew config set
    # agent.model). An explicit pick (not the "auto" sentinel) is propagated into
    # the agent file so kiro-cli's --agent startup load matches it; otherwise the
    # stale agent-file model shadows config.json and session/set_model loses the
    # startup race (Mesh-2292). "auto" defers to managed/shipped resolution above.
    mc_model = (mc_cfg.get("agent") or {}).get("model")
    if mc_model and mc_model != "auto":
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

    # tools/allowedTools: intentionally not modified on existing configs.
    # User controls these lists entirely.


def get_shipped_tools() -> dict[str, list[str]]:
    """Return shipped tool lists. Public API for cross-module use."""
    shipped = _load_json(_shipped_defaults()) or {}
    return {k: shipped.get(k, []) for k in ("tools", "allowedTools")}


def _load_existing_config(path: Path) -> tuple[dict, bool]:
    """Load and refresh an existing kirocrew.json.

    Returns (config, fresh_install).  Falls back to build_agent_config()
    when the file is corrupt or refresh fails.
    """
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        config = None
    if not isinstance(config, dict):
        return build_agent_config(), True
    try:
        _refresh_dynamic_fields(config)
    except (AttributeError, TypeError, RuntimeError) as exc:
        logger.error("Refresh failed, rebuilding from defaults: %s", exc)
        return build_agent_config(), True
    return config, False


def _norm_mcp_spec(spec: Any) -> Any:
    """Return the comparison form of an ``mcpServers`` spec for dedup.

    Setup / re-installs re-emit the same server across runs with slightly
    different optional-key *shapes*: a bare ``{"command": ...}`` one run, then
    ``"env": {}`` or ``"args": []`` the next. Comparing raw dicts treats those
    as distinct servers, so ``_normalize_mcp_server_keys`` mints an ever-growing
    ``-2``/``-3``... suffix on every build / reinstall / update (Mesh-2593).
    Dropping empty optional collections makes semantically identical re-merges
    collapse onto the canonical alias. An empty ``env``/``args`` is a launch
    no-op for kiro-cli (missing == empty), so this is also the cleaner spec to
    persist.
    """
    if not isinstance(spec, dict):
        return spec
    return {k: v for k, v in spec.items() if not (k in ("env", "args") and not v)}


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
    update (Mesh-2593). Convergence: any already-suffixed sibling that is an
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
    if not KIRO_AGENTS_DIR.is_dir():
        return 0
    cleaned = 0
    for spec_path in sorted(KIRO_AGENTS_DIR.glob("*.json")):
        try:
            data = json.loads(spec_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "model_managed" not in data and "cc_model" not in data:
            continue
        name = data.get("name") or spec_path.stem
        if "model_managed" in data:
            # Don't clobber an authoritative sidecar value with a stale spec one.
            if agent_state.get_model_managed(name) is None:
                agent_state.set_model_managed(name, bool(data["model_managed"]))
            del data["model_managed"]
        if "cc_model" in data:
            if agent_state.get_cc_model(name) is None and data["cc_model"]:
                agent_state.set_cc_model(name, str(data["cc_model"]))
            del data["cc_model"]
        try:
            _atomic_json_write(spec_path, data)
            cleaned += 1
        except OSError as exc:
            logger.warning("Could not rewrite cleaned agent spec %s: %s", spec_path, exc)
    if cleaned:
        logger.info("Cleaned %d kiro agent spec(s) of KiroCrew bookkeeping keys", cleaned)
    return cleaned


def rebuild_agent_config(*, clean: bool = False) -> Path:
    """Rebuild and write the merged kirocrew.json to ~/.kiro/agents/.

    This is the single authoritative function for producing the agent config.
    It reads all source files, merges with correct priority, resolves commands,
    and injects fresh AIM skill paths.

    Merge priority (highest wins):
      1. ~/.kirocrew/mcp.json (agent-specific overrides)
      2. ~/.kiro/settings/mcp.json (kiro global, fills gaps)
      3. Existing kirocrew.json (preserves user customizations)
      4. Bundled defaults (security, managed servers)

    --skill-paths are always resolved fresh from AIM manifests regardless
    of what any source file contains.

    When the config already exists and *clean* is False, the existing file
    is used as the base so that **all** user customizations are preserved.
    Only security-critical fields (``deniedCommands``, ``hooks``) and
    dynamic fields (``prompt`` URI, kirocrew MCP server commands) are
    refreshed from defaults.

    Args:
        clean: If True, ignore existing config and regenerate from defaults.
    """
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = KIRO_AGENTS_DIR / AGENT_FILENAME

    # One-time (idempotent) self-heal: strip KiroCrew bookkeeping keys from
    # every kiro agent spec into the sidecar so kiro-cli accepts them all.
    migrate_agent_specs()

    # Managed MCP sync happens after config is fully built (see below).

    if not clean and path.exists():
        # Existing config — preserve user customizations, only refresh
        # security-critical and dynamic fields.
        config, fresh_install = _load_existing_config(path)
    else:
        config = build_agent_config()
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
    # See docs/mcp-architecture.md. CC global is kept only as a gap-filler so
    # the Claude Code provider can be re-enabled later without rework; it must
    # not shadow a Kiro-global entry.
    managed_names = set(_MANAGED_MCP_SERVERS)
    shared_mcp = _load_json(_KIRO_MCP_JSON).get("mcpServers", {})
    for name, spec in shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy so config never aliases the source dict — a later update()
            # (kirocrew merge) must not mutate shared_mcp, which is reused as a
            # fallback candidate during command validation below.
            config.setdefault("mcpServers", {}).setdefault(name, dict(spec))

    # Merge shared MCP servers from ~/.claude.json (Claude Code user-level
    # config) — now LOWER priority than Kiro global; setdefault is a no-op when
    # Kiro already populated the same key, so CC only fills gaps.
    cc_shared_mcp = _load_json(_CC_MCP_JSON).get("mcpServers", {})
    for name, spec in cc_shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy (see note above) so cc_shared_mcp stays pristine for the
            # fallback-candidate lookup.
            config.setdefault("mcpServers", {}).setdefault(name, dict(spec))

    # ~/.kirocrew/mcp.json overrides kiro mcp.json for the kirocrew agent —
    # kirocrew-specific config wins in a tie.
    # Uses update() to merge into existing specs, preserving user-set fields
    # like autoApprove while letting kirocrew's command/args/env win.
    # Skip managed servers for the same reason as above.
    kirocrew_mcp = _load_json(_USER_DIR / "mcp.json").get("mcpServers", {})
    for name, spec in kirocrew_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            mcps = config.setdefault("mcpServers", {})
            if name in mcps and isinstance(mcps[name], dict):
                # mcps[name] is a private copy (globals were copied in above),
                # so update() no longer mutates any source dict.
                mcps[name].update(spec)
            else:
                mcps[name] = dict(spec)

    # Resolve MCP commands to absolute paths and validate.
    #
    # Resolution-aware fallback: a server can be defined in several sources
    # with different commands.  If the merged winner's command does not
    # resolve (e.g. a bare command whose binary isn't on the rebuild PATH —
    # the classic builder-mcp shadowing case), fall back to the SAME server's
    # spec from the other sources before dropping it, in priority order
    # (kirocrew > kiro-global > cc-global).  This prevents one source's
    # unresolvable command from killing a server another source can resolve.
    def _resolve_command(cmd: str, env: dict | None) -> str | None:
        """Resolve an MCP command to an absolute path, or None if not found.

        Accepts an absolute path directly when the file exists and is
        executable — shutil.which can fail inside user-namespace sandboxes
        even when the file is fine.

        Searches the server's own env.PATH first, then the same augmented
        PATH the MCP probe uses (mcp_discovery.probe_server →
        env.augmented_path). Sharing augmented_path — instead of a hand-built
        dir list — keeps agent-config-build resolution and probe resolution
        from diverging: a divergence previously let a server probe healthy on
        the dashboard while being silently dropped from the generated agent
        config ("command not found: kirocrew", Mesh-2329). augmented_path
        covers ~/.aim/mcp-servers and ~/.toolbox/bin (previously hardcoded
        here) and appends the running interpreter's console-scripts dir
        (venv ``Scripts\\`` on Windows, ``bin/`` on POSIX) as a last-resort
        fallback for pip-generated wrappers like ``kirocrew``.
        """
        if not cmd:
            return None
        if os.path.isabs(cmd) and os.path.isfile(cmd) and os.access(cmd, os.X_OK):
            return cmd
        env_path = (env or {}).get("PATH", "")
        base = os.pathsep.join(filter(None, [env_path, os.environ.get("PATH", "")]))
        return shutil.which(cmd, path=augmented_path(base))

    valid_servers: dict[str, Any] = {}
    for name, spec in config.get("mcpServers", {}).items():
        if not isinstance(spec, dict):
            continue
        # Remote Streamable HTTP servers — preserve as-is (url-based, no command)
        if spec.get("url"):
            valid_servers[name] = spec
            continue
        # Build candidate specs in priority order: the merged winner first,
        # then the same server from each source as a resolution fallback.
        candidates: list[tuple[str, dict]] = [("winner", spec)]
        for label, src in (
            ("kirocrew", kirocrew_mcp),
            ("kiro-global", shared_mcp),
            ("cc-global", cc_shared_mcp),
        ):
            alt = src.get(name)
            if isinstance(alt, dict) and alt is not spec:
                candidates.append((label, alt))

        resolved: str | None = None
        chosen: dict = spec
        tried: list[str] = []
        had_any_command = False
        for label, cand in candidates:
            cmd = cand.get("command", "")
            if cmd:
                had_any_command = True
            r = _resolve_command(cmd, cand.get("env"))
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
            valid_servers[name] = merged
        elif not had_any_command:
            # No candidate defined a command at all — distinct from a command
            # that was defined but couldn't be resolved.
            logger.warning("Dropping MCP server %r: no command", name)
        else:
            logger.warning(
                "Dropping MCP server %r: command not found: %s",
                name,
                spec.get("command", ""),
            )
            logger.debug("MCP %r resolution failed; tried %s", name, "; ".join(tried))
    config["mcpServers"] = valid_servers

    # Rewrite slash-containing server keys to kiro-safe aliases (also migrates
    # already-broken configs); runs after merges so global-only servers and
    # their stale @refs are normalized too. See mcp_server_alias / Mesh-1956.
    _normalize_mcp_server_keys(config)

    # Sync shared (user-installed) servers to tools/allowedTools.
    # These are explicitly installed by the user via `aim mcp install` or
    # manual mcp.json edits — unlike managed servers, they should always
    # be registered regardless of fresh/existing config state.
    _shared_added: list[str] = []
    _shared_removed: list[str] = []
    for name, spec in itertools.chain(cc_shared_mcp.items(), shared_mcp.items()):
        if not isinstance(spec, dict) or name in managed_names:
            continue
        alias = mcp_server_alias(name)
        ref = f"@{alias}"
        if spec.get("disabled"):
            for key in ("tools", "allowedTools"):
                lst = config.get(key)
                if lst is not None and ref in lst:
                    lst.remove(ref)
                    if ref not in _shared_removed:
                        _shared_removed.append(ref)
        elif alias in valid_servers:
            valid_servers[alias].pop("disabled", None)
            for key in ("tools", "allowedTools"):
                if ref not in config.get(key, []):
                    config.setdefault(key, []).append(ref)
                    if ref not in _shared_added:
                        _shared_added.append(ref)
    if _shared_added:
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_added)} added to tools/allowedTools (shared)",
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
        for mcp_name in _MANAGED_MCP_SERVERS:
            ref = f"@{mcp_name}"
            if mcp_name in valid_servers:
                if ref not in config.get("tools", []):
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

    # Final dedup (preserves order).
    for key in ("tools", "allowedTools"):
        config[key] = list(dict.fromkeys(config.get(key, [])))

    _atomic_json_write(path, config)
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

    # Bidirectional sync: ensure packages installed for one provider
    # are also available for the other (agents↔plugins, skills).
    sync_aim_packages()

    # Security: enforce deniedCommands + sanitize invalid hook keys
    repair_agent_configs()

    return path


_LITE_AGENT_FILENAME = "kirocrew-lite.json"

_KIROCREW_AIM_PACKAGE = "KiroCrewAICapabilities"

_LITE_AGENT_NAMES = frozenset({_LITE_AGENT_FILENAME, f"{_KIROCREW_AIM_PACKAGE}-kirocrew-lite.json"})


# Backward-compat alias — callers may still use the old name.
install_agent = rebuild_agent_config


def is_aim_package_installed(package: str) -> bool:
    """Check if an AIM agents package is already installed.

    AIM is an Amazon-internal package manager and is absent on a public
    install, so this returns ``False`` unless an ``aim`` binary happens to
    be on PATH and reports the package.
    """
    aim = shutil.which("aim")
    if not aim:
        return False
    try:
        result = subprocess.run(
            [aim, "agents", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return any(line.startswith(package) for line in (result.stdout or "").splitlines())
    except Exception:
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


def _remove_bare_lite_if_aim_installed() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for backward compatibility.  Previously removed the
    bare ``kirocrew-lite.json`` when an AIM-installed duplicate existed; with
    AIM install neutralized there is no AIM-managed copy to deduplicate.
    """
    return None


def _install_lite_agent_fallback() -> None:
    """Write a bare kirocrew-lite config (cheap background agent)."""
    lite_path = KIRO_AGENTS_DIR / _LITE_AGENT_FILENAME
    lite_config = {
        "name": "kirocrew-lite",
        "model": "claude-opus-4.6",
        "tools": [],
        "mcpServers": {},
        "prompt": "",
    }
    _atomic_json_write(lite_path, lite_config)
    # Cheap model for the claude_code (CC) provider. kiro-cli resolves the lite
    # model from `model` via --agent; the CC backend can't, so the provider
    # factory reads this cc_model for the lite agent. Sonnet is plenty for
    # background title/compaction/heartbeat work and far cheaper than the global
    # Opus 4.8 default. Stored in the sidecar (kiro spec stays schema-clean).
    agent_state.set_cc_model("kirocrew-lite", _BACKGROUND_CC_MODEL)


_KNOWLEDGE_AGENT_FILENAME = "kirocrew-knowledge.json"

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
    extraction.  It uses claude-haiku-4.5 (cheapest model).  The previous
    Amazon-internal ``builder-mcp`` / ReadInternalWebsites wiring is omitted
    on public installs; the agent ships without MCP servers and relies on the
    model's own capabilities for extraction.  Symbol preserved for callers.
    """
    path = KIRO_AGENTS_DIR / _KNOWLEDGE_AGENT_FILENAME

    config: dict[str, object] = {
        "name": "kirocrew-knowledge",
        "description": (
            "Dedicated agent for knowledge extraction, categorization, " "and summarization."
        ),
        "model": "claude-haiku-4.5",
        "includeMcpJson": False,
        "prompt": _KNOWLEDGE_SYSTEM_PROMPT,
        "mcpServers": {},
        "tools": [],
    }

    _atomic_json_write(path, config)
    logger.info("Installed knowledge agent config: %s", path)


_RESEARCH_AGENT_FILENAME = "kirocrew-research.json"

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
5. Decide direction: choose the single highest-value next step toward the question —
   a sub-question, a follow-up a prior finding surfaced, or shoring up weak evidence.
   Steer toward closing the goal; don't just walk the list.
6. Investigate that one step using one source/tool.
7. Record: write `findings/cycle_NNN.json` where **NNN = the count of existing
   `findings/cycle_*.json` files, zero-padded to 3 digits** (first cycle ->
   `cycle_000.json`, next -> `cycle_001.json`, ...). NEVER reuse or overwrite an
   existing cycle file. Keys: `cycle` (= NNN), `summary, sources_checked,
   sources_empty, new_findings_count, evidence_strength, key_insight,
   sub_question`; append the cycle to `FINDINGS.md` with citations;
   then rewrite its short `## Research State` (open questions, leads, dead-ends,
   weak spots) for the next cycle.
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
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = KIRO_AGENTS_DIR / _RESEARCH_AGENT_FILENAME
    _atomic_json_write(path, config)
    logger.info("Installed research agent config: %s", path)


_HEARTBEAT_AGENT_FILENAME = "kirocrew-heartbeat.json"

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

    (The Amazon-internal ``builder-mcp`` CR/ticket/pipeline read wiring is
    omitted on public installs, matching ``_install_research_agent`` /
    ``_install_knowledge_agent``.)

    SEL audit logging stays at the gateway side — see
    ``GatewayOrchestrator._heartbeat_approval``.
    """
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = KIRO_AGENTS_DIR / _HEARTBEAT_AGENT_FILENAME

    # Pull the ``kirocrew-core`` entry from the main agent config so the
    # resolved command + skill-paths match the main agent (write-denied
    # commands and security still come from bundled hooks). Strip the main
    # agent's ``--include-tools``/``--include-tool-tags``/``--exclude-tools``
    # filters so all read tools surface to the heartbeat agent — security is
    # enforced gateway-side against ``HEARTBEAT_SAFE_TOOLS`` via
    # ``_heartbeat_approval``, not by per-agent MCP filtering.
    main_config = _load_json(KIRO_AGENTS_DIR / AGENT_FILENAME)
    main_mcp = main_config.get("mcpServers", {}) or {}

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
        "model": "claude-sonnet-4.6",
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
    agent_state.set_cc_model("kirocrew-heartbeat", _BACKGROUND_CC_MODEL)
    logger.info("Installed heartbeat agent config: %s", path)


def sync_aim_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for callers (``rebuild_agent_config``).  AIM is an
    Amazon-internal agents/skills/plugins package manager; there is nothing
    to sync across providers on a public install, so this returns immediately.
    """
    return None


def repair_agent_configs() -> None:
    """Enforce security controls and sanitize invalid keys in all agent configs."""
    _enforce_denied_commands()
    _sanitize_agent_hooks()


def _ensure_cc_parity_for_kiro_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Previously warned when an AIM package was installed for the kiro
    provider but not for Claude Code.  AIM is Amazon-internal, so
    ``installed_kiro_packages_missing_from_cc`` returns an empty list on a
    public machine and there is nothing to warn about.  The call is retained
    (and stays harmless) to preserve the cross-provider parity contract if a
    user happens to have an ``aim`` binary on PATH.
    """
    try:
        _ = installed_kiro_packages_missing_from_cc()
    except Exception:
        logger.debug("CC parity check failed", exc_info=True)


_denied_cmd_mtimes: dict[str, float] = {}
_last_skipped_set: frozenset[str] = frozenset()


def _enforce_denied_commands() -> None:
    """Inject deniedCommands from bundled defaults into agent configs.

    Scope controlled by ``agent.enforce_denied_commands`` in config:
      - ``"all"`` (default): enforce on every installed agent.
      - ``"kirocrew"``: only enforce on kirocrew.json, skip other agents.

    Runs at: install_agent(), start_pool() (gateway startup),
    _cleanup_loop() (~60s periodic). Uses mtime to skip unchanged files.
    """
    bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
    denied = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if not denied:
        return

    # Determine scope from config
    try:
        scope = KiroCrewConfig.load().agent.enforce_denied_commands
    except Exception as exc:
        logger.debug("Failed to load enforce_denied_commands scope, defaulting to 'all': %s", exc)
        scope = "all"

    kirocrew_names = frozenset(
        f.name for f in KIRO_AGENTS_DIR.glob("*.json") if "kirocrew" in f.name.lower()
    )

    skipped: list[str] = []

    for f in KIRO_AGENTS_DIR.glob("*.json"):
        if f.name in _LITE_AGENT_NAMES:
            continue
        if scope == "kirocrew" and f.name not in kirocrew_names:
            skipped.append(f.name)
            continue
        try:
            mtime = f.stat().st_mtime
            if _denied_cmd_mtimes.get(str(f)) == mtime:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        ts = data.setdefault("toolsSettings", {})
        bash = ts.setdefault("execute_bash", {})
        shell = ts.setdefault("shell", {})
        existing_bash = set(bash.get("deniedCommands", []))
        existing_shell = set(shell.get("deniedCommands", []))
        required = set(denied)

        if existing_bash == required and existing_shell == required:
            _denied_cmd_mtimes[str(f)] = mtime
            continue
        # Replace entirely — bundled defaults are the canonical source.
        # User-added patterns via dashboard are not supported; all
        # security patterns must ship in agents/defaults.json.
        bash["deniedCommands"] = sorted(required)
        shell["deniedCommands"] = sorted(required)
        _atomic_json_write(f, data)
        _denied_cmd_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Enforced deniedCommands on %s", f.name)

    if skipped:
        skipped_set = frozenset(skipped)
        global _last_skipped_set
        if skipped_set != _last_skipped_set:
            _last_skipped_set = skipped_set
            sel().log_api_access(
                caller="system",
                operation="enforce_denied_commands.skip",
                outcome="ok",
                source="agent",
                resources=",".join(sorted(skipped)),
            )


_hooks_sanitized_mtimes: dict[str, float] = {}


def _sanitize_agent_hooks() -> None:
    """Remove KiroCrew-internal hook keys from kiro-cli agent configs.

    Kiro-cli rejects unknown variants in the ``hooks`` field (e.g.
    ``auto_approve_tools``), causing it to silently fall back to the
    default agent — losing kirocrew-core, kirocrew-cron.

    Runs alongside ``_enforce_denied_commands`` to auto-repair configs
    for users who already have the invalid key from prior versions.
    """
    for f in KIRO_AGENTS_DIR.glob("*.json"):
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
        clean_hooks = _kiro_hooks_only(hooks)
        if len(clean_hooks) == len(hooks):
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        invalid_keys = [k for k in hooks if k not in _VALID_HOOK_EVENTS]
        data["hooks"] = clean_hooks
        _atomic_json_write(f, data)
        _hooks_sanitized_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Removed invalid hook keys %s from %s", invalid_keys, f.name)
        sel().log_api_access(
            caller="system",
            operation="sanitize_agent_hooks",
            outcome="ok",
            source="agent",
            resources=f"{f.name}: removed {invalid_keys}",
        )
