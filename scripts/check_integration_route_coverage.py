#!/usr/bin/env python3
"""Report (and optionally gate) route coverage of the integration layer.

``test/integration/conftest.py`` records, per pytest process, every aiohttp
route the suite requested and every route the real dashboard app registered.
This script unions those dumps and prints the share of registered routes the
integration suite exercised end-to-end.

Usage (the dump directory must be an ABSOLUTE path outside the checkout; the
conftest refuses anything else, because pytest's CWD is the repository root
and a relative value would leave the dumps in it)::

    HITS="$(mktemp -d)"
    KIROCREW_INTEGRATION=1 KIROCREW_INTEGRATION_HITS_DIR="$HITS" \\
        python -m pytest test/integration -n 2
    python3 scripts/check_integration_route_coverage.py "$HITS" --min 80

Exit status is 0 when coverage >= ``--min`` (default 0, i.e. report only),
1 otherwise, 2 when no dumps were found. ``--missing`` lists the routes the
suite never touched, grouped by first path segment, so the next test file to
write is obvious.

Why routes and not lines: a route answered by the real app on a real boot
proves the wiring between config, auth, store and handler; a line reached
through a mock proves only that the line exists. Line coverage of the layer
is still worth READING (``--cov=kiro_crew`` works as usual) -- it is just not
the number this layer is gated on.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

#: Routes that are registered but are not contracts this layer should have to
#: exercise. Keep this list SHORT and justified; every entry is a route the
#: metric silently forgives. Today it is the SPA root alone: the bundles mount
#: through ``add_static`` and never appear in the registered list.
EXCLUDED_EXACT: frozenset[tuple[str, str]] = frozenset({("GET", "/")})


def _load(directory: Path, stem: str) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for path in sorted(directory.glob(f"{stem}-*.json")):
        for method, route in json.loads(path.read_text(encoding="utf-8")):
            routes.add((method, route))
    return routes


def _excluded(route: tuple[str, str]) -> bool:
    return route in EXCLUDED_EXACT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "hits_dir", type=Path, help="directory KIROCREW_INTEGRATION_HITS_DIR pointed at"
    )
    parser.add_argument(
        "--min", type=float, default=0.0, help="fail below this percent (default: report only)"
    )
    parser.add_argument(
        "--missing", action="store_true", help="list routes the suite never requested"
    )
    args = parser.parse_args(argv)

    if not args.hits_dir.is_dir():
        print(f"no dump directory at {args.hits_dir}", file=sys.stderr)
        return 2
    registered = {r for r in _load(args.hits_dir, "routes") if not _excluded(r)}
    hit = {r for r in _load(args.hits_dir, "hits") if not _excluded(r)}
    if not registered:
        print(f"no routes-*.json dumps under {args.hits_dir}; did the suite boot?", file=sys.stderr)
        return 2

    covered = registered & hit
    pct = 100.0 * len(covered) / len(registered)
    print(
        f"integration route coverage: {len(covered)}/{len(registered)} = {pct:.1f}%  (min {args.min:g}%)"
    )

    if args.missing:
        missing = sorted(registered - hit)
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for method, route in missing:
            parts = route.strip("/").split("/")
            key = (
                "/".join(parts[:2]) if parts and parts[0] == "api" and len(parts) > 1 else parts[0]
            )
            groups[key].append((method, route))
        for key in sorted(groups, key=lambda k: -len(groups[k])):
            print(f"\n[{key}] {len(groups[key])} uncovered")
            for method, route in groups[key]:
                print(f"  {method:6} {route}")

    return 0 if pct + 1e-9 >= args.min else 1


if __name__ == "__main__":
    sys.exit(main())
