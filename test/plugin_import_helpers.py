"""The package and skill builders the plugin import test modules share.

``_write_json`` writes a JSON document, creating its parent directories; ``_skill``
writes one skill directory with its ``SKILL.md``; ``_package`` builds a package with a
vendor-directory manifest, the common real-world shape. ``test_plugin_import.py`` and
its ``_bounds`` and ``_refusals`` siblings import what they use of these by name.
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _skill(root: Path, name: str, description: str = "does a thing") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )


def _package(tmp_path: Path, name: str = "demo-plugin", vendor: str = ".codex-plugin", **manifest):
    """A package with a vendor-directory manifest, the common real-world shape."""
    root = tmp_path / "src" / name
    root.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, **manifest}
    _write_json(root / vendor / "plugin.json", payload)
    return root
