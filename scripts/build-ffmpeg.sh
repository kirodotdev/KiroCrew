#!/usr/bin/env bash
# Build a minimal ffmpeg from source: just the audio inputs Kiro Crew decodes
# for speech-to-text, nothing else (~2.5 MB vs ~50-80 MB kitchen-sink builds).
#
# Two uses:
#   1. A source install on a host with no ffmpeg package (Amazon Linux, RHEL
#      without EPEL). With no arguments it builds ~/ffmpeg/ffmpeg; the gateway
#      only searches package-manager directories, so the script then prints the
#      one command that installs it to /usr/local/bin.
#   2. The decoder the desktop app ships. build-ffmpeg-decoder.yml runs this once
#      per platform with --target and publishes the results, with the exact
#      source tarball, as a GitHub release the runtime pins by digest.
#
# What the decoder is asked to do (transcribe.py, apple_speech):
#   inputs   wav mp3 m4a/mp4 ogg/oga/opus flac webm/mkv aac wma, forced demuxer
#   outputs  16 kHz mono pcm_s16le WAV, a `-c:a copy` webm -> ogg remux, and
#            `-f null` passes (duration probe, silencedetect)
#   protocol file and pipe only (the runtime pins these two protocols)
#
# The build links no external library (--disable-autodetect) and enables only
# ffmpeg's own components; the verify step below checks the resulting
# configuration before the binary is written.
#
# Usage:
#   bash scripts/build-ffmpeg.sh                         # host build -> ~/ffmpeg/ffmpeg
#   bash scripts/build-ffmpeg.sh --target <key> --out <file>
#
# <key> is one of: linux-x86_64 linux-aarch64 macos-aarch64 windows-x86_64,
# the platforms stt.decoder pins (the macOS Intel backend ships no decoder).
# Linux targets and macos-aarch64 build natively on a matching host;
# windows-x86_64 cross-compiles from
# Linux with mingw-w64.
#
# Prerequisites: a C compiler, make, diffutils; nasm for x86_64 targets; a
# static libc for a Linux --target (libc6-dev on Ubuntu, glibc-static on AL2023).
#   AL2023:  sudo dnf install -y gcc make nasm diffutils
#   Ubuntu:  sudo apt install -y gcc make nasm   (+ gcc-mingw-w64-x86-64 for Windows)
#   macOS:   xcode-select --install && brew install nasm

set -euo pipefail

FFMPEG_VERSION="7.1.1"
FFMPEG_SHA256="733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
MACOS_MIN_VERSION="11.0"

TARGET=""
OUT=""
SOURCE_OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    # Copy the verified source tarball here, so the exact source of the
    # binaries can be published beside them.
    --source-out) SOURCE_OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TARGET" ]; then
  PREFIX="${PREFIX:-$HOME/ffmpeg}"
  OUT="$PREFIX/ffmpeg"
elif [ -z "$OUT" ]; then
  echo "--target needs --out" >&2
  exit 2
fi

# The build later cd's into a temp dir that is deleted on exit, so anchor
# relative output paths to the caller's directory first.
abs_path() {
  case "$1" in
    "" | /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$PWD" "$1" ;;
  esac
}
OUT=$(abs_path "$OUT")
SOURCE_OUT=$(abs_path "$SOURCE_OUT")

WORKDIR=$(mktemp -d)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

sha256_check() {
  if command -v sha256sum >/dev/null 2>&1; then
    echo "$1  $2" | sha256sum -c -
  else
    echo "$1  $2" | shasum -a 256 -c -
  fi
}

echo "==> Downloading ffmpeg ${FFMPEG_VERSION} source..."
curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" -o "$WORKDIR/ffmpeg.tar.xz"
echo "==> Verifying SHA256 checksum..."
(cd "$WORKDIR" && sha256_check "$FFMPEG_SHA256" ffmpeg.tar.xz)
if [ -n "$SOURCE_OUT" ]; then
  mkdir -p "$(dirname "$SOURCE_OUT")"
  cp "$WORKDIR/ffmpeg.tar.xz" "$SOURCE_OUT"
fi
tar -xf "$WORKDIR/ffmpeg.tar.xz" -C "$WORKDIR"
cd "$WORKDIR/ffmpeg-${FFMPEG_VERSION}"

# Per-target toolchain flags. Hardening flags are applied where the toolchain
# supports them; the feature set below is identical on every target.
CFLAGS="-fstack-protector-strong -D_FORTIFY_SOURCE=2"
LDFLAGS=""
TOOLCHAIN=()
EXE_NAME="ffmpeg"
case "$TARGET" in
  "")
    [ "$(uname)" = "Linux" ] && LDFLAGS="-Wl,-z,relro,-z,now"
    ;;
  linux-x86_64|linux-aarch64)
    want="${TARGET#linux-}"
    if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "$want" ]; then
      echo "$TARGET builds natively on a Linux $want host" >&2
      exit 2
    fi
    # Fully static, so the binary runs on any glibc rather than only on hosts
    # at least as new as the build machine (a dynamic ubuntu-22.04 build needs
    # glibc 2.35 and fails on Amazon Linux 2023 and RHEL 9). Safe for this
    # build: --disable-network means no NSS lookups and nothing is dlopen'd.
    LDFLAGS="-static -Wl,-z,relro,-z,now"
    ;;
  macos-aarch64)
    # Native only: configure and the verify step below run the built binary.
    if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
      echo "$TARGET builds on an Apple-silicon Mac" >&2
      exit 2
    fi
    CFLAGS="$CFLAGS -arch arm64 -mmacosx-version-min=$MACOS_MIN_VERSION"
    LDFLAGS="-arch arm64 -mmacosx-version-min=$MACOS_MIN_VERSION"
    ;;
  windows-x86_64)
    [ "$(uname -s)" = "Linux" ] || { echo "$TARGET cross-compiles from Linux" >&2; exit 2; }
    # -static folds libgcc/libwinpthread in, so the .exe needs no MinGW DLL
    # beside it. ffmpeg itself uses native Win32 threads on this target.
    # -fstack-protector-strong at link time pulls in libssp, which mingw
    # needs for the stack-protector symbols the hardened CFLAGS emit.
    LDFLAGS="-static -fstack-protector-strong"
    TOOLCHAIN=(--enable-cross-compile --target-os=mingw32 --arch=x86_64
      --cross-prefix=x86_64-w64-mingw32-)
    EXE_NAME="ffmpeg.exe"
    ;;
  *)
    echo "unknown target: $TARGET" >&2
    exit 2
    ;;
