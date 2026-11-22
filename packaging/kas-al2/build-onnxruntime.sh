#!/usr/bin/env bash
# Build onnxruntime 1.21.0 (CPU EP) + its Node-API binding for linux-x64 so the
# KAS bundle's onnxruntime-node runs on Amazon Linux 2 (glibc 2.26).
#
# Everything is compiled inside a pinned amazonlinux:2 container, so the glibc
# floor is the container's 2.26 and the host stays clean. The C++ runtime is
# linked statically (-static-libstdc++ -static-libgcc), so the outputs need no
# libstdc++ / libgcc_s at run time.
#
# Usage:
#   build-onnxruntime.sh build  [--work DIR] [--out DIR] [--jobs N]
#   build-onnxruntime.sh verify --out DIR --bundle-x64 DIR --stock-node FILE --al2-node FILE
#
# `build` writes to --out:
#   onnxruntime-node-1.21.0-al2-x64.tar.xz (+ .sha256)  drop-in for
#     node_modules/onnxruntime-node/bin/napi-v3/linux/x64/
#   glibc-check.txt, provenance.txt, build.log
# `verify` runs the load and inference checks in amazonlinux:2 / :2023 and
# writes verify.txt. --bundle-x64 is an unpacked kas-linux-x64 engine tree
# (holds node_modules/onnxruntime-node), --stock-node the bundle's
# node-v22.23.4-linux-x64/bin/node, --al2-node a Node 22 that runs on AL2.
#
# Requires: docker, bash, coreutils on the host. No root on the host.
set -euo pipefail

# ---- pins -------------------------------------------------------------------
# Every input is checked against a pin below before it is used.
AL2_IMAGE="amazonlinux:2@sha256:7c78b2e1be42be4552d59e4b1590cc915836748b46050aff98b7dbb04a87db59"
AL2023_IMAGE="amazonlinux:2023@sha256:9cf762f5099b606bcd851391317af7cd75b11363c4816ebcb5e91292dd5736a7"

ORT_VERSION="1.21.0"
ORT_COMMIT="e0b66cad282043d4377cea5269083f17771b6dfc" # tag v1.21.0^{}
# github.com/microsoft/onnxruntime is not reachable (404). A fork that carries
# the upstream tag is used instead; the commit id is content-addressed, so a
# matching HEAD is the same tree as upstream's v1.21.0.
ORT_GIT_URL="${ORT_GIT_URL:-https://github.com/ROCm/onnxruntime.git}"
# Must match the bundle's onnxruntime-node build (js/node/script/build.ts).
NAPI_BUILD_VERSION=6

# GNU tarballs: sha256 recorded after `gpgv --keyring gnu-keyring.gpg` passed
# on the .sig (gcc: Jakub Jelinek 09B5FA62, binutils: Nick Clifton DD9E3C4F).
GCC_VERSION="13.4.0"
GCC_SHA256="9c4ce6dbb040568fdc545588ac03c5cbc95a8dbf0c7aa490170843afb59ca8f5"
BINUTILS_VERSION="2.42" # >= 2.36 for MLAS AVX-VNNI (vpdpbusds)
BINUTILS_SHA256="f6e4d41fd5fc778b06b7891457b3620da5ecea1006c6a4a41ae998109f85a800"
GNU_MIRROR="https://mirrors.kernel.org/gnu"
# Kitware's cmake-3.31.12-SHA-256.txt
CMAKE_VERSION="3.31.12"
CMAKE_SHA256="0dc2e9a6860f06bf10bd8fadc03e35d9eeb4df46e33763a7e480e987758f385c"
# python.org release_file API sha256_sum. ORT's CMake needs Python >= 3.10
# (AL2 ships 3.7), only as a build-time interpreter.
PYTHON_VERSION="3.12.16"
PYTHON_SHA256="b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c"
# nodejs.org SHASUMS256.txt (also re-checked against the live file below).
NODE_VERSION="22.23.4"
NODE_HEADERS_SHA256="00615892b14ca159775ccb96d5696fdfeeea9804740fc8c5c1064eac09456a0e"
# node-addon-api: version and sha512 come from js/node/package-lock.json at
# ORT_COMMIT; the sha256 pin is the same bytes.
NODE_ADDON_API_VERSION="6.0.0"
NODE_ADDON_API_SHA256="16c0b0e9fbb10a79250b4dc8a4ab6e712e9cd918d17bc59e1034699ac8336f4a"

