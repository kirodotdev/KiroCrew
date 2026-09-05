"""Theme-pack HTTP handlers — install / list / detail / delete / asset serving.

REST endpoints for the installable theme-pack subsystem. The validation and
parsing core lives in :mod:`kiro_crew.dashboard.theme_validate`; this module
imports from it and never the other way around.

Endpoints
---------
- ``GET    /api/themes``                       list custom + installed themes
- ``POST   /api/themes``                       create an editor custom theme
- ``POST   /api/themes/install``               install a pack (local / GitHub)
- ``GET    /api/themes/{slug}``                installed-pack detail (+ level)
- ``PUT    /api/themes/{slug}``                update an editor custom theme
- ``DELETE /api/themes/{slug}``                remove an installed pack
- ``GET    /api/theme/{slug}/assets/{path}``   static asset (nosniff + CSP)
- ``GET    /api/theme/{slug}/overlay/{id}``    sandboxed overlay HTML
- ``GET    /api/theme/{slug}/topbar/{mode}``   sandboxed topbar HTML
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.theme_validate import (
    _THEME_ASSET_CSP,
    _THEME_ASSET_CT,
    _THEME_CLONE_TIMEOUT_SEC,
    _THEME_DEFAULT_EMOJI,
    _THEME_EMOJI_MAX_LEN,
    _THEME_FILE_CAPS,
    _THEME_GITHUB_HOSTS,
    _THEME_MANIFEST_NAME,
    _THEME_META_IGNORE,
    _THEME_OVERLAY_CSP,
    _THEME_TOTAL_BYTES_BY_LEVEL,
    _installed_theme_dir,
    _read_json_file,
    _resolve_theme_asset,
    _safe_theme_slug,
    _slugify_theme_name,
    _strip_to_allowed_vars,
    _theme_asset_descriptor,
    _themes_dir,
    _validate_theme_data,
    _validate_theme_dir,
)
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import (
    is_unc_shape,
    safe_read_file_bytes_nolink,
    unc_probe_allowed,
)
from kiro_crew.platform_compat import (
    IS_WINDOWS,
    first_linked_ancestor,
    is_link_or_junction,
)
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    run_limited,
    sandboxed_spawn_argv,
)
from kiro_crew.security import (
    is_sensitive_path,
    redact_and_truncate,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

_THEME_GIT_SANDBOX_UNAVAILABLE = "theme install sandbox is unavailable on this server"


def _list_themes_sync() -> list[dict[str, Any]]:
    """Blocking enumeration of custom + installed themes (run off-loop)."""
    themes_path = _themes_dir()
    result: list[dict[str, Any]] = []
    if themes_path.is_dir():
        for f in themes_path.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                result.append(
                    {
                        "slug": f.stem,
                        "name": data.get("name", f.stem),
                        "emoji": data.get("emoji", "🎨"),
                        "created_at": data.get("created_at", ""),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue
        # Installed themes are directories (<slug>/) carrying a theme.json.
        # Dot-prefixed dirs are transient install staging/backup snapshots
        # (.install-staging-*, .<slug>.old-*) — never valid slugs, never listed.
        for d in sorted(
            p
            for p in themes_path.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
        ):
            mpath = d / "theme.json"
            if not mpath.is_file():
                continue
            try:
                m = json.loads(mpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            result.append(
                {
                    "slug": d.name,
                    "name": m.get("name", d.name),
                    "emoji": m.get("emoji", _THEME_DEFAULT_EMOJI),
                    "created_at": m.get("created_at", ""),
                    "source": "installed",
                    "level": m.get("level", 0),
                }
            )
    # Sort by created_at (oldest first), falling back to name
    result.sort(key=lambda t: t.get("created_at") or "9999")
    return result


async def api_themes(request: web.Request) -> web.Response:
    """GET /api/themes — list all custom themes, sorted by creation date."""
    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _list_themes_sync
    )
    return web.json_response({"themes": result})


async def api_themes_create(request: web.Request) -> web.Response:
    """POST /api/themes — create a new custom theme."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    err = _validate_theme_data(body)
    if err:
        return web.json_response({"error": err}, status=400)

    name = body["name"].strip()
    slug = _slugify_theme_name(name)
    emoji = (
        body.get("emoji", _THEME_DEFAULT_EMOJI).strip()[:_THEME_EMOJI_MAX_LEN]
        or _THEME_DEFAULT_EMOJI
    )

    # No NEW filesystem call on the loop: ``_themes_dir()`` returns the
    # ``config_dir()`` memo resolved at boot, and the mkdir, the collision
    # stats, and the write all happen inside ``_create_locked`` on a worker
    # thread. On a UNC/SMB-backed data home a single on-loop stat or mkdir
    # can block the whole gateway.
    themes_path = _themes_dir()
    target = themes_path / f"{slug}.json"

    theme_data = {
        "name": name,
        "slug": slug,
        "emoji": emoji,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dark": _strip_to_allowed_vars(body.get("dark", {})),
        "light": _strip_to_allowed_vars(body.get("light", {})),
    }

    # Serialize the collision check + write per slug so two concurrent POSTs
    # can't both pass the existence check and race into the same file (TOCTOU):
    # the authoritative check happens INSIDE the lock, and the write is atomic.
    # The same per-slug lock is shared with the installer, and we reject BOTH an
    # existing <slug>.json AND an installed <slug>/ dir — otherwise a create that
    # lands right after an install would leave two same-slug themes (a .json
    # record and a dir), the duplicate-slug corruption the installer guards too.
    def _create_locked() -> bool:
        with _theme_install_lock(slug):
            themes_path.mkdir(parents=True, exist_ok=True)
            if target.exists() or _installed_theme_dir(slug).exists():
                return False
            _atomic_write_theme_json(
                target, json.dumps(theme_data, indent=2) + "\n"
            )
            return True

    created = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _create_locked
    )
    if not created:
        return web.json_response({"error": f"theme '{slug}' already exists"}, status=409)
    return web.json_response({"ok": True, "slug": slug, "theme": theme_data})


