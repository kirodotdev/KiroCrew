"""File-backed preview serving for the Design Tweak backend.

``server`` is the composition root and security-policy owner.  Helpers receive
that module as ``runtime`` and resolve mutable state and compatibility seams at
call time, making the server namespace the authoritative runtime boundary.
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler
from typing import Any

from kiro_crew.security import DENIED_ROOT_PARTS

Response = tuple[int, str, bytes]

# Ceiling on ONE statically-served preview file. Generous for real web assets
# (bundles, fonts, images) while bounding the buffer: the static response reads
# the whole file into memory, so without this a large asset sitting in the
# previewed project is enough to exhaust the backend. Distinct from
# `MAX_BODY_BYTES`, which caps an inbound request body rather than an outbound
# file.
MAX_STATIC_BYTES = 64 * 1024 * 1024
CLIENT_READ_TIMEOUT = 30
OVERLAY_PATH = "/__kiro_select_to_edit__.js"

# A closed map keeps attacker-influenced path text out of response headers and
# makes unknown extensions inert and deterministic across hosts.
CTYPE_OVERRIDES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".xml": "text/xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
CTYPE_DEFAULT = "application/octet-stream"

# An upstream header only selects a literal from this map.  It never reaches a
# response-header sink verbatim, which prevents response splitting.
PROXY_CTYPES: dict[str, str] = {
    "text/html": "text/html; charset=utf-8",
    "application/xhtml+xml": "text/html; charset=utf-8",
    "text/css": "text/css; charset=utf-8",
    "text/javascript": "text/javascript; charset=utf-8",
    "application/javascript": "text/javascript; charset=utf-8",
    "application/json": "application/json; charset=utf-8",
    "application/manifest+json": "application/manifest+json",
    "text/plain": "text/plain; charset=utf-8",
    "text/markdown": "text/markdown; charset=utf-8",
    "text/xml": "text/xml; charset=utf-8",
    "application/xml": "application/xml; charset=utf-8",
    "image/svg+xml": "image/svg+xml",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
    "image/avif": "image/avif",
    "image/x-icon": "image/x-icon",
    "image/vnd.microsoft.icon": "image/x-icon",
    "font/woff": "font/woff",
    "font/woff2": "font/woff2",
    "font/ttf": "font/ttf",
    "font/otf": "font/otf",
    "application/wasm": "application/wasm",
    "audio/mpeg": "audio/mpeg",
    "video/mp4": "video/mp4",
    "video/webm": "video/webm",
    "application/octet-stream": "application/octet-stream",
}

ENTRY_CANDIDATES = (
    "index.html",
    "index.htm",
    "public/index.html",
    "dist/index.html",
    "build/index.html",
    "out/index.html",
    "app/index.html",
    "src/index.html",
    "site/index.html",
    "www/index.html",
    "docs/index.html",
    "demo/index.html",
    "example/index.html",
    "examples/index.html",
)

# These trees cannot hold a preview entry and can make a diagnostic scan
# needlessly traverse thousands of files.
SCAN_SKIP_DIRS = {
    "node_modules",
    ".git",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".cache",
    ".turbo",
    "__pycache__",
    ".venv",
    "venv",
    "coverage",
    "htmlcov",
    ".pytest_cache",
    "target",
    "vendor",
    ".idea",
    ".vscode",
}

SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
ATTR_TYPE_MODULE_RE = re.compile(r"""\btype\s*=\s*["']module["']""", re.IGNORECASE)
ATTR_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
UNBUNDLED_EXTS = (".ts", ".tsx", ".jsx")

# Containment alone intentionally does not authorize credential and VCS
# material that happens to live inside a registered project root.
PROJECT_SECRET_NAMES: frozenset[str] = frozenset(
    {
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".git-credentials",
        ".htpasswd",
        ".app_secret",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secring.gpg",
    }
)
# Whole directories, matched on any path component. `.docker` holds
# `config.json`, whose `auths` entries are registry credentials. Derived from the
# shared credential denylist so an entry added there propagates to this
# static-file-serving guard automatically; the extras are VCS metadata and
# credential dirs specific to what a previewed project tree may contain.
PROJECT_SECRET_DIRS: frozenset[str] = DENIED_ROOT_PARTS | frozenset(
    {".git", ".hg", ".svn", ".gpg", ".azure"}
)
PROJECT_SECRET_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)

