#!/usr/bin/env bash
# Build the KAS bundle's Node for linux-x64 so it runs on Amazon Linux 2
# (glibc 2.26).
#
# The version is the -node<N> part of KAS_PIN_VERSION in
# src/kiro_crew/acp/kas_pin.py, so the build always matches the pinned bundle.
# Node is compiled inside a pinned amazonlinux:2 container with AL2's gcc10
# package (GCC 10.5, binutils 2.35, enough for simdutf's AVX-512 `vpcompressb`)
# and `./configure --partly-static`, which links libstdc++ and libgcc
# statically. The glibc floor is the container's 2.26. SHASUMS256.txt is
# signature-checked first in amazonlinux:2023, whose gpg reads EdDSA keys.
#
# Usage:
#   build-node.sh build  [--work DIR] [--out DIR] [--jobs N]
#   build-node.sh verify --out DIR
#
# `build` writes to --out:
#   node-v<N>-linux-x64.tar.xz (+ .sha256)  laid out like nodejs.org's tarball,
#     ready for scripts/kas_bundle_publish.py --node-linux-x64
#   glibc-check.txt, provenance.txt, build.log
# `verify` runs `env -i node --version` in amazonlinux:2 and amazonlinux:2023
# and writes verify-node.txt. For the onnxruntime load and inference checks
# with this Node, pass its bin/node to `build-onnxruntime.sh verify --al2-node`.
#
# Requires: docker, bash, coreutils on the host. No root on the host.
set -euo pipefail

# ---- pins -------------------------------------------------------------------
AL2_IMAGE="amazonlinux:2@sha256:7c78b2e1be42be4552d59e4b1590cc915836748b46050aff98b7dbb04a87db59"
AL2023_IMAGE="amazonlinux:2023@sha256:9cf762f5099b606bcd851391317af7cd75b11363c4816ebcb5e91292dd5736a7"

# The Node source is trusted through nodejs.org's SHASUMS256.txt, whose
# detached signature must verify against a key of nodejs/release-keys at this
# commit. Bump the commit when Node adds a releaser.
RELEASE_KEYS_URL="https://github.com/nodejs/release-keys.git"
RELEASE_KEYS_COMMIT="481637f813e912c4aa3622d7964ab426c97b8e8d"

# python.org release_file API sha256_sum. Node's configure needs Python 3.9+
# (AL2 ships 3.7), only as a build-time interpreter.
PYTHON_VERSION="3.12.16"
PYTHON_SHA256="b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c"
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIN_FILE="$SCRIPT_DIR/../../src/kiro_crew/acp/kas_pin.py"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

pinned_node_version() {
  local v
  v="$(sed -n 's/^KAS_PIN_VERSION = "[^"]*-node\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)\(-al2\)\{0,1\}"$/\1/p' "$PIN_FILE")"
  [ -n "$v" ] || die "no -node<N> suffix on KAS_PIN_VERSION in $PIN_FILE"
  echo "$v"
}

host_build() {
  local work="" out="" jobs=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --work) work="$2"; shift 2 ;;
      --out) out="$2"; shift 2 ;;
      --jobs) jobs="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -n "$work" ] || work="$PWD/kas-al2-node-work"
  [ -n "$out" ] || out="$PWD/kas-al2-node-out"
  if [ -z "$jobs" ]; then jobs="$(nproc)"; fi
  [ "$jobs" -le 32 ] || jobs=32 # keep the host usable
  mkdir -p "$work" "$out"
  work="$(cd "$work" && pwd)"
  out="$(cd "$out" && pwd)"
  command -v docker >/dev/null || die "docker is required"
  local version commit
  version="$(pinned_node_version)"
  commit="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ -n "$(git -C "$SCRIPT_DIR/../.." status --porcelain -- packaging/kas-al2 src/kiro_crew/acp/kas_pin.py 2>/dev/null)" ]; then
    commit="${commit}-dirty"
  fi

  log "checking SHASUMS256.txt for Node $version in $AL2023_IMAGE"
  docker run --rm --platform linux/amd64 -v "$SCRIPT_DIR:/src:ro" -v "$work:/work" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e NODE_VERSION="$version" \
    "$AL2023_IMAGE" bash /src/build-node.sh verify-sums 2>&1 | tee "$out/build.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "SHASUMS256.txt check failed; see $out/build.log"
  log "building Node $version in $AL2_IMAGE (jobs=$jobs); log: $out/build.log"
  # No fixed container name, so parallel builds never stop each other.
  docker run --rm --platform linux/amd64 \
    -v "$SCRIPT_DIR:/src:ro" -v "$work:/work" -v "$out:/out" \
    -e JOBS="$jobs" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -e SCRIPT_COMMIT="$commit" -e NODE_VERSION="$version" \
    "$AL2_IMAGE" bash /src/build-node.sh in-container 2>&1 | tee -a "$out/build.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "container build failed; see $out/build.log"
  log "done: $out/node-v$version-linux-x64.tar.xz"
}

