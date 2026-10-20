"""What a deploy may publish: input specs, path confinement, staging and the content scan.

A deploy publishes either an artifact or a caller-named ``local_dir``. This module holds the
``FieldSpec`` patterns those two inputs are validated with, confines a directory to the
allow-listed roots (``_allowed_local_roots``), resolves a kind=webapp artifact to its servable
``app_dir/public`` (``resolve_webapp_public_dir``, which the preview channel shares), copies a
tree into the private staging snapshot through the hook-gated reader (``_stage_tree_safe``),
renders an artifact to standalone HTML, digests and sizes a tree, and scans it for credentials
and internal data (``_scan_tree``). Its filesystem helpers block; the async callers run them
through ``asyncio.to_thread``. It imports no other deploy handler module.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.deploy.render import render_standalone
from kiro_crew.deploy.scan import Finding, is_credential_finding, scan_content
from kiro_crew.security import is_sensitive_path
from kiro_crew.validation import FieldSpec

try:
    from kiro_crew.artifacts import get_default_store

    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False


# --- local_dir input validation (security-controls) ------------
# local_dir is request-supplied and LLM-influenceable via the chat-native skill,
# and flows to the filesystem + the `aws s3 sync` subprocess. Validate it with a
# validation.py schema (type/length/charset) and confine the resolved real path
# to an allow-listed root before any filesystem/subprocess use.
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z0-9 _\-./~]+$")
_LOCAL_DIR_SPEC = FieldSpec(name="local_dir", type=str, max_len=4096, pattern=_LOCAL_DIR_RE)


# artifact_slug is LLM-influenceable (chat-native skill) and is used in a store
# lookup, so validate it like the other request inputs.
_ARTIFACT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ARTIFACT_SLUG_SPEC = FieldSpec(
    name="artifact_slug", type=str, max_len=128, pattern=_ARTIFACT_SLUG_RE
)

# Pre-publish scan: skip files larger than this (likely binary/media) when
# scanning a local_dir's full contents.
_SCAN_SIZE_LIMIT = 2 * 1024 * 1024  # 2 MiB

# Size cap on a local_dir tree, measured before staging. `_do_deploy` refuses a
# bigger tree before copying it, and `_handle_pending_confirm` applies the same
# cap at confirm time.
_MAX_STAGE_BYTES = 200 * 1024 * 1024  # 200 MiB


def _safe_resolve(p: Path) -> Path:
    """Resolve a path (following symlinks); fall back to the raw path on error."""
    try:
        return p.resolve()
    except OSError:
        return p


def _allowed_local_roots() -> list[Path]:
    """Resolved directories a publishable local_dir may live under.

    Aligned with the subagent_cwd_allowed_roots config and the file-explorer
    authorization boundary — only the caller's known workspace roots are
    permitted, not arbitrary filesystem locations.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    roots: list[Path] = []
    try:
        cfg = KiroCrewConfig.load()
        configured = cfg.agent.subagent_cwd_allowed_roots
    except Exception:
        configured = []
    if configured:
        for r in configured:
            expanded = Path(os.path.expanduser(r))
            try:
                if expanded.exists():
                    roots.append(expanded.resolve())
            except OSError:
                pass
    # Fallback: if no config, allow ~/workplace and ~/workspace (standard devbox)
    if not roots:
        for cand in (
            Path.home() / "workplace",
            Path.home() / "workspace",
            Path("/local/home") / os.environ.get("USER", "") / "workplace",
        ):
            try:
                if cand.exists():
                    roots.append(cand.resolve())
            except OSError:
                pass
    # Always allow the agent's own config-dir workspace (e.g. ~/.kiro/crew/workspace).
    try:
        cdir_ws = config_dir() / "workspace"
        if cdir_ws.exists() and cdir_ws.resolve() not in roots:
            roots.append(cdir_ws.resolve())
    except OSError:
        pass
    # Also include any registered workspaces (cfg.workspaces[*].dir).
    try:
        cfg = KiroCrewConfig.load()
        for _ws_name, ws_cfg in cfg.workspaces.items():
            ws_dir = Path(os.path.expanduser(ws_cfg.dir))
            try:
                if ws_dir.exists():
                    resolved = ws_dir.resolve()
                    if resolved not in roots:
                        roots.append(resolved)
            except OSError:
                pass
    except Exception:
        pass
    # Always allow the deploy staging dir, where artifacts are staged for publish.
    try:
        sr = _staging_root()
        roots.append(sr.resolve())
    except OSError:
        pass
    return roots