# eigen: cmake/deps.txt pins the sha1 of a GitLab-generated zip, and GitLab
# regenerates those archives, so the bytes drift. We never edit the hash.
# Instead: if the served zip does not match, it must be file-for-file equal to
# a git checkout of the pinned commit, and that checkout is used as the source.
EIGEN_COMMIT="1d8b82b0740839c0de7f1242a3585e3390ff5f33"
EIGEN_GIT_URL="https://gitlab.com/libeigen/eigen.git"

TARBALL="onnxruntime-node-${ORT_VERSION}-al2-x64.tar.xz"
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

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
  [ -n "$work" ] || work="$PWD/kas-al2-onnxruntime-work"
  [ -n "$out" ] || out="$PWD/kas-al2-onnxruntime-out"
  if [ -z "$jobs" ]; then jobs="$(nproc)"; fi
  [ "$jobs" -le 32 ] || jobs=32 # keep the host usable
  mkdir -p "$work" "$out"
  work="$(cd "$work" && pwd)"
  out="$(cd "$out" && pwd)"
  command -v docker >/dev/null || die "docker is required"

  local commit
  commit="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ -n "$(git -C "$SCRIPT_DIR" status --porcelain -- . 2>/dev/null)" ]; then
    commit="${commit}-dirty"
  fi

  log "building in $AL2_IMAGE (jobs=$jobs); log: $out/build.log"
  # Root inside the container only; files are handed back to the host user.
  docker run --rm --platform linux/amd64 \
    -v "$SCRIPT_DIR:/src:ro" -v "$work:/work" -v "$out:/out" \
    -e JOBS="$jobs" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -e SCRIPT_COMMIT="$commit" -e ORT_GIT_URL="$ORT_GIT_URL" \
    "$AL2_IMAGE" bash /src/build-onnxruntime.sh in-container 2>&1 | tee "$out/build.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "container build failed; see $out/build.log"
  log "done: $out/$TARBALL"
}

fetch() { # url sha256 dest
  local url="$1" sum="$2" dest="$3"
  if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum -c --quiet - 2>/dev/null; then
    curl -fsSL --retry 3 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  echo "$sum  $dest" | sha256sum -c --quiet - || die "sha256 mismatch: $url"
}