fetch() { # url sha256 dest
  local url="$1" sum="$2" dest="$3"
  if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum -c --quiet - 2>/dev/null; then
    curl -fsSL --retry 3 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  echo "$sum  $dest" | sha256sum -c --quiet - || die "sha256 mismatch: $url"
}

# Runs in amazonlinux:2023: AL2's gpg 2.0 cannot read EdDSA keys, which some
# Node releasers sign with. Writes the signature-checked SHASUMS256 list and the
# signing key's fingerprint into /work/dl for the AL2 build step.
verify_sums() {
  local v="${NODE_VERSION:?}" dist="https://nodejs.org/dist/v${NODE_VERSION:?}"
  trap 'chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /work 2>/dev/null || true' EXIT
  cd /work
  mkdir -p dl
  rm -f dl/SHASUMS256.verified dl/SHASUMS256.signer
  dnf install -y -q git >/dev/null # gpg ships in the image (gnupg2-minimal)
  git config --global --add safe.directory '*'
  local keys=/work/release-keys gnupg
  if [ "$(git -C "$keys" rev-parse HEAD 2>/dev/null)" != "$RELEASE_KEYS_COMMIT" ]; then
    rm -rf "$keys" && git init -q "$keys"
    git -C "$keys" fetch -q --depth 1 "$RELEASE_KEYS_URL" "$RELEASE_KEYS_COMMIT"
    git -C "$keys" checkout -q FETCH_HEAD
  fi
  [ "$(git -C "$keys" rev-parse HEAD)" = "$RELEASE_KEYS_COMMIT" ] || die "release-keys HEAD mismatch"
  gnupg="$(mktemp -d)"
  # A key that fails to import cannot sign; the --verify below decides.
  gpg --homedir "$gnupg" --batch --quiet --import "$keys"/keys/*.asc 2>/dev/null || true
  curl -fsSL --retry 3 -o dl/SHASUMS256.txt "$dist/SHASUMS256.txt"
  curl -fsSL --retry 3 -o dl/SHASUMS256.txt.sig "$dist/SHASUMS256.txt.sig"
  local gpg_out signer
  gpg_out="$(gpg --homedir "$gnupg" --batch --status-fd 1 --verify dl/SHASUMS256.txt.sig dl/SHASUMS256.txt 2>/dev/null)" \
    || die "SHASUMS256.txt signature does not verify against nodejs/release-keys"
  signer="$(echo "$gpg_out" | sed -n 's/^\[GNUPG:\] VALIDSIG \([0-9A-F]*\) .*/\1/p')"
  [ -n "$signer" ] || die "no valid signature on SHASUMS256.txt"
  rm -rf "$gnupg"
  cp dl/SHASUMS256.txt dl/SHASUMS256.verified
  echo "$signer" > dl/SHASUMS256.signer
  echo "SHASUMS256.txt for v$v: good signature, key $signer"
}

