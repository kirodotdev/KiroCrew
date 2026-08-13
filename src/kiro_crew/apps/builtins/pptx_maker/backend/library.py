"""PPTX Maker — the style and template library.

A *style* is an HTML document that defines the visual mood of a deck; a
*template* is a .pptx whose slide layouts supply the structure. Both come in
two flavours: the ones bundled with the engine (read-only) and the ones the user
imported or the agent authored, which live in the engine's user config dir and
are editable here.

Every mutating operation returns ``(status, payload)`` so the route layer stays a
thin adapter, and every one of them resolves its target through
``paths.resolve_library_file`` — a name that fails the segment allow-list or
resolves outside the library dir is refused before any filesystem call.

Every failure payload carries a machine-readable ``code`` next to its English
``error`` prose. The identifier is minted HERE rather than at the route boundary
because this is the layer that knows which condition fired; the route only
re-emits it. The dashboard renders ``error`` verbatim into a localized UI, so the
prose is advisory and the ``code`` is the contract the client switches on (RFC
9457 3.1.3; see ``test/test_error_code_contract.py``).

Everything here is BLOCKING (engine subprocess + file IO) and must be called
through ``routes.off_loop``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from kiro_crew import hooks
from kiro_crew.apps.builtins.pptx_maker.backend import engine, paths
from kiro_crew.atomic_write import atomic_write
from kiro_crew.hooks import FileTooLargeError

logger = logging.getLogger("kirocrew.app.pptx-maker")

STYLE_SUFFIX = ".html"
TEMPLATE_SUFFIX = ".pptx"

# Upload ceilings. A style is a single HTML document and a template is one
# .pptx; these are an order of magnitude above any real one, and they exist so a
# request body cannot be used to fill the disk.
MAX_STYLE_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_BYTES = 64 * 1024 * 1024

# .pptx is a zip — its first two bytes are the local-file-header magic. Checked
# so a mislabelled upload is refused before it reaches the engine's analyzer.
_ZIP_MAGIC = b"PK"

# A style must at least look like markup; the engine renders it as HTML.
_HTML_HINT = "<"

_SLIDE_DIV_RE = re.compile(r'<div class="slide[\s"]')
_HEAD_RE = re.compile(r"<head[^>]*>([\s\S]*?)</head>", re.IGNORECASE)

# Injected into a cover-slide extract so the thumbnail iframe shows the slide
# alone, without the source document's own page padding or zoom.
_COVER_RESET_CSS = (
    "<style>body{margin:0!important;padding:0!important;"
    "background:transparent!important;overflow:hidden!important}"
    ".slide{margin:0 auto!important}</style>"
)

_USER_SOURCE = "user"


def _user_dir(sub: str) -> Path | None:
    """The engine's user styles/templates dir, created on demand."""
    directory = engine.user_subdir(sub)
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("pptx-maker: cannot create %s dir: %s", sub, exc)
        return None
    return directory


def _not_ready() -> tuple[int, dict]:
    return 503, {"error": "engine not ready", "code": "engine_not_ready"}


def cover_html(full: str) -> str:
    """A standalone HTML document containing only the FIRST slide of *full*.

    The library list renders a thumbnail per style, and a style document can hold
    a dozen slides. Extracting the first one keeps each thumbnail's iframe to a
    single slide instead of a stack, matching what the engine's own preview does.
    """
    head_match = _HEAD_RE.search(full)
    head = head_match.group(1) if head_match else ""
    slides = list(_SLIDE_DIV_RE.finditer(full))
    if not slides:
        body = full
    elif len(slides) > 1:
        body = full[slides[0].start() : slides[1].start()]
    else:
        end = full.lower().find("</body", slides[0].start())
        body = full[slides[0].start() : end if end != -1 else len(full)]
    return (
        "<!DOCTYPE html><html><head>"
        + head
        + _COVER_RESET_CSS
        + "</head><body>"
        + body
        + "</body></html>"
    )


