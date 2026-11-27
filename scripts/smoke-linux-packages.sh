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
    # \`/opt/KiroCrew Nightly/...\` does. Parsing it would work on stable and return
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

# The container's apt uses archive.ubuntu.com alone, the public host every
# Ubuntu machine shares. When it is overloaded, apt does not fail: it waits up
# to two minutes per file and moves on (`Ign:`), so a fetch that takes seconds
# runs until the job's time limit cancels it. A GitHub-hosted runner names its
# cloud's own Ubuntu mirror first in /etc/apt/apt-mirrors.txt, the list its own
# apt uses. Probe that list's plain-http entries from the host and give the
# container the first one that answers; with no list, or no answer, the
# container keeps archive.ubuntu.com.
#
# One mirror, chosen once, rather than the list itself: apt walks a mirror
# list per file, so a stalled first entry costs its full timeout on every file.
# And plain http only: the image has no ca-certificates, so an https entry
# could never answer inside it.
UBUNTU_MIRROR=""
if [ -r /etc/apt/apt-mirrors.txt ] && command -v curl >/dev/null 2>&1; then
  while read -r uri _; do
    [[ "$uri" =~ ^http://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)*/?$ ]] || continue
    if curl -fsS --max-time 10 -o /dev/null "${uri%/}/dists/noble/InRelease"; then
      UBUNTU_MIRROR="${uri%/}/"
      break
    fi
  done < /etc/apt/apt-mirrors.txt
fi
echo "Ubuntu mirror for the container: ${UBUNTU_MIRROR:-archive.ubuntu.com (image default)}"

echo "▶ Installing '${DEB_NAME}' on Ubuntu 24.04 (the t64 release)…"
docker run --rm -v "${ABS_DIST}:/dist:ro" -w /dist ubuntu:24.04 bash -euxc "
  export DEBIAN_FRONTEND=noninteractive
  if [ -n '${UBUNTU_MIRROR}' ]; then
    src=/etc/apt/sources.list.d/ubuntu.sources
    sed 's#^URIs: http://archive.ubuntu.com/ubuntu/\$#URIs: ${UBUNTU_MIRROR}#' \"\$src\" > \"\$src.new\"
    mv \"\$src.new\" \"\$src\"
    grep -qF 'URIs: ${UBUNTU_MIRROR}' /etc/apt/sources.list.d/ubuntu.sources \
      || echo 'WARNING: ubuntu.sources has no archive.ubuntu.com entry to replace' >&2
  fi
  # A stalled connection is dropped after 30 seconds rather than apt's default
  # 120, and retried, so one slow request does not hold the whole fetch.
  printf '%s\n' 'Acquire::Retries \"3\";' 'Acquire::http::Timeout \"30\";' \
    > /etc/apt/apt.conf.d/80smoke-network
  apt-get update -qq
  # The package's OWN declared name, so a nightly build (kirocrew-nightly) is
  # queried and removed under the identity it actually installed as.
  pkg=\$(dpkg-deb -f './${DEB_NAME}' Package)
  apt-get install -y --no-install-recommends './${DEB_NAME}'
$(common_asserts 'dpkg -L "$pkg"')
  grep -q 'DISTRIBUTION = \"deb\"' \"\$stamp\"
  # Installing is not linking: apt can satisfy libasound2 with OSS4's stand-in,
  # which installs cleanly and lacks ALSA symbols the app needs to start.
  bin=\$(printf '%s\\n' \"\$files\" | grep -E \"^/opt/[^/]+/\${exe}\\\$\" | head -1)
  test -n \"\$bin\"
  test -z \"\$(ldd -r \"\$bin\" 2>&1 | grep 'undefined symbol: snd_')\"
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
