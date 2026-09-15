#!/usr/bin/env bash
# Turn a Kiro Crew crash artifact into readable frames.
#
# The problem this closes: a macOS `.ips` report from a shipped build has
# `asi: null` and every frame symbol is a NEAREST-NEIGHBOUR GUESS, because the
# Electron Framework in a release build is stripped. Read literally, such a
# report implicates whatever exported symbol happens to sit below the crash
# address — which is how one main-process crash got filed against a component
# that never appeared in the actual call chain. Symbolizing is not a nicety
# here; unsymbolized, the stack is actively misleading.
#
# What was missing was never really the symbols. Electron publishes them with
# every release and keeps them forever, and a `.ips` even names the exact build
# it needs (`usedImages[].CFBundleShortVersionString` plus a Mach-O `uuid`).
# What was missing was the RECIPE: that these archives exist, which of the two
# formats matches which artifact, that `.ips` frame addresses are image-relative
# and have to be rebased before atos sees them, and that a dSYM from the wrong
# build fails SILENTLY — printing plausible, wrong frames rather than an error.
# This script is that recipe, and the UUID check below is what makes the silent
# failure loud.
#
#   .ips report   -> dSYM (Mach-O DWARF, ~1.4-1.7 GB) -> atos
#   .dmp minidump -> Breakpad .sym (~128-146 MB)      -> minidump_stackwalk
#
# Usage:
#   symbolize-crash.sh <crash-artifact> [--manifest <symbols-manifest.json>]
#                                       [--electron <version>] [--arch arm64|x64]
#
# A `.ips` needs no version argument: the report names its own. A `.dmp` does,
# because a minidump identifies modules by debug ID rather than by semver — pass
# `--manifest` (CI archives one beside each build's artifacts, see
# scripts/emit-symbols-manifest.mjs) or `--electron` if you know the version.
#
# Downloads are cached, because these run 128 MB to 1.7 GB per arch:
#   KIROCREW_SYMBOL_CACHE   default ${XDG_CACHE_HOME:-~/.cache}/kirocrew/electron-symbols
set -euo pipefail

ARTIFACT=""
MANIFEST=""
ELECTRON_VERSION=""
ARCH=""
REPORT_UUID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --manifest)  MANIFEST="${2:?--manifest needs a path}"; shift 2 ;;
    --electron)  ELECTRON_VERSION="${2:?--electron needs a version}"; shift 2 ;;
    --arch)      ARCH="${2:?--arch needs arm64 or x64}"; shift 2 ;;
    -h|--help)   sed -n '2,34p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    -*)          echo "ERROR: unknown option '$1'" >&2; exit 2 ;;
    *)
      if [ -n "$ARTIFACT" ]; then
        echo "ERROR: one crash artifact at a time (got '$ARTIFACT' and '$1')" >&2
        exit 2
      fi
      ARTIFACT="$1"; shift ;;
  esac
done

if [ -z "$ARTIFACT" ]; then
  echo "usage: symbolize-crash.sh <crash-artifact> [--manifest <file>] [--electron <version>] [--arch arm64|x64]" >&2
  exit 2
fi
if [ ! -f "$ARTIFACT" ]; then
  echo "ERROR: no such crash artifact: $ARTIFACT" >&2
  exit 1
fi

# Picked by extension rather than asked for: the artifact already says which it
# is, and the two formats are not interchangeable inputs.
case "$ARTIFACT" in
  *.ips)  KIND="ips" ;;
  *.dmp)  KIND="dmp" ;;
  *)
    echo "ERROR: unrecognized artifact '$ARTIFACT' (expected a .ips report or a .dmp minidump)" >&2
    exit 1 ;;
esac

CACHE="${KIROCREW_SYMBOL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/kirocrew/electron-symbols}"
WORK="$(mktemp -d)"
# Set early and empty so the trap below can name it under `set -u`. The extraction
# staging directory has to live inside $CACHE rather than in $WORK, because it is
# renamed into place and `mv` is only atomic within one filesystem.
STAGE=""
trap 'rm -rf "$WORK" ${STAGE:+"$STAGE"}' EXIT

# --- Which Electron build, and which slice ----------------------------------
#
# Never the locally installed Electron: that is whatever this workspace happens
# to have resolved, and has nothing to do with the build that crashed.

