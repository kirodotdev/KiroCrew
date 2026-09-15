#!/usr/bin/env python3
"""Verify the vendored ``_vendor`` tree against a committed sha256 manifest.

``src/kiro_crew/_vendor`` (vendored llama-cpp-python: executable Python plus
per-platform native libraries) is deliberately excluded from every
source-level content review: semgrep, the AI reviewers' reviewable diff, and
the formatter/linter configs all skip it. Without a content gate, a PR that
modifies vendored Python source, swaps a native library, or drops a rogue
``.py`` into the vendored ``sys.path`` root passes every review unnoticed.

This script closes that gap with a checksum manifest committed OUTSIDE the
vendored tree (``scripts/vendor_manifest.sha256``), where a change to it IS
reviewed. CI runs the default ``--check`` mode on every PR: it recomputes the
SHA-256 of every file under ``_vendor`` and fails listing each MODIFIED,
MISSING, and UNEXPECTED (extra) file — all three classes matter, because an
added importable file is as dangerous as a modified one.

The manifest uses ``sha256sum``-compatible lines
(``<hex>  src/kiro_crew/_vendor/<relpath>``), sorted with a trailing newline,
so it is plain-text line-diffable in review and independently verifiable with
``sha256sum -c scripts/vendor_manifest.sha256`` from the repository root.

Legitimate vendored bumps regenerate it with ``--write`` (see the "Updating
the vendored tree" section of ``src/kiro_crew/_vendor/README.md``) and commit
the manifest diff alongside the vendored changes.

This is a different concern from ``scripts/verify_vendored_payload.py``, which
checks that built wheel/sdist artifacts CONTAIN the declared native libs
(artifact completeness); this script checks that the source tree's CONTENT is
the reviewed content (integrity).

Usage: ``python scripts/verify_vendor_manifest.py [--check | --write]``
(default ``--check``). ``--check`` exits non-zero on any mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEFAULT_VENDOR_DIR = _REPO_ROOT / "src" / "kiro_crew" / "_vendor"
_DEFAULT_MANIFEST = _REPO_ROOT / "scripts" / "vendor_manifest.sha256"

# Manifest paths are repo-root-relative so `sha256sum -c` works from the root.
_MANIFEST_PATH_PREFIX = "src/kiro_crew/_vendor/"

# One read buffer per chunk keeps peak memory flat while hashing the ~26MB of
# native libraries.
_CHUNK_BYTES = 1 << 20


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_tree_hashes(
    vendor_dir: pathlib.Path,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return ``({posix-relpath: sha256}, [symlinks], [cache files])``.

    The walk is deterministic (sorted POSIX-relative paths) so two runs over
    the same tree always produce byte-identical manifests.

    Symlinks are returned as violations rather than hashed or skipped: a
    broken symlink is invisible to ``is_file()`` on the CI runner but can
    resolve on another OS (e.g. a ``.so`` pointing at a macOS system library),
    so silently ignoring one would let a PR add a link the gate never sees.

    ``__pycache__`` entries are ALSO violations, never silently skipped: a
    committed hash-based ``.pyc`` (PEP 552, unchecked variant) executes on
    import in place of its matching source file WITHOUT source validation, so
    a skipped cache directory would be an attacker-grade bypass of the whole
    gate. They are not hashed into the manifest either — locally generated
    caches are machine-specific, so ``--write`` would bake state into the
    manifest that fails on CI's clean checkout. Refusal keeps both properties:
    the tree the manifest attests carries no bytecode, and local caches are
    surfaced with a deletion hint instead of poisoning a regeneration.
    """
    hashes: dict[str, str] = {}
    symlinks: list[str] = []
    caches: list[str] = []
    if vendor_dir.is_symlink():
        return hashes, ["."], caches
    for path in sorted(vendor_dir.rglob("*")):
        rel = path.relative_to(vendor_dir).as_posix()
        if path.is_symlink():
            symlinks.append(rel)
        elif "__pycache__" in path.parts:
            if path.is_file():
                caches.append(rel)
        elif path.is_file():
            hashes[rel] = _sha256_file(path)
    return hashes, symlinks, caches


def render_manifest(hashes: dict[str, str]) -> str:
    """Render hashes as sorted ``sha256sum``-compatible lines (two spaces)."""
    lines = [
        f"{digest}  {_MANIFEST_PATH_PREFIX}{relpath}" for relpath, digest in sorted(hashes.items())
    ]
    return "\n".join(lines) + "\n"