def _style_file_in_dirs(name: str, dirs: list[str]) -> Path | None:
    """Find a style by name across the engine's style dirs; first match wins.

    First-match ordering is the engine's own shadowing rule: a user style with
    the same name as a bundled one replaces it.
    """
    for raw_dir in dirs:
        resolved = paths.resolve_library_file(Path(raw_dir), name, STYLE_SUFFIX)
        if resolved is not None and resolved.is_file():
            return resolved
    return None


def _read_style_text(path: Path, within: Path) -> str | None:
    """Read a style file, pinned to the inode actually opened, or ``None``.

    NOT ``path.read_text()``. ``resolve_library_file`` resolves and re-checks
    containment, which stops a symlink — but a **hardlink is indistinguishable from
    the real file**: it has no link target to resolve, ``is_symlink()`` is False, and
    ``resolve()`` returns the path itself, so every path-based check passes. The
    styles dir is agent-writable, so the agent can create one: ``os.link(
    "~/.ssh/config", "styles/pwned.html")`` made ``/styles`` and ``/style`` serve SSH
    configuration to the dashboard. Verified before this guard.

    ``safe_read_file_bytes_nolink`` is the gateway's own answer to exactly this: it
    opens with ``O_NOFOLLOW``, then ``fstat``s that descriptor and rejects
    ``st_nlink > 1`` (its documented R30 F1 case) and any non-regular file, and with
    ``within_root`` it also requires the OPENED descriptor's real path to sit inside
    the library dir and to be non-sensitive. Path-based checks cannot express either
    condition.

    Decoded here with ``errors="replace"`` to match the previous behaviour: a style is
    agent-authored, so a malformed byte must degrade rather than raise on a worker
    thread and surface as an opaque 500.
    """
    try:
        raw = hooks.safe_read_file_bytes_nolink(str(path), within_root=str(within))
    except FileTooLargeError:
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _style_dir_for(path: Path, dirs: list[str]) -> Path | None:
    """Which of the engine's style dirs *path* was resolved out of.

    The containment root has to be the dir the lookup MATCHED, not ``path.parent``:
    a parent taken from the path under attack would validate an escape against
    itself, the same reasoning as the deck-artifact reader.
    """
    for raw_dir in dirs:
        candidate = paths.resolve_library_file(Path(raw_dir), path.stem, STYLE_SUFFIX)
        if candidate is not None and candidate == path:
            return Path(raw_dir)
    return None


def list_styles() -> list[dict]:
    """Styles from the engine, each with a cover-slide HTML for its thumbnail.

    BLOCKING — call through ``off_loop``.
    """
    data = engine.load_lists()
    dirs = data.get("stylesDirs") or []
    out: list[dict] = []
    for style in data.get("styles") or []:
        if not isinstance(style, dict):
            continue
        name = str(style.get("name") or "")
        cover = ""
        path = _style_file_in_dirs(name, dirs) if name else None
        if path is not None:
            within = _style_dir_for(path, dirs)
            text = _read_style_text(path, within) if within is not None else None
            cover = cover_html(text) if text is not None else ""
        out.append({**style, "coverHtml": cover})
    return out


def style_html(name: str) -> str | None:
    """One style's full HTML, or ``None`` when it does not exist.

    BLOCKING — call through ``off_loop``.
    """
    dirs = engine.load_lists().get("stylesDirs") or []
    path = _style_file_in_dirs(name, dirs)
    if path is None:
        return None
    within = _style_dir_for(path, dirs)
    if within is None:
        return None
    return _read_style_text(path, within)


def list_templates() -> list[dict]:
    """Templates from the engine, with theme colours / fonts / layout counts.

    BLOCKING — call through ``off_loop``.
    """
    return [t for t in engine.load_lists().get("templates") or [] if isinstance(t, dict)]


