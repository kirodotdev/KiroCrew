#!/usr/bin/env bash
# Pack the signed app zip into a disk image for the Apple notary service, and
# print the image's path on stdout (everything else goes to stderr).
#
# Usage: notarize-image.sh <signed.zip> <work-dir>
#
# The workflow runs this only after notarytool refused the zip itself (exit 64,
# "must be a zip archive", before anything is uploaded). Apple notarizes everything
# inside a disk image, so the app gets its ticket and the staple step that follows
# is unchanged. The image exists only to be submitted: it is unsigned, cannot be
# stapled and is never shipped; the shipping DMG is built later from the stapled app.
#
# Why notarytool refused is not known from the job log, so this first prints what
# the zip is: size, first bytes, the end record's fields and the entries Python
# finds. That is only a log: if Python cannot read the zip, the unpack below still
# runs, because `unzip` may. The universal app has about 94,000 entries, over the
# 65,535 a classic zip end record can count, and its signed zip is the only one
# refused so far; single-arch zips (about 47,000) and the earlier universal zip
# (about 48,000) were accepted.
set -euo pipefail

ZIP="${1:-}"
WORK="${2:-}"
if [ ! -f "$ZIP" ] || [ ! -d "$WORK" ]; then
  echo "Usage: $0 <signed.zip> <work-dir>" >&2
  exit 2
fi

python3 - "$ZIP" >&2 <<'PY' || echo "signed zip: could not be inspected; going on to unpack it" >&2
import os, struct, sys, zipfile

path = sys.argv[1]
size = os.path.getsize(path)
with open(path, "rb") as fh:
    first = fh.read(4)
    fh.seek(max(0, size - 65557))  # 64 KiB comment + 22-byte end record
    tail = fh.read()
print(f"signed zip: {size} bytes, first bytes {first.hex()}")
at = tail.rfind(b"PK\x05\x06")
if at < 0:
    print("signed zip: no end-of-central-directory record in the last 64 KiB")
else:
    _, _, _, _, entries, cd_size, cd_offset, _ = struct.unpack("<4s4H2LH", tail[at : at + 22])
    zip64 = tail[max(0, at - 20) : at].startswith(b"PK\x06\x07")
    print(
        f"signed zip: end record entries={entries} (65535 means the count is in the zip64 record) directory_size={cd_size}"
        f" directory_offset={cd_offset} zip64_locator={zip64}"
    )
with zipfile.ZipFile(path) as archive:
    print(f"signed zip: {len(archive.namelist())} entries read by python")
PY

SRC="$(mktemp -d "$WORK/notarize-src.XXXXXX")"
trap 'rm -rf -- "$SRC"' EXIT
# -d, not a cd: the workflow passes ZIP and WORK as paths relative to its cwd.
unzip -q "$ZIP" -d "$SRC"
IMAGE="$WORK/notarize-input.dmg"
# No -quiet: it would close stderr and hide the reason for a failure.
hdiutil create -srcfolder "$SRC" -volname notarize -format UDZO -ov "$IMAGE" >&2
echo "$IMAGE"
