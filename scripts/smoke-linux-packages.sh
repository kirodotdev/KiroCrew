#!/usr/bin/env bash
# Install the Linux desktop packages for real, in the distros they target, and
# assert what only a real install can show.
#
# WHY A SCRIPT AND NOT INLINE YAML: two callers need the identical assertions --
# ci.yml runs it on a packaging-path PR, build-desktop.yml runs it on every
# nightly and release. Inlining is how those two drift, and a drifted smoke test
# is worse than none: the lane that matters would be the one missing a check.
#
# Everything here is invisible to a unit test, because it lives in metadata the
# package manager interprets rather than in code we run:
#
#   * dependency names must EXIST in the target distro's repositories. They
#     differ per distro (libgtk-3-0 vs gtk3), and Ubuntu 24.04's 64-bit time_t
#     transition renamed several, which is why the deb declares alternatives
#     (`libgtk-3-0 | libgtk-3-0t64`). Ubuntu 24.04 is used deliberately here:
#     it is the release that proves the alternative resolves.
#   * the .desktop entry electron-builder generates, including the
#     StartupWMClass that must equal Electron's app_id for a desktop
#     environment to associate the running window with the launcher.
#   * the maintainer scripts, which place the /usr/bin launcher and remove it.
#   * the beacon distribution stamp, which must name THIS format. One backend
#     tree is packaged three times, so a single stamp would label one artifact
#     as another -- the exact mislabel scripts/stamp-distribution.sh exists to
#     prevent, and it is only observable from inside an installed package.
#
# A wrong dependency name produces a package that builds green and refuses to
# install, which no other gate in this repository would catch.
#
# Usage: smoke-linux-packages.sh <dist-dir>
set -euo pipefail

DIST_DIR="${1:?usage: smoke-linux-packages.sh <dir containing the built .deb and .rpm>}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to smoke-install the packages" >&2
  exit 1
fi

# Exactly one artifact per format. Zero means the build silently dropped it (a
# glob that no longer matches); more than one means an ambiguous input, and
# picking arbitrarily would test bytes nobody ships.
resolve_one() {
  local ext="$1" found
  mapfile -t found < <(find "$DIST_DIR" -maxdepth 1 -type f -name "*.${ext}")
  if [ "${#found[@]}" -ne 1 ]; then
    echo "ERROR: expected exactly one .${ext} in ${DIST_DIR}, found ${#found[@]}" >&2
    printf '  %s\n' "${found[@]}" >&2
    exit 1
  fi
  printf '%s' "$(basename "${found[0]}")"
}

DEB_NAME="$(resolve_one deb)"
RPM_NAME="$(resolve_one rpm)"
ABS_DIST="$(cd "$DIST_DIR" && pwd)"

# Shared assertions, run inside the container after the package manager has
# installed the package. `$1` is the command that lists the package's own files
# (`dpkg -L <pkg>` / `rpm -ql <pkg>`).
#
# EVERY identity is DERIVED from that list rather than written here, because the
# nightly channel deliberately ships different ones so it can sit beside stable:
# build-desktop.sh overrides packageName, executableName and desktopName for a
# `-nightly.` version. Hardcoding stable's spelling would fail this gate on every
# nightly build -- and because a failed job inside build-desktop.yml fails the
# CALLER's `build-desktop` job, which every `publish-linux-*` lane depends on,
# that would silently skip nightly's entire Linux publication. What is asserted
# is the INVARIANT, not one channel's names.
common_asserts() {
  cat <<ASSERTS
    files=\$($1)
    entry=\$(printf '%s\\n' "\$files" | grep -E '^/usr/share/applications/[^/]+[.]desktop\$' | head -1)
    stamp=\$(printf '%s\\n' "\$files" | grep -E '/kiro_crew/_build_info[.]py\$' | head -1)
    test -n "\$entry" && test -f "\$entry"
    test -n "\$stamp"
    # The identity comes from the desktop entry's FILENAME, not from parsing its
    # Exec value. linux.syncDesktopName ties four things to one name -- the entry
    # filename, executableName, Electron's app_id and StartupWMClass -- so the
    # filename IS the identity, and reading it needs no string parsing at all.
    #
    # Exec is deliberately NOT the source: electron-builder quotes that path when
    # it contains a character outside [/0-9A-Za-z._-], which the nightly channel's
    # `/opt/KiroCrew Nightly/...` does. Parsing it would work on stable and return
    # empty on nightly -- the third time this gate assumed stable's shape.
    exe=\$(basename "\$entry" .desktop)
    launcher=/usr/bin/\$exe
    test -x "\$launcher"
    # Window association: StartupWMClass must equal Electron's app_id, which
    # electron-builder derives from desktopName -- the same value the launcher
    # name follows. A mismatch leaves the running window unassociated with its
    # launcher icon, silently, at runtime.
    grep -q "^StartupWMClass=\${exe}\$" "\$entry"
    # Installed under the fixed /opt prefix, which is what makes the AppArmor
    # profile, the PATH launcher and in-place updates durable.
    grep -qE "^Exec=\\"?/opt/.*/\${exe}" "\$entry"
    # The bundled CLI that fixed prefix makes reachable -- what an AppImage
    # cannot offer, and what makes \`kirocrew service install\` usable.
    printf '%s\\n' "\$files" | grep -qE '/backend-dist/kirocrew-backend/bin/kirocrew\$'
ASSERTS
}

echo "▶ Installing '${DEB_NAME}' on Ubuntu 24.04 (the t64 release)…"
docker run --rm -v "${ABS_DIST}:/dist:ro" -w /dist ubuntu:24.04 bash -euxc "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # The package's OWN declared name, so a nightly build (kirocrew-nightly) is
  # queried and removed under the identity it actually installed as.
  pkg=\$(dpkg-deb -f './${DEB_NAME}' Package)
  apt-get install -y --no-install-recommends './${DEB_NAME}'
$(common_asserts 'dpkg -L "$pkg"')
  grep -q 'DISTRIBUTION = \"deb\"' \"\$stamp\"
  # \`dpkg -r\` rather than \`apt-get remove\`: assert OUR package's removal and
  # its maintainer script, not the package manager's dependency bookkeeping.
  dpkg -r \"\$pkg\"
  test ! -e \"\$launcher\"
"

echo "▶ Installing '${RPM_NAME}' on Amazon Linux 2023…"
docker run --rm -v "${ABS_DIST}:/dist:ro" -w /dist amazonlinux:2023 bash -euxc "
  pkg=\$(rpm -qp --queryformat '%{NAME}' './${RPM_NAME}')
  dnf install -y './${RPM_NAME}'
$(common_asserts 'rpm -ql "$pkg"')
  grep -q 'DISTRIBUTION = \"rpm\"' \"\$stamp\"
  # \`rpm -e\` rather than \`dnf remove\`: dnf also plans the removal of
  # now-unneeded DEPENDENCIES, and in a bare container several of ours are
  # reverse-depended on by protected packages (systemd-udev), so it refuses the
  # whole transaction.
  rpm -e \"\$pkg\"
  test ! -e \"\$launcher\"
"

echo "✅ Both Linux packages install, register, and uninstall cleanly."