# Parsed for every .ips, INCLUDING when --electron already named a version. The
# three fields answer three different questions and only one of them is the
# version: the uuid is what the dwarfdump gate below compares the downloaded
# dSYM against, and the arch names the slice the crashed process was running.
# Gating this parse on an empty $ELECTRON_VERSION meant `--electron` left both
# unset, which silently disabled that gate -- the one check that makes a
# wrong-build dSYM fail loudly instead of printing plausible, wrong frames,
# which is the failure this whole script exists to end. An explicit version is
# an assertion about WHICH build to fetch; it is not evidence that the symbols
# fetched match the report in hand.
if [ "$KIND" = "ips" ]; then
  # The report's own record of its framework image. Preferred over the manifest
  # because it describes THIS crash rather than a build that may not be the one
  # the reporter was running.
  read -r REPORT_VERSION REPORT_UUID REPORT_ARCH <<EOF || true
$(node -e '
const fs = require("fs");
const text = fs.readFileSync(process.argv[1], "utf8").split("\n");
const payload = JSON.parse(text.slice(1).join("\n"));
const image = (payload.usedImages || [])
  .find(i => /Electron Framework/.test(`${i.name || ""} ${i.path || ""}`));
if (!image) process.exit(0);
process.stdout.write([
  image.CFBundleShortVersionString || "",
  image.uuid || "",
  image.arch || "",
].join(" "));
' "$ARTIFACT")
EOF
  # The report also names the slice the crashed process was running, which beats
  # guessing from this host: a universal app can crash in either. Applied
  # regardless of where the version came from -- an explicit --electron says
  # nothing about which slice, so deriving arch from the report is strictly
  # better than the host fallback further down.
  if [ -z "$ARCH" ]; then
    case "${REPORT_ARCH:-}" in
      arm64*) ARCH="arm64" ;;
      x86_64) ARCH="x64" ;;
    esac
  fi
  if [ -n "${REPORT_VERSION:-}" ]; then
    if [ -z "$ELECTRON_VERSION" ]; then
      ELECTRON_VERSION="$REPORT_VERSION"
      echo "Report names Electron $ELECTRON_VERSION (framework uuid ${REPORT_UUID:-unknown})"
    elif [ "$ELECTRON_VERSION" != "$REPORT_VERSION" ]; then
      # Not an error: overriding is legitimate when a report's version string is
      # missing or known-wrong. Said out loud because the uuid gate below is now
      # the thing standing between this override and a wrong-symbol stack, and an
      # operator who mistyped the version should learn it here rather than from a
      # confusing stack.
      echo "NOTE: --electron $ELECTRON_VERSION overrides the version this report names ($REPORT_VERSION)."
      echo "      Fetching $ELECTRON_VERSION; the uuid gate still checks the dSYM against the report."
    fi
  fi
fi

if [ -z "$ELECTRON_VERSION" ]; then
  if [ -z "$MANIFEST" ]; then
    # CI uploads the manifest beside the build's artifacts, so a downloaded run
    # directory usually already holds one.
    for candidate in \
      "$(dirname "$ARTIFACT")/symbols-manifest.json" \
      "website/electron/dist/symbols-manifest.json"; do
      if [ -f "$candidate" ]; then MANIFEST="$candidate"; break; fi
    done
  fi
  if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
    echo "ERROR: no Electron version to symbolize against." >&2
    echo "       A .dmp identifies its modules by debug ID, not by version, so the" >&2
    echo "       version has to come from the build: pass --manifest with the" >&2
    echo "       symbols-manifest.json from that build's CI artifact, or --electron" >&2
    echo "       if you know it. Guessing is not an option — the wrong symbols do" >&2
    echo "       not fail loudly, they print plausible, wrong frames." >&2
    exit 1
  fi
  ELECTRON_VERSION="$(node -e '
    const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
    process.stdout.write(String((m.electron && m.electron.version) || ""));
  ' "$MANIFEST")"
  if [ -z "$ELECTRON_VERSION" ]; then
    echo "ERROR: $MANIFEST has no electron.version" >&2
    exit 1
  fi
  echo "Manifest: $MANIFEST (electron $ELECTRON_VERSION)"
