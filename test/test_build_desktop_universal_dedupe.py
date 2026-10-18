"""The universal macOS build shares its duplicate half instead of shipping it twice.

Background
----------
A universal ``KiroCrew.app`` carries two backend trees and two KAS engine roots,
one per architecture, and most of each pair is the same bytes: every pure-Python
package, the dashboard's ``static/dist``, the stdlib's ``.py`` modules and
their pycs, and the KAS engine's JavaScript. Shipped twice, that pushed the
update zip past 1 GiB -- the size at which the Squirrel.Mac inside every
installed app reads the zip into one ``NSData``, doubles it to 2 GiB, and is
killed by PartitionAlloc (docs/build/desktop-app.md, "the update zip must
stay under 1 GiB").

``packaging/dedupe_universal_backend.py`` leaves the arm64 tree whole and turns
each byte-identical ENTRY of the x64 tree's stdlib and site-packages containers
into a relative symlink into the arm64 tree, and points the x64 KAS root's
``bundle/`` at the arm64 root's. These tests pin the rules that make that safe:
whole entries only (a package with one arch-specific file stays whole in both
trees), the per-arch exclusions, relative links that resolve, and the KAS
record check. The last group pins the build script's use of the tool: the
neutral ``compileall -d`` prefix without which no pyc is ever identical, the
re-run of the self-containment probes after sharing, and the two zip ceilings.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TOOL = ROOT / "packaging" / "dedupe_universal_backend.py"
SCRIPT = ROOT / "packaging" / "build-desktop.sh"
SIGN_WORKFLOW = ROOT / ".github" / "workflows" / "sign-and-notarize.yml"

symlinks_only = pytest.mark.skipif(
    os.name == "nt", reason="the tool plants POSIX symlinks inside a macOS .app"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("dedupe_universal_backend", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, data: bytes = b"same bytes", mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)


def _backend_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Two backend trees that differ exactly where a real pair does."""
    keep, link = tmp_path / "kirocrew-backend-arm64", tmp_path / "kirocrew-backend-x64"
    for tree, arch in ((keep, b"arm64"), (link, b"x64")):
        sp = tree / "lib" / "python3.12" / "site-packages"
        # Per-arch: the interpreter, a compiled extension, the build config.
        _write(tree / "bin" / "python3.12", arch, 0o755)
        _write(tree / "lib" / "python3.12" / "lib-dynload" / "_ssl.so", arch)
        _write(tree / "lib" / "python3.12" / "_sysconfigdata__darwin_darwin.py", arch)
        _write(tree / "lib" / "python3.12" / "config-3.12-darwin" / "Makefile", arch)
        # Identical stdlib module and package.
        _write(tree / "lib" / "python3.12" / "os.py")
        _write(tree / "lib" / "python3.12" / "json" / "__init__.py")
        _write(tree / "lib" / "python3.12" / "json" / "__pycache__" / "__init__.cpython-312.pyc")
        # Identical pure package with its dist-info.
        _write(sp / "pure_pkg" / "__init__.py")
        _write(sp / "pure_pkg" / "static" / "dist" / "index.html", b"<html>")
        _write(sp / "pure_pkg-1.0.dist-info" / "RECORD", b"pure_pkg/__init__.py,sha256=x")
        # A package whose ONE native file differs: must stay whole in both trees.
        _write(sp / "mixed_pkg" / "__init__.py")
        _write(sp / "mixed_pkg" / "_native.abi3.so", arch)
        # A pure module whose bytes agree but whose mode does not.
        _write(sp / "modebit.py", mode=0o755 if arch == b"arm64" else 0o644)
        # An entry present on one side only.
        if arch == b"arm64":
            _write(sp / "arm64_only.py")
    return keep, link


