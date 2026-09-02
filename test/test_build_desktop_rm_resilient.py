"""Regression guard for the macOS ``.DS_Store``/``ENOTEMPTY`` retry helper in
``packaging/build-desktop.sh``.

Background
----------
``make desktop`` clears its build-output dirs with ``rm -rf`` before (re)staging
them.  On macOS, Desktop Services can drop a fresh ``.DS_Store`` into a subdir
*between* ``rm``'s child-sweep and its final ``rmdir``, so a plain ``rm -rf``
aborts with ``ENOTEMPTY`` (``-f`` suppresses ``ENOENT``, not ``ENOTEMPTY``;
electron-builder#6890) and the whole build fails.  ``rm_rf_resilient`` defeats
that race by sweeping ``.DS_Store`` and retrying until the tree is actually gone.

Why this test exists
--------------------
The *race* is timing-dependent and can't be reproduced deterministically, but
the *retry mechanism* can: we shadow ``rm`` so the first removal is a no-op
(the tree survives, exactly as the race leaves it) and assert the helper loops
and succeeds on a later attempt.  If the retry loop is ever reverted to a single
``rm``, ``test_retries_after_transient_failure`` goes red instead of the desktop
build silently breaking while CI stays green.

The helper body is extracted from the shipped script (not copied) so the test
tracks the real source of truth.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="rm_rf_resilient is a POSIX bash helper (find/rm)"
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract_helper() -> str:
    """Pull the ``rm_rf_resilient`` function body out of the shipped script.

    Extraction (rather than a hard-coded copy) means a revert or edit of the
    real helper is what this test runs against.
    """
    text = SCRIPT.read_text()
    m = re.search(r"^rm_rf_resilient\(\) \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    assert m, "rm_rf_resilient() not found in packaging/build-desktop.sh"
    return m.group(0)


def _run(harness: str) -> subprocess.CompletedProcess:
    """Run a bash snippet that has the extracted helper sourced above it."""
    script = "set -euo pipefail\n" + _extract_helper() + "\n" + harness
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


def test_preclean_sites_route_through_helper():
    """Wiring guard: the two build-output pre-cleans MUST call ``rm_rf_resilient``.

    ``test_retries_after_transient_failure`` proves the helper *works*, but the
    whole point of this fix is that the pre-clean removals — which previously used
    a bare ``rm -rf`` — now go through it. Without this assertion, reverting either
    call site back to ``rm -rf`` would leave every behavioral test green while the
    ``.DS_Store`` race again aborts desktop builds *before* electron-builder runs.
    So assert the wiring directly, at both sites.
    """
    text = SCRIPT.read_text()
    assert "rm_rf_resilient() {" in text, "helper definition missing"

    # Section 3 backend-dist pre-clean.
    assert re.search(r'(?m)^\s*rm_rf_resilient\s+"\$ELECTRON_DIR/backend-dist"\s*$', text), (
        "backend-dist pre-clean must call rm_rf_resilient (not bare rm -rf)"
    )
    # Section 4 electron dist pre-clean, just before the electron-builder retry loop.
    assert re.search(r"(?m)^\s*rm_rf_resilient\s+dist\s*$", text), (
        "electron dist pre-clean must call rm_rf_resilient (not bare rm -rf)"
    )
    # And the reverted forms must NOT be present at those pre-clean sites. (A bare
    # `rm -rf dist` still legitimately exists for the deep site-packages staging
    # and `rm -rf dist/*-temp` inside the retry loop, so we forbid only the exact
    # backend-dist revert, which is unambiguous.)
    assert not re.search(r'(?m)^\s*rm -rf\s+"\$ELECTRON_DIR/backend-dist"\s*$', text), (
        "backend-dist pre-clean reverted to bare rm -rf"
    )


def test_retries_after_transient_failure(tmp_path):
    """The core race guard: a first removal that leaves the tree in place (as the
    ``.DS_Store`` race does) must be retried until the tree is actually gone."""
    target = tmp_path / "dist"
    (target / "mac-universal").mkdir(parents=True)
    (target / "mac-universal" / ".DS_Store").write_text("x")
    counter = tmp_path / "rm_calls"
    counter.write_text("")

    # Shadow `rm` in the helper's shell: the FIRST call is a no-op (the tree
    # survives -> simulates the race re-creating a file mid-removal); every
    # later call delegates to the real /bin/rm. A functioning retry loop makes
    # a second call and wins.
    harness = f"""
rm() {{
  printf 'x' >> "{counter}"
  if [ "$(wc -c < "{counter}" | tr -d ' ')" = "1" ]; then
    return 0            # first attempt: pretend success but remove nothing
  fi
  command /bin/rm "$@"  # subsequent attempts: real removal
}}
rm_rf_resilient "{target}"
echo "EXIT=$?"
"""
    proc = _run(harness)
    assert proc.returncode == 0, proc.stderr
    assert "EXIT=0" in proc.stdout, proc.stdout
    assert not target.exists(), "tree should be removed after retry"
    assert len(counter.read_text()) >= 2, "helper must have retried (>=2 rm calls)"


def test_noop_on_absent_path(tmp_path):
    """An already-absent path is a clean no-op (no error under set -e)."""
    proc = _run(f'rm_rf_resilient "{tmp_path / "does-not-exist"}"; echo "EXIT=$?"')
    assert proc.returncode == 0, proc.stderr
    assert "EXIT=0" in proc.stdout


def test_removes_tree_with_ds_store(tmp_path):
    """A tree containing .DS_Store is removed."""
    target = tmp_path / "backend-dist"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / ".DS_Store").write_text("x")
    (target / "file").write_text("y")
    proc = _run(f'rm_rf_resilient "{target}"')
    assert proc.returncode == 0, proc.stderr
    assert not target.exists()


def test_removes_dangling_symlink(tmp_path):
    """A dangling symlink is "not -e" but MUST still be removed (the -L guard);
    leaving it would make the following build step operate on a broken link."""
    link = tmp_path / "dist"
    link.symlink_to(tmp_path / "nonexistent-target")
    assert link.is_symlink() and not link.exists()  # dangling
    proc = _run(f'rm_rf_resilient "{link}"')
    assert proc.returncode == 0, proc.stderr
    assert not os.path.lexists(link), "dangling symlink should be removed"
