#!/usr/bin/env bash
# Detach helpers for disk images mounted by hdiutil. Sourced by build-dmg.sh.
#
# The helpers address the device node (/dev/diskN) that `hdiutil attach`
# reports, never the mount path. `hdiutil detach` is two operations -- unmount
# the volume, then eject the device -- and it exits non-zero if EITHER fails.
# When Spotlight or XProtect hold the volume, the unmount can succeed while the
# eject reports "Resource busy": the mount path is gone, the device is still
# attached. A retry addressed by mount path then fails with "No such file or
# directory" on every attempt, force included, and the caller reads a volume
# that is already unmounted as a failure. Addressing the device keeps every
# retry meaningful, and asking hdiutil whether the device is still attached
# (rather than trusting the exit status alone) recognises the eject that
# completed asynchronously after a busy verdict.
#
# Tuning knobs (defaults are production values; drills shorten them):
#   DMG_DETACH_ATTEMPTS         plain detach attempts before -force (5)
#   DMG_DETACH_RETRY_BASE_SECS  sleep after attempt N is N * this many seconds (3)

# Print the whole-disk device node from `hdiutil attach -plist` output.
# Every attached image reports its whole disk plus one dev-entry per slice;
# the whole disk is the node without a slice suffix. Fails loudly rather than
# returning an empty string, because an empty device would turn every detach
# below into a no-op that reports success.
dmg_device_from_attach_plist() {
  local plist="$1" device
  # Newlines are dropped first so the key/value pair matches whether hdiutil
  # prints it on one line or, as it does, indented across two.
  device="$(printf '%s' "$plist" | tr -d '\n' | awk '
    {
      rest = $0
      while (match(rest, /<key>dev-entry<\/key>[[:space:]]*<string>[^<]*<\/string>/)) {
        entry = substr(rest, RSTART, RLENGTH)
        sub(/.*<string>/, "", entry); sub(/<\/string>.*/, "", entry)
        if (entry ~ /^\/dev\/disk[0-9]+$/) { print entry; exit }
        rest = substr(rest, RSTART + RLENGTH)
      }
    }
  ')"
  if [ -z "$device" ]; then
    echo "ERROR: could not find the whole-disk dev-entry in hdiutil attach output" >&2
    printf 'hdiutil said: %s\n' "$plist" >&2
    return 1
  fi
  printf '%s\n' "$device"
}

# Succeed while the device (or any of its slices) is still listed by hdiutil.
# If hdiutil itself cannot answer, assume the device IS still attached: the
# only consequence is another detach attempt, whereas the opposite guess would
# report success over a volume that is still held.
dmg_device_attached() {
  local device="$1" info
  if ! info="$(hdiutil info -plist 2>/dev/null)"; then
    return 0
  fi
  printf '%s\n' "$info" | grep -Eq "<string>${device}(s[0-9]+)?</string>"
}

# Detach the device with bounded retries and a force fallback. Returns 0 as
# soon as the device is no longer attached, however that came about.
#
# Callers flush their writes first (`sync`), so a force on the final attempt
# cannot lose data; the resize/convert/verification stages that follow re-read
# the image and fail loudly on a damaged filesystem.
dmg_detach_device() {
  local device="$1"
  local attempts="${DMG_DETACH_ATTEMPTS:-5}"
  local retry_base="${DMG_DETACH_RETRY_BASE_SECS:-3}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if hdiutil detach "$device"; then
      return 0
    fi
    if ! dmg_device_attached "$device"; then
      echo "NOTE: $device is no longer attached after a failed detach (attempt ${attempt}/${attempts}); treating as detached" >&2
      return 0
    fi
    echo "WARN: detach of $device failed (attempt ${attempt}/${attempts}); retrying" >&2
    sleep $((attempt * retry_base))
  done
  echo "WARN: detach still busy after ${attempts} attempts; forcing" >&2
  if hdiutil detach "$device" -force; then
    return 0
  fi
  if ! dmg_device_attached "$device"; then
    echo "NOTE: $device is no longer attached after a failed forced detach; treating as detached" >&2
    return 0
  fi
  echo "ERROR: $device is still attached after a forced detach" >&2
  return 1
}
