#!/usr/bin/env bash
# Write src/kiro_crew/_build_info.py so a packaged build reports which install
# path it came from in the usage beacon's `dist` field.
#
# WHY A GENERATED MODULE, not an env var: `KIROCREW_DISTRIBUTION` is inherited by
# every child process and settable by anyone with a shell, so a stray export
# relabels that host's daily count. A module inside the package tree ships with
# the artifact and a running install cannot change it. `beacon.distribution()`
# prefers this file and falls back to the env var.
#
# WHY A SHARED SCRIPT: six packaging paths need the identical file (wheel,
# dmg, appimage, deb, rpm, docker). Inlining the write in each one is how they drift,
# and a `dist` value that disagrees with the artifact is worse than no value,
# because it is indistinguishable from a real population shift on the dashboard.
#
# Usage: stamp-distribution.sh <dmg|appimage|deb|rpm|wheel|source|docker> [package_dir]
set -euo pipefail

DIST="${1:?usage: stamp-distribution.sh <dmg|appimage|deb|rpm|wheel|source|docker> [package_dir]}"
PKG_DIR="${2:-}"

# Keep in sync with beacon.KNOWN_DISTRIBUTIONS. A typo here would otherwise bake
# a value the clamp rejects, silently falling back to "source": the exact
# failure this script exists to remove.
case "$DIST" in
  dmg|appimage|deb|rpm|wheel|source|docker) ;;
  *) echo "ERROR: unknown distribution '$DIST' (want: dmg|appimage|deb|rpm|wheel|source|docker)" >&2
     exit 1 ;;
esac

if [ -z "$PKG_DIR" ]; then
  PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/kiro_crew"
fi

if [ ! -d "$PKG_DIR" ]; then
  echo "ERROR: package dir not found: $PKG_DIR" >&2
  exit 1
fi

cat > "$PKG_DIR/_build_info.py" <<EOF
"""Build-time provenance. GENERATED - do not edit and do not commit.

Written by scripts/stamp-distribution.sh during packaging. Absent from a git
checkout, which is why beacon.baked_distribution() treats ImportError as the
normal case and reports "source".
"""

from __future__ import annotations

DISTRIBUTION = "$DIST"
EOF

echo "Stamped distribution=$DIST -> $PKG_DIR/_build_info.py"
