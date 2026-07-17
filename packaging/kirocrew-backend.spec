# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone KiroCrew backend binary.

Produces ``dist/kirocrew-backend/kirocrew-backend`` — a self-contained
executable that runs the full ``kirocrew`` CLI (gateway, setup, chat, …) with
NO system Python, pip, or site-packages required. The Electron desktop app
bundles this directory as ``extraResources`` and spawns
``kirocrew-backend gateway`` (see website/electron/find-bin.js + main.js).

Build (from repo root, after the frontend dist is staged into
src/kiro_crew/static/dist):

    pyinstaller packaging/kirocrew-backend.spec --noconfirm

The output is a one-folder bundle (NOT one-file): faster startup, and the
gateway can read its bundled data files from a stable on-disk layout.
"""

from __future__ import annotations

import os
from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)

# ``SPECPATH`` is the dir containing this spec (packaging/); the repo root is
# its parent. PyInstaller injects SPECPATH into the spec namespace.
REPO_ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # noqa: F821
SRC = os.path.join(REPO_ROOT, "src")
PKG = os.path.join(SRC, "kiro_crew")

# --- Data files: everything in setup.cfg [options.package_data] PLUS the
#     pre-built dashboard (static/dist), which package_data intentionally
#     omits (content-hashed names). PyInstaller needs them on disk next to the
#     binary so the gateway can serve the SPA and load configs/prompts/skills.
datas = []


def _add_tree(rel_src: str, rel_dst: str) -> None:
    """Stage a file or directory tree under the bundle, preserving layout."""
    abs_src = os.path.join(PKG, rel_src)
    if os.path.isdir(abs_src):
        for root, _dirs, files in os.walk(abs_src):
            for f in files:
                full = os.path.join(root, f)
                # Destination dir mirrors kiro_crew/<...> under the bundle.
                dst_dir = os.path.join(
                    "kiro_crew", os.path.relpath(os.path.dirname(full), PKG)
                )
                datas.append((full, dst_dir))
    elif os.path.isfile(abs_src):
        datas.append((abs_src, os.path.join("kiro_crew", os.path.dirname(rel_dst))))


# Individual data files (mirror setup.cfg package_data).
for rel in [
    "py.typed",
    "slack-manifest.yaml",
    "model_tokens.json",
    "model_registry.json",
    "config/defaults.json",
    "config/prompt.md",
    "config/persona-lumon.md",
    "config/persona-lcars.md",
    "config/persona-bikini-bottom.md",
    "config/persona-knight-rider.md",
    "config/prompt-orchestrator.md",
    "apps/app-registry.json",
]:
    _add_tree(rel, rel)

# Directory trees (data-bearing).
for rel in [
    "config",          # all prompts/personas/schema json
    "apps",            # builtins + app.json manifests + registry
    "eval/scenarios",  # eval specs
    "docs",            # in-app docs served by the dashboard
    "static",          # css/js + the staged dashboard dist
    "skills",          # bundled SKILL.md (if present under the pkg)
]:
    _add_tree(rel, rel)

# Project-level agents/ + skills/ + prompt live at the repo root (loaded via
# KIROCREW_PROJECT_DIR). Bundle them so the standalone app has its agent config
# and skills without a source checkout.
for proj_rel in ["agents", "skills"]:
    abs_src = os.path.join(REPO_ROOT, proj_rel)
    if os.path.isdir(abs_src):
        for root, _dirs, files in os.walk(abs_src):
            for f in files:
                full = os.path.join(root, f)
                dst_dir = os.path.join(
                    proj_rel, os.path.relpath(os.path.dirname(full), abs_src)
                )
                datas.append((full, dst_dir))

# Third-party packages that ship data/templates needed at runtime.
datas += collect_data_files("cron_descriptor")  # locale .mo translation files

# --- Hidden imports: modules reached via importlib / conditional import that
#     PyInstaller's static analysis misses.
hiddenimports = []
# All builtin apps are imported dynamically (apps/routes.py, cli.py, server.py).
hiddenimports += collect_submodules("kiro_crew.apps.builtins")
# The whole package, to be safe against lazy submodule imports.
hiddenimports += collect_submodules("kiro_crew")
# Optional native SQLite shim (imported under try/except on Linux x86_64).
hiddenimports += ["pysqlite3"]
# numpy / aiohttp internals occasionally need explicit pickup.
hiddenimports += collect_submodules("numpy")

# Native shared libs (numpy, optional lxml/pysqlite3 wheels).
binaries = []
for mod in ["numpy", "pysqlite3", "lxml"]:
    try:
        binaries += collect_dynamic_libs(mod)
    except Exception:
        pass

block_cipher = None

# Entry point is __main__.py, NOT cli.py: __main__ runs _ensure_ssl_certs()
# BEFORE importing kiro_crew.cli (which pulls aiohttp and caches its SSL
# context at import time). cli.py has no __main__ guard and is not runnable
# directly.
a = Analysis(
    [os.path.join(PKG, "__main__.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim heavy optional libs that are NOT required for the core app. boto3 /
    # amazon-transcribe are optional extras (lazy-imported); excluding keeps
    # the bundle lean. Remove from excludes if shipping the [aws]/[voice] build.
    excludes=["boto3", "botocore", "amazon_transcribe", "matplotlib", "tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kirocrew-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # headless gateway: log to stdout/stderr for the Electron parent
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # honor the host arch (arm64 / x86_64)
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="kirocrew-backend",
)