in_container() {
  local jobs="${JOBS:-8}"
  # Hand every file back to the host user, even when the run dies on a signal.
  trap 'chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /work /out 2>/dev/null || true' EXIT
  trap 'exit 130' INT TERM HUP PIPE
  cd /work
  mkdir -p dl tc

  log "yum packages"
  yum install -y -q gcc gcc-c++ make tar xz gzip bzip2 unzip patch git file which \
    findutils diffutils binutils gmp-devel mpfr-devel libmpc-devel zlib-devel \
    libffi-devel perl m4 openssl >/dev/null
  # /work may hold trees chowned to the host user by an earlier run.
  git config --global --add safe.directory '*'

  fetch "$GNU_MIRROR/binutils/binutils-$BINUTILS_VERSION.tar.xz" "$BINUTILS_SHA256" dl/binutils.tar.xz
  fetch "$GNU_MIRROR/gcc/gcc-$GCC_VERSION/gcc-$GCC_VERSION.tar.xz" "$GCC_SHA256" dl/gcc.tar.xz
  fetch "https://github.com/Kitware/CMake/releases/download/v$CMAKE_VERSION/cmake-$CMAKE_VERSION-linux-x86_64.tar.gz" \
    "$CMAKE_SHA256" dl/cmake.tar.gz
  fetch "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tar.xz" "$PYTHON_SHA256" dl/python.tar.xz
  fetch "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-headers.tar.xz" "$NODE_HEADERS_SHA256" dl/node-headers.tar.xz
  curl -fsSL --retry 3 -o dl/SHASUMS256.txt "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt"
  grep -q "^$NODE_HEADERS_SHA256  node-v$NODE_VERSION-headers.tar.xz\$" dl/SHASUMS256.txt \
    || die "node headers pin does not match nodejs.org SHASUMS256.txt"
  fetch "https://registry.npmjs.org/node-addon-api/-/node-addon-api-$NODE_ADDON_API_VERSION.tgz" \
    "$NODE_ADDON_API_SHA256" dl/node-addon-api.tgz

  local tc=/work/tc
  if [ ! -x "$tc/bin/as" ]; then
    log "binutils $BINUTILS_VERSION"
    rm -rf src-binutils b-binutils && mkdir src-binutils b-binutils
    tar -xf dl/binutils.tar.xz -C src-binutils --strip-components=1
    (cd b-binutils && ../src-binutils/configure --prefix="$tc" --disable-nls --disable-werror \
      --disable-gdb --disable-gdbserver --disable-sim --disable-gprofng --enable-gold=no >/dev/null \
      && make -j"$jobs" MAKEINFO=true >/dev/null && make install MAKEINFO=true >/dev/null)
    rm -rf src-binutils b-binutils
  fi
  if [ ! -x "$tc/bin/g++" ]; then
    log "gcc $GCC_VERSION (stage1 with AL2 gcc 7)"
    rm -rf src-gcc b-gcc && mkdir src-gcc b-gcc
    tar -xf dl/gcc.tar.xz -C src-gcc --strip-components=1
    # Target libraries get -fPIC so libstdc++.a / libgcc.a can be linked
    # into shared objects (-static-libstdc++ in a .so).
    (cd b-gcc && ../src-gcc/configure --prefix="$tc" --with-as="$tc/bin/as" --with-ld="$tc/bin/ld" \
      --disable-bootstrap --disable-multilib --disable-nls --disable-libsanitizer \
      --enable-languages=c,c++ --with-system-zlib \
      CFLAGS_FOR_TARGET="-O2 -fPIC" CXXFLAGS_FOR_TARGET="-O2 -fPIC" >/dev/null \
      && make -j"$jobs" >/dev/null && make install-strip >/dev/null)
    rm -rf src-gcc b-gcc
  fi
  if [ ! -x "$tc/cmake/bin/cmake" ]; then
    mkdir -p "$tc/cmake" && tar -xf dl/cmake.tar.gz -C "$tc/cmake" --strip-components=1
  fi
  if [ ! -x "$tc/py/bin/python3" ]; then
    log "python $PYTHON_VERSION (build-time interpreter only)"
    rm -rf src-py && mkdir src-py && tar -xf dl/python.tar.xz -C src-py --strip-components=1
    (cd src-py && ./configure --prefix="$tc/py" --without-ensurepip >/dev/null \
      && make -j"$jobs" >/dev/null 2>&1 && make install >/dev/null 2>&1)
    rm -rf src-py
  fi
  export PATH="$tc/bin:$tc/cmake/bin:$tc/py/bin:$PATH"
  [ "$("$tc/bin/g++" -print-prog-name=as)" = "$tc/bin/as" ] || die "g++ does not use binutils $BINUTILS_VERSION"
  echo 'vpdpbusds %zmm1, %zmm2, %zmm3' | as -o /dev/null - || die "assembler lacks AVX-VNNI"

  log "onnxruntime source at $ORT_COMMIT"
  if [ ! -d ort/.git ] || [ "$(git -C ort rev-parse HEAD)" != "$ORT_COMMIT" ]; then
    rm -rf ort && git init -q ort
    git -C ort fetch -q --depth 1 "$ORT_GIT_URL" "$ORT_COMMIT"
    git -C ort checkout -q FETCH_HEAD
  fi
  local ort_head
  ort_head="$(git -C ort rev-parse HEAD)"
  [ "$ort_head" = "$ORT_COMMIT" ] || die "onnxruntime HEAD is not $ORT_COMMIT"
  [ -z "$(git -C ort status --porcelain --untracked-files=no)" ] || die "onnxruntime checkout is modified"
  local tagged
  tagged="$(git ls-remote "$ORT_GIT_URL" "refs/tags/v$ORT_VERSION^{}" | cut -f1)"
  [ "$tagged" = "$ORT_COMMIT" ] || die "tag v$ORT_VERSION at $ORT_GIT_URL is $tagged, not $ORT_COMMIT"
  [ "$(cat ort/VERSION_NUMBER)" = "$ORT_VERSION" ] || die "VERSION_NUMBER mismatch"

  log "eigen $EIGEN_COMMIT"
  local eigen_line eigen_url eigen_sha1 eigen_note
  eigen_line="$(grep '^eigen;' ort/cmake/deps.txt)"
  eigen_url="$(echo "$eigen_line" | cut -d';' -f2)"
  eigen_sha1="$(echo "$eigen_line" | cut -d';' -f3)"
  case "$eigen_url" in *"$EIGEN_COMMIT"*) ;; *) die "deps.txt eigen url does not name $EIGEN_COMMIT" ;; esac
  local eigen_args=()
  curl -fsSL --retry 3 -o dl/eigen.zip "$eigen_url"
  local got_sha1
  got_sha1="$(sha1sum dl/eigen.zip | cut -d' ' -f1)"
  if [ "$got_sha1" = "$eigen_sha1" ]; then
    eigen_note="served zip matches deps.txt sha1 $eigen_sha1"
  else
    rm -rf eigen-git eigen-zip
    git init -q eigen-git
    git -C eigen-git fetch -q --depth 1 "$EIGEN_GIT_URL" "$EIGEN_COMMIT"
    git -C eigen-git checkout -q FETCH_HEAD
    [ "$(git -C eigen-git rev-parse HEAD)" = "$EIGEN_COMMIT" ] || die "eigen HEAD mismatch"
    mkdir eigen-zip && unzip -q dl/eigen.zip -d eigen-zip
    diff -r -x .git "eigen-git" "eigen-zip/eigen-$EIGEN_COMMIT" >/dev/null \
      || die "eigen zip differs from git commit $EIGEN_COMMIT"
    rm -rf eigen-zip
    eigen_args=("-DFETCHCONTENT_SOURCE_DIR_EIGEN=/work/eigen-git")
    eigen_note="served zip sha1 $got_sha1 != deps.txt $eigen_sha1 (archive drift); zip contents equal git commit $EIGEN_COMMIT file-for-file; built from that git checkout, deps.txt unchanged"
  fi
  echo "$eigen_note"

  log "node headers + node-addon-api"
  rm -rf node-headers && mkdir node-headers
  tar -xf dl/node-headers.tar.xz -C node-headers --strip-components=1
  local lock_integrity got_integrity
  lock_integrity="$(grep -A4 '"node_modules/node-addon-api"' ort/js/node/package-lock.json | sed -n 's/.*"integrity": "\(.*\)".*/\1/p')"
  got_integrity="sha512-$(openssl dgst -sha512 -binary dl/node-addon-api.tgz | openssl base64 -A)"
  [ "$lock_integrity" = "$got_integrity" ] || die "node-addon-api does not match package-lock.json integrity"
  grep -A2 '"node_modules/node-addon-api"' ort/js/node/package-lock.json | grep -q "\"version\": \"$NODE_ADDON_API_VERSION\"" \
    || die "node-addon-api version differs from package-lock.json"
  rm -rf ort/js/node/node_modules/node-addon-api && mkdir -p ort/js/node/node_modules/node-addon-api
  tar -xzf dl/node-addon-api.tgz -C ort/js/node/node_modules/node-addon-api --strip-components=1

  # Same hardening flags as the upstream Linux release build.
  local cflags="-DNDEBUG -Wp,-D_FORTIFY_SOURCE=2 -Wp,-D_GLIBCXX_ASSERTIONS -fstack-protector-strong -O3 -pipe -fstack-clash-protection -fcf-protection -ffunction-sections -fdata-sections"
  local ldflags="-static-libstdc++ -static-libgcc"
  local common=(
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_C_COMPILER="$tc/bin/gcc" -DCMAKE_CXX_COMPILER="$tc/bin/g++"
    -DCMAKE_C_FLAGS="$cflags" -DCMAKE_CXX_FLAGS="$cflags"
    -DCMAKE_SHARED_LINKER_FLAGS="$ldflags" -DCMAKE_EXE_LINKER_FLAGS="$ldflags"
    -DCMAKE_MODULE_LINKER_FLAGS="$ldflags"
  )

  log "configure onnxruntime"
  rm -rf ort-build
  # ${a[@]+...}: AL2's bash 4.2 treats an empty array as unbound under set -u.
  cmake -S ort/cmake -B ort-build "${common[@]}" ${eigen_args[@]+"${eigen_args[@]}"} \
    -DPython_EXECUTABLE="$tc/py/bin/python3" -DPython3_EXECUTABLE="$tc/py/bin/python3" \
    -DPYTHON_EXECUTABLE="$tc/py/bin/python3" \
    -DFETCHCONTENT_TRY_FIND_PACKAGE_MODE=NEVER \
    -DFETCHCONTENT_BASE_DIR=/work/ort-build/_deps \
    -Donnxruntime_BUILD_SHARED_LIB=ON \
    -Donnxruntime_BUILD_UNIT_TESTS=OFF \
    -Donnxruntime_BUILD_NODEJS=OFF \
    -Donnxruntime_ENABLE_PYTHON=OFF \
    -Donnxruntime_BUILD_FOR_NATIVE_MACHINE=OFF \
    -Donnxruntime_RUN_ONNX_TESTS=OFF \
    -Donnxruntime_USE_FULL_PROTOBUF=OFF \
    -Donnxruntime_DEV_MODE=OFF
  log "build onnxruntime"
  cmake --build ort-build --target onnxruntime -j"$jobs"

  log "build onnxruntime_binding (napi $NAPI_BUILD_VERSION, node $NODE_VERSION headers)"
  # js/node/CMakeLists.txt asks `node` for process.platform only to name its
  # output folder; no Node runs on AL2 here, so answer with a fixed shim.
  mkdir -p shim && printf '#!/bin/sh\necho linux\n' > shim/node && chmod +x shim/node
  rm -rf ort/js/node/bin binding-build
  PATH="/work/shim:$PATH" cmake -S ort/js/node -B binding-build "${common[@]}" \
    -DCMAKE_JS_INC=/work/node-headers/include/node \
    -Dnapi_build_version="$NAPI_BUILD_VERSION" -DNODE_ARCH=x64 \
    -DONNXRUNTIME_BUILD_DIR=/work/ort-build
  cmake --build binding-build -j"$jobs"

  log "stage"
  local stage=/work/stage
  rm -rf "$stage" && mkdir "$stage"
  # Same layout the bundle ships: real file .so.1, versioned name a symlink.
  cp "ort-build/libonnxruntime.so.$ORT_VERSION" "$stage/libonnxruntime.so.1"
  strip --strip-unneeded "$stage/libonnxruntime.so.1"
  ln -s libonnxruntime.so.1 "$stage/libonnxruntime.so.$ORT_VERSION"
  cp ort/js/node/bin/napi-v3/linux/x64/onnxruntime_binding.node "$stage/"

  log "glibc check"
  local report=/out/glibc-check.txt fail=0
  : > "$report"
  for f in libonnxruntime.so.1 onnxruntime_binding.node; do
    local p="$stage/$f" maxg cxx
    maxg="$(objdump -T "$p" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
    cxx="$(objdump -T "$p" | grep -cE 'GLIBCXX_|CXXABI_' || true)"
    {
      echo "== $f"
      echo "max GLIBC: $maxg"
      echo "GLIBCXX_/CXXABI_ symbols: $cxx"
      echo "GLIBC versions used: $(objdump -T "$p" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tr '\n' ' ')"
      echo "NEEDED: $(readelf -d "$p" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | tr '\n' ' ')"
      echo "RUNPATH/RPATH: $(readelf -d "$p" | sed -n 's/.*(R.*PATH).*\[\(.*\)\]/\1/p')"
      echo "ldd:"; (cd "$stage" && ldd "./$f" | sed 's/ (0x[0-9a-f]*)//')
      echo
    } >> "$report"
    printf '%s\n' "$maxg" "GLIBC_2.26" | sort -CV || { echo "FAIL $f needs $maxg" >> "$report"; fail=1; }
    [ "$cxx" = 0 ] || { echo "FAIL $f has GLIBCXX/CXXABI symbols" >> "$report"; fail=1; }
    if (cd "$stage" && ldd "./$f") | grep -qE 'libstdc\+\+|libgcc_s|not found'; then
      echo "FAIL $f ldd shows libstdc++/libgcc_s or a missing lib" >> "$report"; fail=1
    fi
  done
  echo "container: $(cat /etc/system-release), $(ldd --version | head -1)" >> "$report"
  cat "$report"
  [ "$fail" = 0 ] || die "glibc check failed"

  log "package"
  local mtime
  mtime="$(git -C ort log -1 --format=%cI)" # fixed mtime: the ORT commit date
  (cd "$stage" && tar --owner=0 --group=0 --numeric-owner --mtime="$mtime" -cJf "/out/$TARBALL" \
    libonnxruntime.so.1 "libonnxruntime.so.$ORT_VERSION" onnxruntime_binding.node)
  (cd /out && sha256sum "$TARBALL" > "$TARBALL.sha256")

  {
    echo "onnxruntime $ORT_VERSION commit $ort_head (git rev-parse HEAD; tag v$ORT_VERSION^{} at $ORT_GIT_URL = $tagged)"
    echo "cmake/deps.txt deps fetched by CMake FetchContent with their deps.txt SHA1 (URL_HASH)"
    echo "eigen: $eigen_note"
    echo "napi_build_version: $NAPI_BUILD_VERSION; Node-API headers: node-v$NODE_VERSION-headers.tar.xz sha256 $NODE_HEADERS_SHA256 (matches nodejs.org SHASUMS256.txt)"
    echo "node-addon-api $NODE_ADDON_API_VERSION sha256 $NODE_ADDON_API_SHA256 ($lock_integrity, = js/node/package-lock.json)"
    echo "gcc: $("$tc/bin/gcc" --version | head -1); source gcc-$GCC_VERSION.tar.xz sha256 $GCC_SHA256 (GNU .sig verified when pinned)"
    echo "binutils: $(as --version | head -1); source binutils-$BINUTILS_VERSION.tar.xz sha256 $BINUTILS_SHA256 (GNU .sig verified when pinned)"
    echo "cmake: $(cmake --version | head -1); cmake-$CMAKE_VERSION-linux-x86_64.tar.gz sha256 $CMAKE_SHA256 (Kitware SHA-256.txt)"
    echo "python (build-time only): $("$tc/py/bin/python3" --version); Python-$PYTHON_VERSION.tar.xz sha256 $PYTHON_SHA256 (python.org)"
    echo "bootstrap compiler: AL2 $(rpm -q gcc gcc-c++ | tr '\n' ' ')"
    echo "build image: $AL2_IMAGE"
    echo "flags: CFLAGS/CXXFLAGS=$cflags; LDFLAGS=$ldflags"
    echo "cmake options: onnxruntime_BUILD_SHARED_LIB=ON BUILD_UNIT_TESTS=OFF BUILD_NODEJS=OFF ENABLE_PYTHON=OFF BUILD_FOR_NATIVE_MACHINE=OFF; CPU EP only"
    echo "script: packaging/kas-al2/build-onnxruntime.sh at commit ${SCRIPT_COMMIT:-unknown}"
    echo "outputs (sha256):"
    (cd "$stage" && sha256sum libonnxruntime.so.1 onnxruntime_binding.node | sed 's/^/  /')
    echo "  $(cat "/out/$TARBALL.sha256")"
  } > /out/provenance.txt
  cat /out/provenance.txt
}

