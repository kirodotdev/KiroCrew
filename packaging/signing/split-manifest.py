#!/usr/bin/env python3
"""Cut a signing manifest that is too big for one request into several.

The signing service refuses a request body over 102,400 bytes. The universal
app lists every nested Mach-O in `embedded_requirements`, so its manifest grows
with the backend and can pass that limit. One task also seals the whole app,
so cutting the list and merging the outputs is not enough: a seal made over
half the signed files is wrong for the merged tree. The service's own nested
code workflow is the order used here: sign the nested files first, put the
signed files into the app, then sign the app.

  plan   Cut the manifest into PART requests (regular files only, each body
         under --max-bytes, each writing its own output key) and ONE FINAL
         request (the bundle entries, reading the merged input, writing the
         real output key). Prints the number of parts.
  merge  Copy the signed bytes of exactly one part's files from the signed zip
         the service returned into the local .app.

Usage:
    split-manifest.py plan <manifest.json> <App.app> <out-dir> --max-bytes N
        --part-output-prefix KEY_PREFIX --merged-input-key KEY
    split-manifest.py merge <part.json> <signed.zip> <App.app>
"""

import argparse
import copy
import json
import os
import stat
import sys
import tempfile
import zipfile


def encode(doc: dict) -> bytes:
    """The request body, byte for byte as generate-manifest.py prints it."""
    return json.dumps(doc, indent=2).encode("utf-8")


def _with_entries(doc: dict, entries: dict) -> dict:
    out = copy.deepcopy(doc)
    out["manifest"]["app"]["embedded_requirements"] = entries
    return out


def split_manifest(
    doc: dict,
    app_path: str,
    max_bytes: int,
    part_output_prefix: str,
    merged_input_key: str,
) -> "tuple[list[dict], dict]":
    """Return (parts, final). Every entry lands in exactly one request, and
    every request body is under max_bytes."""
    entries = doc["manifest"]["app"]["embedded_requirements"]
    # A bundle entry (helper .app, framework) rewrites several files when it is
    # signed; it stays in the final request, where the service signs it in
    # place, instead of being copied out of a zip piece by piece.
    files = {
        rel: entry
        for rel, entry in entries.items()
        if os.path.isfile(os.path.join(app_path, rel))
        and not os.path.islink(os.path.join(app_path, rel))
    }
    rest = {rel: entry for rel, entry in entries.items() if rel not in files}
    names = list(files)

    def part(first: int, count: int, number: int) -> dict:
        request = _with_entries(doc, {rel: files[rel] for rel in names[first : first + count]})
        request["s3ArtifactLocations"]["outputKey"] = f"{part_output_prefix}{number}.zip"
        return request

    parts: "list[dict]" = []
    first = 0
    while first < len(names):
        if len(encode(part(first, 1, len(parts) + 1))) >= max_bytes:
            raise ValueError(f"{names[first]} alone does not fit under {max_bytes} bytes")
        low, high = 1, len(names) - first  # the largest count that still fits
        while low < high:
            mid = (low + high + 1) // 2
            if len(encode(part(first, mid, len(parts) + 1))) < max_bytes:
                low = mid
            else:
                high = mid - 1
        parts.append(part(first, low, len(parts) + 1))
        first += low

    final = _with_entries(doc, rest)
    final["s3ArtifactLocations"]["inputKey"] = merged_input_key
    if len(encode(final)) >= max_bytes:
        raise ValueError(f"the bundle entries alone do not fit under {max_bytes} bytes")
    return parts, final


def merge_signed(part: dict, signed_zip: str, app_path: str) -> int:
    """Copy this part's signed files from the service's zip into app_path.

    A file the service left unchanged was not signed, so it is refused here
    instead of surfacing as a notarization rejection later.
    """
    app_name = part["manifest"]["name"]
    entries = part["manifest"]["app"]["embedded_requirements"]
    with zipfile.ZipFile(signed_zip) as archive:
        for rel in entries:
            member = f"{app_name}/{rel}"
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise ValueError(f"signed zip has no {member}") from None
            kind = stat.S_IFMT(info.external_attr >> 16)  # 0 when the zip records none
            if kind not in (0, stat.S_IFREG):
                raise ValueError(f"{member} is not a regular file in the signed zip")
            signed = archive.read(info)
            dest = os.path.join(app_path, rel)
            with open(dest, "rb") as fh:
                original = fh.read()
            if not signed or signed[:4] != original[:4]:
                raise ValueError(f"{rel}: signed bytes do not look like the same binary")
            if signed == original:
                raise ValueError(f"{rel}: the service returned it unsigned")
            mode = stat.S_IMODE(os.stat(dest).st_mode)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest))
            with os.fdopen(fd, "wb") as out:
                out.write(signed)
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("manifest")
    plan.add_argument("app")
    plan.add_argument("out_dir")
    plan.add_argument("--max-bytes", type=int, required=True)
    plan.add_argument("--part-output-prefix", required=True)
    plan.add_argument("--merged-input-key", required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("part")
    merge.add_argument("signed_zip")
    merge.add_argument("app")
    args = parser.parse_args()

    try:
        if args.command == "plan":
            with open(args.manifest, encoding="utf-8") as fh:
                doc = json.load(fh)
            parts, final = split_manifest(
                doc, args.app, args.max_bytes, args.part_output_prefix, args.merged_input_key
            )
            os.makedirs(args.out_dir, exist_ok=True)
            named = [(f"part-{i}.json", p) for i, p in enumerate(parts, 1)]
            for name, request in named + [("final.json", final)]:
                body = encode(request)
                print(f"{name}: {len(body)} bytes", file=sys.stderr)
                with open(os.path.join(args.out_dir, name), "wb") as fh:
                    fh.write(body)
            print(len(parts))
        else:
            with open(args.part, encoding="utf-8") as fh:
                part_doc = json.load(fh)
            count = merge_signed(part_doc, args.signed_zip, args.app)
            print(f"merged {count} signed files", file=sys.stderr)
    except (OSError, ValueError, zipfile.BadZipFile) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