def _resolve_local_source(path_str: str) -> tuple[Path | None, str | None]:
    """Resolve a user-supplied local folder path to an existing directory."""
    if not isinstance(path_str, str) or not path_str.strip():
        return None, "local 'path' is required"
    p = Path(path_str).expanduser()
    # EVERY filesystem call below is too late for these two, so both are screened
    # first. `expanduser` only reads the environment and touches nothing.
    #
    # UNC: a UNC path names a HOST, so `is_dir()` on user-supplied
    # `\\attacker\share` makes Windows open an SMB connection and authenticate,
    # handing this gateway's credentials to a host the caller picked. The check is
    # purely lexical (`normpath`/`normcase`) and never touches the network itself
    # -- the same chokepoint acp/prompt_blocks.py applies to attachment paths.
    # `unc_probe_allowed` keeps the one legitimate case working: a roaming profile
    # whose home directory is itself a UNC share. Both the raw text and the
    # expanded form are screened, since expansion is what actually gets stat-ed.
    if IS_WINDOWS:
        for candidate in (path_str, str(p)):
            if is_unc_shape(candidate) and not unc_probe_allowed(candidate):
                return None, "local path is not an allowed location"
        # A LINKED ANCESTOR defeats the lexical screen above, because the path
        # being probed is not itself UNC-shaped -- only the link's target is.
        # This has to run before EVERY check below, including the leaf one:
        # `is_link_or_junction` is an `lstat`, and while an lstat does not
        # follow the FINAL component it still resolves every ancestor, so the
        # leaf probe would itself traverse the junction and make the SMB
        # connection this screen exists to prevent. Windows-only on purpose:
        # the harm here is that the PROBE is the attack, which is a UNC
        # property. On POSIX, stat-ing through a symlink is harmless and the
        # real guard is `is_sensitive_path` on the RESOLVED path below -- so
        # gating this keeps macOS installs from `/tmp` and `/var` (symlinks to
        # `/private/*`) working, which an unconditional walk would refuse.
        # The reply matches the leaf case on purpose: which ancestor is a link
        # is filesystem layout, and the caller supplied a path to guess at it.
        if first_linked_ancestor(p) is not None:
            return None, "local path must not be a symlink"
    # Root junction: `Path.is_symlink()` is False for a Windows junction, so a
    # junction as the pack ROOT would pass an islink-only check and be resolved
    # through -- the same predicate gap the copy walk closes for subdirectories.
    if is_link_or_junction(p):
        return None, "local path must not be a symlink"
    if not p.is_dir():
        return None, f"not a directory: {path_str}"
    resolved = p.resolve()
    # The source folder is user/agent-influenced (CodeQL "uncontrolled data in
    # path expression", agents.py). Block credential / trust-root locations at
    # the source so a theme install can never read them — defense-in-depth on
    # top of the downstream allowlist, which already drops non-theme files.
    # Same guard used for the other user-path surfaces in this module.
    if is_sensitive_path(str(resolved)):
        return None, "local path is not an allowed location"
    return resolved, None