fi

# The manifest declares its own shape, so read that declaration rather than
# assuming it. The emitter stamps `schema` for exactly this reason, and a field
# nothing checks is a field that can be wrong for a year without anyone noticing:
# a future schema 2 that moves `assets[].url` would arrive here as "lists no
# breakpad asset for arch arm64", which sends the reader looking for a build
# problem that does not exist. Refuse instead, and say which script is behind.
if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  MANIFEST_SCHEMA="$(node -e '
    const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
    process.stdout.write(String(m.schema === undefined ? "" : m.schema));
  ' "$MANIFEST")"
  if [ "$MANIFEST_SCHEMA" != "1" ]; then
    echo "ERROR: $MANIFEST declares schema '$MANIFEST_SCHEMA', not 1." >&2
    echo "       This script reads schema 1 (electron.version + assets[] with" >&2
    echo "       kind/arch/name/url). Update it alongside" >&2
    echo "       scripts/emit-symbols-manifest.mjs, which writes the schema." >&2
    exit 1
  fi
fi

if [ -z "$ARCH" ]; then
  case "$(uname -m)" in
    arm64|aarch64) ARCH="arm64" ;;
    *)             ARCH="x64" ;;
  esac
  echo "Arch: $ARCH (from this host; pass --arch for a report from the other slice)"
fi

# Which asset, from the manifest when there is one.
#
# This used to build the name with `darwin` hardcoded, which was silently wrong
# for exactly the case the manifest exists to serve: a Linux build records a
# Breakpad entry, and reconstructing the name here asked for
# `…-darwin-x64-symbols.zip` instead. Breakpad trees are keyed by debug ID, so
# the mismatch does not error — `minidump_stackwalk` just finds no symbols and
# the report reads as "no symbols cover these frames", which is the wrong
# conclusion. The manifest already carries the right `name` and `url` per
# (kind, arch); read them instead of re-deriving them.
if [ "$KIND" = "ips" ]; then WANT_KIND="dsym"; else WANT_KIND="breakpad"; fi

ASSET=""
URL=""
if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  # Name then URL, both newline-terminated, or no output when nothing matches.
  # `|| true` because the second `read` reports EOF on an empty match, which is a
  # normal outcome handled just below, not a failure `set -e` should abort on.
  { read -r ASSET; read -r URL; } <<EOF || true
$(node -e '
  const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
  const hit = (m.assets || []).find(
    (a) => a.kind === process.argv[2] && a.arch === process.argv[3]);
  if (hit) process.stdout.write(`${hit.name}\n${hit.url}\n`);
' "$MANIFEST" "$WANT_KIND" "$ARCH")
EOF
  if [ -z "$ASSET" ] || [ -z "$URL" ]; then
    # Fail loudly rather than falling back to a guess. The manifest lists what
    # this build actually has; if it has no matching entry, the answer is that
    # these symbols were not published for that slice, not that we should try a
    # plausible name.
    PLATFORM="$(node -e '
      const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
      process.stdout.write(String(m.platform || "?"));
    ' "$MANIFEST")"
    echo "ERROR: $MANIFEST lists no $WANT_KIND asset for arch $ARCH." >&2
    echo "       That manifest is for platform '$PLATFORM'; its assets are:" >&2
    node -e '
      const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
      for (const a of m.assets || []) process.stdout.write(`         ${a.kind} ${a.arch} ${a.name}\n`);
    ' "$MANIFEST" >&2
    echo "       Pass --arch for the other slice, or use the manifest from the" >&2
    echo "       build that produced this report." >&2
    exit 1
  fi
  echo "Asset: $ASSET (from manifest, kind=$WANT_KIND arch=$ARCH)"
else
  # No manifest, so the version came from either an explicit --electron or the
  # report's own `usedImages` entry — a `.dmp` cannot reach here without the
  # former, having already exited above. Derive the platform from this host
  # rather than assuming macOS, so a Linux operator gets a Linux asset name.
  case "$(uname -s)" in
    Darwin) PLATFORM="darwin" ;;
    Linux)  PLATFORM="linux" ;;
    *)      PLATFORM="win32" ;;
  esac
  if [ "$KIND" = "ips" ] && [ "$PLATFORM" != "darwin" ]; then
    echo "ERROR: a .ips report is a macOS artifact, but this host is $PLATFORM." >&2
    echo "       Pass --manifest from the macOS build that produced it." >&2
    exit 1
  fi
  if [ "$WANT_KIND" = "dsym" ]; then
    ASSET="electron-v${ELECTRON_VERSION}-${PLATFORM}-${ARCH}-dsym.tar.xz"
  else
    ASSET="electron-v${ELECTRON_VERSION}-${PLATFORM}-${ARCH}-symbols.zip"
  fi
  URL="https://github.com/electron/electron/releases/download/v${ELECTRON_VERSION}/${ASSET}"
  echo "Asset: $ASSET (no manifest; platform $PLATFORM from this host)"