esac

echo "==> Configuring minimal LGPL audio-decode build (${TARGET:-host})..."
# `${arr[@]+...}` keeps an empty array legal under `set -u` on macOS's bash 3.2.
./configure \
  ${TOOLCHAIN[@]+"${TOOLCHAIN[@]}"} \
  --disable-doc \
  --disable-htmlpages \
  --disable-manpages \
  --disable-podpages \
  --disable-txtpages \
  --disable-network \
  --disable-autodetect \
  --disable-everything \
  --disable-ffplay \
  --disable-ffprobe \
  --disable-shared \
  --enable-static \
  --enable-small \
  --enable-protocol=file,pipe \
  --enable-demuxer=wav,mp3,mov,ogg,flac,matroska,aac,asf \
  --enable-decoder=aac,aac_latm,mp3,mp3float,opus,vorbis,flac,alac,wmav1,wmav2,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_u8,pcm_alaw,pcm_mulaw \
  --enable-parser=aac,aac_latm,mpegaudio,opus,vorbis,flac \
  --enable-encoder=pcm_s16le \
  --enable-muxer=wav,ogg,null \
  --enable-filter=aresample,aformat,anull,atrim,silencedetect \
  --extra-cflags="$CFLAGS" \
  ${LDFLAGS:+--extra-ldflags="$LDFLAGS"}

echo "==> Building (this takes 1-3 minutes)..."
# Build the target-specific name: on mingw the only program rule is ffmpeg.exe.
make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)" "$EXE_NAME"

# Never ship or install a binary whose configuration drifted from the one
# above. This catches a future edit that enables a component configure would
# only build under different terms.
echo "==> Verifying license..."
if ! grep -q '^#define FFMPEG_CONFIGURATION ' config.h; then
  echo "    ✗ config.h has no FFMPEG_CONFIGURATION" >&2
  exit 1
fi
if grep -E '^#define CONFIG_(GPL|GPLV3|NONFREE|VERSION3) 1' config.h; then
  echo "    ✗ build is not LGPL-2.1-or-later" >&2
  exit 1
fi
grep -q '^#define FFMPEG_LICENSE "LGPL version 2.1 or later"' config.h \
  || { echo "    ✗ FFMPEG_LICENSE is not LGPL version 2.1 or later" >&2; exit 1; }
echo "    ✓ LGPL version 2.1 or later"

mkdir -p "$(dirname "$OUT")"
cp "$EXE_NAME" "$OUT"
chmod +x "$OUT"

# Only the Windows cross build cannot run here; verify-windows runs it.
if [ "$TARGET" != "windows-x86_64" ]; then
  echo "==> Verifying..."
  version=$("$OUT" -hide_banner -version)
  echo "${version%%$'\n'*}"
  # Capture first, then match: piping into `grep -q` lets the writer die of
  # SIGPIPE, which pipefail would report as a failed check. `-L` wraps its
  # text, so whitespace is collapsed before matching the phrase.
  license_text=$("$OUT" -hide_banner -L | tr -s '\r\n ' '   ')
  case "$license_text" in
    *"GNU Lesser General Public License"*) echo "    ✓ binary reports LGPL" ;;
    *) echo "    ✗ binary does not report LGPL" >&2; exit 1 ;;
  esac
  # A Linux target must stay static: a program interpreter means the binary
  # needs the build host's glibc or newer.
  if [[ "$TARGET" == linux-* ]]; then
    headers=$(readelf -l "$OUT")
    case "$headers" in
      *"program interpreter"*) echo "    ✗ binary is dynamically linked" >&2; exit 1 ;;
      *) echo "    ✓ statically linked" ;;
    esac
  fi
  decoders=$("$OUT" -hide_banner -decoders 2>/dev/null)
  for dec in aac opus vorbis mp3 flac; do
    # A here-string, not a pipe, so grep -q exiting early cannot SIGPIPE a writer.
    if grep -qE "^ A[^ ]* ${dec} " <<<"$decoders"; then
      echo "    ✓ ${dec} decoder present"
    else
      echo "    ✗ ${dec} decoder MISSING" >&2
      exit 1
    fi
  done
fi

echo ""
echo "Done. ffmpeg written to: $OUT"
if [ -z "$TARGET" ]; then
  # The gateway only searches system and package-manager directories.
  echo "For the gateway to find it:  sudo install -m 0755 \"$OUT\" /usr/local/bin/ffmpeg"
fi