#: Serializes every read-modify-write of the engine's ``state.json``.
#:
#: Each verb below loads the whole document, changes one key and writes it back, and
#: the routes run these on the subprocess executor (``off_loop``) — so two requests
#: genuinely execute at once. Pinning two different styles from two tabs had both
#: workers read the same ``pinned_styles``, each append its own name, and the later
#: write discard the earlier pin while BOTH responses reported success.
#:
#: ``atomic_write`` never helped here: the WRITE was atomic, the read-modify-write
#: around it was not. Same hazard and same remedy as the Meetings app's
#: ``_TASKS_LOCK``.
#:
#: Module level, because a handler has no instance to hang a lock on; one lock for
#: the whole file rather than one per key, since the critical section is a small
#: read plus a write and every verb touches the same document. Held only across
#: local file IO, never across an await.
_STATE_LOCK = threading.Lock()


def state_transaction() -> "threading.Lock":
    """The lock guarding ``state.json``. Use as ``with``.

    Exposed so a caller that must hold it across a read AND a write — the rename
    verbs, which also move a file — can take it once around the whole sequence
    instead of relying on each helper to re-acquire it.
    """
    return _STATE_LOCK


def _load_state() -> tuple[Path | None, dict]:
    """The engine's ``state.json`` path and contents (``{}`` when unreadable).

    The caller MUST hold :func:`state_transaction` across this and its matching
    save, or two requests interleave and the later write silently discards the
    earlier one.
    """
    base = engine.user_config_dir()
    if base is None:
        return None, {}
    state_path = base / "state.json"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state_path, {}
    return state_path, data if isinstance(data, dict) else {}


def _save_state(state_path: Path, state: dict) -> None:
    """Persist ``state.json`` atomically.

    Atomic because the engine reads this file from its own process on every call;
    a torn write would surface there as a corrupt-state error.
    """
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _save_state_or_undo_rename(
    state_path: Path, state: dict, *, moved_from: Path, moved_to: Path, kind: str
) -> tuple[int, dict] | None:
    """Persist state after a rename, undoing the rename if the write fails.

    Returns ``None`` on success, or the caller's error response.

    The rename has ALREADY committed by the time state is written, so a failing
    write left the file under its new name while ``state.json`` still referred to
    the old one — a pin or a template's metadata pointing at a name that no longer
    exists, with the request reporting 500 as though nothing had happened. Undoing
    the rename is the only outcome that keeps the two consistent: the operation
    fails cleanly and the user can retry.

    A failure of the undo itself is logged and reported, not swallowed — at that
    point the two really are inconsistent and saying so is better than pretending.
    """
    try:
        _save_state(state_path, state)
    except OSError as exc:
        logger.warning("pptx-maker: %s rename state write failed: %s", kind, exc)
        try:
            moved_to.rename(moved_from)
        except OSError:
            logger.error(
                "pptx-maker: could not undo the %s rename %s -> %s; state.json and "
                "the library are now inconsistent",
                kind, moved_from.name, moved_to.name,
            )
        return 500, {
            "error": f"could not rename the {kind}",
            "code": f"{kind}_rename_failed",
        }
    return None