fi

# --- The asset name and URL are untrusted input ------------------------------
#
# Both branches above produce $ASSET, which becomes a FILESYSTEM PATH two lines
# down (`$CACHE/$ASSET`, written by curl and then by mv), and $URL, which is
# fetched. Neither value is this script's own: the manifest branch reads them out
# of a JSON file that arrived as a CI artifact download, and the no-manifest
# branch interpolates --electron and --arch straight from argv. A name of
# `../../../../etc/something` puts the download outside the cache and lets a
# chosen URL's bytes land on a writable host file; the extraction step then
# unpacks an attacker-chosen archive from there.
#
# So both are checked against what the honest emitter can produce, and the check
# is EXACT rather than sanitizing. A repaired path is still a manifest that lied,
# and there is no legitimate caller whose value needs repairing:
# scripts/emit-symbols-manifest.mjs derives every `name` and `url` from
# (platform, arch, version) by one formula, so exact matching loses nothing.
case "$ELECTRON_VERSION" in
  # Electron's own release versions, and nothing that could reshape the URL.
  *[!0-9A-Za-z.+-]*|"")
    echo "ERROR: implausible Electron version '$ELECTRON_VERSION'." >&2
    echo "       Expected a release version like 43.2.0. This came from the" >&2
    echo "       crash report, the manifest, or --electron; whichever it was is" >&2
    echo "       not describing a real Electron release." >&2
    exit 1 ;;
esac

