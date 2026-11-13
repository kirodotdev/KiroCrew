#!/usr/bin/env python3
"""Verify the public build ships no internal host.

Scans the BUILT artifacts, not the source tree: every member of every wheel in
the dist directory, and every file of the website ``dist/``. The source tree
may name internal hosts in test fixtures and comments; what matters is whether
one reaches a file a user installs or a page the dashboard serves.

The denylist is stored as sha256 digests only, so this public file names no
internal host. Every dotted host-like token in a file is lowercased and hashed,
together with each of its dot-suffixes (``a.b.c`` -> ``a.b.c``, ``b.c``, ``c``),
so a digest of a domain also covers every host under it. Undotted tokens are
hashed as written and compared with the name digests. A hit prints the file,
the line and the matched digest, never the token.

A ``ks.mjs`` or ``kiro-space-mcp.mjs`` also fails unless a ``SOURCE.json``
beside it exists and names no internal build (a host digest, or a token that
starts with one of the build-marker digests' words).

The sdist is not scanned: it is the source tree, fixtures included, and it is
what the wheel is built from, so the wheel is the artifact that decides.

Usage: ``python scripts/verify_no_internal_host.py [dist-dir] [website-dist-dir]``
(defaults ``dist`` and ``website/dist``). Exits 1 listing every hit, 2 when there
is nothing to scan.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import zipfile

#: Internal hosts and domains (a domain covers every host under it), lowercase.
HOST_DIGESTS = frozenset(
    {
        "942b2ec306b17957484bcc4136985f46b560ae90cb6d4910ac582f26e65c59e0",
        "accf5d46ee3936b66555503d0548a9fcb63901f5eeb53547c7dc59cf2a31caac",
        "578b135c970700784f941f50b3b7f3281fff79d38016a553c12126ce33ae1c5f",
        "ed73cb5c8e0e77e93095d5ad55b7b9e532d0ae38f5ff1006978d56a26adcf41b",
        "71aebe44d1690bab5beb249abdd432c098fd91aff8d6099b9d8896cb9c264063",
        "35793e066e341bfc2265c26fab0b0ba4dd06a4c03a2f30107fc3954bb9c3c742",
        "acc0e211a3e3d504b51e3d0e0dd24d597472cfbb2f5e9897483154589826f4d4",
    }
)
#: Internal package names, matched as written.
NAME_DIGESTS = frozenset({"75d135ac1c385abaaf23c1ce44d8d2f50442fc1356bb9822ec4dfcc63edcc060"})
#: Words (lowercase) that mark an internal build when a SOURCE.json token starts with one.
BUILD_MARKER_DIGESTS = frozenset(
    {
        "498b5212472b1d96ef54660e60c4f172e40a9c965866642ece58c374f77dd45c",
        "5c8f52ca28966103ff0aad98160bc8e978c9ca0285a2043a521481d11ed17506",
        "1a1160b80eb1717de3bd81807a2f339ce0a6f82fbce61f1ef1e4f435f4ee46d7",
    }
)
KS_BUNDLES = ("ks.mjs", "kiro-space-mcp.mjs")
#: Longest SOURCE.json token prefix compared with the build markers.
_MARKER_PREFIX_MAX = 32

#: Wheel members whose job is to DETECT internal hosts, so they spell the
#: domains they look for. Each maps to (why, the digests it may contain); any
#: other denied host in the same file is still reported.
EXCLUSIONS = {
    "kiro_crew/deploy/scan.py": (
        "the deploy scanner's internal-host heuristic, which warns before a site "
        "that names an internal domain is published",
        frozenset(
            {
                "35793e066e341bfc2265c26fab0b0ba4dd06a4c03a2f30107fc3954bb9c3c742",
                "71aebe44d1690bab5beb249abdd432c098fd91aff8d6099b9d8896cb9c264063",
                "ed73cb5c8e0e77e93095d5ad55b7b9e532d0ae38f5ff1006978d56a26adcf41b",
            }
        ),
    ),
    "kiro_crew/deploy/skills/artifact-deploy/scripts/deploy.sh": (
        "the artifact-deploy skill's grep fallback for the same heuristic",
        frozenset({"ed73cb5c8e0e77e93095d5ad55b7b9e532d0ae38f5ff1006978d56a26adcf41b"}),
    ),
}

_HOST_CHARS = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
#: Maps every byte that cannot be part of a host to a space, so split() tokenizes.
_TOKENIZE = bytes(b if b in _HOST_CHARS else 0x20 for b in range(256))


def _digest(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


class _Denylist:
    def __init__(self, hosts: frozenset, names: frozenset, markers: frozenset) -> None:
        self.hosts, self.names, self.markers = hosts, names, markers
        self._seen: dict[bytes, str | None] = {}

    def match(self, token: bytes) -> str | None:
        """The digest *token* matches, or ``None``. Cached per token."""
        if token in self._seen:
            return self._seen[token]
        found = None
        if b"." in token:
            host = token.strip(b".-").lower()
            while host and found is None:
                digest = _digest(host)
                if digest in self.hosts:
                    found = digest
                host = host.partition(b".")[2]
        elif _digest(token) in self.names:
            found = _digest(token)
        self._seen[token] = found
        return found

    def marker(self, source: bytes) -> str | None:
        """The build-marker digest a SOURCE.json token starts with, or ``None``."""
        for token in source.translate(_TOKENIZE).split():
            for part in token.lower().split(b"."):
                for end in range(1, min(len(part), _MARKER_PREFIX_MAX) + 1):
                    digest = _digest(part[:end])
                    if digest in self.markers:
                        return digest
        return None


def _hits_in(data: bytes, deny: _Denylist) -> list[tuple[int, str]]:
    """(line, digest) for each distinct digest found in *data*, first occurrence.

    ``\\.`` is read as ``.`` first: an escaped dot is how code writes a host in a
    regex, and without the fold such a host would split into undotted tokens.
    Folding two bytes into one keeps line numbers.
    """
    data = data.replace(b"\\.", b".")
    found: dict[str, int] = {}
    for token in data.translate(_TOKENIZE).split():
        digest = deny.match(token)
        if digest is not None and digest not in found:
            found[digest] = data.find(token)
    return sorted((data.count(b"\n", 0, at) + 1, digest) for digest, at in found.items())


def _check_files(label: str, files: dict[str, bytes], deny: _Denylist) -> list[str]:
    """Report lines for one artifact, given its files by relative path."""
    hits = []
    for name, data in sorted(files.items()):
        excused = EXCLUSIONS[name][1] if name in EXCLUSIONS else frozenset()
        hits += [
            f"{label}{name}:{line}: matches {d}"
            for line, d in _hits_in(data, deny)
            if d not in excused
        ]
        base = name.rsplit("/", 1)[-1]
        if base not in KS_BUNDLES:
            continue
        source = files.get(name[: len(name) - len(base)] + "SOURCE.json")
        if source is None:
            hits.append(f"{label}{name}: no SOURCE.json beside it, provenance unknown")
            continue
        internal = deny.marker(source) or next((d for _, d in _hits_in(source, deny)), None)
        if internal:
            hits.append(f"{label}{name}: SOURCE.json names an internal build (matches {internal})")
    return hits


def scan(
    dist: pathlib.Path,
    web: pathlib.Path,
    *,
    hosts: frozenset | None = None,
    names: frozenset | None = None,
    markers: frozenset | None = None,
) -> list[str]:
    """Every hit in the wheels under *dist* and the files under *web*."""
    deny = _Denylist(
        HOST_DIGESTS if hosts is None else hosts,
        NAME_DIGESTS if names is None else names,
        BUILD_MARKER_DIGESTS if markers is None else markers,
    )
    hits = []
    for wheel in sorted(dist.glob("*.whl")):
        with zipfile.ZipFile(wheel) as whl:
            files = {i.filename: whl.read(i) for i in whl.infolist() if not i.is_dir()}
        hits += _check_files(f"{wheel.name}!", files, deny)
    web_files = {
        p.relative_to(web).as_posix(): p.read_bytes() for p in web.rglob("*") if p.is_file()
    }
    hits += _check_files("website dist: ", web_files, deny)
    return hits


def main(argv: list[str]) -> int:
    dist = pathlib.Path(argv[1] if len(argv) > 1 else "dist")
    web = pathlib.Path(argv[2] if len(argv) > 2 else "website/dist")
    if not list(dist.glob("*.whl")) or not web.is_dir() or not any(web.rglob("*")):
        print(f"nothing to scan: expected a wheel in {dist} and files in {web}", file=sys.stderr)
        return 2
    hits = scan(dist, web)
    if hits:
        print("internal host in the public build:", file=sys.stderr)
        for line in hits:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"no internal host in {dist} wheels or {web}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