host_verify() {
  local out="" bundle="" al2node="" stock_node=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) out="$2"; shift 2 ;;
      --bundle-x64) bundle="$2"; shift 2 ;;
      --al2-node) al2node="$2"; shift 2 ;;
      --stock-node) stock_node="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -f "$out/$TARBALL" ] || die "missing $out/$TARBALL; run build first"
  (cd "$out" && sha256sum -c --quiet "$TARBALL.sha256") || die "tarball sha256 mismatch"
  out="$(cd "$out" && pwd)"
  bundle="$(cd "$bundle" && pwd)"
  [ -f "$al2node" ] && [ -f "$stock_node" ] || die "--al2-node and --stock-node are required"
  al2node="$(readlink -f "$al2node")"
  stock_node="$(readlink -f "$stock_node")"
  local pkg="$bundle/node_modules/onnxruntime-node"
  [ -d "$pkg" ] || die "no onnxruntime-node under $bundle/node_modules"

  # Scratch overlay: the bundle's onnxruntime-node + onnxruntime-common with
  # our files dropped in. The model comes from the ORT source tree.
  local scratch
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' RETURN
  mkdir -p "$scratch/node_modules"
  cp -a "$pkg" "$bundle/node_modules/onnxruntime-common" "$scratch/node_modules/"
  local bin="$scratch/node_modules/onnxruntime-node/bin/napi-v3/linux/x64"
  rm -f "$bin/libonnxruntime.so.1" "$bin/libonnxruntime.so.$ORT_VERSION" "$bin/onnxruntime_binding.node"
  tar -xJf "$out/$TARBALL" -C "$bin"
  curl -fsSL -o "$scratch/mul_1.onnx" \
    "https://raw.githubusercontent.com/ROCm/onnxruntime/$ORT_COMMIT/onnxruntime/test/testdata/mul_1.onnx"
  cat > "$scratch/dlopen.js" <<'EOF'