def _clone_github(url: str, dest: Path) -> str | None:
    """Shallow-clone an https github.com repo into ``dest``. Error or None.

    https-only + host allowlist, argv (never shell) so the URL can't inject a
    command, and a bounded timeout.
    """
    if not isinstance(url, str) or not url.strip():
        return "github 'url' is required"
    parsed = urllib.parse.urlparse(url)
    # Reject credential-bearing / decorated URLs outright: userinfo could leak
    # via git error output, and query/fragment have no meaning for a repo URL.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "github URL must not contain credentials, query, or fragment"
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _THEME_GITHUB_HOSTS:
        return "only https github.com URLs are allowed"
    # The URL is agent/user-influenced and git clone runs arbitrary remote
    # content, so route through the sandbox chokepoint (OS filesystem isolation
    # + credential-scrubbed env) and apply the fork-bomb/resource ceiling via
    # run_limited — same discipline as git_coord._git.
    try:
        argv, env, cleanup = sandboxed_spawn_argv(
            ["git", "clone", "--depth", "1", "--quiet", "--", url, str(dest)]
        )
    except SandboxUnavailableError:
        # Translate the typed sandbox refusal at this boundary. In particular,
        # Windows has no process-sandbox backend, but local theme installs and
        # every descriptor-contained read route remain supported there.
        return _THEME_GIT_SANDBOX_UNAVAILABLE
    try:
        proc = run_limited(
            argv,
            capture_output=True,
            timeout=_THEME_CLONE_TIMEOUT_SEC,
            env=env,
            **UTF8_TEXT,
        )
    except FileNotFoundError:
        return "git is not available on the server"
    except subprocess.TimeoutExpired:
        return "git clone timed out"
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    if proc.returncode != 0:
        # Redact the FULL text before the bound: a credential straddling the
        # slice would otherwise be cut into fragments no redaction regex can
        # match (same class as PR #7316 / #7350).
        _red = redact_and_truncate(proc.stderr.strip(), 200)
        return f"git clone failed: {_red}"
    return None


def _copy_installed_theme(src: Path, dst: Path) -> None:
    """Copy the theme tree from ``src`` into a private ``dst`` snapshot via a
    per-file, symlink-rejecting, byte-bounded loop, overwriting same-name files.

    TOCTOU hardening: the source dir is
    user-controlled and stays writable throughout, so NOTHING read from it can
    be trusted against an earlier walk. First: ``shutil.copytree(...,
    symlinks=False)`` FOLLOWS links, so a file swapped for a symlink between
    validate and copy would get its *target's* bytes copied — we walk without
    following directory links, refuse any non-regular entry, and read each
    file through ``safe_read_file_bytes_nolink`` (open with ``O_NOFOLLOW`` +
    fd-path containment inside ``src``) so a swapped symlink is rejected,
    never dereferenced. Second: even a regular-file→regular-file swap could
    promote unvalidated content, and an unbounded ``read_bytes`` on a swapped
    huge file could exhaust worker memory. So (a) this copy enforces a hard
    cumulative byte ceiling (the max tier total, checked per file BEFORE and
    AFTER each read), and (b) the caller validates the *copied snapshot* in
    ``dst`` — which no attacker can touch — and promotes only that. Raises
    ``ValueError`` on the first unsafe or over-budget entry.
    """
    src_root = str(src.resolve())
    budget = max(_THEME_TOTAL_BYTES_BY_LEVEL.values())
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        # A subdirectory that is itself a link must never be descended.
        # `is_link_or_junction`, not `os.path.islink`: islink() is False for a
        # Windows JUNCTION, and `os.walk` reports a junction as an ordinary
        # directory, so an islink-only guard descends it. A pack carrying a
        # junction back to its own root would then recurse until a path-length
        # OSError escaped as a 500 -- reachable as soon as local install is
        # enabled on Windows, which is what this change does.
        for d in dirnames:
            if is_link_or_junction(os.path.join(dirpath, d)):
                raise ValueError(
                    "refusing to install symlinked directory: "
                    f"{os.path.relpath(os.path.join(dirpath, d), src)}"
                )
        # Prune meta/VCS directories in-place (matches the old _ignore set).
        dirnames[:] = [d for d in dirnames if d.lower() not in _THEME_META_IGNORE]
        for name in filenames:
            if name.lower() in _THEME_META_IGNORE:
                continue
            spath = os.path.join(dirpath, name)
            rel = os.path.relpath(spath, src)
            st = os.lstat(spath)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"refusing to install non-regular file: {rel}")
            # Pre-read bound: reject before allocating for an oversized file.
            if copied + st.st_size > budget:
                raise ValueError(
                    f"theme exceeds the maximum install size ({budget} bytes) at: {rel}"
                )
            data = safe_read_file_bytes_nolink(spath, within_root=src_root)
            if data is None:
                raise ValueError(f"refusing to install unreadable/unsafe file: {rel}")
            # Post-read bound: the file may have been swapped for a bigger
            # regular file between lstat and read — re-check actual bytes.
            copied += len(data)
            if copied > budget:
                raise ValueError(
                    f"theme exceeds the maximum install size ({budget} bytes) at: {rel}"
                )
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


