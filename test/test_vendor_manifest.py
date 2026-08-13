"""Content-integrity guards for the vendored ``_vendor`` tree.

Everything under ``src/kiro_crew/_vendor`` is excluded from source-level
content review — semgrep, the AI reviewers' reviewable diff, and the
lint/format configs all skip it — so a modified vendored ``.py``, a swapped
native library, or an added rogue file would pass every review gate unnoticed.
``scripts/verify_vendor_manifest.py`` closes that gap: a committed sha256
manifest (``scripts/vendor_manifest.sha256``, deliberately OUTSIDE the
excluded tree) pins every file's content, and the ``vendor-manifest`` CI job
fails any PR whose tree diverges from it.

These tests exercise the script's LOGIC against small temp-dir fixture trees:
detection of each divergence class (modified / missing / unexpected),
``--write`` → ``--check`` round-trips, deterministic output, and manifest
parsing strictness. They never hash the real ~26MB vendored tree — that is
the CI job's role, and ``test_ci_wires_the_manifest_gate`` pins that the job
and the committed manifest actually exist.

This is a separate concern from ``test_vendored_llama_payload.py``, which
guards artifact COMPLETENESS (the packaging lanes shipping every declared
native lib); this file guards source-tree CONTENT (integrity against the
reviewed manifest).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "verify_vendor_manifest.py"

# Load the script as a module so the tests exercise the exact code CI runs
# (scripts/ is not a package, so a plain import cannot reach it).
_spec = importlib.util.spec_from_file_location("verify_vendor_manifest", _SCRIPT)
assert _spec is not None and _spec.loader is not None
vvm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vvm)


def _make_tree(root: Path, files: dict[str, bytes]) -> Path:
    """Create a fixture vendored tree under ``root/vendor``."""
    vendor = root / "vendor"
    for rel, content in files.items():
        path = vendor / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    vendor.mkdir(exist_ok=True)
    return vendor


_FIXTURE = {
    "llama_cpp/__init__.py": b"__version__ = '0.3.34'\n",
    "llama_cpp/llama.py": b"class Llama: ...\n",
    "llama_cpp_libs/linux_x86_64/libllama.so": b"\x7fELF-original",
    "README.md": b"# vendored\n",
}


def _run(argv: list[str]) -> int:
    return vvm.main(["verify_vendor_manifest.py", *argv])


def _args(vendor: Path, manifest: Path, *mode: str) -> list[str]:
    return [*mode, "--vendor-dir", str(vendor), "--manifest", str(manifest)]


class TestRoundTrip:
    def test_write_then_check_is_green(self, tmp_path: Path, capsys) -> None:
        """The documented vendored-bump path: ``--write`` → ``--check`` passes."""
        vendor = _make_tree(tmp_path, _FIXTURE)
        manifest = tmp_path / "manifest.sha256"

        assert _run(_args(vendor, manifest, "--write")) == 0
        assert _run(_args(vendor, manifest)) == 0
        assert "matches the manifest (4 files)" in capsys.readouterr().out

    def test_manifest_is_deterministic_and_sha256sum_compatible(self, tmp_path: Path) -> None:
        """Two writes over the same tree are byte-identical; lines are sorted
        ``<64-hex>␣␣src/kiro_crew/_vendor/<relpath>`` with a trailing newline —
        the exact shape ``sha256sum -c`` accepts, keeping the manifest
        independently verifiable outside this script.
        """
        vendor = _make_tree(tmp_path, _FIXTURE)
        m1, m2 = tmp_path / "m1", tmp_path / "m2"
        assert _run(_args(vendor, m1, "--write")) == 0
        assert _run(_args(vendor, m2, "--write")) == 0

        text = m1.read_text(encoding="utf-8")
        assert text == m2.read_text(encoding="utf-8")
        assert text.endswith("\n") and not text.endswith("\n\n")
        lines = text.splitlines()
        assert lines == sorted(lines, key=lambda ln: ln.split("  ", 1)[1])
        for line in lines:
            digest, sep, name = line.partition("  ")
            assert sep and len(digest) == 64 and set(digest) <= set("0123456789abcdef")
            assert name.startswith("src/kiro_crew/_vendor/")


class TestDivergenceDetection:
    """Each divergence class must fail ``--check`` and be named in the report."""

    @pytest.fixture()
    def green_baseline(self, tmp_path: Path) -> tuple[Path, Path]:
        vendor = _make_tree(tmp_path, _FIXTURE)
        manifest = tmp_path / "manifest.sha256"
        assert _run(_args(vendor, manifest, "--write")) == 0
        return vendor, manifest

    def test_detects_a_modified_file(self, green_baseline, capsys) -> None:
        """A swapped native lib — same path, different bytes — must fail."""
        vendor, manifest = green_baseline
        (vendor / "llama_cpp_libs/linux_x86_64/libllama.so").write_bytes(b"\x7fELF-swapped")

        assert _run(_args(vendor, manifest)) == 1
        err = capsys.readouterr().err
        assert "MODIFIED: src/kiro_crew/_vendor/llama_cpp_libs/linux_x86_64/libllama.so" in err

    def test_detects_a_missing_file(self, green_baseline, capsys) -> None:
        vendor, manifest = green_baseline
        (vendor / "llama_cpp/llama.py").unlink()

        assert _run(_args(vendor, manifest)) == 1
        assert "MISSING: src/kiro_crew/_vendor/llama_cpp/llama.py" in capsys.readouterr().err

    def test_detects_an_unexpected_extra_file(self, green_baseline, capsys) -> None:
        """An ADDED file must fail too: a rogue ``.py`` dropped into the
        vendored ``sys.path`` root is importable and as dangerous as a
        modified one, and no hash-of-known-files scheme would see it.
        """
        vendor, manifest = green_baseline
        (vendor / "llama_cpp/sitecustomize.py").write_bytes(b"import os\n")

        assert _run(_args(vendor, manifest)) == 1
        err = capsys.readouterr().err
        assert "UNEXPECTED: src/kiro_crew/_vendor/llama_cpp/sitecustomize.py" in err

    def test_reports_every_class_in_one_run(self, green_baseline, capsys) -> None:
        """One run names ALL divergences, not just the first — a reviewer of a
        failing CI log needs the complete list to judge the change.
        """
        vendor, manifest = green_baseline
        (vendor / "README.md").write_bytes(b"# tampered\n")
        (vendor / "llama_cpp/llama.py").unlink()
        (vendor / "extra.bin").write_bytes(b"\x00")

        assert _run(_args(vendor, manifest)) == 1
        err = capsys.readouterr().err
        assert "MODIFIED: src/kiro_crew/_vendor/README.md" in err
        assert "MISSING: src/kiro_crew/_vendor/llama_cpp/llama.py" in err
        assert "UNEXPECTED: src/kiro_crew/_vendor/extra.bin" in err

    def test_failure_names_the_regeneration_procedure(self, green_baseline, capsys) -> None:
        """The failure message must point at the documented ``--write`` path so
        a legitimate vendored bump is unblocked by the CI log alone.
        """
        vendor, manifest = green_baseline
        (vendor / "README.md").write_bytes(b"# bumped\n")

        assert _run(_args(vendor, manifest)) == 1
        assert "--write" in capsys.readouterr().err

    def test_a_symlink_is_refused_not_skipped(self, green_baseline, capsys) -> None:
        """A symlink must FAIL the check even when its target does not exist.

        ``is_file()`` is False for a broken symlink, so a skip-silently walk
        would pass a PR that adds ``libllama.so -> /usr/lib/...`` on the Linux
        runner — while the link resolves on the OS it targets. The gate cannot
        attest content it did not hash, so any symlink is a violation.
        """
        vendor, manifest = green_baseline
        (vendor / "llama_cpp_libs/linux_x86_64/evil.so").symlink_to("/nonexistent/target")

        assert _run(_args(vendor, manifest)) == 1
        err = capsys.readouterr().err
        assert "SYMLINK: src/kiro_crew/_vendor/llama_cpp_libs/linux_x86_64/evil.so" in err

    def test_write_refuses_a_symlinked_tree_too(self, green_baseline, capsys) -> None:
        """``--write`` over a tree with a symlink must refuse, not regenerate —
        regenerating would bake the un-hashed blind spot into the manifest.
        """
        vendor, manifest = green_baseline
        before = manifest.read_bytes()
        (vendor / "llama_cpp/link.py").symlink_to("/etc/hostname")

        assert _run(_args(vendor, manifest, "--write")) == 1
        assert manifest.read_bytes() == before, "--write mutated the manifest despite refusing"

    def test_pycache_is_refused_never_silently_skipped(self, green_baseline, capsys) -> None:
        """``__pycache__`` entries must FAIL both modes, not be skipped: a
        committed hash-based ``.pyc`` (PEP 552 unchecked variant) executes on
        import in place of its source file without validation, so a skipped
        cache directory would be a full bypass of the gate. Refusal also keeps
        machine-local caches out of a regenerated manifest — ``--write`` must
        not bake them in OR quietly proceed past them.
        """
        vendor, manifest = green_baseline
        before = manifest.read_bytes()
        cache = vendor / "llama_cpp/__pycache__"
        cache.mkdir()
        (cache / "llama.cpython-312.pyc").write_bytes(b"\x00compiled")

        assert _run(_args(vendor, manifest)) == 1
        err = capsys.readouterr().err
        assert "PYCACHE: src/kiro_crew/_vendor/llama_cpp/__pycache__/llama.cpython-312.pyc" in err
        assert "rm -rf" in err, "refusal must include the local-cache deletion hint"

        assert _run(_args(vendor, manifest, "--write")) == 1
        assert manifest.read_bytes() == before, "--write mutated the manifest despite refusing"
        assert "__pycache__" not in manifest.read_text(encoding="utf-8")


class TestErrorHandling:
    def test_absent_manifest_is_a_distinct_error(self, tmp_path: Path, capsys) -> None:
        """No manifest is exit 2 (setup error), never a silent pass."""
        vendor = _make_tree(tmp_path, _FIXTURE)

        assert _run(_args(vendor, tmp_path / "nope.sha256")) == 2
        assert "--write" in capsys.readouterr().err

    def test_absent_vendor_dir_is_a_distinct_error(self, tmp_path: Path) -> None:
        assert _run(_args(tmp_path / "nope", tmp_path / "m.sha256")) == 2

    def test_malformed_manifest_fails_loudly(self, tmp_path: Path, capsys) -> None:
        """A corrupted manifest line must fail the run, not silently narrow
        what is checked to the lines that still parse.
        """
        vendor = _make_tree(tmp_path, _FIXTURE)
        manifest = tmp_path / "manifest.sha256"
        manifest.write_text("not-a-digest  some/path\n", encoding="utf-8")

        assert _run(_args(vendor, manifest)) == 2
        assert "malformed manifest line 1" in capsys.readouterr().err


def test_committed_manifest_exists_and_parses() -> None:
    """The bootstrap manifest must be committed and well-formed; the CI job
    hashes the real tree against it, so an absent or corrupt manifest would
    turn the gate red on every PR.
    """
    manifest = _REPO_ROOT / "scripts" / "vendor_manifest.sha256"
    assert manifest.is_file(), "scripts/vendor_manifest.sha256 is not committed"
    entries = vvm.parse_manifest(manifest.read_text(encoding="utf-8"))
    assert entries, "committed manifest is empty"


def test_ci_wires_the_manifest_gate() -> None:
    """ci.yml must run the verifier unconditionally — the vendored tree gets
    no other content review, so a gate that exists but is not wired (or is
    hidden behind a path filter's surface outputs) guards nothing.
    """
    text = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/verify_vendor_manifest.py" in text, "ci.yml does not run the manifest gate"
