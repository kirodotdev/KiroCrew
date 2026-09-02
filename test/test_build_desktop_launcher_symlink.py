"""Regression guard for the bundled ``kirocrew`` launcher's symlink resolution.

Background
----------
``packaging/build-desktop.sh`` writes a small bash launcher into every backend
bundle at ``<bundle>/bin/kirocrew``.  It derives its own directory in order to
exec the interpreter sitting next to it (``$DIR/python3.12``).  The naive form::

    DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is wrong whenever the launcher is reached through a symlink, because
``${BASH_SOURCE[0]}`` is the *symlink* path, not its target.  The gateway plants
exactly such a symlink at ``~/.local/bin/kirocrew`` on every start
(``agent.ensure_kirocrew_on_path``), so on a packaged install ``$DIR`` became
``~/.local/bin`` and the launcher exec'd a non-existent
``~/.local/bin/python3.12`` — every ``kirocrew ...`` invocation from a shell
failed (issue #845).  The fix (#188) walks the symlink chain first.

Why this test exists
--------------------
The shipped fix had no test.  ``resolver_gate`` in the same script only checks
that ``find-bin.js`` and the builder agree on the launcher's *path*; nothing ever
invoked the launcher *through a symlink*, which is the failing mode users hit.
So a revert to the naive one-liner would keep the whole suite green and break the
CLI again on the next desktop build.

These tests extract the launcher from the shipped script (not a copy, so a revert
is what runs here), drop it into a fake bundle next to a stub ``python3.12`` that
reports the directory it was exec'd from, and invoke it through the symlink
shapes a real install produces: a direct call, an absolute symlink from another
directory, a relative symlink, and a chain of symlinks.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the POSIX launcher is a bash script; Windows ships kirocrew.cmd instead",
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract_launcher() -> str:
    """Pull the launcher heredoc body out of the shipped script.

    Extraction rather than a hard-coded copy: editing or reverting the real
    launcher is what these tests then exercise.
    """
    text = SCRIPT.read_text()
    m = re.search(
        r"cat > \"\$out/bin/kirocrew\" <<'LAUNCH'\n(.*?)\nLAUNCH\n",
        text,
        re.DOTALL,
    )
    assert m, "launcher heredoc not found in packaging/build-desktop.sh"
    return m.group(1)


def _make_bundle(root: Path) -> Path:
    """Create a fake backend bundle: the real launcher + a stub interpreter.

    The stub stands in for the bundled CPython.  It prints the directory it was
    exec'd from, which is precisely the value the launcher computed for ``$DIR``,
    plus the arguments it received — so a test can assert both the resolution and
    the argument forwarding without a 200 MB interpreter.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)

    launcher = bin_dir / "kirocrew"
    launcher.write_text(_extract_launcher() + "\n")
    launcher.chmod(0o755)

    stub = bin_dir / "python3.12"
    stub.write_text(
        "#!/bin/bash\n"
        # $0 is the path the launcher exec'd, i.e. "$DIR/python3.12".
        'echo "DIR=$(cd "$(dirname "$0")" && pwd)"\n'
        'echo "ARGS=$*"\n'
    )
    stub.chmod(0o755)
    return launcher


def _invoke(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(path), *args],
        capture_output=True,
        text=True,
    )


def test_resolves_when_invoked_directly(tmp_path):
    """Baseline: called by its real path, the launcher finds its sibling."""
    bundle = tmp_path / "kirocrew-backend-arm64"
    launcher = _make_bundle(bundle)

    proc = _invoke(launcher)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_resolves_through_absolute_symlink(tmp_path):
    """The issue #845 case: reached via the PATH symlink the gateway plants.

    This is the assertion that goes red on a revert to the naive
    ``dirname "${BASH_SOURCE[0]}"`` form, which would resolve ``$DIR`` to the
    symlink's own directory and exec a python3.12 that does not exist there.
    """
    bundle = tmp_path / "app" / "backend-dist" / "kirocrew-backend-arm64"
    launcher = _make_bundle(bundle)

    # Mirrors ~/.local/bin/kirocrew -> .../backend-dist/.../bin/kirocrew.
    local_bin = tmp_path / "home" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    shim = local_bin / "kirocrew"
    shim.symlink_to(launcher)

    # Guard the premise: no interpreter next to the symlink, so a launcher that
    # resolves $DIR to the symlink's directory cannot accidentally pass.
    assert not (local_bin / "python3.12").exists()

    proc = _invoke(shim)

    assert proc.returncode == 0, (
        f"launcher failed when invoked through a PATH symlink (issue #845): {proc.stderr}"
    )
    assert f"DIR={bundle / 'bin'}" in proc.stdout, (
        "launcher must resolve $DIR to the real bundle bin dir, not the symlink's dir; "
        f"got: {proc.stdout}"
    )