# Filename classifiers cannot cover arbitrary private-key names, so the bytes
# also carry a cheap, first-line PEM backstop.
PEM_PRIVATE_KEY_RE = re.compile(rb"^-{5}BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-{5}")
PEM_MARKER_RE = re.compile(r"-{5}(?:BEGIN|END)[A-Z0-9 ]*PRIVATE KEY-{5}")
PEM_BODY_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")


def guess_ctype(runtime: Any, path: Any) -> str:
    """Return a literal content type for a file served from disk."""

    return runtime._CTYPE_OVERRIDES.get(path.suffix.lower(), runtime._CTYPE_DEFAULT)


def safe_upstream_ctype(runtime: Any, raw: str | None, path: str) -> str:
    """Return a response-safe content type selected by an upstream reply."""

    media = (raw or "").split(";", 1)[0].strip().lower()
    mapped = runtime._PROXY_CTYPES.get(media)
    if mapped:
        return mapped
    ext = runtime.os.path.splitext(runtime.urlparse(path).path)[1].lower()
    return runtime._CTYPE_OVERRIDES.get(ext, runtime._CTYPE_DEFAULT)


def unbundled_entry(runtime: Any, body: bytes) -> str:
    """Return the first TS/JSX module script, or ``""`` for static HTML."""

    try:
        text = body.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError):
        return ""
    for match in runtime._SCRIPT_TAG_RE.finditer(text):
        attrs = match.group(1)
        if not runtime._ATTR_TYPE_MODULE_RE.search(attrs):
            continue
        source_match = runtime._ATTR_SRC_RE.search(attrs)
        if not source_match:
            continue
        source = source_match.group(1)
        bare = source.split("?", 1)[0].split("#", 1)[0].lower()
        if bare.endswith(runtime._UNBUNDLED_EXTS):
            return source
    return ""


def find_entry(runtime: Any, folder: Any, root: Any | None = None) -> Any | None:
    """Return the first safe entry HTML below ``folder``, if one exists."""

    project_root = root if root is not None else folder
    for relative in runtime._ENTRY_CANDIDATES:
        # Re-contain each candidate because a directory request screens the
        # directory before entry selection, while the selected file may be a
        # symlink to a secret or to a path outside that directory.
        try:
            candidate = runtime._contained(folder, relative)
        except runtime._PathEscape:
            continue
        if not candidate.is_file():
            continue
        if (
            runtime.is_sensitive_path(str(candidate))
            or runtime._is_project_secret(project_root, candidate)
            or runtime._is_kirocrew_internal(candidate)
        ):
            continue
        return candidate
    return None


def scan_html(runtime: Any, root: Any, limit: int = 40, max_depth: int = 3) -> list[str]:
    """Return a shallow, root-relative list of previewable HTML files."""

    found: list[str] = []

    def walk(directory: Any, depth: int) -> None:
        if len(found) >= limit or depth > max_depth:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda entry: (entry.is_dir(), entry.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if len(found) >= limit:
                return
            # Check before ``is_dir()``, which follows links.  Diagnostic names
            # are visible to the preview page, so even listing a linked private
            # directory would disclose information.
            if entry.is_symlink():
                continue
            name = entry.name
            if entry.is_dir():
                if name.startswith(".") or name in runtime._SCAN_SKIP_DIRS:
                    continue
                walk(entry, depth + 1)
            elif entry.suffix.lower() in (".html", ".htm"):
                try:
                    found.append(entry.relative_to(root).as_posix())
                except ValueError:
                    continue

    walk(root, 0)
    return found


def rewrite_html(runtime: Any, body: bytes, base: str | None, script: str) -> bytes:
    """Inject the Select-to-Edit overlay and an optional base URL."""

    del runtime  # The uniform signature lets the composition root delegate directly.
    try:
        document = body.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError):
        return body
    inject_tag = f'<script src="{script}"></script>'
    if base is not None:
        base_tag = f'<base href="{base}">'
        lowered = document.lower()
        head = lowered.find("<head")
        if head != -1:
            end = lowered.find(">", head)
            if end != -1:
                document = document[: end + 1] + base_tag + document[end + 1 :]
        else:
            document = base_tag + document
    body_end = document.lower().rfind("</body>")
    if body_end != -1:
        document = document[:body_end] + inject_tag + document[body_end:]
    else:
        document += inject_tag
    return document.encode("utf-8")