@symlinks_only
def test_identical_entries_become_relative_symlinks_into_the_kept_tree(tmp_path: Path) -> None:
    tool = _load_tool()
    keep, link = _backend_pair(tmp_path)

    saved = tool.dedupe_backend(keep, link, out=open(os.devnull, "w"))

    sp = link / "lib" / "python3.12" / "site-packages"
    for entry in (
        sp / "pure_pkg",
        sp / "pure_pkg-1.0.dist-info",
        link / "lib" / "python3.12" / "os.py",
        link / "lib" / "python3.12" / "json",
    ):
        assert entry.is_symlink(), f"{entry} should be shared"
        target = os.readlink(entry)
        assert not os.path.isabs(target), f"{entry} -> {target} must be relative to survive a move"
        assert entry.resolve().is_relative_to(keep), f"{entry} must resolve into the kept tree"
    # The shared package reads through the link, whole.
    assert (sp / "pure_pkg" / "static" / "dist" / "index.html").read_bytes() == b"<html>"
    assert saved > 0


@symlinks_only
def test_an_entry_with_one_differing_file_stays_whole_in_both_trees(tmp_path: Path) -> None:
    """Granularity is the whole entry: a package is never half symlinked, so
    ``Path(__file__).resolve()`` inside it always lands beside its own files."""
    tool = _load_tool()
    keep, link = _backend_pair(tmp_path)

    tool.dedupe_backend(keep, link, out=open(os.devnull, "w"))

    mixed = link / "lib" / "python3.12" / "site-packages" / "mixed_pkg"
    assert mixed.is_dir() and not mixed.is_symlink()
    assert not (
        mixed / "__init__.py"
    ).is_symlink(), "identical file inside a differing package stays real"
    assert (mixed / "_native.abi3.so").read_bytes() == b"x64"


@symlinks_only
def test_per_arch_entries_and_bin_are_never_touched(tmp_path: Path) -> None:
    tool = _load_tool()
    keep, link = _backend_pair(tmp_path)
    # Make the excluded entries identical too, so only the exclusion keeps them.
    for rel in (
        "lib/python3.12/lib-dynload/_ssl.so",
        "lib/python3.12/_sysconfigdata__darwin_darwin.py",
        "lib/python3.12/config-3.12-darwin/Makefile",
        "bin/python3.12",
    ):
        _write(link / rel, (keep / rel).read_bytes(), 0o755 if rel.startswith("bin") else None)
        (keep / rel).chmod((link / rel).stat().st_mode)

    tool.dedupe_backend(keep, link, out=open(os.devnull, "w"))

    for rel in (
        "lib/python3.12/lib-dynload",
        "lib/python3.12/_sysconfigdata__darwin_darwin.py",
        "lib/python3.12/config-3.12-darwin",
        "bin/python3.12",
        "bin",
    ):
        assert not (link / rel).is_symlink(), f"{rel} is per-arch and must stay real"


@symlinks_only
def test_same_bytes_different_mode_and_one_sided_entries_are_kept(tmp_path: Path) -> None:
    tool = _load_tool()
    keep, link = _backend_pair(tmp_path)

    tool.dedupe_backend(keep, link, out=open(os.devnull, "w"))

    sp = link / "lib" / "python3.12" / "site-packages"
    assert not (sp / "modebit.py").is_symlink(), "a mode difference is a difference"
    assert not (sp / "arm64_only.py").exists(), "nothing is added to the x64 tree"
    assert (keep / "lib" / "python3.12" / "site-packages" / "arm64_only.py").is_file()


def _kas_pair(
    tmp_path: Path, *, same_engine: bool = True, rewrite: bool = False
) -> tuple[Path, Path]:
    keep, link = tmp_path / "kas-arm64", tmp_path / "kas-x64"
    for root, arch in ((keep, "arm64"), (link, "x64")):
        home = root / "0.66.33"
        home.mkdir(parents=True)
        (root / "BUNDLED-VERSION").write_text("0.66.33\n")
        _write(
            home
            / "bundle"
            / "node_modules"
            / "@kiro"
            / "agent"
            / "dist"
            / "server"
            / "acp-server.js",
            b"js",
        )
        _write(home / "bundle" / "node_modules" / "z3-solver" / "z3-built.wasm", b"wasm")
        _write(
            home / "node" / f"node-v22.23.3-darwin-{arch}" / "bin" / "node", arch.encode(), 0o755
        )
        (home / "install.json").write_text(
            json.dumps(
                {
                    "version": "0.66.33",
                    "engine_sha256": "e" * 64 if same_engine or arch == "arm64" else "f" * 64,
                    "node_key": f"darwin-{arch}",
                    "acp_server": "bundle/node_modules/@kiro/agent/dist/server/acp-server.js",
                    "node_bin": f"node/node-v22.23.3-darwin-{arch}/bin/node",
                }
            )
        )
    if rewrite:
        _write(
            link / "0.66.33" / "bundle" / "node_modules" / "z3-solver" / "z3-built.wasm",
            b"resigned",
        )
    return keep, link