# Concurrent installs of the SAME slug must not clobber each other's staging
# or backup trees (_do_install runs in executor threads, so these are
# threading locks, not asyncio locks). Unique per-attempt dir names plus a
# per-slug lock around the stage+swap make re-install safe under races.
_THEME_INSTALL_LOCKS: dict[str, threading.Lock] = {}
_THEME_INSTALL_LOCKS_GUARD = threading.Lock()


def _theme_install_lock(slug: str) -> threading.Lock:
    with _THEME_INSTALL_LOCKS_GUARD:
        return _THEME_INSTALL_LOCKS.setdefault(slug, threading.Lock())


def _atomic_write_theme_json(target: Path, text: str) -> None:
    """Write a theme JSON file atomically (temp file in the same dir + os.replace).

    A plain ``write_text`` truncates-then-writes, so two concurrent same-slug
    writers can interleave into a torn/half-written file. Writing to a unique
    temp file and ``os.replace``-ing it into place makes the swap atomic — a
    reader ever sees either the old or the new complete file, never a partial
    one. Callers MUST hold ``_theme_install_lock(slug)`` so the exists-check and
    this write are one critical section (closes the create/update TOCTOU).

    Delegates to :func:`kiro_crew.atomic_write.atomic_write`, which is the same
    ``mkstemp``-plus-rename shape this used to hand-roll -- including the
    ``except BaseException`` temp cleanup, so a Ctrl-C mid-write still leaves no
    scratch file -- plus the Windows sharing-violation rename retry. ``fsync``
    stays off, as before.

    ``mode=0o600`` is not a tightening, it is what this site already published:
    ``mkstemp`` creates its file owner-only and the hand-rolled form never
    chmod'd it, so the theme JSON landed at ``0o600`` and the rename carried
    that through. Passing it explicitly keeps that, because ``atomic_write``
    without a *mode* applies the umask default instead and would widen a
    previously owner-only file to ``0o644``.
    """
    atomic_write(target, text, mode=0o600)


def _read_theme_bytes_nolink(slug: str, target: Path) -> bytes | None:
    """TOCTOU-safe read of a resolved theme asset for the serving routes.

    ``_resolve_theme_asset`` CHECKS the path, but a plain ``read_bytes`` OPENS
    it later — a file swapped for a symlink in that window would be followed,
    exfiltrating arbitrary readable files through the authenticated asset
    endpoint. Read through the hooks chokepoint instead: ``O_NOFOLLOW`` open +
    fd-path containment inside the theme dir, so the inode opened is a regular
    file inside the pack or the read returns ``None`` (route responds 404).
    """
    safe = _safe_theme_slug(slug)
    if safe is None:  # invalid slug — fail closed (route responds 404)
        return None
    base = _installed_theme_dir(safe)
    return safe_read_file_bytes_nolink(str(target), within_root=str(base))