def needs_dev_server_body(runtime: Any, root: Any, page: Any, entry: str) -> bytes:
    """Explain why a bundler template cannot render as static HTML."""

    built = [name for name in ("dist", "build", "out", ".output/public") if (root / name).is_dir()]
    built_hint = (
        (
            "<p>This project has a <code>"
            + "</code>, <code>".join(built)
            + "</code> folder — if that is a finished build, register THAT folder "
            "instead and it will preview from disk.</p>"
        )
        if built
        else ""
    )
    page_display = runtime._html.escape(page.name)
    entry_display = runtime._html.escape(entry)
    body = (
        f"<h3>{page_display} needs a dev server</h3>"
        f"<p>Its only script is <code>{entry_display}</code> — TypeScript/JSX, which "
        "the browser cannot run. A bundler has to transform it, so serving these "
        "files from disk renders an empty page.</p>"
        "<p><b>Start this project's dev server</b> (<code>npm run dev</code>), then "
        "press <b>Dev server</b> in the bar below the preview. Design Tweak will "
        "frame it directly, hot reload keeps working, and select-to-edit still "
        "works.</p>"
        f"{built_hint}"
    )
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<style>"
        "body{font:14px/1.6 system-ui,-apple-system,sans-serif;padding:28px 32px;"
        "color:#e6e6e6;background:#151517;max-width:56ch}"
        "h3{margin:0 0 12px;font-size:15px;font-weight:600}"
        "code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
        "background:#26262a;padding:1px 5px;border-radius:4px}"
        "p{margin:0 0 10px}b{color:#fff}"
        "</style>"
        f"{body}"
    ).encode("utf-8")


def no_entry_response(
    runtime: Any, root: Any, folder: Any, base: str, missing: str = ""
) -> Response:
    """Return a diagnostic 404 with safe links to HTML files that exist."""

    try:
        # Re-contain both paths so every scan and stat consumes the canonical
        # barrier's return value, regardless of what the caller supplied.
        root = runtime._contained(root)
        folder = runtime._contained(root, runtime.os.fspath(folder))
    except runtime._PathEscape:
        return 403, "text/plain", b"forbidden"
    scan_from = folder if folder.is_dir() else root
    candidates = runtime._scan_html(scan_from)
    if not candidates and scan_from != root:
        scan_from = root
        candidates = runtime._scan_html(scan_from)
    try:
        prefix = scan_from.relative_to(root).as_posix()
    except ValueError:
        prefix = ""
    prefix = "" if prefix in ("", ".") else prefix + "/"

    if missing:
        head = f"<code>{runtime._html.escape(missing)}</code> was not found in this project."
    else:
        head = (
            "No <code>index.html</code> in " f"<code>{runtime._html.escape(str(scan_from))}</code>."
        )

    if candidates:
        links = "".join(
            f'<li><a href="{runtime._html.escape(base + prefix + candidate)}">'
            f"{runtime._html.escape(prefix + candidate)}</a></li>"
            for candidate in candidates
        )
        body = (
            "<p>Design Tweak serves the folder as a static site and looks for an "
            "entry <code>index.html</code>. These HTML files are in the project — "
            "click one to preview it:</p>"
            f"<ul>{links}</ul>"
            "<p class='hint'>If the right entry point isn't listed, register the "
            "<em>subfolder</em> that contains it (<code>+ load new app</code>), or point "
            "Design Tweak at a running dev server URL for framework projects.</p>"
        )
    else:
        body = (
            "<p>No HTML files were found here, so there is nothing to serve "
            "statically. This usually means the project is a framework app "
            "(React / Vite / Next) that needs its dev server, or the previewable "
            "site lives in a subfolder that wasn't registered.</p>"
            "<p class='hint'>Fix it by either registering the subfolder that "
            "contains <code>index.html</code>, running the project's build "
            "(<code>npm run build</code>) and registering <code>dist/</code>, or "
            "starting <code>npm run dev</code> and pointing Design Tweak at "
            "<code>http://localhost:PORT</code>.</p>"
        )

    document = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>"
        "body{font:14px/1.6 system-ui,-apple-system,sans-serif;padding:28px 32px;"
        "color:#e6e6e6;background:#151517}"
        "h3{margin:0 0 12px;font-size:15px;font-weight:600}"
        "code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
        "background:#26262a;padding:1px 5px;border-radius:4px}"
        "ul{margin:12px 0;padding-left:20px}li{margin:3px 0}"
        "a{color:#7cc4ff}.hint{color:#9a9aa2;font-size:13px}"
        "</style>"
        f"<h3>{head}</h3>{body}"
    )
    return 404, "text/html; charset=utf-8", document.encode("utf-8")