@symlinks_only
def test_kas_bundle_is_shared_when_both_roots_record_the_same_engine(tmp_path: Path) -> None:
    tool = _load_tool()
    keep, link = _kas_pair(tmp_path)

    saved = tool.dedupe_kas(keep, link, out=open(os.devnull, "w"))

    bundle = link / "0.66.33" / "bundle"
    assert bundle.is_symlink() and not os.path.isabs(os.readlink(bundle))
    assert bundle.resolve() == (keep / "0.66.33" / "bundle").resolve()
    # The record's entry still resolves through the link, which is what the
    # gateway's installed_bundle() reader checks (acp_server.is_file()).
    record = json.loads((link / "0.66.33" / "install.json").read_text())
    assert (link / "0.66.33" / record["acp_server"]).is_file()
    assert (link / "0.66.33" / record["node_bin"]).read_bytes() == b"x64", "Node stays per-arch"
    assert saved > 0


@symlinks_only
def test_kas_roots_recording_different_engines_are_both_kept(tmp_path: Path) -> None:
    tool = _load_tool()
    keep, link = _kas_pair(tmp_path, same_engine=False)

    assert tool.dedupe_kas(keep, link, out=open(os.devnull, "w")) == 0
    assert not (link / "0.66.33" / "bundle").is_symlink()


@symlinks_only
def test_kas_same_engine_but_rewritten_tree_is_refused(tmp_path: Path) -> None:
    """Equal records with differing bytes mean something rewrote one copy after
    extraction; sharing would silently ship the other arch's rewrite."""
    tool = _load_tool()
    keep, link = _kas_pair(tmp_path, rewrite=True)

    with pytest.raises(SystemExit, match="refusing to share a rewritten tree"):
        tool.dedupe_kas(keep, link, out=open(os.devnull, "w"))
    assert not (link / "0.66.33" / "bundle").is_symlink()


# --- The runtime's side of the contract ----------------------------------------


@symlinks_only
def test_the_x64_process_resolves_its_own_launcher_through_a_shared_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kiro_crew`` imported from the x64 tree lives, resolved, in the arm64 tree.

    The resolver that names the launcher for every built-in MCP server walks up
    from the package to find a sibling ``bin/kirocrew``. Walking from the
    RESOLVED path finds the arm64 tree's launcher, whose interpreter cannot run
    on an Intel Mac; the walk must start from the path the import system used.
    """
    import unittest.mock

    import kiro_crew.agent as agent_mod

    dist = tmp_path / "backend-dist"
    launchers = {}
    for arch in ("arm64", "x64"):
        tree = dist / f"kirocrew-backend-{arch}"
        (tree / "bin").mkdir(parents=True)
        launcher = tree / "bin" / "kirocrew"
        launcher.write_text("#!/bin/sh\nexec true\n")
        launcher.chmod(0o755)
        (tree / "bin" / "python3.12").write_text(arch)
        (tree / "bin" / "python3.12").chmod(0o755)
        launchers[arch] = launcher
    arm_pkg = dist / "kirocrew-backend-arm64" / "lib" / "python3.12" / "site-packages" / "kiro_crew"
    arm_pkg.mkdir(parents=True)
    (arm_pkg / "__init__.py").write_text("")
    x64_sp = dist / "kirocrew-backend-x64" / "lib" / "python3.12" / "site-packages"
    x64_sp.mkdir(parents=True)
    (x64_sp / "kiro_crew").symlink_to(os.path.relpath(arm_pkg, x64_sp), target_is_directory=True)

    mock_pkg = unittest.mock.MagicMock()
    mock_pkg.__file__ = str(x64_sp / "kiro_crew" / "__init__.py")
    real_works = agent_mod._launcher_works
    monkeypatch.setattr(agent_mod, "_KIROCREW_BIN", None)
    monkeypatch.setattr(
        agent_mod,
        "_launcher_works",
        lambda p: str(p).startswith(str(tmp_path)) and real_works(Path(p)),
    )
    monkeypatch.setattr("sys.exec_prefix", str(dist / "kirocrew-backend-x64"))
    monkeypatch.setattr("shutil.which", lambda c: None)
    with unittest.mock.patch.dict("sys.modules", {"kiro_crew": mock_pkg}):
        resolved = agent_mod._resolve_kirocrew_bin()
    assert resolved == str(launchers["x64"]), resolved


# --- The build script's side of the contract ---------------------------------


def test_build_script_compiles_pycs_under_a_neutral_prefix() -> None:
    """Without ``-d`` every pyc embeds its tree's directory name as co_filename
    and no stdlib or kiro_crew entry can ever be byte-identical across the trees."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(
        r"compileall -q -f -d kirocrew-backend/lib \\\n\s*--invalidation-mode checked-hash", text
    ), "compileall must record the neutral kirocrew-backend/lib prefix"