def parse_manifest(text: str) -> dict[str, str]:
    """Parse manifest text back into ``{posix-relpath: sha256}``.

    Accepts exactly the format ``render_manifest`` emits; any malformed line
    raises ``ValueError`` so a corrupted manifest fails loudly instead of
    silently narrowing what is checked.
    """
    hashes: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64 or not name.startswith(_MANIFEST_PATH_PREFIX):
            raise ValueError(f"malformed manifest line {lineno}: {line!r}")
        hashes[name[len(_MANIFEST_PATH_PREFIX) :]] = digest
    return hashes


def diff_tree_against_manifest(
    actual: dict[str, str], expected: dict[str, str]
) -> dict[str, list[str]]:
    """Compare tree hashes against the manifest.

    Returns ``{"modified": [...], "missing": [...], "unexpected": [...]}``
    with sorted relpaths; all lists empty means the tree matches. UNEXPECTED
    (extra) files are a failure class of their own: a rogue ``.py`` added to
    the vendored ``sys.path`` root is as dangerous as a modified one.
    """
    return {
        "modified": sorted(
            name for name, digest in expected.items() if name in actual and actual[name] != digest
        ),
        "missing": sorted(name for name in expected if name not in actual),
        "unexpected": sorted(name for name in actual if name not in expected),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="verify the tree against the manifest (default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="regenerate the manifest from the current tree (vendored bumps)",
    )
    parser.add_argument(
        "--vendor-dir",
        type=pathlib.Path,
        default=_DEFAULT_VENDOR_DIR,
        help=argparse.SUPPRESS,  # test/dev override; production uses the default
    )
    parser.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=_DEFAULT_MANIFEST,
        help=argparse.SUPPRESS,  # test/dev override; production uses the default
    )
    args = parser.parse_args(argv[1:])

    if not args.vendor_dir.is_dir():
        print(f"vendored tree not found: {args.vendor_dir}", file=sys.stderr)
        return 2

    actual, symlinks, caches = compute_tree_hashes(args.vendor_dir)
    if symlinks or caches:
        # Both classes are refused in BOTH modes: --check because the gate
        # cannot attest content it did not hash (and a committed unchecked
        # .pyc EXECUTES on import in place of its source), and --write
        # because regenerating over them would bake the blind spot — or
        # machine-local cache state — into the manifest itself.
        print("vendored tree contains entries the gate refuses:", file=sys.stderr)
        for name in symlinks:
            print(f"  SYMLINK: {_MANIFEST_PATH_PREFIX}{name}", file=sys.stderr)
        for name in caches:
            print(f"  PYCACHE: {_MANIFEST_PATH_PREFIX}{name}", file=sys.stderr)
        if caches:
            print(
                "\nLocally generated __pycache__ dirs (an imported vendored tree) can be\n"
                "removed with: find src/kiro_crew/_vendor -name __pycache__ -type d "
                "-exec rm -rf {} +\nCommitted bytecode must never land in _vendor.",
                file=sys.stderr,
            )
        return 1

    if args.write:
        args.manifest.write_text(render_manifest(actual), encoding="utf-8")
        print(f"wrote {len(actual)} entries to {args.manifest}")
        return 0

    if not args.manifest.is_file():
        print(
            f"manifest not found: {args.manifest}\n"
            "bootstrap it with: python scripts/verify_vendor_manifest.py --write",
            file=sys.stderr,
        )
        return 2

    try:
        expected = parse_manifest(args.manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"unreadable manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2

    problems = diff_tree_against_manifest(actual, expected)
    if any(problems.values()):
        print("vendored tree does not match scripts/vendor_manifest.sha256:", file=sys.stderr)
        for kind in ("modified", "missing", "unexpected"):
            for name in problems[kind]:
                print(f"  {kind.upper()}: {_MANIFEST_PATH_PREFIX}{name}", file=sys.stderr)
        print(
            "\nIf this vendored change is intentional, regenerate the manifest\n"
            "(python scripts/verify_vendor_manifest.py --write) and commit it —\n"
            "see 'Updating the vendored tree' in src/kiro_crew/_vendor/README.md.",
            file=sys.stderr,
        )
        return 1

    print(f"vendored tree matches the manifest ({len(actual)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