def contains_credential(runtime: Any, data: bytes) -> bool:
    """Return whether textual file content carries a recognizable credential."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Images, fonts, and wasm have no text credentials to scan and must not
        # become false positives merely because they are arbitrary bytes.
        return False
    for pattern in runtime.get_credential_patterns():
        for match in pattern.finditer(text):
            hit = match.group()
            if "PRIVATE KEY" in hit and not runtime._PEM_BODY_RE.search(
                runtime._PEM_MARKER_RE.sub("", hit)
            ):
                # Documentation often quotes a PEM marker without carrying key
                # material; require an armoured body before refusing that case.
                continue
            return True
    return False


def is_project_secret(runtime: Any, root: Any, target: Any) -> bool:
    """Return whether ``target`` is credential material inside ``root``."""

    try:
        relative_parts = target.relative_to(root).parts
    except ValueError:
        # The canonical containment barrier normally makes this unreachable;
        # refuse an unrelatable path instead of guessing.
        return True
    for part in relative_parts:
        lowered = part.lower()
        if lowered in runtime._PROJECT_SECRET_DIRS or lowered in runtime._PROJECT_SECRET_NAMES:
            return True
        # The prefix covers .env variants and direnv's .envrc family.  A web
        # preview does not need those dotfiles, while serving one can expose a
        # live key to same-origin project JavaScript.
        if lowered.startswith(".env"):
            return True
        if lowered.endswith(runtime._PROJECT_SECRET_SUFFIXES):
            return True
    return False


def read_static_file(runtime: Any, root: Any, target: Any) -> Response:
    """Read one contained file with size and descriptor-bound no-link checks."""

    # This server buffers the whole response, so the advisory stat prevents an
    # ordinary large project asset from exhausting the backend before the open.
    try:
        size = target.stat().st_size
    except OSError as exc:
        return 500, "text/plain", str(exc).encode()
    if size > runtime.MAX_STATIC_BYTES:
        message = (
            f"file is {size} bytes, over the " f"{runtime.MAX_STATIC_BYTES}-byte preview limit"
        )
        return 413, "text/plain", message.encode()

    # The stat checks a name, not the inode ultimately read.  The no-link helper
    # opens first, validates the descriptor's real path against the approved
    # root, and reapplies the byte ceiling to the bytes it actually serves.
    data = runtime.safe_read_file_bytes_nolink(
        str(target),
        within_root=str(root),
        max_bytes=runtime.MAX_STATIC_BYTES,
    )
    if data is None:
        return 403, "text/plain", b"forbidden"
    return 200, "", data


def static_response(
    runtime: Any, root_string: str, relative: str, base: str, script: str
) -> Response:
    """Build a contained, credential-screened static preview response."""

    relative = relative.lstrip("/")
    try:
        # One canonical barrier normalizes the root and proves the request path
        # is inside it; every filesystem operation consumes these return values.
        root = runtime._contained(root_string)
        target = runtime._contained(root, relative)
    except runtime._PathEscape:
        return 403, "text/plain", b"forbidden"

    # A registered root can legitimately contain sensitive home directories or
    # project-local credentials.  Containment therefore needs all three policy
    # classifiers before a directory lookup, stat, or read is authorized.
    if runtime.is_sensitive_path(str(target)):
        return 403, "text/plain", b"forbidden"
    if runtime._is_project_secret(root, target):
        return 403, "text/plain", b"forbidden"
    if runtime._is_kirocrew_internal(target):
        return 403, "text/plain", b"forbidden"

    if target.is_dir():
        entry = runtime._find_entry(target, root)
        if entry is None:
            return runtime._no_entry_response(root, target, base)
        target = entry
    if not target.is_file():
        return runtime._no_entry_response(root, target, base, missing=relative)

    status, content_type, data = read_static_file(runtime, root, target)
    if status != 200:
        return status, content_type, data

    # Name-based secret rules cannot cover arbitrary private-key filenames or
    # credentials embedded in ordinary config, script, and fixture files.
    if runtime._PEM_PRIVATE_KEY_RE.match(data.lstrip()[:64]):
        return 403, "text/plain", b"forbidden"
    if runtime._contains_credential(data):
        return 403, "text/plain", b"forbidden"

    content_type = runtime._guess_ctype(target)
    if "text/html" in content_type:
        entry = runtime._unbundled_entry(data)
        if entry:
            # The file exists and the explanation is the successful preview;
            # returning 4xx would replace it with the panel's generic error UI.
            return (
                200,
                "text/html; charset=utf-8",
                runtime._needs_dev_server_body(root, target, entry),
            )
        try:
            subdirectory = target.parent.relative_to(root).as_posix()
        except ValueError:
            subdirectory = "."
        html_base = base if subdirectory in ("", ".") else base + subdirectory + "/"
        return (
            200,
            "text/html; charset=utf-8",
            runtime._rewrite_html(data, html_base, script=script),
        )
    return 200, content_type, data


class StaticInjectHandlerBase(BaseHTTPRequestHandler):
    """Read-only file preview handler bound to a server runtime by a subclass."""

    runtime: Any
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_READ_TIMEOUT

    def log_message(self, *args: Any) -> None:
        """Suppress the standard-library request log."""

    def _refuse(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(message)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        # No CORS header is intentional: the preview must not become a
        # cross-origin file API for an unrelated page.
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        runtime = self.runtime
        path = runtime.urlparse(self.path).path
        if path == runtime._OVERLAY_PATH:
            try:
                javascript = runtime.INJECT_FILE.read_bytes()
            except OSError:
                javascript = b"// overlay not found"
            return self._send(200, "application/javascript; charset=utf-8", javascript)

        relative = runtime.unquote(path).lstrip("/")
        first, _, rest = relative.partition("/")
        # The bound id makes one listener answer for exactly one project.  A URL
        # path cannot cross into another project's browser-storage origin.
        bound_id: str = getattr(self.server, "kiro_project_id", "")
        if first != bound_id:
            return self._refuse(404, b"unknown project")
        project = next((item for item in runtime._CFG["projects"] if item["id"] == first), None)
        if project is None:
            return self._refuse(404, b"unknown project")
        root = runtime._valid_root(project["path"])
        if root is None:
            return self._refuse(404, b"project folder no longer readable")
        status, content_type, body = runtime._static_response(
            str(root), rest, f"/{first}/", script=runtime._OVERLAY_PATH
        )
        return self._send(status, content_type, body)

    do_GET = _dispatch
    do_HEAD = _dispatch

    def do_POST(self) -> None:  # noqa: N802
        self._refuse(405, b"read-only preview server")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


def static_preview_base(runtime: Any, project_id: str) -> str:
    """Return the dedicated static-preview base URL, starting it on demand."""

    # Every project needs its own loopback port because browser storage is
    # scoped by scheme, host, and port, not by the URL path's project id.
    record = runtime._STATIC_SRV.get(project_id)
    if record and record.get("url"):
        return str(record["url"])
    try:
        server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime._StaticInjectHandler)
    except OSError:
        return ""
    server.daemon_threads = True
    setattr(server, "kiro_project_id", project_id)
    runtime.threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name=f"kiro-static-preview-{project_id}",
    ).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    runtime._STATIC_SRV[project_id] = {"srv": server, "url": url}
    return url


def stop_static_preview(runtime: Any, project_id: str) -> None:
    """Shut down and forget one project's dedicated static listener."""

    # Pop first so a failed shutdown cannot leave a dead cache hit behind.
    record = runtime._STATIC_SRV.pop(project_id, None)
    if record is None:
        return
    server = record.get("srv")
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001
        pass


