#!/usr/bin/env python3
"""Cut a signing manifest that is too big for one request into several.

The signing service refuses a request body over 102,400 bytes. The universal
app lists every nested Mach-O in `embedded_requirements`, so its manifest grows
with the backend and can pass that limit. One task also seals the whole app,
so cutting the list and merging the outputs is not enough: a seal made over
half the signed files is wrong for the merged tree. The service's own nested
code workflow is the order used here: sign the nested files first, put the
signed files into the app, then sign the app.

Every task, a part included, ends with the service's app pass, which signs the
frameworks under Contents/Frameworks and fails on one whose nested executables
(Electron's Helpers, Squirrel's ShipIt) are not signed yet. So the entries
outside Contents/Resources (the shell) go into EVERY request, and only the
backend files under Contents/Resources are cut across the parts.

A task's time follows the tree it unpacks, signs and seals, not the length of
its list: a final request that lists only the shell takes about as long as one
that lists everything. So each part is signed from its OWN input, the app with
every backend file it does not list taken out. Only its backend files come back
out of it, and the final request seals the whole merged app, so what a part's
seal covered never reaches the release.

  plan   Cut the manifest into PART requests (the shell plus a slice of the
         backend files, each body under --max-bytes, each reading its own input
         key and writing its own output key) and ONE FINAL request (the shell
         alone, reading the merged input, writing the real output key). Prints
         the number of parts.
  stage  Build in a new directory the tree one part's input is packed from.
  merge  Copy the signed bytes of exactly one part's backend files from the
         signed zip the service returned into the local .app. The shell files
         of a part are signed again by the final request, so they are not copied.

Usage:
    split-manifest.py plan <manifest.json> <App.app> <out-dir> --max-bytes N
        --part-output-prefix KEY_PREFIX --part-input-prefix KEY_PREFIX
        --merged-input-key KEY
    split-manifest.py stage <manifest.json> <part.json> <package-dir> <stage-dir>
    split-manifest.py merge <part.json> <signed.zip> <App.app>
"""

import argparse
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile


def encode(doc: dict) -> bytes:
    """The request body, byte for byte as generate-manifest.py prints it."""
    return json.dumps(doc, indent=2).encode("utf-8")


def is_backend_file(app_path: str, rel: str) -> bool:
    """A regular file under Contents/Resources: the entries cut across the parts.
    Everything else the manifest lists (helper apps, frameworks and the
    executables inside them) is the shell, which every request carries."""
    full = os.path.join(app_path, rel)
    return (
        rel.startswith("Contents/Resources/") and os.path.isfile(full) and not os.path.islink(full)
    )


def _with_entries(doc: dict, entries: dict) -> dict:
    out = copy.deepcopy(doc)
    out["manifest"]["app"]["embedded_requirements"] = entries
    return out


def backend_files(doc: dict, app_path: str) -> "list[str]":
    """The listed paths cut across the parts, in the manifest's order."""
    entries = doc["manifest"]["app"]["embedded_requirements"]
    return [rel for rel in entries if is_backend_file(app_path, rel)]


def split_manifest(
    doc: dict,
    app_path: str,
    max_bytes: int,
    part_output_prefix: str,
    merged_input_key: str,
    part_input_prefix: str,
) -> "tuple[list[dict], dict]":
    """Return (parts, final). Every backend file lands in exactly one part, every
    shell entry in every request, and every request body is under max_bytes.
    Part N reads `<part_input_prefix>N.tar.gz`, which sign.sh packs from
    stage_part_input()."""
    entries = doc["manifest"]["app"]["embedded_requirements"]
    names = backend_files(doc, app_path)
    backend = set(names)
    shell = {rel: entry for rel, entry in entries.items() if rel not in backend}

    def part(first: int, count: int, number: int) -> dict:
        cut = set(names[first : first + count])
        # One dict in the manifest's own order, so the deepest paths still sign first.
        request = _with_entries(
            doc, {rel: entry for rel, entry in entries.items() if rel in cut or rel in shell}
        )
        request["s3ArtifactLocations"]["inputKey"] = f"{part_input_prefix}{number}.tar.gz"
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

    final = _with_entries(doc, shell)
    final["s3ArtifactLocations"]["inputKey"] = merged_input_key
    if len(encode(final)) >= max_bytes:
        raise ValueError(f"the shell entries alone do not fit under {max_bytes} bytes")
    return parts, final