def test_build_script_shares_the_universal_pair_and_reproves_both_trees() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    step = text[
        text.index("# --- 3e. Share the duplicate half") : text.index(
            "# --- 4. Package the desktop app"
        )
    ]
    assert 'if [ "$UNIVERSAL" = "1" ]; then' in step, "sharing is a universal-only step"
    assert (
        '--backend "$ELECTRON_DIR/backend-dist/kirocrew-backend-arm64" "$ELECTRON_DIR/backend-dist/kirocrew-backend-x64"'
        in step
    ), "arm64 keeps its files; x64 gets the links"
    assert (
        '--kas "$ELECTRON_DIR/backend-dist/kas-arm64" "$ELECTRON_DIR/backend-dist/kas-x64"' in step
    )
    assert (
        step.count("-m kiro_crew --version") == 1 and "import kiro_crew.cli" in step
    ), "both self-containment probes run again after sharing"
    assert "for tree in kirocrew-backend-arm64 kirocrew-backend-x64" in step
    assert (
        "kas_bundle.installed_bundle(root=root, version=KAS_PIN_VERSION)" in step
    ), "the x64 KAS root is re-read through the gateway's reader"
    assert "from kiro_crew.agent import _resolve_kirocrew_bin" in step and (
        "x64 interpreter resolved the arm64 launcher" in step
    ), "the x64 interpreter must never resolve the arm64 tree's launcher"


def test_both_zip_ceilings_sit_below_the_installed_updaters_cliff() -> None:
    """1024 MiB is the crash; both gates hold the same 1000 MiB ceiling, so the
    build fails before signing and the publish step fails on the shipped bytes."""
    build = SCRIPT.read_text(encoding="utf-8")
    assert "MAC_ZIP_MAX_MIB=1000\n" in build, "the ceiling is a constant; nothing may raise it"
    assert '[ "$MAC_ZIP_MIB" -ge "$MAC_ZIP_MAX_MIB" ]' in build
    assert "find \"$ELECTRON_DIR/dist\" -maxdepth 1 -name '*-mac.zip'" in build
    # An archiver that dereferences the links ships the x64 half twice again
    # without any error, so the link shape is asserted before the size.
    assert "grep -c '^l.*backend-dist/kirocrew-backend-x64/'" in build
    assert build.index("MAC_ZIP_LINKS") < build.index('MAC_ZIP_BYTES="$(stat -f%z')

    sign = SIGN_WORKFLOW.read_text(encoding="utf-8")
    assert 'ZIP_MIB=$(( $(stat -f%z "../${NOTARIZED_ZIP}") / 1048576 ))' in sign
    assert 'if [ "$ZIP_MIB" -ge 1000 ]; then' in sign
    # The check reads the zip ditto just produced, before any upload step.
    assert (
        sign.index("ditto -c -k --keepParent")
        < sign.index('if [ "$ZIP_MIB" -ge 1000 ]')
        < sign.index("Upload notarized artifact to S3")
    )