def test_resolves_through_relative_symlink(tmp_path):
    """A symlink whose target is *relative* must be joined against the link's dir.

    ``ln -s`` records exactly what it is given, so a relative target is a real
    shape on disk.  This covers the launcher's
    ``[ "${SOURCE:0:1}" != "/" ] && SOURCE="$DIR/$SOURCE"`` branch, which is what
    keeps the chain walk from producing a bare, unresolvable filename.
    """
    bundle = tmp_path / "bundle"
    _make_bundle(bundle)

    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "kirocrew"
    # Relative to shim_dir: ../bundle/bin/kirocrew
    shim.symlink_to(Path("..") / "bundle" / "bin" / "kirocrew")

    proc = _invoke(shim)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_resolves_through_symlink_chain(tmp_path):
    """Multi-hop chains must be walked to the end, not just one level.

    Two hops occur in practice — e.g. a package manager's bin shim pointing at
    ``~/.local/bin/kirocrew``, which points at the bundle — so a single
    ``readlink`` (rather than the ``while`` loop) is not enough.
    """
    bundle = tmp_path / "bundle"
    launcher = _make_bundle(bundle)

    hop1_dir = tmp_path / "hop1"
    hop1_dir.mkdir()
    hop1 = hop1_dir / "kirocrew"
    hop1.symlink_to(launcher)

    hop2_dir = tmp_path / "hop2"
    hop2_dir.mkdir()
    hop2 = hop2_dir / "kirocrew"
    hop2.symlink_to(hop1)

    proc = _invoke(hop2)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_forwards_arguments_through_symlink(tmp_path):
    """Resolution is worthless if the argv is mangled on the way through.

    Asserts the module invocation contract (``-s -m kiro_crew``) and that a
    quoted argument containing a space survives as ONE argument.
    """
    bundle = tmp_path / "bundle"
    launcher = _make_bundle(bundle)
    shim = tmp_path / "kirocrew"
    shim.symlink_to(launcher)

    proc = _invoke(shim, "config", "set", "a b")

    assert proc.returncode == 0, proc.stderr
    assert "ARGS=-s -m kiro_crew config set a b" in proc.stdout, proc.stdout

    # And the word-splitting guard: "$@" (not $@) keeps "a b" a single argv entry.
    stub = bundle / "bin" / "python3.12"
    stub.write_text('#!/bin/bash\nprintf "COUNT=%s\\n" "$#"\n')
    stub.chmod(0o755)
    proc = _invoke(shim, "config", "set", "a b")
    assert proc.returncode == 0, proc.stderr
    # -s, -m, kiro_crew, config, set, "a b" == 6
    assert "COUNT=6" in proc.stdout, proc.stdout


def test_launcher_keeps_the_symlink_walk(tmp_path):
    """Wiring guard: the shipped launcher must still contain the chain walk.

    The behavioural tests above already fail on a revert, but this asserts the
    mechanism directly and forbids the exact naive one-liner the bug shipped, so
    the reason for the failure is unambiguous when someone edits the heredoc.
    """
    launcher = _extract_launcher()

    assert 'while [ -h "$SOURCE" ]' in launcher, (
        "launcher lost its symlink-chain walk (issue #845 regression)"
    )
    assert "readlink" in launcher, "launcher must readlink its way to the real path"
    # `cd -P` (physical) is what makes the final dirname immune to a symlinked
    # parent directory; `cd` alone would keep the logical path.
    assert 'cd -P "$(dirname "$SOURCE")"' in launcher, (
        "final DIR must be computed with `cd -P` so a symlinked parent resolves"
    )
    assert 'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' not in launcher, (
        "launcher reverted to the naive BASH_SOURCE form that broke issue #845"
    )
