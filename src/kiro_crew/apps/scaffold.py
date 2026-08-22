"""App scaffolding — generate a new app skeleton with `kirocrew app init`.

Creates a minimal app directory structure with a valid manifest,
sample agent, sample skill, and optional backend/UI stubs.
"""
from __future__ import annotations

import json
import logging
import struct
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_for_write(root: Path, *rel: str) -> Path:
    """Resolve ``root/*rel`` for writing; raise ``ValueError`` on any symlink.

    The resolved target must EQUAL its lexical path beneath the resolved root.
    Plain containment (``is_relative_to``) is not enough: it refuses escapes
    but admits an intra-root alias -- ``out/victim -> out/existing`` resolves
    inside the root, and scaffolding "victim" would truncate the sibling
    project's files. Exact equality refuses every symlink beneath the root:
    an escaping one (``Path.exists()`` is ``False`` for a dangling symlink, so
    an existence test falls through to a write that follows the link), a
    symlinked parent directory, and an in-root alias alike. An ABSOLUTE
    component is refused before the comparison: ``joinpath`` discards the root
    for one, making target equal expected trivially while both point outside
    it. The root itself may still be a symlink (a symlinked home, ``/tmp`` on
    macOS): both sides are built from ``root.resolve()``, so it compares
    equal.

    A refusal aborts the scaffold rather than skipping the site: a skipped
    write would leave a partial app (no ``app.json``, no agent) while the CLI
    reports success. Resolution failing outright (a symlink loop raises
    ``OSError`` on Python 3.10/3.11 and ``RuntimeError`` on 3.12+) is refused
    the same way rather than crashing with an unexplained traceback.
    """
    target = root.joinpath(*rel)
    try:
        resolved_root = root.resolve()
        expected = resolved_root.joinpath(*rel)
        # An absolute component (or a Windows drive-relative one) makes
        # joinpath DISCARD the root, so target equals expected trivially while
        # both point outside it; require the expected path to sit strictly
        # beneath the resolved root before comparing. is_relative_to is
        # lexical, so a ".." component passes here and is refused by the
        # equality check below instead (resolve() normalizes it away).
        if not expected.is_relative_to(resolved_root) or expected == resolved_root:
            raise ValueError(f"refusing a path component that leaves {root}: {rel}")
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve {target} under {root}: {exc}") from exc
    if resolved != expected:
        raise ValueError(
            f"refusing to write through a symlink or traversal: "
            f"{target} resolves to {resolved}, expected {expected}"
        )
    return resolved


#: Geometry and palette of the scaffolded placeholder icon: a plate inset in a
#: darker field, at the size the publishing guide asks for and neutral enough to
#: read on both a light and a dark card.
_ICON_PX = 512
_ICON_INSET = 128
_ICON_FIELD = (46, 52, 64)
_ICON_PLATE = (67, 76, 94)