def _staging_root() -> Path:
    """Return (and validate) the private staging root under config_dir.

    Raises RuntimeError if the path is a symlink or not owned by the current user.
    """
    import stat

    sr = config_dir() / "deploy-staging"
    os.makedirs(str(sr), mode=0o700, exist_ok=True)
    st = os.lstat(str(sr))
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(
            f"deploy staging root {sr} is a symlink — refusing (possible symlink attack)"
        )
    # Ownership check is POSIX-only: on Windows st_uid is a meaningless 0
    # and os.getuid() does not exist. The symlink refusal above plus the
    # user-profile staging location carry the protection there.
    if platform_compat.IS_POSIX and st.st_uid != os.getuid():
        raise RuntimeError(
            f"deploy staging root {sr} not owned by current user (uid {os.getuid()}, "
            f"owner {st.st_uid})"
        )
    return sr


def _stage_tree_safe(source: Path, staging_root: Path) -> Path:
    """Hook-gated staging copy with hardlink rejection.

    Replaces shutil.copytree: walks the source tree, reads each file through
    hooks.safe_read_file_bytes_nolink (O_NOFOLLOW + fstat nlink + is_sensitive_path gate), and
    rejects files with st_nlink > 1 (hardlinks that could stage sensitive
    content bypassing the symlink check). Dirs are recreated with 0o700.

    Returns the path to the staged tree root. Raises RuntimeError on rejection.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    dst = staging_root / "tree"
    os.makedirs(str(dst), mode=0o700, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(str(source), followlinks=False):
        # A linked DIRECTORY appears in dirnames but is not
        # descended (followlinks=False) and carries no file entries — it would
        # silently vanish from the snapshot, deploying something different
        # from the approved tree. Reject explicitly instead.
        #
        # ``is_link_or_junction``, not ``Path.is_symlink()``: a Windows directory
        # junction is a reparse point ``is_symlink()`` does not report, and
        # ``followlinks=False`` does NOT stop ``os.walk`` descending one, so a
        # junction reached this fence and passed it. It is not a way into the
        # deployment — ``within_root`` below pins containment to the OPENED
        # descriptor, so a file behind the link is refused there — but it was
        # refused as ``staging-read-blocked`` rather than as the link it is, and a
        # link with nothing readable behind it passed the whole walk and was
        # staged as an ordinary empty directory: the snapshot-differs-from-the-
        # approved-tree outcome this fence exists to reject.
        for dname in dirnames:
            dpath = Path(dirpath) / dname
            if platform_compat.is_link_or_junction(dpath):
                raise RuntimeError(f"symlink-in-tree: linked directory at {dpath} — deploy blocked")
        rel_dir = os.path.relpath(dirpath, str(source))
        target_dir = dst / rel_dir if rel_dir != "." else dst
        os.makedirs(str(target_dir), mode=0o700, exist_ok=True)

        for fname in filenames:
            src_file = Path(dirpath) / fname
            # Reject symlinks (already handled upstream, defense in depth)
            if src_file.is_symlink():
                raise RuntimeError(f"symlink-in-tree: symlink found at {src_file} — deploy blocked")
            # Reject hardlinks (st_nlink > 1)
            try:
                st = os.lstat(str(src_file))
            except OSError:
                raise RuntimeError(f"hardlink-in-tree: cannot stat {src_file} — deploy blocked")
            if st.st_nlink > 1:
                raise RuntimeError(
                    f"hardlink-in-tree: {src_file} has nlink={st.st_nlink} "
                    f"(hardlinked file) — deploy blocked"
                )
            # Read through the hook gate (sensitive-path + O_NOFOLLOW).
            # The nolink variant fstat()s the OPENED descriptor and
            # rejects st_nlink > 1 / non-regular inodes — the lstat() above is
            # only a fast-path pre-check; the authoritative hardlink rejection
            # is pinned to the same inode that gets read (no lstat->open race).
            # within_root pins containment to the OPENED fd — a nested
            # dir swapped for a symlink after the walk cannot smuggle files
            # from outside the approved tree into the public deployment.
            try:
                data = safe_read_file_bytes_nolink(str(src_file), within_root=str(source))
            except FileTooLargeError as e:
                # tree cap (200 MiB) > per-file cap — surface as a
                # structured staging rejection (409), not an escaping 500.
                raise RuntimeError(
                    f"file-too-large: {src_file} exceeds the per-file read cap "
                    f"({e}) — deploy blocked"
                ) from e
            if data is None:
                raise RuntimeError(
                    f"staging-read-blocked: {src_file} rejected by safe_read_file_bytes_nolink "
                    f"— deploy blocked"
                )
            target_file = target_dir / fname
            target_file.write_bytes(data)

    return dst


def _compute_content_digest(src_dir: str) -> str:
    """Compute a deterministic content digest for a staged/source directory.

    Produces a sha256 hash of a sorted manifest of (relative_path, size, content_hash)
    per file. Used at preview time and re-verified at confirm time to detect
    content changes between preview and confirmation.
    """
    import hashlib

    root = Path(src_dir)
    entries: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                rel = str(p.relative_to(root))
                content = p.read_bytes()
                file_hash = hashlib.sha256(content).hexdigest()[:16]
                entries.append(f"{rel}:{len(content)}:{file_hash}")
            except OSError:
                pass
    manifest = "\n".join(entries)
    return hashlib.sha256(manifest.encode()).hexdigest()


def _compute_tree_size_global(root: Path) -> int:
    """Total bytes of all regular files in a directory tree. Blocking."""
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _is_within(path: Path, root: Path) -> bool:
    # normpath + prefix check is the canonical containment barrier (recognized
    # by static analyzers); equivalent to relative_to for absolute resolved paths.
    p = os.path.normpath(str(path))
    r = os.path.normpath(str(root))
    return p == r or p.startswith(r + os.sep)


# Data dir resolved via config_dir() so pods/tests isolate correctly.


def _data_dir() -> Path:
    return config_dir() / "deploy"


def _stage_artifact_html(kind: str, content: str, name: str) -> tuple[list, str, int]:
    """Render + scan + write an artifact to a temp dir. Blocking (filesystem +
    CPU) -- must be called via asyncio.to_thread, never directly on the loop.

    Raises ValueError for webapp-kind artifacts (not deployable HTML).
    """
    if kind == "webapp":
        raise ValueError(
            "webapp artifacts contain an app summary, not deployable HTML; "
            "deploy the app's built directory via local_dir instead"
        )
    html = render_standalone(kind, content, title=name)
    findings = scan_content(html)
    tmp_dir = tempfile.mkdtemp(prefix="deploy-web-")
    Path(tmp_dir, "index.html").write_text(html, encoding="utf-8")
    return findings, tmp_dir, len(html.encode("utf-8"))


class WebAppRootError(Exception):
    """A kind=webapp artifact whose servable static root cannot be resolved.

    Carries two strings on purpose: ``str(exc)`` is the plain sentence a
    non-AWS reader gets in the banner, and ``details`` holds the field and
    directory names that only mean something to an operator — shown behind the
    UI's Details toggle rather than in the red banner.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so this module's name must not leak into it.