def static_preview_url(runtime: Any, project_id: str) -> str:
    """Return the iframe URL for a project's file-backed preview."""

    base = runtime._static_preview_base(project_id)
    return f"{base}{project_id}/" if base else ""


__all__ = [
    "ATTR_SRC_RE",
    "ATTR_TYPE_MODULE_RE",
    "CLIENT_READ_TIMEOUT",
    "CTYPE_DEFAULT",
    "CTYPE_OVERRIDES",
    "ENTRY_CANDIDATES",
    "MAX_STATIC_BYTES",
    "OVERLAY_PATH",
    "PEM_BODY_RE",
    "PEM_MARKER_RE",
    "PEM_PRIVATE_KEY_RE",
    "PROJECT_SECRET_DIRS",
    "PROJECT_SECRET_NAMES",
    "PROJECT_SECRET_SUFFIXES",
    "PROXY_CTYPES",
    "SCAN_SKIP_DIRS",
    "SCRIPT_TAG_RE",
    "StaticInjectHandlerBase",
    "UNBUNDLED_EXTS",
    "contains_credential",
    "find_entry",
    "guess_ctype",
    "is_project_secret",
    "needs_dev_server_body",
    "no_entry_response",
    "rewrite_html",
    "safe_upstream_ctype",
    "scan_html",
    "static_preview_base",
    "static_preview_url",
    "static_response",
    "stop_static_preview",
    "unbundled_entry",
]