def _covered(rel: str, prefixes: "set[str]") -> bool:
    """rel is one of prefixes, or lies inside one."""
    segments = rel.split("/")
    return any("/".join(segments[:i]) in prefixes for i in range(1, len(segments) + 1))


def stage_part_input(doc: dict, part: dict, package_dir: str, stage_dir: str) -> "tuple[int, int]":
    """Recreate package_dir in stage_dir as the input one part is signed from.

    A backend root is the top-level entry of Contents/Resources that holds a
    backend file (the whole backend-dist tree of the real app). The input keeps
    every path outside the backend roots as it is: the main executable, the
    frameworks with their symlinks, the app's own resources, the entitlements,
    which is everything the service's app pass works on. Of the backend roots it
    keeps only what this part lists and the directories above it, so another
    part's binaries and the sources and bytecode around them stay out.

    Files are hard links into package_dir (a copy where a link fails), so the
    stage costs no space and nothing in package_dir is changed. stage_dir must
    not exist, so a leftover input never carries one part's files into the
    next. Returns the number of files staged and their total size.
    """
    app_prefix = part["manifest"]["name"] + "/"
    app_path = os.path.join(package_dir, part["manifest"]["name"])
    roots = {"/".join(rel.split("/")[:3]) for rel in backend_files(doc, app_path)}
    listed = set(part["manifest"]["app"]["embedded_requirements"])
    above = {"/".join(rel.split("/")[:i]) for rel in listed for i in range(1, rel.count("/") + 1)}

    def wanted(rel: str, is_dir: bool) -> bool:
        if not rel.startswith(app_prefix):
            return True
        rel = rel[len(app_prefix) :]
        if not _covered(rel, roots) or _covered(rel, listed):
            return True
        return is_dir and rel in above

    os.makedirs(stage_dir)
    made = [(package_dir, stage_dir)]
    count = size = 0
    for top, dirs, files in os.walk(package_dir):
        rel_top = os.path.relpath(top, package_dir).replace(os.sep, "/")
        rel_top = "" if rel_top == "." else rel_top + "/"
        descend, dir_names = [], set(dirs)
        for name in dirs + files:
            src = os.path.join(top, name)
            rel = rel_top + name
            dest = os.path.join(stage_dir, rel)
            is_dir = name in dir_names and not os.path.islink(src)
            if not wanted(rel, is_dir):
                continue
            if os.path.islink(src):
                os.symlink(os.readlink(src), dest)
            elif is_dir:
                os.mkdir(dest)
                made.append((src, dest))
                descend.append(name)
            else:
                try:
                    os.link(src, dest)
                except OSError:
                    shutil.copy2(src, dest)
                count += 1
                size += os.path.getsize(src)
        dirs[:] = descend
    for src, dest in reversed(made):  # after their contents, so a read-only one still fills
        os.chmod(dest, stat.S_IMODE(os.stat(src).st_mode))
    return count, size


def merge_signed(part: dict, signed_zip: str, app_path: str) -> int:
    """Copy this part's signed backend files from the service's zip into app_path.

    A file the service left unchanged was not signed, so it is refused here
    instead of surfacing as a notarization rejection later.
    """
    app_name = part["manifest"]["name"]
    entries = [
        rel
        for rel in part["manifest"]["app"]["embedded_requirements"]
        if is_backend_file(app_path, rel)
    ]
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
    plan.add_argument("--part-input-prefix", required=True)
    plan.add_argument("--merged-input-key", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("manifest")
    stage.add_argument("part")
    stage.add_argument("package_dir")
    stage.add_argument("stage_dir")
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
                doc,
                args.app,
                args.max_bytes,
                args.part_output_prefix,
                args.merged_input_key,
                args.part_input_prefix,
            )
            os.makedirs(args.out_dir, exist_ok=True)
            named = [(f"part-{i}.json", p) for i, p in enumerate(parts, 1)]
            for name, request in named + [("final.json", final)]:
                body = encode(request)
                print(f"{name}: {len(body)} bytes", file=sys.stderr)
                with open(os.path.join(args.out_dir, name), "wb") as fh:
                    fh.write(body)
            print(len(parts))
        elif args.command == "stage":
            with open(args.manifest, encoding="utf-8") as fh:
                doc = json.load(fh)
            with open(args.part, encoding="utf-8") as fh:
                part_doc = json.load(fh)
            count, size = stage_part_input(doc, part_doc, args.package_dir, args.stage_dir)
            print(f"staged {count} files, {size} bytes", file=sys.stderr)
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