WebAppRootError.__module__ = "kiro_crew.deploy.handlers"


def resolve_webapp_public_dir(slug: str, art: Any | None = None) -> Path | None:
    """Resolve + authorize a kind=webapp artifact's servable static root.

    Returns None when the artifact is NOT kind=webapp — the caller then renders
    its content as standalone HTML, the pre-existing behaviour for every other
    kind. Raises :class:`WebAppRootError` when the artifact IS a webapp but has
    no servable root, so a caller can report *why* instead of failing blankly.
    Propagates the store's own not-found / invalid errors.

    The root is REQUIRED to be ``app_dir/public``, the deploy contract's own
    layout. ``app_dir`` itself is deliberately NOT a fallback: a directory that
    merely happens to contain an ``index.html`` would otherwise be published
    whole — sources, config JSON, dotfiles — and the destinations reached through
    here are a world-readable CloudFront URL and the preview channel, so a
    fallback would leak the app's source tree in both. ``app_dir`` is
    LLM-written, so it is re-checked against the allow-listed roots here rather
    than trusted from the metadata.

    This is the ONE place that rule lives: the preview channel
    (``dashboard/handlers/webapp_preview.py``) delegates here, so the two
    surfaces cannot drift into disagreeing about what is servable.

    Pass ``art`` when the caller has ALREADY read the artifact and needs the
    root to describe that same read. Two adjacent reads of one slug can
    straddle a delete-and-recreate, and a caller pairing the root with a
    separately-read generation would then hold identity from one artifact and
    content from another. Omitted, this reads the artifact itself, which is the
    right thing for a caller that wants only the root.

    Blocking (artifact store + filesystem) — call via ``asyncio.to_thread``.
    """
    if not _HAS_ARTIFACTS:
        return None
    if art is None:
        art = get_default_store().get(slug)
    meta = getattr(art, "webapp_metadata", None)
    if art.kind != "webapp" or meta is None:
        return None
    raw = (getattr(meta, "app_dir", "") or "").strip()
    if not raw:
        raise WebAppRootError(
            "This app has no saved folder on this machine, so there is nothing "
            "to publish. Ask the agent to rebuild it.",
            "webapp_metadata.app_dir is empty",
        )
    try:
        app_dir = Path(os.path.expanduser(raw)).resolve()
    except (OSError, ValueError):
        # ValueError: embedded NUL in a crafted app_dir. The write path rejects
        # control characters, but metadata predating that validation must fail
        # closed here rather than raise OSError out of the handler.
        raise WebAppRootError(
            "This app's saved folder cannot be read, so there is nothing to " "publish.",
            f"webapp_metadata.app_dir could not be resolved: {raw!r}",
        ) from None
    if not app_dir.is_dir():
        raise WebAppRootError(
            "This app's folder is gone from this machine, so there is nothing "
            "to publish. Ask the agent to rebuild it.",
            f"webapp_metadata.app_dir does not exist: {app_dir}",
        )
    for root in _allowed_local_roots():
        try:
            app_dir.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise WebAppRootError(
            "This app is saved outside the folders Kiro Crew is allowed to "
            "publish from, so it cannot be published from here.",
            f"webapp_metadata.app_dir {app_dir} is outside the allowed local " "roots",
        )
    public = app_dir / "public"
    if not public.is_dir():
        raise WebAppRootError(
            "This app has not been built yet, so there is no finished page to "
            "publish. Ask the agent to build it, then try again.",
            f"no public/ directory under {app_dir} — the deploy contract's "
            "static root is app_dir/public",
        )
    try:
        resolved = public.resolve()
        # `public` may itself be a symlink planted in a crafted app_dir tree.
        # The resolved root must stay INSIDE the validated app_dir.
        resolved.relative_to(app_dir)
    except (OSError, ValueError):
        raise WebAppRootError(
            "This app's built folder points somewhere outside the app, so it "
            "cannot be published.",
            f"{public} resolves outside its app_dir {app_dir}",
        ) from None
    if not (resolved / "index.html").is_file():
        raise WebAppRootError(
            "This app's built folder has no home page, so the published link "
            "would not open. Ask the agent to rebuild it.",
            f"no index.html in {resolved}",
        )
    return resolved