def import_style(name: str, html: str) -> tuple[int, dict]:
    """Save a new user style. Refuses to overwrite an existing name.

    BLOCKING — call through ``off_loop``.
    """
    if len(html.encode("utf-8")) > MAX_STYLE_BYTES:
        return 413, {"error": "style file is too large", "code": "payload_too_large"}
    if _HTML_HINT not in html:
        return 400, {"error": "not an HTML file", "code": "not_html"}
    directory = _user_dir("styles")
    if directory is None:
        return _not_ready()
    target = paths.resolve_library_file(directory, name, STYLE_SUFFIX)
    if target is None:
        return 400, {
            "error": "invalid style name (use letters, digits, . _ or -)",
            "code": "invalid_style_name",
        }
    # The probe is the FAST PATH; the exclusive create is what actually decides.
    #
    # `exists()` then `atomic_write` is check-then-act, and `atomic_write` REPLACES —
    # so two concurrent imports of the same name both passed the check and the later
    # write silently destroyed the first user's style while both requests answered
    # 200. Creating with `O_EXCL` makes the filesystem arbitrate: exactly one wins and
    # the loser gets the same 409 it would have got sequentially.
    #
    # Kept as create-then-write rather than a lock because the guarantee then holds
    # against anything sharing this directory (a second gateway, the engine's own
    # tooling, a shell), not only against two threads in this process. Same lesson as
    # Papyrus's `create_file`: the probe is the fast path, the syscall is the correct
    # one.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return 409, {"error": f"style {name!r} already exists", "code": "style_exists"}
    except OSError as exc:
        logger.warning("pptx-maker: style import failed: %s", exc)
        return 500, {"error": "could not save the style", "code": "style_write_failed"}
    try:
        # `os.fdopen` takes ownership of `fd` only once it SUCCEEDS. If it raises, the
        # raw descriptor is still open and must be closed by hand — on Windows an open
        # handle makes the `unlink` below fail with a sharing violation, so the
        # placeholder survives and every retry then answers `style_exists` against an
        # empty file. That is a Windows-only failure of this very cleanup path, which
        # is how it slipped past a green macOS run.
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except OSError as exc:
        logger.warning("pptx-maker: style import failed: %s", exc)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - best effort
            logger.debug("pptx-maker: could not close the style descriptor")
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            logger.debug("pptx-maker: could not remove the partial style")
        return 500, {"error": "could not save the style", "code": "style_write_failed"}
    try:
        with handle:
            handle.write(html)
    except OSError as exc:
        logger.warning("pptx-maker: style import failed: %s", exc)
        # We created it, so a partial file here is ours to remove — leaving it would
        # make every retry answer `style_exists` against a truncated document. The
        # `with` above has already closed the handle, so the unlink can succeed on
        # Windows too.
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            logger.debug("pptx-maker: could not remove the partial style")
        return 500, {"error": "could not save the style", "code": "style_write_failed"}
    return 200, {"imported": name}


def delete_style(name: str) -> tuple[int, dict]:
    """Delete a user style (and drop it from the pinned list).

    BLOCKING — call through ``off_loop``.
    """
    directory = _user_dir("styles")
    if directory is None:
        return _not_ready()
    target = paths.resolve_library_file(directory, name, STYLE_SUFFIX)
    if target is None:
        return 400, {"error": "invalid style name", "code": "invalid_style_name"}
    if not target.is_file():
        return 404, {"error": "style not found", "code": "style_not_found"}
    with _STATE_LOCK:
        state_path, state = _load_state()
        pins = state.get("pinned_styles")
        changed = False
        if isinstance(pins, list) and name in pins:
            state["pinned_styles"] = [p for p in pins if p != name]
            changed = True
        return _delete_with_state(
            target, state_path, state, changed=changed, kind="style"
        )


def _delete_with_state(
    target: Path, state_path: Path | None, state: dict, *, changed: bool, kind: str
) -> tuple[int, dict]:
    """Remove *target*, keeping ``state.json`` consistent with it.

    Same hazard as :func:`_save_state_or_undo_rename`, reached by the other verb.
    The old order unlinked FIRST and wrote state after, so a failing write left the
    file gone while a pin or the template's metadata still named it — and the caller
    reported 500, so the user believed nothing had happened.

    The file is therefore STAGED (renamed aside), state is written, and only then is
    the staged copy removed. A failing write restores it, so the operation fails
    with the library and the state still agreeing.
    """
    staged = target.with_name(f".{target.name}.{os.getpid()}.deleting")
    try:
        os.replace(target, staged)
    except OSError as exc:
        logger.warning("pptx-maker: %s delete failed: %s", kind, exc)
        return 500, {"error": f"could not delete the {kind}", "code": f"{kind}_delete_failed"}

    if changed and state_path is not None:
        try:
            _save_state(state_path, state)
        except OSError as exc:
            logger.warning("pptx-maker: %s delete state write failed: %s", kind, exc)
            try:
                os.replace(staged, target)
            except OSError:
                logger.error(
                    "pptx-maker: could not restore %s after a failed state write; "
                    "state.json and the library are now inconsistent", target.name,
                )
            return 500, {
                "error": f"could not delete the {kind}",
                "code": f"{kind}_delete_failed",
            }

    try:
        staged.unlink()
    except OSError:  # pragma: no cover - best effort
        # State already agrees that the entry is gone, and the staged name is
        # hidden and never listed, so a leftover is cosmetic rather than a bug.
        logger.debug("pptx-maker: could not remove the staged %s", target.name)
    return 200, {"deleted": target.stem}


