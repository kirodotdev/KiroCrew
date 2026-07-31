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
import re
from pathlib import Path

from kiro_crew.apps.builtins.pptx_maker.backend import engine, paths
from kiro_crew.atomic_write import atomic_write

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
            try:
                cover = cover_html(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                cover = ""
        out.append({**style, "coverHtml": cover})
    return out


def style_html(name: str) -> str | None:
    """One style's full HTML, or ``None`` when it does not exist.

    BLOCKING — call through ``off_loop``.
    """
    path = _style_file_in_dirs(name, engine.load_lists().get("stylesDirs") or [])
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def list_templates() -> list[dict]:
    """Templates from the engine, with theme colours / fonts / layout counts.

    BLOCKING — call through ``off_loop``.
    """
    return [t for t in engine.load_lists().get("templates") or [] if isinstance(t, dict)]


def _load_state() -> tuple[Path | None, dict]:
    """The engine's ``state.json`` path and contents (``{}`` when unreadable)."""
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
    if target.exists():
        return 409, {"error": f"style {name!r} already exists", "code": "style_exists"}
    try:
        atomic_write(target, html)
    except OSError as exc:
        logger.warning("pptx-maker: style import failed: %s", exc)
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
    try:
        target.unlink()
    except OSError as exc:
        logger.warning("pptx-maker: style delete failed: %s", exc)
        return 500, {"error": "could not delete the style", "code": "style_delete_failed"}
    state_path, state = _load_state()
    pins = state.get("pinned_styles")
    if state_path is not None and isinstance(pins, list) and name in pins:
        state["pinned_styles"] = [p for p in pins if p != name]
        _save_state(state_path, state)
    return 200, {"deleted": name}


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
    if target.exists():
        return 409, {"error": f"style {new_name!r} already exists", "code": "style_exists"}
    try:
        source.rename(target)
    except OSError as exc:
        logger.warning("pptx-maker: style rename failed: %s", exc)
        return 500, {"error": "could not rename the style", "code": "style_rename_failed"}
    state_path, state = _load_state()
    pins = state.get("pinned_styles")
    if state_path is not None and isinstance(pins, list) and name in pins:
        state["pinned_styles"] = [new_name if p == name else p for p in pins]
        _save_state(state_path, state)
    return 200, {"renamed": {"from": name, "to": new_name}}


def pin_style(name: str, pinned: bool) -> tuple[int, dict]:
    """Pin or unpin a style so the agent prefers it by default.

    BLOCKING — call through ``off_loop``.
    """
    if not paths.SEGMENT_RE.match(name or ""):
        return 400, {"error": "invalid style name", "code": "invalid_style_name"}
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
    if target.exists():
        return 409, {"error": f"template {name!r} already exists", "code": "template_exists"}
    try:
        target.write_bytes(data)
    except OSError as exc:
        logger.warning("pptx-maker: template import failed: %s", exc)
        return 500, {"error": "could not save the template", "code": "template_write_failed"}
    # Analysis is best effort: an un-analyzed template still works, it just has
    # no theme colours / layout count to show, so a failure here must not undo
    # an import the user already sees on disk.
    return 200, {"imported": name, "metadata": engine.analyze_template(target, description)}


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
    try:
        target.unlink()
    except OSError as exc:
        logger.warning("pptx-maker: template delete failed: %s", exc)
        return 500, {"error": "could not delete the template", "code": "template_delete_failed"}
    state_path, state = _load_state()
    metadata = state.get("template_metadata")
    if state_path is not None and isinstance(metadata, dict) and name in metadata:
        del metadata[name]
        _save_state(state_path, state)
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
    if target.exists():
        return 409, {"error": f"template {new_name!r} already exists", "code": "template_exists"}
    try:
        source.rename(target)
    except OSError as exc:
        logger.warning("pptx-maker: template rename failed: %s", exc)
        return 500, {"error": "could not rename the template", "code": "template_rename_failed"}
    state_path, state = _load_state()
    metadata = state.get("template_metadata")
    if state_path is not None and isinstance(metadata, dict) and name in metadata:
        metadata[new_name] = {**metadata[name], "name": new_name}
        del metadata[name]
        _save_state(state_path, state)
    return 200, {"renamed": {"from": name, "to": new_name}}


def is_user_owned(entry: dict) -> bool:
    """Whether a listed style/template is the user's (and so mutable here)."""
    return str(entry.get("source") or "") == _USER_SOURCE