def _do_install(stype: Any, source: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, int]:
    """Blocking theme-install worker — fetch → staged copy → validate the
    snapshot → promote. Runs OFF the
    event loop (discovery pool): ``_clone_github`` (subprocess), ``_validate_theme_dir``
    (os.walk + read_bytes) and ``_copy_installed_theme`` (copytree) are all
    synchronous filesystem/process work.

    Returns ``(theme, error, status)``: on success ``theme`` is the descriptor
    dict and ``error`` is ``None``; on failure ``error`` is the message and
    ``status`` the HTTP code. Manages its own temp dir lifecycle.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="theme-install-"))
    try:
        if stype == "local":
            src, err = _resolve_local_source(source.get("path", ""))
        elif stype == "github":
            src = tmp_root / "clone"
            err = _clone_github(source.get("url", ""), src)
        else:
            return None, "source.type must be 'local' or 'github'", 400
        if err or src is None:
            status = 503 if err == _THEME_GIT_SANDBOX_UNAVAILABLE else 400
            return None, err or "invalid source", status

        # ── Stage-first (TOCTOU class fix) ──
        # The source dir stays writable by its owner throughout, so a
        # validate-then-copy order can promote content that was swapped in
        # AFTER validation (regular-file→regular-file swaps pass the symlink
        # checks). Instead: copy the source into a PRIVATE staging snapshot
        # first (bounded, symlink-safe), then validate THAT snapshot — which
        # nothing else can touch — and promote only the validated bytes.
        _themes_dir().mkdir(parents=True, exist_ok=True)
        # Staging lives INSIDE _themes_dir(), so a source that equals or
        # contains it would make os.walk recursively copy the staging dir's
        # own output (unbounded nesting → ENAMETOOLONG → residue). Reject by
        # resolved-path containment before creating the snapshot.
        themes_root = _themes_dir().resolve()
        src_resolved = src.resolve()
        if src_resolved == themes_root or src_resolved in themes_root.parents:
            return None, "source directory must not contain the themes directory", 400
        token = uuid.uuid4().hex[:12]
        stage = _themes_dir() / f".install-staging-{token}"
        try:
            _copy_installed_theme(src, stage)
            # installing=True: only the install path refuses a pack for pinning
            # the UI font, so a pack installed before that rule keeps loading on
            # the read path (see _validate_overrides_css).
            summary, err = _validate_theme_dir(stage, installing=True)
        except ValueError as ve:
            shutil.rmtree(stage, ignore_errors=True)
            return None, str(ve), 400
        except BaseException:
            # Any unexpected failure (OSError etc.) must not leave a staging
            # snapshot behind — residue accumulates across retries.
            shutil.rmtree(stage, ignore_errors=True)
            raise
        if err or summary is None:
            shutil.rmtree(stage, ignore_errors=True)
            return None, err or "invalid theme", 400

        slug = summary["slug"]
        dest = _installed_theme_dir(slug)
        # Historical guard kept for a clear message (with stage-first the copy
        # out of dest already happened safely, but re-installing the installed
        # dir onto itself is a user error worth naming). This is a source-path
        # check, not a registry-state race, so it stays outside the lock.
        if src.resolve() == dest.resolve():
            shutil.rmtree(stage, ignore_errors=True)
            return None, "source is already the installed theme directory", 400
        old = dest.with_name(f".{slug}.old-{token}")
        with _theme_install_lock(slug):
            # Collision check INSIDE the lock, immediately before promotion: an
            # editor-created custom record with the same slug is a hard collision
            # (don't clobber the user's editor theme). Doing it here (not before
            # the lock) closes the race where a concurrent create writes
            # <slug>.json between an outside-the-lock check and the promote,
            # leaving BOTH a .json record and a <slug>/ dir (duplicate slug). An
            # existing installed <slug>/ dir IS overwritten — that's the update path.
            if (_themes_dir() / f"{slug}.json").exists():
                shutil.rmtree(stage, ignore_errors=True)
                return None, f"a custom theme named '{slug}' already exists", 409
            try:
                if dest.exists():
                    dest.replace(old)
                stage.replace(dest)
            except OSError:
                if not dest.exists() and old.exists():
                    old.rename(dest)  # roll back
                shutil.rmtree(stage, ignore_errors=True)
                raise
            shutil.rmtree(old, ignore_errors=True)
        return (
            {
                "slug": slug,
                "name": summary["name"],
                "emoji": summary["emoji"],
                "level": summary["level"],
                "source": stype,
            },
            None,
            200,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _audit_theme_install_governance(
    outcome: str, decision: object, reason: str = ""
) -> None:
    """Best-effort SEL audit of the theme-install admission decision.

    Writes to the JSONL SEL file (never stdout) and NEVER raises. A theme
    install ingests third-party content (local move / server-side git clone) and
    serves sandboxed JS into the dashboard, so BOTH outcomes belong in the
    security audit trail: the ``allowed`` case records that external content was
    admitted, the ``denied`` case records the policy block (and the
    governance-unavailable fail-closed). Mirrors mcp_core._audit_governance_deny,
    extended to also record the permitted admission (an install is a rare,
    high-consequence mutation, unlike a routine per-call tool gate).
    """
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key="",
            tool_name="api_themes_install",
            scope="capabilities.theme_install",
            outcome=outcome,
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=reason or getattr(decision, "reason", ""),
        )
    except Exception:
        # SEL writes to a file, but an audit failure must never wedge install.
        pass


async def api_themes_install(request: web.Request) -> web.Response:
    """POST /api/themes/install — install a Level-0 theme directory.

    Body: ``{"source": {"type": "local", "path": "..."}}`` or
    ``{"source": {"type": "github", "url": "https://github.com/..."}}``.
    Fetch/move -> validate (data + structure) -> register as
    ``_themes_dir()/<slug>/``.
    """
    # Governance admission gate: installing a pack ingests third-party content
    # (local move or server-side git clone) and serves sandboxed JS into the
    # dashboard, so an enterprise POLICY must be able to ban it wholesale
    # (capabilities.theme_install SCOPE_CATALOG row, default-allow standalone).
    # fail_closed=True: this is an admission chokepoint for external content, so
    # a governance-evaluation error DENIES rather than silently ingesting; the
    # DENY is produced inside governance_permits (it swallows its own errors).
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        _gd = governance_permits(
            "capabilities.theme_install", "", log_warning=False, fail_closed=True
        )
        _permitted = getattr(_gd, "permitted", False)
        _audit_theme_install_governance("allowed" if _permitted else "denied", _gd)
        if not _permitted:
            return web.json_response(
                {"error": getattr(_gd, "reason", "theme installation disabled by policy")},
                status=403,
            )
    except Exception:
        _audit_theme_install_governance(
            "denied", None, reason="governance unavailable (fail-closed)"
        )
        return web.json_response(
            {"error": "theme installation blocked (governance unavailable)"}, status=403
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    source = body.get("source") if isinstance(body, dict) else None
    if not isinstance(source, dict):
        return web.json_response({"error": "missing 'source' object"}, status=400)
    stype = source.get("type")

    # Offload the blocking fetch/validate/copy to the discovery pool so the
    # coroutine never runs subprocess/os.walk/copytree on the event loop —
    # same off-loop discipline as api_agents_installed. Only request parsing
    # and response construction stay on the loop.
    theme, err, status = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _do_install, stype, source
    )
    if err or theme is None:
        payload = {"error": err or "install failed"}
        if err == _THEME_GIT_SANDBOX_UNAVAILABLE:
            payload["code"] = "theme_install_sandbox_unavailable"
        return web.json_response(payload, status=status)

    return web.json_response({"ok": True, "slug": theme["slug"], "theme": theme})


async def api_theme_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/themes/{slug} — get, update, or delete a custom theme."""
    slug = request.match_info["slug"]
    # Sanitize slug to prevent path traversal
    safe_slug = re.sub(r"[^a-z0-9\-]", "", slug)
    if not safe_slug or safe_slug != slug:
        return web.json_response({"error": "invalid theme slug"}, status=400)

    target = _themes_dir() / f"{safe_slug}.json"
    dir_target = _installed_theme_dir(safe_slug)

    # One stat pass for the whole handler: ``exists()``/``is_dir()`` are
    # SMB-backed on a UNC data home and can block for as long as the network
    # takes, so they never run on the event loop — the same off-loop
    # discipline as the asset routes' ``_resolve_theme_asset``. Both stats
    # ride a single executor hop because every method branch consumes the
    # pair as one logical check; the DELETE-dir branch re-checks under the
    # per-slug install lock off-loop.
    def _stat_targets() -> tuple[bool, bool]:
        return target.exists(), dir_target.is_dir()

    loop = asyncio.get_running_loop()
    target_exists, dir_is_dir = await loop.run_in_executor(
        discovery_executor(), _stat_targets
    )

    if request.method == "DELETE":
        if target_exists:
            # ``missing_ok``: the stat rode an earlier hop, so a concurrent
            # delete can win the race; deleting an already-gone file is the
            # outcome the caller asked for, not a 500.
            await loop.run_in_executor(
                discovery_executor(), lambda: target.unlink(missing_ok=True)
            )
            return web.json_response({"ok": True})
        if dir_is_dir:
            # Recursive delete of a many-file theme dir is blocking; run off-loop.
            # Acquire the per-slug install lock (same key _do_install stages/swaps
            # under) so we never rmtree mid-reinstall and race its stage→rename;
            # re-check is_dir() under the lock in case a reinstall just replaced it.
            def _locked_remove() -> None:
                with _theme_install_lock(safe_slug):
                    if dir_target.is_dir():
                        shutil.rmtree(dir_target, ignore_errors=True)

            await loop.run_in_executor(discovery_executor(), _locked_remove)
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "PUT":
        if dir_is_dir and not target_exists:
            return web.json_response(
                {"error": "installed themes are read-only; reinstall to update"},
                status=400,
            )
        if not target_exists:
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        err = _validate_theme_data(body)
        if err:
            return web.json_response({"error": err}, status=400)
        name = body["name"].strip()
        emoji = (
            body.get("emoji", _THEME_DEFAULT_EMOJI).strip()[:_THEME_EMOJI_MAX_LEN]
            or _THEME_DEFAULT_EMOJI
        )

        # Preserve created_at from the existing file, then write — all under the
        # per-slug lock so a concurrent create/update can't torn-write the file
        # (same TOCTOU class as create). The read + atomic write are one section.
        def _update_locked() -> dict:
            with _theme_install_lock(safe_slug):
                try:
                    existing = json.loads(target.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing = {}
                td = {
                    "name": name,
                    "slug": safe_slug,
                    "emoji": emoji,
                    "created_at": existing.get(
                        "created_at", datetime.now(timezone.utc).isoformat()
                    ),
                    "dark": _strip_to_allowed_vars(body.get("dark", {})),
                    "light": _strip_to_allowed_vars(body.get("light", {})),
                }
                _atomic_write_theme_json(target, json.dumps(td, indent=2) + "\n")
                return td

        theme_data = await loop.run_in_executor(
            discovery_executor(), _update_locked
        )
        return web.json_response({"ok": True, "theme": theme_data})

    # GET — file reads and the validation walk are blocking; run off-loop.
    if target_exists:
        try:
            raw = await loop.run_in_executor(
                discovery_executor(), target.read_text, "utf-8"
            )
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return web.json_response({"error": "failed to read theme"}, status=500)
        return web.json_response(data)
    if dir_is_dir:
        summary, err = await loop.run_in_executor(
            discovery_executor(), _validate_theme_dir, dir_target
        )
        if err or summary is None:
            # ``err`` can name the on-disk theme directory; keep it server-side
            # and send the client a generic message (rendered verbatim in the UI).
            logger.warning("invalid installed theme: %s", err)
            return web.json_response(
                {"error": "invalid installed theme", "code": "invalid_installed_theme"},
                status=500,
            )
        manifest, _m_err = await loop.run_in_executor(
            discovery_executor(),
            _read_json_file,
            dir_target / _THEME_MANIFEST_NAME,
            _THEME_FILE_CAPS["manifest"],
        )
        if not isinstance(manifest, dict):
            manifest = {}
        # Descriptor build walks the pack (globs, JSON/audio/persona reads) —
        # blocking filesystem work, so run it off-loop like its neighbours.
        assets = await loop.run_in_executor(
            discovery_executor(),
            _theme_asset_descriptor,
            dir_target,
            manifest,
            summary["level"],
        )
        return web.json_response(
            {
                "slug": safe_slug,
                "name": summary["name"],
                "emoji": summary["emoji"],
                "level": summary["level"],
                "source": "installed",
                "dark": summary["dark"],
                "light": summary["light"],
                "assets": assets,
            }
        )
    return web.json_response({"error": "not found"}, status=404)


# ── Installed-theme asset serving (fonts / images / audio / overlay HTML) ──
#
# Static assets are served with a strict Content-Type + ``nosniff``; overlay and
# topbar HTML get a locked-down CSP (they run in sandboxed iframes, §8.2). All
# routes resolve the requested path *within* the theme directory (no traversal).

def _theme_html_response(text: str) -> web.Response:
    """Serve overlay/topbar HTML with the sandbox CSP + nosniff."""
    return web.Response(
        text=text,
        content_type="text/html",
        headers={
            "Content-Security-Policy": _THEME_OVERLAY_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def api_theme_asset(request: web.Request) -> web.Response:
    """GET /api/theme/{slug}/assets/{path} — serve a static theme asset."""
    # Offloaded, not called inline: `_resolve_theme_asset` does `resolve()` /
    # `is_file()`, which are SMB-backed on a UNC data home (roaming profile),
    # so running it on the loop stalls the whole gateway per request. Rides the
    # same executor the read below already uses.
    target, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _resolve_theme_asset,
        request.match_info["slug"],
        request.match_info.get("path", ""),
    )
    if err or target is None:
        return web.json_response(
            {"error": err or "not found"},
            status=400 if err and "invalid" in err else 404,
        )
    ct = _THEME_ASSET_CT.get(target.suffix.lower())
    if ct is None:
        return web.json_response({"error": "unsupported asset type"}, status=400)
    body = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_theme_bytes_nolink, request.match_info["slug"], target
    )
    if body is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.Response(
        body=body,
        content_type=ct,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _THEME_ASSET_CSP,
        },
    )


