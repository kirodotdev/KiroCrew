#!/usr/bin/env bash
# Build the shipping DMG by replacing the unsigned app inside the branded
# electron-builder DMG with the signed, notarized, stapled app.
#
# Reusing the original image is load-bearing: Finder stores the background as
# a volume-bound alias in .DS_Store. Copying that file into a newly-created DMG
# leaves first-time users with a plain folder instead of the designed layout.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <layout-template.dmg> <app-path> <output.dmg>" >&2
  exit 2
fi

template_path="$1"
app_path="$2"
output_path="$3"

if [ ! -f "$template_path" ] || [[ "$template_path" != *.dmg ]]; then
  echo "ERROR: expected an existing DMG layout template: $template_path" >&2
  exit 1
fi
if [ ! -d "$app_path" ] || [[ "$app_path" != *.app ]]; then
  echo "ERROR: expected an existing .app bundle: $app_path" >&2
  exit 1
fi

scratch_dir="$(mktemp -d "${TMPDIR:-/tmp}/kirocrew-dmg.XXXXXX")"
mount_dir="$scratch_dir/mount"
read_write_image="$scratch_dir/layout-rw.dmg"
mounted=0
cleanup() {
  if [ "$mounted" -eq 1 ]; then
    hdiutil detach "$mount_dir" -force >/dev/null 2>&1 || true
  fi
  rm -rf -- "$scratch_dir"
}
trap cleanup EXIT

# Spotlight (mds) and XProtect race this script: the moment the app bundle
# lands on the mounted volume they start reading it, and a volume with open
# files refuses to eject ("Resource busy"). Whether the eject wins is purely
# load-dependent -- the step has failed nightly runs on exactly this race.
# Ejecting on a multitasking OS is inherently cooperative, so the mitigation
# is layered rather than absolute: keep the volume out of Finder (-nobrowse
# at both attach sites) and give the eject bounded retries with a force
# fallback. electron-builder classifies hdiutil's "Resource busy" as
# transient-retry for the same reason.
detach_mount() {
  # Flush first so a force-detach on the final attempt cannot lose writes.
  # Force is acceptable here because every write we issued has completed and
  # synced, and the resize/convert/verification stages that follow re-read the
  # image and fail loudly on a damaged filesystem.
  sync
  local attempt
  for attempt in 1 2 3 4 5; do
    if hdiutil detach "$mount_dir"; then
      mounted=0
      return 0
    fi
    echo "WARN: detach of $mount_dir failed (attempt ${attempt}/5); retrying" >&2
    sleep $((attempt * 3))
  done
  echo "WARN: detach still busy after 5 attempts; forcing" >&2
  hdiutil detach "$mount_dir" -force
  mounted=0
}

mkdir -p "$mount_dir" "$(dirname "$output_path")"
# hdiutil's -quiet is NOT used anywhere on this path: unlike most tools' quiet
# flags it closes stderr as well as stdout, which turned three nightly failures
# of this script into zero-diagnostic exits. Progress spew goes to stdout, so
# redirecting stdout alone keeps the log clean while errors still surface.
hdiutil convert "$template_path" -format UDRW -o "$read_write_image" >/dev/null

# Code signatures and staple tickets make the final app slightly larger than
# its unsigned template. Add 256 MiB of temporary workspace, then shrink the
# filesystem again before compression so the released image carries no bloat.
#
# `hdiutil resize -limits` reports min, current and max in 512-byte sectors, and
# its exact shape is not contractual — some releases precede the numbers with a
# header row. Take the last all-numeric three-field line and refuse anything
# else, because an unvalidated parse here does not fail loudly: it feeds a
# non-numeric value into the arithmetic below and resizes a release image to a
# size nobody chose.
limits_output="$(hdiutil resize -limits "$read_write_image")"
limits_line="$(printf '%s\n' "$limits_output" | awk '
  $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { line = $0 }
  END { print line }
')"
if [ -z "$limits_line" ]; then
  echo "ERROR: could not read sector limits from hdiutil resize -limits" >&2
  printf 'hdiutil said: %s\n' "$limits_output" >&2
  exit 1
