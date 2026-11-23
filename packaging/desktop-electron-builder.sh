# shellcheck shell=bash
# The electron-builder call packaging/build-desktop.sh makes, and its retry on
# known transient failures. Sourced by that script, which owns ROOT and runs
# these from the website/electron directory.

# Invoke electron-builder. Split into its own function purely so a test can
# shadow it with a stub -- CSC_IDENTITY_AUTO_DISCOVERY=false disables macOS
# codesign identity auto-discovery for local/CI builds.
_eb_invoke() {
  CSC_IDENTITY_AUTO_DISCOVERY=false ./node_modules/.bin/electron-builder "$@"
}

# Detach the disk image dmgbuild left attached when its eject reported
# "Resource busy" (device read from the log line, e.g. "disk5"). Only a device
# hdiutil still lists is touched. Returns non-zero when the device is still
# attached after hdiutil-detach.sh's retries and force, so the caller fails the
# build instead of retrying beside a held image. Split out so a test of the
# retry loop can shadow it.
_eb_release_busy_dmg() {
  local disk
  # A slice ("disk5s1") is reduced to its whole disk: detach addresses the disk.
  disk="$(grep -oE 'couldn.t (eject|unmount) "disk[0-9]+(s[0-9]+)?"' "$1" | head -n 1 | grep -oE 'disk[0-9]+' | head -n 1 || true)"
  [ -n "$disk" ] || return 0
  (
    # shellcheck source=signing/hdiutil-detach.sh
    . "$ROOT/packaging/signing/hdiutil-detach.sh"
    if dmg_device_attached "/dev/$disk"; then
      sync
      dmg_detach_device "/dev/$disk"
    fi
  )
}

# Run electron-builder (EB_ARGS as "$@"), retrying a bounded number of times
# on three transient failure classes -- and still failing the build on anything
# else, or once the retry budget is exhausted:
#
#   - macOS .DS_Store/ENOTEMPTY temp-dir race (electron-userland/electron-builder#6890):
#     the universal build's lipo-merge stage removes dist/mac-universal-<arch>-temp
#     dirs with a recursive fs.rm that has no retries of its own, and Desktop
#     Services (Finder/Spotlight) can drop a fresh .DS_Store into one mid-removal.
#   - transient network/TLS failures in electron-builder's OWN mid-build
#     fetches: the AppImage and NSIS/Squirrel targets pull their own tooling
#     through `got` AFTER the electron zip has already reported
#     progress=100%. A dropped connection ("socket hang up"), a
#     TLS-intercepted response ("self-signed certificate"), or an HTTP-level
#     answer from the artifact CDN that is retryable rather than final
#     ("Response code 504") aborts the whole build there. These are
#     per-execution network events, not build errors -- the same commit
#     passes on a plain re-run, and one matrix leg can fail while its
#     siblings go green in the same attempt (#3088, #6795).
#   - macOS dmgbuild eject race: dmgbuild's final `hdiutil detach` fails with
#     "couldn't eject "diskN" - Resource busy" when Spotlight or XProtect still
#     hold the freshly written volume (the same race packaging/signing/
#     build-dmg.sh handles for its own mounts). Before the retry the device
#     dmgbuild left behind is detached through hdiutil-detach.sh, which waits
#     with bounded retries and a force fallback, so the next attempt does not
#     mount a second copy beside it. A device that stays attached fails the
#     build without a retry.
#
# Split out of the packaging step (rather than left inline) so the
# retry/classification logic is testable in isolation, mirroring
# rm_rf_resilient in build-desktop.sh: a test extracts this function's body, shadows
# _eb_invoke with a stub that fails N times, and asserts the real retry/
# classification logic here recovers (or correctly gives up).
run_electron_builder_with_retry() {
  local attempt=1 max_attempts=3 eb_log eb_transient eb_backoff
  while : ; do
    eb_log="$(mktemp "${TMPDIR:-/tmp}/kc-eb.XXXXXX")"
    if _eb_invoke "$@" 2>&1 | tee "$eb_log"; then
      rm -f "$eb_log"; return 0
    fi
    # Classify the failure before deciding: each transient class needs its
    # own cleanup, and anything unrecognised must still abort on the first
    # failure.
    eb_transient=""
    if grep -q "ENOTEMPTY" "$eb_log"; then
      eb_transient="ds_store"
    elif grep -qE "hdiutil: couldn.t (eject|unmount) .*Resource busy" "$eb_log"; then
      eb_transient="dmg_busy"
    # Socket-level errno strings and HTTP-level statuses share ONE class
    # because the class boundary here is cleanup, not protocol layer (see the
    # header comment): both arise in the same `got` fetches, and both recover
    # by simply re-fetching on the next attempt with nothing to sweep first.
    # A 5xx is transient by definition and a 429 is a rate limit that clears;
    # every OTHER 4xx (401/403/404/400) is a configuration or authorisation
    # fault that would fail identically on all three attempts, so it stays
    # unrecognised and still aborts on the first one.
    elif grep -qE "socket hang up|self[- ]signed certificate|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND" "$eb_log" \
      || grep -qE "Response code (429|5[0-9][0-9])" "$eb_log"; then
      eb_transient="network"
    fi
    if [ -n "$eb_transient" ] && [ "$attempt" -lt "$max_attempts" ]; then
      if [ "$eb_transient" = "ds_store" ]; then
        echo "  ⚠ macOS .DS_Store/ENOTEMPTY temp-dir race (attempt $attempt/$max_attempts); sweeping .DS_Store and retrying…" >&2
        find dist -name .DS_Store -delete 2>/dev/null || true
        rm -rf dist/*-temp 2>/dev/null || true
        eb_backoff=2
      elif [ "$eb_transient" = "dmg_busy" ]; then
        echo "  ⚠ dmgbuild could not eject its disk image (Resource busy, attempt $attempt/$max_attempts); detaching it and retrying…" >&2
        if ! _eb_release_busy_dmg "$eb_log"; then
          rm -f "$eb_log"
          echo "❌ the disk image dmgbuild left attached could not be detached; not retrying." >&2
          return 1
        fi
        eb_backoff=5
      else
        echo "  ⚠ transient network/TLS/HTTP failure in an electron-builder fetch (attempt $attempt/$max_attempts); retrying…" >&2
        eb_backoff=$((attempt * 10))
      fi
      rm -f "$eb_log"; attempt=$((attempt + 1)); sleep "$eb_backoff"; continue
    fi
    rm -f "$eb_log"
    echo "❌ electron-builder failed (not a known transient class, or retries exhausted)." >&2
    return 1
  done
}