def rename_style(name: str, new_name: str) -> tuple[int, dict]:
    """Rename a user style, carrying its pinned state across.

    BLOCKING — call through ``off_loop``.
    """
    directory = _user_dir("styles")
    if directory is None:
        return _not_ready()
    source = paths.resolve_library_file(directory, name, STYLE_SUFFIX)
    target = paths.resolve_library_file(directory, new_name, STYLE_SUFFIX)
    if source is None or target is None:
        return 400, {
            "error": "invalid style name (use letters, digits, . _ or -)",
            "code": "invalid_style_name",
        }
    if not source.is_file():
        return 404, {"error": "style not found", "code": "style_not_found"}
    # The MOVE and the state update are ONE critical section.
    #
    # Splitting them let a concurrent delete of `new_name` interleave between the link
    # and the state write: both verbs returned 200 while `state.json` referenced a file
    # that no longer existed. The move is what makes the state stale, so the lock has
    # to span both — the same rule `delete_style` above already follows, and the same
    # lost-update shape as the template-metadata fix. `_load_state`/`_save_state` do
    # not acquire the lock themselves, so this cannot self-deadlock on a plain Lock.
    with _STATE_LOCK:
        # `os.link` + `unlink`, not `rename`. `Path.rename` REPLACES an existing target
        # on POSIX, so `exists()` then `rename` is check-then-act: two tabs renaming
        # different entries onto one unused name both passed the probe and the second
        # silently destroyed the first while both answered 200. `os.link` refuses an
        # existing target atomically (FileExistsError), so the filesystem arbitrates and
        # the loser gets the 409 it would have got sequentially. Kept even under the
        # lock: it also guards against anything else sharing this directory, which a
        # process-local lock cannot.
        try:
            os.link(source, target)
        except FileExistsError:
            return 409, {"error": f"style {new_name!r} already exists", "code": "style_exists"}
        except OSError as exc:
            logger.warning("pptx-maker: style rename failed: %s", exc)
            return 500, {"error": "could not rename the style", "code": "style_rename_failed"}
        try:
            source.unlink()
        except OSError as exc:
            # The new name is linked but the old one survives, so the entry would appear
            # twice. Undo the link rather than leave a duplicate the user cannot explain.
            logger.warning("pptx-maker: style rename cleanup failed: %s", exc)
            try:
                target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                logger.debug("pptx-maker: could not remove the duplicate style link")
            return 500, {"error": "could not rename the style", "code": "style_rename_failed"}
        state_path, state = _load_state()
        pins = state.get("pinned_styles")
        if state_path is not None and isinstance(pins, list) and name in pins:
            state["pinned_styles"] = [new_name if p == name else p for p in pins]
            failed = _save_state_or_undo_rename(
                state_path, state, moved_from=source, moved_to=target, kind="style"
            )
            if failed is not None:
                return failed
        return 200, {"renamed": {"from": name, "to": new_name}}


def pin_style(name: str, pinned: bool) -> tuple[int, dict]:
    """Pin or unpin a style so the agent prefers it by default.

    BLOCKING — call through ``off_loop``.
    """
    if not paths.SEGMENT_RE.match(name or ""):
        return 400, {"error": "invalid style name", "code": "invalid_style_name"}
    with _STATE_LOCK:
        state_path, state = _load_state()
        if state_path is None:
            return _not_ready()
        current = state.get("pinned_styles")
        current = [str(p) for p in current] if isinstance(current, list) else []
        if pinned and name not in current:
            current.append(name)
        elif not pinned:
            current = [p for p in current if p != name]
        state["pinned_styles"] = current
        try:
            _save_state(state_path, state)
        except OSError as exc:
            logger.warning("pptx-maker: pin write failed: %s", exc)
            return 500, {"error": "could not save the pinned styles", "code": "pin_write_failed"}
        return 200, {"pinnedStyles": current}