fi
read -r _minimum_sectors current_sectors maximum_sectors <<<"$limits_line"
expanded_sectors=$((current_sectors + 524288))
if [ "$expanded_sectors" -gt "$maximum_sectors" ]; then
  expanded_sectors="$maximum_sectors"
fi
hdiutil resize -sectors "$expanded_sectors" "$read_write_image" >/dev/null

hdiutil attach -readwrite -noverify -noautoopen -nobrowse \
  -mountpoint "$mount_dir" "$read_write_image" >/dev/null
mounted=1

# The background is a volume-bound alias recorded INSIDE .DS_Store, so that file
# is the layout. Fingerprint the template's copy now, while it is still the one
# Finder wrote, and compare it after the round trip: presence alone cannot tell a
# surviving alias from a broken one, because a broken alias leaves every file in
# place.
if [ ! -f "$mount_dir/.DS_Store" ]; then
  echo "ERROR: the DMG template carries no .DS_Store, so it has no Finder layout to reuse" >&2
  exit 1
fi
template_layout_digest="$(shasum -a 256 "$mount_dir/.DS_Store" | awk '{print $1}')"

shopt -s nullglob
template_apps=("$mount_dir"/*.app)
shopt -u nullglob
if [ "${#template_apps[@]}" -ne 1 ]; then
  echo "ERROR: expected exactly one app in the DMG template" >&2
  exit 1
fi
if [ "$(basename "${template_apps[0]}")" != "$(basename "$app_path")" ]; then
  echo "ERROR: template and signed app names differ; Finder icon placement would be lost" >&2
  exit 1
fi

rm -rf -- "${template_apps[0]}"
/usr/bin/ditto "$app_path" "$mount_dir/$(basename "$app_path")"

detach_mount
hdiutil resize -size min "$read_write_image" >/dev/null
hdiutil convert "$read_write_image" -format UDZO -o "$output_path" -ov >/dev/null

# The whole point of reusing the template is that Finder's background survives as
# a volume-bound alias in .DS_Store. That survival is not guaranteed by anything
# we control -- it depends on how hdiutil and dmgbuild happen to behave -- and if
# it ever stops working the DMG still builds, still signs and still ships, just
# looking like an unstyled folder. So assert it on the FINAL image.
#
# What this proves and what it does not: comparing the digest shows the layout
# RECORD carrying the alias came through the round trip byte-for-byte, which is
# strictly stronger than the files merely existing (a broken alias leaves them
# all present). It still cannot prove Finder RESOLVES that alias, because
# resolution also depends on the volume identity the alias binds to. Nothing
# available on a runner re-renders the window, so the definitive check is the
# first real signing run -- see README.md, which also names the plan-B that
# removes this question entirely.
hdiutil attach -readonly -noverify -noautoopen -nobrowse \
  -mountpoint "$mount_dir" "$output_path" >/dev/null
mounted=1
missing=()
[ -f "$mount_dir/.DS_Store" ] || missing+=(".DS_Store (Finder icon placement)")
if [ ! -e "$mount_dir/.background.tiff" ] && [ ! -d "$mount_dir/.background" ]; then
  missing+=("background image")
fi
[ -d "$mount_dir/$(basename "$app_path")" ] || missing+=("$(basename "$app_path")")
if [ "${#missing[@]}" -ne 0 ]; then
  echo "ERROR: the shipping DMG lost its branded layout; missing: ${missing[*]}" >&2
  exit 1
fi
final_layout_digest="$(shasum -a 256 "$mount_dir/.DS_Store" | awk '{print $1}')"
if [ "$final_layout_digest" != "$template_layout_digest" ]; then
  echo "ERROR: the shipping DMG's Finder layout record changed during the rebuild;" >&2
  echo "       the background alias can no longer be assumed intact." >&2
  echo "       template .DS_Store: $template_layout_digest" >&2
  echo "       shipping .DS_Store: $final_layout_digest" >&2
  exit 1
fi
detach_mount
echo "Branded layout verified on $output_path (layout record $template_layout_digest)"
