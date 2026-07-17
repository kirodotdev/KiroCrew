#!/usr/bin/env bash
# Build a minimal ffmpeg from source with only the audio decoders whisper needs.
# Produces a static binary at ~/ffmpeg/ffmpeg (~4MB vs ~80MB kitchen-sink builds).
#
# Whisper calls: ffmpeg -i <file> -f s16le -ac 1 -ar 16000 pipe:1
# So we only need: decoders (aac, opus, vorbis, mp3, pcm_s16le, flac)
#                  demuxers (mov/mp4, matroska/webm, ogg, mp3, wav, flac)
#                  output   (pcm_s16le encoder, s16le/pipe muxer)
#
# License: LGPLv2.1 (no --enable-gpl, no --enable-nonfree)
# Per Amazon Open Source guidance: https://w.amazon.com/bin/view/Open_Source/Package_Guidance/FFmpeg/
#
# Usage: bash scripts/build-ffmpeg.sh
# Prerequisites: gcc, make, nasm/yasm (for x86 asm optimizations)
#   AL2023:  sudo dnf install -y gcc make nasm diffutils
#   Ubuntu:  sudo apt install -y gcc make nasm
#   macOS:   xcode-select --install && brew install nasm

set -euo pipefail

FFMPEG_VERSION="7.1.1"
FFMPEG_SHA256="733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
PREFIX="${PREFIX:-$HOME/ffmpeg}"
WORKDIR=$(mktemp -d)

cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "==> Downloading ffmpeg ${FFMPEG_VERSION} source..."
cd "$WORKDIR"
curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" -o ffmpeg.tar.xz

echo "==> Verifying SHA256 checksum..."
if command -v sha256sum >/dev/null 2>&1; then
  echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c -
else
  echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | shasum -a 256 -c -
fi
tar xf ffmpeg.tar.xz
cd "ffmpeg-${FFMPEG_VERSION}"

echo "==> Configuring minimal audio-decode-only build..."
EXTRA_LDFLAGS=""
if [ "$(uname)" = "Linux" ]; then
  EXTRA_LDFLAGS="-Wl,-z,relro,-z,now"
fi
./configure \
  --prefix="$PREFIX" \
  --disable-doc \
  --disable-htmlpages \
  --disable-manpages \
  --disable-podpages \
  --disable-txtpages \
  --disable-network \
  --disable-autodetect \
  --disable-everything \
  --disable-shared \
  --enable-static \
  --enable-small \
  --enable-protocol=file,pipe \
  --enable-filter=aresample \
  --enable-decoder=aac,opus,vorbis,mp3,mp3float,flac,pcm_s16le,pcm_s16be,pcm_f32le \
  --enable-demuxer=mov,matroska,ogg,mp3,wav,flac,aac,m4v \
  --enable-encoder=pcm_s16le \
  --enable-muxer=pcm_s16le,null \
  --enable-parser=aac,opus,vorbis,mpegaudio,flac \
  --extra-cflags="-fstack-protector-strong -D_FORTIFY_SOURCE=2" \
  ${EXTRA_LDFLAGS:+--extra-ldflags="$EXTRA_LDFLAGS"}

echo "==> Building (this takes 1-3 minutes)..."
make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"

echo "==> Installing to ${PREFIX}..."
mkdir -p "$PREFIX"
cp ffmpeg "$PREFIX/ffmpeg"
chmod +x "$PREFIX/ffmpeg"

echo "==> Verifying..."
"$PREFIX/ffmpeg" -version
echo ""
echo "==> Checking AAC decoder..."
if "$PREFIX/ffmpeg" -decoders 2>/dev/null | grep -q "aac"; then
  echo "    ✓ AAC decoder present"
else
  echo "    ✗ AAC decoder MISSING"
  exit 1
fi

echo ""
echo "Done. ffmpeg installed to: $PREFIX/ffmpeg"
echo "Add to PATH:  export PATH=\"$PREFIX:\$PATH\""
echo ""
echo "KiroCrew will auto-detect ~/ffmpeg/ffmpeg — no PATH change needed for the gateway."