const m = { exports: {} };
process.dlopen(m, '/t/node_modules/onnxruntime-node/bin/napi-v3/linux/x64/onnxruntime_binding.node');
console.log('dlopen OK', Object.keys(m.exports));
EOF
  # mul_1.onnx: Y = X * W, W = [1..6] as float [3,2].
  cat > "$scratch/infer.js" <<'EOF'
const ort = require('/t/node_modules/onnxruntime-node');
(async () => {
  const s = await ort.InferenceSession.create('/t/mul_1.onnx');
  const x = new ort.Tensor('float32', Float32Array.from([1, 2, 3, 4, 5, 6]), [3, 2]);
  const r = await s.run({ [s.inputNames[0]]: x });
  const y = Array.from(r[s.outputNames[0]].data);
  console.log('inputs', s.inputNames, 'outputs', s.outputNames, 'X', Array.from(x.data), 'Y', y);
  const want = [1, 4, 9, 16, 25, 36];
  if (JSON.stringify(y) !== JSON.stringify(want)) { console.error('unexpected result'); process.exit(1); }
  console.log('inference OK');
})().catch((e) => { console.error(e); process.exit(1); });
EOF
  local res="$out/verify.txt" rc
  : > "$res"
  run() { # label image node script expect(0|fail)
    local label="$1" image="$2" node="$3" script="$4" expect="$5"
    echo "== $label ($image, env -i)" | tee -a "$res"
    set +e
    docker run --rm --platform linux/amd64 -v "$scratch:/t:ro" -v "$node:/n/node:ro" "$image" \
      env -i /n/node "/t/$script" 2>&1 | tee -a "$res"
    rc="${PIPESTATUS[0]}"
    set -e
    echo "exit=$rc (expected: $expect)" | tee -a "$res"
    if [ "$expect" = 0 ]; then [ "$rc" = 0 ] || die "$label failed"; else [ "$rc" != 0 ] || die "$label unexpectedly passed"; fi
    echo >> "$res"
  }
  run "AL2: AL2-built node, dlopen binding" "$AL2_IMAGE" "$al2node" dlopen.js 0
  run "AL2: bundle stock node $NODE_VERSION, dlopen binding" "$AL2_IMAGE" "$stock_node" dlopen.js fail
  run "AL2: AL2-built node, inference" "$AL2_IMAGE" "$al2node" infer.js 0
  run "AL2023: AL2-built node, inference" "$AL2023_IMAGE" "$al2node" infer.js 0
  run "AL2023: bundle stock node $NODE_VERSION, inference" "$AL2023_IMAGE" "$stock_node" infer.js 0
  echo "model: onnxruntime/test/testdata/mul_1.onnx at $ORT_COMMIT (sha256 $(sha256sum "$scratch/mul_1.onnx" | cut -d' ' -f1))" | tee -a "$res"
  echo "al2 node: $(docker run --rm -v "$al2node:/n/node:ro" "$AL2_IMAGE" env -i /n/node --version)" | tee -a "$res"
}

case "${1:-}" in
  build) shift; host_build "$@" ;;
  verify) shift; host_verify "$@" ;;
  in-container) shift; in_container "$@" ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