def import_template(name: str, data: bytes, description: str = "") -> tuple[int, dict]:
    """Save a new user .pptx template and analyze it via the engine.

    BLOCKING — call through ``off_loop``.
    """
    if len(data) > MAX_TEMPLATE_BYTES:
        return 413, {"error": "template file is too large", "code": "payload_too_large"}
    if data[:2] != _ZIP_MAGIC:
        return 400, {"error": "not a .pptx file", "code": "not_pptx"}
    directory = _user_dir("templates")
    if directory is None:
        return _not_ready()
    target = paths.resolve_library_file(directory, name, TEMPLATE_SUFFIX)
    if target is None:
        return 400, {
            "error": "invalid template name (use letters, digits, . _ or -)",
            "code": "invalid_template_name",
        }
    # CLAIM the name atomically before staging the bytes. `exists()` then `os.replace`
    # is check-then-act and `os.replace` overwrites, so two concurrent imports of the
    # same name both passed the check and the later one destroyed the first user's
    # template while both answered 200 (see the style sibling above). An `O_EXCL`
    # create makes the filesystem arbitrate, and it holds against anything else
    # sharing this directory rather than only against two threads here.
    #
    # A zero-byte placeholder is safe: it is replaced by the real bytes below, and if
    # that fails it is removed — so no retry can find a truncated `.pptx` at the
    # target and answer `template_exists` forever.
    try:
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return 409, {"error": f"template {name!r} already exists", "code": "template_exists"}
    except OSError as exc:
        logger.warning("pptx-maker: template import failed: %s", exc)
        return 500, {"error": "could not save the template", "code": "template_write_failed"}
    # Same-directory temp + `os.replace`, not a direct `write_bytes`. A `.pptx` is
    # megabytes, so a mid-write failure (disk full, the upload cut short) is a real
    # outcome — and a direct write leaves a PARTIAL file at the target, which then
    # answers `template_exists` on every retry. The user is stuck with a corrupt
    # template they cannot replace. `atomic_write` takes `bytes` as of the helper
    # extension, so this is now the shared helper rather than a hand-rolled copy:
    # it picks a unique `mkstemp` name in the target's own directory (so the
    # replace is a rename within one filesystem, hence atomic) and carries the
    # Windows `os.replace` sharing-violation retry this copy lacked. The final
    # mode is unchanged: the replace already overwrote the 0o600 placeholder with
    # the temp file's umask-default mode.
    try:
        atomic_write(target, data)
    except OSError as exc:
        logger.warning("pptx-maker: template import failed: %s", exc)
        # `atomic_write` removes its own temp file. `target` is ours to clean:
        # we created the zero-byte placeholder that claimed the name above, so
        # leaving it would answer `template_exists` on every retry against a
        # file holding nothing.
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            logger.debug("pptx-maker: could not remove the partial template")
        return 500, {"error": "could not save the template", "code": "template_write_failed"}
    # Analysis is best effort: an un-analyzed template still works, it just has
    # no theme colours / layout count to show, so a failure here must not undo
    # an import the user already sees on disk.
    #
    # Held under `_STATE_LOCK`, because `analyze_template` is a READ-MODIFY-WRITE of
    # `state.json`, not just an analysis: its engine snippet does `get_state()` ->
    # mutate `template_metadata` -> `update_state(...)`. Two concurrent imports each
    # read the map before either wrote it, so the second write dropped the first
    # template's metadata while both answered 200 — the same lost-update shape the
    # `O_EXCL` claim above fixes for the FILE, one level up at the metadata document.
    # This is exactly what `state_transaction`'s docstring describes: a caller
    # spanning a read AND a write takes the lock once around the whole sequence.
    #
    # Safe to hold here: the lock guards only local file IO and is never held across
    # an await — this whole function is already `BLOCKING` and runs via `off_loop`, so
    # the subprocess wait does not occupy the event loop. It does serialize concurrent
    # template imports, which is the intended cost: correctness of the shared document
    # over parallelism on a rare, user-initiated action.
    with state_transaction():
        metadata = engine.analyze_template(target, description)
    return 200, {"imported": name, "metadata": metadata}