def _dir_contains_sensitive(src: Path, resolved: Path) -> bool:
    """Recursive sensitive-path walk. Blocking -- call via asyncio.to_thread."""
    if is_sensitive_path(str(src)) or is_sensitive_path(str(resolved)):
        return True
    for p in resolved.rglob("*"):
        resolved_child = _safe_resolve(p)
        # A symlink pointing outside the tree is itself suspicious --
        # treat it as sensitive (fail closed, block the deploy).
        if not _is_within(resolved_child, resolved):
            return True
        if is_sensitive_path(str(resolved_child)):
            return True
    return False


def _scan_tree(src: Path) -> tuple[list, int]:
    """Recursive content scan + size sum. Blocking -- call via asyncio.to_thread."""
    from kiro_crew.hooks import safe_read_file_bytes

    findings: list = []
    resolved_root = _safe_resolve(src)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        # Symlink containment: resolve the target and ensure it stays within
        # the source tree. A symlink pointing outside -> fail closed (finding
        # surfaced, target content NEVER read). This blocks attacks where an
        # LLM-supplied dir contains `link -> /etc/passwd` or similar.
        resolved_file = _safe_resolve(f)
        if not _is_within(resolved_file, resolved_root):
            findings.append(
                Finding(
                    kind="symlink-escape",
                    snippet=f"{f.name} -> {resolved_file} escapes source tree",
                    line=0,
                    severity="credential",
                )
            )
            continue
        try:
            file_size = f.stat().st_size
        except OSError:
            findings.append(
                Finding(
                    kind="unreadable-file",
                    snippet=f"{f.name} — cannot stat (permission denied or I/O error)",
                    line=0,
                    severity="credential",
                )
            )
            continue
        if file_size >= _SCAN_SIZE_LIMIT:
            # Fail closed: a file too large to scan must not slip through to a
            # public upload unexamined -- surface it as a finding instead.
            findings.append(
                Finding(
                    kind="unscanned-large-file",
                    snippet=f"{f.name} ({file_size} bytes) exceeds the scan size limit",
                    line=0,
                    severity="credential",
                )
            )
            continue
        # Read as BYTES first to handle binary files (images, fonts, etc.)
        # without crashing on UnicodeDecodeError. Binary files are identified by
        # null bytes in the first 8KiB — they skip content scanning but are NOT
        # exempt from sensitive-path rejection (already checked via safe_read_file_bytes).
        raw_bytes = safe_read_file_bytes(str(resolved_file))
        if raw_bytes is None:
            # safe_read_file_bytes returns None when is_sensitive_path rejects
            # or file is unreadable — treat as credential finding (fail closed).
            findings.append(
                Finding(
                    kind="hook-denied",
                    snippet=f"{f.name} — denied by file-read security gate",
                    line=0,
                    severity="credential",
                )
            )
            continue
        # Credential patterns MUST be evaluated on every file regardless
        # of binary classification. A NUL-prepended HTML/JS file is still parseable
        # by browsers and can contain secrets. Decode with errors="replace" so NUL
        # bytes become U+FFFD but AKIA patterns remain intact.
        content = raw_bytes.decode("utf-8", errors="replace")
        cred_findings = [f for f in scan_content(content) if is_credential_finding(f)]
        findings.extend(cred_findings)

        # Binary detection: null byte in first 8KiB -> skip non-credential
        # content scanning (internal-host, ARN, account-id heuristics produce
        # noise on binary files). Credential findings above are kept regardless.
        if b"\x00" in raw_bytes[:8192]:
            continue
        # Full content scan (non-credential findings) for text files only.
        non_cred_findings = [f for f in scan_content(content) if not is_credential_finding(f)]
        findings.extend(non_cred_findings)
    # A file whose stat fails is already recorded as an unreadable-file
    # finding above; the size helper skips it.
    byte_size = _compute_tree_size_global(src)
    return findings, byte_size