in_container() {
  local jobs="${JOBS:-8}" v="${NODE_VERSION:?}"
  local top="node-v$v-linux-x64" dist="https://nodejs.org/dist/v$v"
  # Hand every file back to the host user, even when the run dies on a signal.
  trap 'chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /work /out 2>/dev/null || true' EXIT
  trap 'exit 130' INT TERM HUP PIPE
  cd /work
  mkdir -p dl tc

  log "yum packages"
  yum install -y -q gcc10 gcc10-c++ gcc10-binutils gcc gcc-c++ make tar xz gzip \
    file which findutils diffutils binutils zlib-devel libffi-devel bzip2-devel xz-devel >/dev/null

  # verify-sums ran first in this same `build` and left only a verified list.
  [ -f dl/SHASUMS256.verified ] || die "no verified SHASUMS256 list; run build, not in-container"
  local src_sha signer
  signer="$(cat dl/SHASUMS256.signer)"
  src_sha="$(sed -n "s/^\([0-9a-f]\{64\}\)  node-v$v\.tar\.xz\$/\1/p" dl/SHASUMS256.verified)"
  [ -n "$src_sha" ] || die "node-v$v.tar.xz is not in SHASUMS256.txt"
  fetch "$dist/node-v$v.tar.xz" "$src_sha" "dl/node-v$v.tar.xz"
  fetch "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tar.xz" "$PYTHON_SHA256" dl/python.tar.xz

  local tc=/work/tc
  # configure.py imports bz2; rebuild a cached interpreter that lacks it.
  if ! "$tc/py/bin/python3" -c "import bz2, lzma, zlib" 2>/dev/null; then
    log "python $PYTHON_VERSION (build-time interpreter only)"
    rm -rf src-py && mkdir src-py && tar -xf dl/python.tar.xz -C src-py --strip-components=1
    (cd src-py && ./configure --prefix="$tc/py" --without-ensurepip >/dev/null \
      && make -j"$jobs" >/dev/null 2>&1 && make install >/dev/null 2>&1)
    rm -rf src-py
  fi
  # AL2's own `as` (binutils 2.29) rejects vpcompressb; anything that calls
  # as/ld directly gets the gcc10 ones.
  mkdir -p "$tc/shim"
  ln -sf /usr/bin/gcc10-as "$tc/shim/as"
  ln -sf /usr/bin/gcc10-ld "$tc/shim/ld"
  export PATH="$tc/shim:$tc/py/bin:$PATH" CC=gcc10-gcc CXX=gcc10-g++
  printf 'vpcompressb %%zmm1, %%zmm2{%%k1}\n' > /work/vbmi2.s
  "$CC" -c /work/vbmi2.s -o /work/vbmi2.o || die "gcc10 toolchain cannot assemble vpcompressb"
  rm -f /work/vbmi2.s /work/vbmi2.o

  log "configure + build Node $v (--partly-static)"
  rm -rf src-node && mkdir src-node
  tar -xf "dl/node-v$v.tar.xz" -C src-node --strip-components=1
  (cd src-node && python3 ./configure --prefix=/ --partly-static && make -j"$jobs")

  log "stage $top"
  local stage=/work/stage
  rm -rf "$stage" && mkdir -p "$stage/$top"
  (cd src-node && make install DESTDIR="$stage/$top" PREFIX=/ >/dev/null)
  cp src-node/LICENSE src-node/README.md src-node/CHANGELOG.md "$stage/$top/"
  local node="$stage/$top/bin/node"
  [ "$(env -i "$node" --version)" = "v$v" ] || die "built node does not print v$v"

  log "glibc check"
  local report=/out/glibc-check.txt fail=0 maxg cxx
  maxg="$(objdump -T "$node" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
  cxx="$(objdump -T "$node" | grep -cE 'GLIBCXX_|CXXABI_' || true)"
  {
    echo "== $top/bin/node"
    echo "max GLIBC: $maxg"
    echo "GLIBCXX_/CXXABI_ symbols: $cxx"
    echo "GLIBC versions used: $(objdump -T "$node" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tr '\n' ' ')"
    echo "NEEDED: $(readelf -d "$node" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | tr '\n' ' ')"
    echo "ldd:"; ldd "$node" | sed 's/ (0x[0-9a-f]*)//'
    echo "env -i node --version: $(env -i "$node" --version)"
    echo "container: $(cat /etc/system-release), $(ldd --version | head -1)"
  } > "$report"
  printf '%s\n' "$maxg" "GLIBC_2.26" | sort -CV || { echo "FAIL node needs $maxg" >> "$report"; fail=1; }
  [ "$cxx" = 0 ] || { echo "FAIL node has GLIBCXX/CXXABI symbols" >> "$report"; fail=1; }
  if ldd "$node" | grep -qE 'libstdc\+\+|libgcc_s|not found'; then
    echo "FAIL ldd shows libstdc++/libgcc_s or a missing lib" >> "$report"; fail=1
  fi
  cat "$report"
  [ "$fail" = 0 ] || die "glibc check failed"

  log "package"
  local tarball="node-v$v-linux-x64.tar.xz"
  # A fixed mtime: GNU tar reads it from the source release's own configure file.
  (cd "$stage" && tar --owner=0 --group=0 --numeric-owner --mtime=/work/src-node/configure -cJf "/out/$tarball" "$top")
  (cd /out && sha256sum "$tarball" > "$tarball.sha256")

  {
    echo "node $v from $dist/node-v$v.tar.xz sha256 $src_sha"
    echo "SHASUMS256.txt signed by $signer (verified against nodejs/release-keys at $RELEASE_KEYS_COMMIT)"
    echo "configure: --prefix=/ --partly-static (no version suffix)"
    echo "compiler: $("$CC" --version | head -1); assembler: $(gcc10-as --version | head -1)"
    echo "packages: $(rpm -q gcc10 gcc10-c++ gcc10-binutils make | tr '\n' ' ')"
    echo "python (build-time only): $(python3 --version); Python-$PYTHON_VERSION.tar.xz sha256 $PYTHON_SHA256 (python.org)"
    echo "build image: $AL2_IMAGE"
    echo "script: packaging/kas-al2/build-node.sh at commit ${SCRIPT_COMMIT:-unknown}"
    echo "outputs (sha256):"
    echo "  $(cd "$stage" && sha256sum "$top/bin/node")"
    echo "  $(cat "/out/$tarball.sha256")"
  } > /out/provenance.txt
  cat /out/provenance.txt
}

host_verify() {
  local out=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) out="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -n "$out" ] || die "--out is required"
  local v tarball
  v="$(pinned_node_version)"
  tarball="node-v$v-linux-x64.tar.xz"
  [ -f "$out/$tarball" ] || die "missing $out/$tarball; run build first"
  (cd "$out" && sha256sum -c --quiet "$tarball.sha256") || die "tarball sha256 mismatch"
  out="$(cd "$out" && pwd)"
  local scratch
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' RETURN
  tar -xJf "$out/$tarball" -C "$scratch"
  local res="$out/verify-node.txt" image got
  : > "$res"
  for image in "$AL2_IMAGE" "$AL2023_IMAGE"; do
    got="$(docker run --rm --platform linux/amd64 -v "$scratch:/n:ro" "$image" \
      env -i "/n/node-v$v-linux-x64/bin/node" --version 2>&1)" || true
    echo "== $image: env -i node --version -> $got" | tee -a "$res"
    [ "$got" = "v$v" ] || die "node does not run on $image"
  done
}

case "${1:-}" in
  build) shift; host_build "$@" ;;
  verify) shift; host_verify "$@" ;;
  verify-sums) shift; verify_sums "$@" ;;
  in-container) shift; in_container "$@" ;;
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