ASSET_OK=0
case "$ASSET" in
  # CHARSET FIRST, and the same allowlist the version above uses. The two shape
  # patterns below are globs, so their `*` matches a path separator, a space, `;`,
  # and `$(...)` alike — and $ASSET does not merely become a path. It is
  # interpolated into a `gh api --jq` PROGRAM below, whose output becomes $SIZE,
  # which then reaches `$(( ))`; arithmetic expansion re-evaluates its operand, so
  # a manifest that smuggles a command substitution through this gate gets it run.
  # Bounding the characters closes that whole route rather than the one hop.
  *[!0-9A-Za-z.+-]*|"") ;;
  # A path separator, in a value that is about to be one path COMPONENT. Redundant
  # after the charset rule and kept anyway: it is the property most obviously
  # required of this value, and a later widening of the charset must not silently
  # take it with it.
  */*|*'\'*) ;;
  electron-v*-symbols.zip) ASSET_OK=1 ;;
  electron-v*-dsym.tar.xz) ASSET_OK=1 ;;
esac
if [ "$ASSET_OK" -ne 1 ]; then
  echo "ERROR: refusing asset name '$ASSET'." >&2
  echo "       A symbol asset is a BARE FILENAME of the form" >&2
  echo "       electron-v<version>-<platform>-<arch>-symbols.zip (or -dsym.tar.xz)." >&2
  echo "       built from [0-9A-Za-z.+-] only. This is not one, so it carries a" >&2
  echo "       character no honest emitter produces (a path separator would place" >&2
  echo "       the download outside $CACHE; a space, quote or \$ would reach a" >&2
  echo "       command line), or it names an archive shape this script cannot" >&2
  echo "       extract. Either way it is rejected rather than trimmed into" >&2
  echo "       something plausible." >&2
  if [ -n "$MANIFEST" ]; then
    echo "       Source: $MANIFEST — that manifest is not one this build wrote." >&2
  fi
  exit 1
fi

EXPECT_URL="https://github.com/electron/electron/releases/download/v${ELECTRON_VERSION}/${ASSET}"
if [ "$URL" != "$EXPECT_URL" ]; then
  echo "ERROR: refusing download URL for $ASSET." >&2
  echo "       expected: $EXPECT_URL" >&2
  echo "       manifest: $URL" >&2
  echo "       Symbols come from electron/electron's own releases and nowhere" >&2
  echo "       else. A manifest pointing elsewhere is either stale or forged;" >&2
  echo "       either way these are not the symbols for this build." >&2
  exit 1
fi

# --- Fetch, once ------------------------------------------------------------
mkdir -p "$CACHE"
DOWNLOAD="$CACHE/$ASSET"
EXTRACTED="$CACHE/${ASSET%.tar.xz}"
EXTRACTED="${EXTRACTED%.zip}"

if [ -d "$EXTRACTED" ]; then
  echo "Symbols: $EXTRACTED (cached)"
else
  if [ ! -f "$DOWNLOAD" ]; then
    # Announce the size FIRST. A dSYM is well over a gigabyte, and a tool should
    # say that before it starts rather than after.
    if command -v gh >/dev/null 2>&1; then
      SIZE="$(gh api "repos/electron/electron/releases/tags/v${ELECTRON_VERSION}" \
        --jq ".assets[] | select(.name == \"${ASSET}\") | .size" 2>/dev/null || true)"
    else
      SIZE=""
    fi
    # DIGITS ONLY, because the next line is arithmetic expansion, and `$(( ))`
    # re-evaluates its operand: a $SIZE of `x[$(cmd)]` runs cmd. That is not a
    # theoretical shape for a value fetched from the network by a program built
    # from $ASSET — nor is a `.size` of `null`, which is what a real release
    # missing this asset returns and what would otherwise be printed as a size.
    # Anything that is not a plain count of bytes is discarded, not repaired: the
    # size is a courtesy line, so losing it costs nothing.
    case "$SIZE" in
      "" | *[!0-9]*) SIZE="" ;;
    esac
    if [ -n "$SIZE" ]; then
      echo "Downloading $ASSET ($(( SIZE / 1024 / 1024 )) MB)…"
    else
      echo "Downloading $ASSET…"
    fi
    # Write to a temp name and move on success only: a truncated archive would
    # extract to MISSING symbols rather than to an error.
    curl -fL --progress-bar -o "$DOWNLOAD.part" "$URL"
    mv "$DOWNLOAD.part" "$DOWNLOAD"
  fi
  # Extract into a staging directory and rename it into place only once the
  # extractor has succeeded, for the same reason the download uses `.part`:
  # `[ -d "$EXTRACTED" ]` above is the ONLY cache check, so a directory that
  # exists is treated as complete forever. Extracting in place made every
  # interruption permanent — Ctrl-C, a full disk, or a killed unzip left a partial
  # tree that no later run would ever repair, and the symbolizer would then report
  # missing frames rather than "no symbols", which reads as a stripped build
  # rather than as a broken cache. `mv` of a directory within one filesystem is
  # atomic, and the staging path sits inside $CACHE so it always is one. The EXIT
  # trap removes the staging tree, so an abort leaves the cache as it was rather
  # than littering it with half-unpacked directories.
  STAGE="$EXTRACTED.partial.$$"
  rm -rf "$STAGE"
  echo "Extracting into $EXTRACTED…"
  mkdir -p "$STAGE"
  case "$ASSET" in
    *.tar.xz) tar -xJf "$DOWNLOAD" -C "$STAGE" ;;
    *.zip)    unzip -q "$DOWNLOAD" -d "$STAGE" ;;
  esac
  mv "$STAGE" "$EXTRACTED"
  STAGE=""
fi

# --- Crashpad minidump ------------------------------------------------------
if [ "$KIND" = "dmp" ]; then
  # No UUID cross-check needed on this path: the Breakpad tree is keyed BY debug
  # ID, so a mismatched download yields "no symbols" rather than wrong ones.
  if command -v minidump_stackwalk >/dev/null 2>&1; then
    STACKWALK="minidump_stackwalk"
  elif command -v minidump-stackwalk >/dev/null 2>&1; then
    STACKWALK="minidump-stackwalk"
  else
    echo "ERROR: no minidump stackwalker found. Install one with:" >&2
    echo "       brew install rust-minidump      # provides minidump-stackwalk" >&2
    echo "       (then re-run; the symbols stay cached in $CACHE)" >&2
    exit 1
  fi
  echo
  exec "$STACKWALK" "$ARTIFACT" "$EXTRACTED"
fi

# --- macOS .ips report ------------------------------------------------------
if ! command -v atos >/dev/null 2>&1; then
  echo "ERROR: atos not found (it ships with the Xcode command line tools):" >&2
  echo "       xcode-select --install" >&2
  exit 1
fi

DWARF="$(find "$EXTRACTED" -type f \
  -path "*Electron Framework.dSYM/Contents/Resources/DWARF/*" -print -quit)"
if [ -z "$DWARF" ]; then
  echo "ERROR: no Electron Framework DWARF binary under $EXTRACTED" >&2
  echo "       The archive layout may have changed; list it and look for the .dSYM:" >&2
  echo "       find '$EXTRACTED' -maxdepth 3 -name '*.dSYM'" >&2
  exit 1
fi
echo "DWARF: $DWARF"

# The check that makes a wrong dSYM loud. Without it, symbolizing against a
# neighbouring Electron release succeeds and prints a stack that looks right and
# is not — the exact failure mode this whole script exists to end.
#
# Three branches, and NONE of them may be silent. A skipped check that prints
# nothing is indistinguishable from a check that passed, which is how a wrong
# stack gets believed:
#   1. uuid present + dwarfdump present -> compare, and exit 1 on a mismatch.
#   2. no uuid in the report            -> say so; the report is hand-edited,
#      truncated, or names no Electron Framework image at all.
#   3. dwarfdump missing                -> say so; nothing here can verify the
#      dSYM, so every frame below is unverified.
if [ -z "$REPORT_UUID" ]; then
  echo "WARNING: this report carries no framework uuid, so the dSYM could not be" >&2
  echo "         checked against it. Frames below are UNVERIFIED -- a dSYM from a" >&2
  echo "         neighbouring Electron release would symbolize without complaint." >&2
elif ! command -v dwarfdump >/dev/null 2>&1; then
  echo "WARNING: dwarfdump not found, so the dSYM could not be checked against the" >&2
  echo "         report's framework uuid ($REPORT_UUID). Frames below are" >&2
  echo "         UNVERIFIED. It ships with the Xcode command line tools:" >&2
  echo "         xcode-select --install" >&2
else
  DSYM_UUIDS="$(dwarfdump --uuid "$DWARF" 2>/dev/null || true)"
  # Compared case-insensitively without dashes: the .ips spells it lowercase
  # with dashes, dwarfdump uppercase with dashes.
  NORM_REPORT="$(printf '%s' "$REPORT_UUID" | tr -d '-' | tr '[:upper:]' '[:lower:]')"

  # The report uuid must be a FULL 128-bit uuid — exactly 32 hex digits once the
  # dashes are gone. A real macOS `.ips` always carries the whole 36-char value;
  # a shorter one is a hand-edited or mangled report. It must NOT reach the
  # comparison below, because a partial value could match the middle of an
  # unrelated dSYM uuid and print a false ✓ — a plausible, WRONG stack is the
  # single outcome this whole check exists to prevent.
  if ! printf '%s' "$NORM_REPORT" | grep -qiE '^[0-9a-f]{32}$'; then
    echo "ERROR: the crash report's framework uuid is not a full 128-bit UUID" >&2
    echo "       (got '$REPORT_UUID'). A partial uuid cannot verify the dSYM, and" >&2
    echo "       symbolizing against an unverified dSYM would print plausible," >&2
    echo "       WRONG frames. Re-export the report so it carries the full uuid." >&2
    exit 1
  fi

  # Compare for EQUALITY against each uuid dwarfdump actually PARSED, one per
  # arch line ("UUID: <uuid> (<arch>) <path>"), never as a substring of the
  # whole dwarfdump blob: a substring test is what let a short value match part
  # of an unrelated uuid.
  match=""
  for dsym_uuid in $(printf '%s\n' "$DSYM_UUIDS" | awk 'tolower($1) == "uuid:" { print $2 }'); do
    norm_dsym="$(printf '%s' "$dsym_uuid" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
    if [ "$norm_dsym" = "$NORM_REPORT" ]; then
      match="yes"
      break
    fi
  done
  if [ -n "$match" ]; then
    echo "UUID: matches the report's framework image ✓"
  else
    echo "ERROR: dSYM UUID does not match the crash report's framework image." >&2
    echo "       report: $REPORT_UUID" >&2
    echo "       dSYM:   $DSYM_UUIDS" >&2
    echo "       Symbolizing anyway would print plausible, WRONG frames. If the" >&2
    echo "       report came from a different slice, pass --arch; if from a" >&2
    echo "       different build, pass --electron <that version>." >&2
    exit 1
  fi
fi
echo

# Frames from images we hold no symbols for are printed as-is rather than
# dropped: knowing a frame was in the app's own binary, or in a system library,
# is itself part of reading the report. Only the Electron Framework group goes to
# atos, which takes exactly one -l load address per run.
node -e '
const fs = require("fs");
const [reportPath, argsPath] = process.argv.slice(1);
// A .ips is JSON-lines: a small header object on line 1, the payload after it.
const text = fs.readFileSync(reportPath, "utf8").split("\n");
const payload = JSON.parse(text.slice(1).join("\n"));
const images = payload.usedImages || [];

const groups = new Map();
for (const [tIndex, thread] of (payload.threads || []).entries()) {
  for (const [fIndex, frame] of (thread.frames || []).entries()) {
    const image = images[frame.imageIndex];
    if (!image) continue;
    if (!groups.has(frame.imageIndex)) groups.set(frame.imageIndex, { image, frames: [] });
    // The report stores an offset INTO the image, so every address has to be
    // rebased onto that image load address before it means anything.
    groups.get(frame.imageIndex).frames.push({
      slot: `${tIndex}.${fIndex}`,
      address: "0x" + (BigInt(image.base) + BigInt(frame.imageOffset)).toString(16),
    });
  }
}

console.log(`Faulting thread: ${payload.faultingThread}`);
if (payload.exception) console.log(`Exception: ${JSON.stringify(payload.exception)}`);
console.log("");

let electron = null;
for (const [index, group] of groups) {
  const name = group.image.name || "(anonymous)";
  console.log(`# image ${index}: ${name}  base=${group.image.base}`);
  if (group.image.path) console.log(`#   ${group.image.path}`);
  console.log(`#   thread.frame ${group.frames.map(f => f.slot).join(" ")}`);
  console.log(group.frames.map(f => f.address).join(" "));
  console.log("");
  if (/Electron Framework/.test(`${name} ${group.image.path || ""}`)) electron = group;
}

// Two lines, or an empty file when this report has no framework frames at all.
// The caller tests emptiness rather than parsing a sentinel string.
//
// The load address goes out as HEX. A .ips stores `base` as a decimal JSON
// number, and `atos -l` parses its argument as hex unconditionally — so passing
// the decimal digits through silently slides every address out of range and atos
// echoes the inputs back unresolved instead of erroring. That looks exactly like
// "the symbols do not cover these frames", which is the wrong conclusion to
// hand someone reading a crash.
fs.writeFileSync(
  argsPath,
  electron
    ? `0x${BigInt(electron.image.base).toString(16)}\n`
      + `${electron.frames.map(f => f.address).join(" ")}\n`
    : "",
);
' "$ARTIFACT" "$WORK/atos-args"

echo "--- atos (Electron Framework frames) ---"
if [ -s "$WORK/atos-args" ]; then
  { read -r LOAD; read -r ADDRESSES; } < "$WORK/atos-args"
  # shellcheck disable=SC2086  # ADDRESSES is a deliberate list of arguments.
  atos -o "$DWARF" \
    -arch "$([ "$ARCH" = "x64" ] && echo x86_64 || echo arm64)" \
    -l "$LOAD" $ADDRESSES
else
  echo "(no Electron Framework frames in this report)"
fi

echo
echo "Frames from other images are listed above with their rebased addresses."
echo "Symbolize those against their own binaries — the app's own slice lives in"
echo "the .app bundle the report names, and its symbols are NOT in this dSYM."