async def api_theme_overlay(request: web.Request) -> web.Response:
    """GET /api/theme/{slug}/overlay/{id} — serve overlay HTML (id = file stem)."""
    oid = request.match_info["id"].lower()
    if not oid or _safe_theme_slug(oid) != oid:
        return web.json_response({"error": "invalid overlay id"}, status=400)
    # Offloaded, not called inline: `_resolve_theme_asset` does `resolve()` /
    # `is_file()`, which are SMB-backed on a UNC data home (roaming profile),
    # so running it on the loop stalls the whole gateway per request. Rides the
    # same executor the read below already uses.
    target, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _resolve_theme_asset,
        request.match_info["slug"],
        f"overlays/{oid}.html",
    )
    if err or target is None:
        return web.json_response(
            {"error": err or "not found"},
            status=400 if err and "invalid" in err else 404,
        )
    raw = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_theme_bytes_nolink, request.match_info["slug"], target
    )
    if raw is None:
        return web.json_response({"error": "not found"}, status=404)
    return _theme_html_response(raw.decode("utf-8", errors="replace"))


async def api_theme_topbar(request: web.Request) -> web.Response:
    """GET /api/theme/{slug}/topbar/{mode} — serve topbar HTML (mode dark|light)."""
    mode = request.match_info["mode"]
    if mode not in ("dark", "light"):
        return web.json_response({"error": "mode must be dark or light"}, status=400)
    # Offloaded, not called inline: `_resolve_theme_asset` does `resolve()` /
    # `is_file()`, which are SMB-backed on a UNC data home (roaming profile),
    # so running it on the loop stalls the whole gateway per request. Rides the
    # same executor the read below already uses.
    target, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _resolve_theme_asset,
        request.match_info["slug"],
        f"topbar/{mode}.html",
    )
    if err or target is None:
        return web.json_response(
            {"error": err or "not found"},
            status=400 if err and "invalid" in err else 404,
        )
    raw = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_theme_bytes_nolink, request.match_info["slug"], target
    )
    if raw is None:
        return web.json_response({"error": "not found"}, status=404)
    return _theme_html_response(raw.decode("utf-8", errors="replace"))