def delete_template(name: str) -> tuple[int, dict]:
    """Delete a user template and its cached engine metadata.

    BLOCKING — call through ``off_loop``.
    """
    directory = _user_dir("templates")
    if directory is None:
        return _not_ready()
    target = paths.resolve_library_file(directory, name, TEMPLATE_SUFFIX)
    if target is None:
        return 400, {"error": "invalid template name", "code": "invalid_template_name"}
    if not target.is_file():
        return 404, {"error": "template not found", "code": "template_not_found"}
    with _STATE_LOCK:
        state_path, state = _load_state()
        metadata = state.get("template_metadata")
        changed = False
        if isinstance(metadata, dict) and name in metadata:
            del metadata[name]
            changed = True
        status, payload = _delete_with_state(
            target, state_path, state, changed=changed, kind="template"
        )
        if status != 200:
            return status, payload
        return 200, {"deleted": name}


def rename_template(name: str, new_name: str) -> tuple[int, dict]:
    """Rename a user template, carrying its analyzed metadata across.

    BLOCKING — call through ``off_loop``.
    """
    directory = _user_dir("templates")
    if directory is None:
        return _not_ready()
    source = paths.resolve_library_file(directory, name, TEMPLATE_SUFFIX)
    target = paths.resolve_library_file(directory, new_name, TEMPLATE_SUFFIX)
    if source is None or target is None:
        return 400, {
            "error": "invalid template name (use letters, digits, . _ or -)",
            "code": "invalid_template_name",
        }
    if not source.is_file():
        return 404, {"error": "template not found", "code": "template_not_found"}
    # The MOVE and the state update are ONE critical section — see `rename_style`.
    # Splitting them let a concurrent delete of `new_name` interleave between the link
    # and the state write, leaving `state.json` naming a file that no longer exists
    # while both verbs answered 200.
    with _STATE_LOCK:
        # `os.link` + `unlink`, not `rename`. `Path.rename` REPLACES an existing target
        # on POSIX, so `exists()` then `rename` is check-then-act: two tabs renaming
        # different entries onto one unused name both passed the probe and the second
        # silently destroyed the first while both answered 200. `os.link` refuses an
        # existing target atomically, so the filesystem arbitrates — kept under the lock
        # because it also guards against anything else sharing this directory.
        try:
            os.link(source, target)
        except FileExistsError:
            return 409, {
                "error": f"template {new_name!r} already exists",
                "code": "template_exists",
            }
        except OSError as exc:
            logger.warning("pptx-maker: template rename failed: %s", exc)
            return 500, {
                "error": "could not rename the template",
                "code": "template_rename_failed",
            }
        try:
            source.unlink()
        except OSError as exc:
            # The new name is linked but the old one survives, so the entry would appear
            # twice. Undo the link rather than leave a duplicate the user cannot explain.
            logger.warning("pptx-maker: template rename cleanup failed: %s", exc)
            try:
                target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                logger.debug("pptx-maker: could not remove the duplicate template link")
            return 500, {
                "error": "could not rename the template",
                "code": "template_rename_failed",
            }
        state_path, state = _load_state()
        metadata = state.get("template_metadata")
        if state_path is not None and isinstance(metadata, dict) and name in metadata:
            metadata[new_name] = {**metadata[name], "name": new_name}
            del metadata[name]
            failed = _save_state_or_undo_rename(
                state_path, state, moved_from=source, moved_to=target, kind="template"
            )
            if failed is not None:
                return failed
        return 200, {"renamed": {"from": name, "to": new_name}}


def is_user_owned(entry: dict) -> bool:
    """Whether a listed style/template is the user's (and so mutable here)."""
    return str(entry.get("source") or "") == _USER_SOURCE