def _placeholder_icon_png() -> bytes:
    """Encode the placeholder store icon, standard library only.

    Pillow is not a dependency of this path, and taking one on so that
    ``app init`` can draw a rectangle would be a poor trade: a PNG is a signature
    followed by length-tag-payload-CRC chunks, so emitting one directly is
    shorter than the argument for the dependency would be.

    Truecolor (colour type 2), not RGBA. The publishing guide requires an opaque
    icon -- an opaque tile carries its own background, which is what makes the
    dark variant optional rather than a latent bug -- so carrying an alpha
    channel would model a degree of freedom the icon is not allowed to use.

    The bytes are identical for every app, which is deliberate: a single known
    digest stays recognisable as "still the placeholder", which a per-app colour
    would trade away for nothing.
    """
    field = bytes(_ICON_FIELD) * _ICON_PX
    margin = bytes(_ICON_FIELD) * _ICON_INSET
    plate = bytes(_ICON_PLATE) * (_ICON_PX - 2 * _ICON_INSET)
    raw = bytearray()
    for y in range(_ICON_PX):
        # Leading byte is the scanline filter type: 0, meaning the row is stored
        # as-is. Every row is a run of at most two colours, which deflate folds
        # down to well under a kilobyte.
        inside = _ICON_INSET <= y < _ICON_PX - _ICON_INSET
        raw += b"\x00" + (margin + plate + margin if inside else field)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    # width, height, bit depth, colour type, compression, filter, interlace
    ihdr = struct.pack(">IIBBBBB", _ICON_PX, _ICON_PX, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


_MANIFEST_TEMPLATE = {
    "name": "",
    "version": "0.1.0",
    "displayName": "",
    "description": "",
    "author": "",
    # The store's card and row icon, repo-relative. Scaffolded rather than left
    # to the publishing guide: a field nobody knows exists is a field nobody
    # fills, and an entry that reaches the catalog without one renders as a
    # generated placeholder that looks like a store bug rather than an
    # incomplete manifest.
    "iconPath": "assets/icon.png",
    "agents": ["agents/sample-agent.json"],
    "skills": ["skills/sample-skill"],
    "tags": [],
}

_AGENT_TEMPLATE = {
    "name": "sample-agent",
    "model": "auto",
    "description": "A sample agent — customize this for your use case",
    "prompt": "You are a helpful assistant.",
    "tools": [],
}

_SKILL_TEMPLATE = """---
description: Sample skill for {display_name}
always: false
---

# {display_name} — Sample Skill

This skill provides domain knowledge for the {name} app.

## What This Skill Does

Describe what this skill teaches the agent.

## Key Concepts

- Concept 1
- Concept 2

## Common Patterns

Describe common patterns the agent should know about.
"""

_BACKEND_TEMPLATE = '''"""Minimal backend for {name} — a KiroCrew app.

Run with: python backend/server.py
Or let KiroCrew manage it via the app manifest backend section.
"""
import json
import os

from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "{name}")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {{"status": "ok", "app": APP_NAME}})
        elif self.path == "/api/apps/{name}/status":
            self._json(200, {{"app": APP_NAME, "version": "0.1.0"}})
        else:
            self._json(404, {{"error": "not found"}})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"{{APP_NAME}} backend on port {{PORT}}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

_UI_PACKAGE_JSON_TEMPLATE = """\
{{
  "name": "{name}-ui",
  "private": true,
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "vite build"
  }},
  "dependencies": {{
    "react": "^18.2.0",
    "react-dom": "^18.2.0"
  }},
  "devDependencies": {{
    "@vitejs/plugin-react": "^4.2.0",
    "vite": "^5.0.0"
  }},
  "peerDependencies": {{
    "@kirocrew/app-sdk": "*",
    "lucide-react": "*"
  }}
}}
"""

_UI_VITE_CONFIG_TEMPLATE = """\
import {{ defineConfig }} from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({{
  plugins: [react()],
  build: {{
    lib: {{
      entry: 'src/App.tsx',
      formats: ['es'],
      fileName: () => 'index.mjs',
    }},
    outDir: 'dist',
    rollupOptions: {{
      external: [
        'react', 'react-dom', 'react/jsx-runtime',
        '@kirocrew/app-sdk', '@kirocrew/app-sdk/ui', 'lucide-react',
      ],
    }},
  }},
}})
"""

_UI_APP_TSX_TEMPLATE = """\
import {{ useAppApi }} from '@kirocrew/app-sdk'
import {{ Card, CardTitle, PageHeader, StatCard }} from '@kirocrew/app-sdk/ui'
import {{ useState, useEffect }} from 'react'

export default function {component_name}() {{
  const api = useAppApi()
  const [loading, setLoading] = useState(true)

  useEffect(() => {{
    // Fetch initial data here
    setLoading(false)
  }}, [])

  return (
    <>
      <PageHeader title="{display_name}" subtitle="{description}" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Status" value="OK" accent />
        </div>
        <Card>
          <CardTitle>Overview</CardTitle>
          {{loading
            ? <p className="text-sm text-muted">Loading…</p>
            : <p className="text-sm text-muted">Your app content goes here.</p>
          }}
        </Card>
      </div>
    </>
  )
}}
"""

_UI_GITIGNORE_TEMPLATE = """\
node_modules/
"""

_README_TEMPLATE = """# {display_name}

{description}

## Installation

```bash
kirocrew app install /path/to/{name}
kirocrew app enable {name}
```

## Development

Edit agents, skills, and backend code. Changes to agents and skills
take effect on next agent invocation. Backend changes require restart.

## Structure

```
{name}/
├── app.json              ← manifest
├── assets/
│   └── icon.png          ← store icon; replace this placeholder
├── agents/               ← agent definitions
│   └── sample-agent.json
├── skills/               ← skill files
│   └── sample-skill/
│       └── SKILL.md
├── backend/              ← optional backend
│   └── server.py
└── README.md
```
"""


def _write_sites(
    *, include_backend: bool, include_ui: bool
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    """The directories and files a scaffold run creates, in creation order.

    The single source of truth for what `scaffold_app` touches: it validates
    this list before its first write, and the test suite drives its containment
    cases from the same list, so a newly added write site cannot be covered by
    one and missed by the other.

    Each site is a tuple of path COMPONENTS relative to the app directory, not a
    joined string. Components are what `Path.joinpath` and `_resolve_for_write`
    both take, so the separator is never chosen here -- a joined form would have
    to be split back apart, and that round-trip is where a hardcoded '/' becomes
    a real path on Windows.
    """
    dirs = [("assets",), ("agents",), ("skills",), ("skills", "sample-skill")]
    files = [
        ("app.json",),
        ("assets", "icon.png"),
        ("agents", "sample-agent.json"),
        ("skills", "sample-skill", "SKILL.md"),
    ]
    if include_backend:
        dirs.append(("backend",))
        files.append(("backend", "server.py"))
    if include_ui:
        dirs += [("ui",), ("ui", "src")]
        files += [
            ("ui", "package.json"),
            ("ui", "vite.config.ts"),
            ("ui", "src", "App.tsx"),
            ("ui", ".gitignore"),
        ]
    files.append(("README.md",))
    return tuple(dirs), tuple(files)


def _validate_write_sites(
    app_dir: Path, *, include_backend: bool, include_ui: bool
) -> None:
    """Prove every path the scaffold will write is writable, BEFORE writing any.

    Ordering is half the point. `app.json` is the first file written and the
    optional trees are written last, so a refusal raised AT a write site aborts a
    run that has already overwritten the manifest of an existing app -- turning a
    refused path into lost data. Deciding up front makes the run all-or-nothing:
    either nothing on disk is touched, or every path was already proven writable.

    Two independent properties, and containment alone is not enough. A site can
    resolve squarely inside the app directory and still be unwritable because it
    is there as the WRONG KIND: `mkdir(exist_ok=True)` raises `FileExistsError`
    when the path is a regular file (exist_ok only forgives an existing
    DIRECTORY), and `write_text` raises `IsADirectoryError` against a directory.
    Neither is a `ValueError`, so both escape the caller's error contract as a
    raw traceback on top of the lost manifest.

    So each site is checked for both:

    * every site resolves to its own lexical path inside `app_dir`
      (`_resolve_for_write` -- refuses traversal, escaping and in-root symlinks);
    * a directory site is absent or already a directory, and a file site is not
      an existing directory.

    `exists()`/`is_dir()` are unambiguous here precisely because containment ran
    first: it refuses every symlink beneath the root, so there is no link left
    for these to follow somewhere else.

    Not a substitute for the per-site `_resolve_for_write` calls, because
    validation and use are separate moments: a path swapped in between is still
    refused at the write. Both layers refuse it; only this one refuses it while
    the app is still exactly as it was found.
    """
    dirs, files = _write_sites(
        include_backend=include_backend, include_ui=include_ui
    )
    for rel in dirs:
        target = _resolve_for_write(app_dir, *rel)
        if target.exists() and not target.is_dir():
            raise ValueError(
                f"refusing to scaffold {app_dir}: {Path(*rel)} exists and is not a "
                "directory, so it cannot hold the scaffolded files. Remove or "
                "rename it and retry."
            )
    for rel in files:
        target = _resolve_for_write(app_dir, *rel)
        if target.is_dir():
            raise ValueError(
                f"refusing to scaffold {app_dir}: {Path(*rel)} exists and is a "
                "directory, so it cannot be written as a file. Remove or rename "
                "it and retry."
            )


def scaffold_app(
    output_dir: Path,
    name: str,
    *,
    display_name: str = "",
    description: str = "",
    author: str = "",
    include_backend: bool = False,
    include_ui: bool = False,
    include_cron: bool = False,
) -> Path:
    """Create a new app skeleton at *output_dir*.

    Returns the path to the created app directory.
    """
    if not display_name:
        display_name = name.replace("-", " ").title()
    if not description:
        description = f"A Kiro Crew app: {display_name}"
    if not author:
        import os
        author = os.environ.get("USER", "developer")

    app_dir = output_dir / name
    # Refuses both a traversal in *name* and a pre-existing symlink at
    # output_dir/name that would relocate every write outside the output dir.
    _resolve_for_write(output_dir, name)
    app_dir.mkdir(parents=True, exist_ok=True)

    # Every write path is decided here, while nothing has been written yet: the
    # manifest below overwrites an existing app's app.json, so a refusal raised
    # later would cost the developer that file.
    _validate_write_sites(
        app_dir, include_backend=include_backend, include_ui=include_ui
    )

    # Manifest
    manifest: dict[str, object] = {**_MANIFEST_TEMPLATE}
    manifest["name"] = name
    manifest["displayName"] = display_name
    manifest["description"] = description
    manifest["author"] = author
    if include_backend:
        manifest["backend"] = {
            "entryPoint": "backend/server.py",
            "port": "auto",
            "healthCheck": "/health",
        }
    if include_ui:
        manifest["ui"] = {
            "entry": "dist/index.mjs",
            "pages": [
                {
                    "route": f"/apps/{name}",
                    "label": display_name,
                    "icon": "Package",
                }
            ],
        }
    if include_cron:
        manifest["crons"] = [
            {
                "name": f"{name}-check",
                "every": 300,
                "message": f"Run periodic check for {display_name}",
            }
        ]
    _resolve_for_write(app_dir, "app.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Store icon. Real bytes, not just the manifest key: an `iconPath` naming a
    # file that does not exist publishes worse than naming nothing, because the
    # store's fallback is identical either way and the developer gets a broken
    # reference instead of a working default. Shipping a valid opaque square
    # makes an iconless app a state someone has to CREATE by deleting this, not
    # one they fall into by never reading the publishing guide.
    #
    # Written only when absent, unlike every other file here. The rest are
    # GENERATED -- re-running `app init` reproduces them from the same arguments,
    # so overwriting costs nothing. This one is the developer's ARTWORK the moment
    # they replace it, which is the entire point of scaffolding it, so an
    # unconditional write would make a second `app init` destroy the icon.
    #
    # Contained by the up-front pass like every other site here, which is what
    # makes the escapes this write went through unreachable: a symlinked
    # `assets`, a dangling `icon.png` link (`exists()` reads False for one, so an
    # unresolved existence test falls through to a write that follows it), an
    # in-root alias, and `assets` present as a regular file. The `exists()` test
    # below is a keep-the-artwork check on a path already proven both contained
    # and of the right kind, not a containment check.
    _resolve_for_write(app_dir, "assets").mkdir(exist_ok=True)
    icon = _resolve_for_write(app_dir, "assets", "icon.png")
    if not icon.exists():
        icon.write_bytes(_placeholder_icon_png())

    # Agent
    _resolve_for_write(app_dir, "agents").mkdir(exist_ok=True)
    _resolve_for_write(app_dir, "agents", "sample-agent.json").write_text(
        json.dumps(_AGENT_TEMPLATE, indent=2) + "\n", encoding="utf-8"
    )

    # Skill
    _resolve_for_write(app_dir, "skills", "sample-skill").mkdir(
        parents=True, exist_ok=True
    )
    _resolve_for_write(app_dir, "skills", "sample-skill", "SKILL.md").write_text(
        _SKILL_TEMPLATE.format(name=name, display_name=display_name),
        encoding="utf-8",
    )

    # Backend (optional)
    if include_backend:
        _resolve_for_write(app_dir, "backend").mkdir(exist_ok=True)
        _resolve_for_write(app_dir, "backend", "server.py").write_text(
            _BACKEND_TEMPLATE.format(name=name), encoding="utf-8"
        )

    # UI (optional)
    if include_ui:
        _resolve_for_write(app_dir, "ui").mkdir(exist_ok=True)
        _resolve_for_write(app_dir, "ui", "src").mkdir(exist_ok=True)

        component_name = name.replace("-", " ").title().replace(" ", "")

        _resolve_for_write(app_dir, "ui", "package.json").write_text(
            _UI_PACKAGE_JSON_TEMPLATE.format(name=name), encoding="utf-8"
        )
        _resolve_for_write(app_dir, "ui", "vite.config.ts").write_text(
            _UI_VITE_CONFIG_TEMPLATE.format(), encoding="utf-8"
        )
        _resolve_for_write(app_dir, "ui", "src", "App.tsx").write_text(
            _UI_APP_TSX_TEMPLATE.format(
                component_name=component_name,
                display_name=display_name,
                description=description,
            ),
            encoding="utf-8",
        )
        _resolve_for_write(app_dir, "ui", ".gitignore").write_text(
            _UI_GITIGNORE_TEMPLATE, encoding="utf-8"
        )

    # README
    _resolve_for_write(app_dir, "README.md").write_text(
        _README_TEMPLATE.format(
            name=name, display_name=display_name, description=description
        ),
        encoding="utf-8",
    )

    logger.info("Scaffolded app %s at %s", name, app_dir)
    return app_dir
