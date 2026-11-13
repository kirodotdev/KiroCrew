"""Drills for signing a universal app whose manifest passes the service body limit.

The signing service refuses a request body over 102,400 bytes. The universal
app lists one `embedded_requirements` entry per nested binary, and 337 of them
made a 136,880 byte body. `split-manifest.py` cuts the regular files into parts
and `sign.sh` signs the parts first, copies their signed bytes into the app, and
signs the app once more over that tree.

A task's time follows the tree it unpacks, signs and seals, not the length of
its list, so each part is signed from its own input: the app minus every backend
file the part does not list. Only the final request reads the whole app.

The fake service below answers like the real one in the four ways that matter
here: it rejects an over-limit body, it signs exactly the listed paths of the
input it was pointed at, its app pass fails on a framework whose nested
executables are still unsigned, and the pass seals whatever tree it was given.
So a merge that drops, repeats or reorders a file shows up as a missing marker,
a doubled marker or a wrong seal, and a part request without the shell fails
the way the real one did. It also records the files of each input it read, so a
part whose input carries the whole backend shows up too.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pytest

SIGNING_DIR = Path(__file__).resolve().parents[1] / "packaging" / "signing"
GENERATOR = SIGNING_DIR / "generate-manifest.py"
SPLITTER = SIGNING_DIR / "split-manifest.py"
SIGN_SH = SIGNING_DIR / "sign.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="sign.sh is a Bash script for the signing runners",
)

SERVICE_LIMIT = 102_400
PART_LIMIT = 90_000
MACHO = struct.pack(">I", 0xFEEDFACF) + struct.pack(">I", 0x0100000C)
APP = "Demo.app"
ENV = {
    "SIGNER_ACCESS_ROLE_ARN": "arn:aws:iam::123456789012:role/signer-access",
    "SIGNING_BUCKET": "signing-bucket",
    "INPUT_KEY": "pre-signed/nightly/1.0.0/Demo.tar.gz",
    "OUTPUT_KEY": "signed/nightly/1.0.0/Demo.zip",
}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def splitter():
    return _load(SPLITTER)


# What every request must carry: the paths outside Contents/Resources that the
# generator lists for the synthetic app (helper app, framework and its Helpers
# executable, Squirrel's ShipIt), in the manifest's own order.
SHELL = [
    "Contents/Frameworks/Demo Framework.framework/Versions/A/Helpers/crashpad",
    "Contents/Frameworks/Squirrel.framework/Versions/A/Resources/ShipIt",
    "Contents/Frameworks/Demo Framework.framework",
    "Contents/Frameworks/Demo Helper.app",
]
PLIST = (
    b'<plist version="1.0"><dict><key>CFBundleIdentifier</key>'
    b"<string>%s</string></dict></plist>"
)


def _make_app(root: Path, files: int) -> Path:
    """An app with `files` backend binaries and the Electron-like shell.

    Beside its binaries the backend carries the kind of files a real one is
    mostly made of (Python sources, a symlink), none of which the manifest
    lists, and Contents/Resources carries the app's own files next to it."""
    app = root / APP
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / "Demo").write_bytes(MACHO + b"main")
    (contents / "PkgInfo").write_bytes(b"APPL????")
    helper = contents / "Frameworks" / "Demo Helper.app" / "Contents"
    (helper / "MacOS").mkdir(parents=True)
    (helper / "MacOS" / "Demo Helper").write_bytes(MACHO + b"helper")
    (helper / "Info.plist").write_bytes(PLIST % b"org.example.helper")
    version = contents / "Frameworks" / "Demo Framework.framework" / "Versions" / "A"
    (version / "Helpers").mkdir(parents=True)
    (version / "Helpers" / "crashpad").write_bytes(MACHO + b"crashpad")
    (version / "Resources").mkdir()
    (version / "Resources" / "Info.plist").write_bytes(PLIST % b"org.example.framework")
    shipit = contents / "Frameworks" / "Squirrel.framework" / "Versions" / "A" / "Resources"
    shipit.mkdir(parents=True)
    (shipit / "ShipIt").write_bytes(MACHO + b"shipit")
    resources = contents / "Resources"
    (resources / "en.lproj").mkdir(parents=True)
    (resources / "en.lproj" / "locale.pak").write_bytes(b"locale")
    (resources / "app.asar").write_bytes(b"asar")
    (resources / "backend" / "bin").mkdir(parents=True)
    (resources / "backend" / "bin" / "python3.12").write_bytes(b"#!launcher\n")
    (resources / "backend" / "bin" / "python3").symlink_to("python3.12")
    for i in range(files):
        pkg = resources / "backend" / "lib" / "python3.12" / "site-packages"
        pkg = pkg / f"crypto_pkg_{i:04d}"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_bytes(f"# package {i}\n".encode())
        binary = pkg / f"_native_{i:04d}.cpython-312-darwin.so"
        binary.write_bytes(MACHO + f"payload {i}".encode())
        binary.chmod(0o755 if i % 2 else 0o644)
    return app


def _manifest(app: Path) -> bytes:
    """The request exactly as sign.sh gets it back from `$(...)`."""
    done = subprocess.run(
        [sys.executable, str(GENERATOR), str(SIGNING_DIR / "manifest-template.json"), str(app)],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, **ENV},
        timeout=60,  # flake-ok: bounds a hung child process, not a test boundary
        check=True,
    )
    return done.stdout.rstrip("\n").encode("utf-8")


def _entries(body: bytes) -> dict:
    return json.loads(body)["manifest"]["app"]["embedded_requirements"]


# ── the planner ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("files", [340, 700])
def test_an_over_limit_manifest_is_split_into_requests_under_the_part_limit(
    splitter, tmp_path, files
):
    app = _make_app(tmp_path, files)
    body = _manifest(app)
    assert len(body) > SERVICE_LIMIT, "the synthetic app must reproduce the failure"

    parts, final = splitter.split_manifest(
        json.loads(body),
        str(app),
        PART_LIMIT,
        "signed/p.part-",
        "pre-signed/p.merged.tar.gz",
        "pre-signed/p.part-",
    )

    assert len(parts) > 1
    for request in [*parts, final]:
        assert len(splitter.encode(request)) < PART_LIMIT


@pytest.mark.parametrize("files", [100, 340, 700])
def test_every_request_carries_the_shell_and_each_backend_file_is_in_one_part(
    splitter, tmp_path, files
):
    app = _make_app(tmp_path, files)
    doc = json.loads(_manifest(app))
    original = doc["manifest"]["app"]["embedded_requirements"]
    assert len(original) == files + len(SHELL)
    # A tiny limit forces parts even for 100 files.
    limit = PART_LIMIT if files > 100 else 20_000

    parts, final = splitter.split_manifest(doc, str(app), limit, "k.part-", "m.tar.gz", "in.part-")

    assert len(parts) > 1
    backend = Counter()
    for request in [*parts, final]:
        entries = request["manifest"]["app"]["embedded_requirements"]
        assert [rel for rel in entries if rel in SHELL] == SHELL
        for rel, entry in entries.items():
            assert entry == original[rel]
            if rel not in SHELL:
                backend[rel] += 1
        # Same relative order as the single request: the deepest paths sign first.
        assert list(entries) == [rel for rel in original if rel in entries]
    assert backend == Counter({rel: 1 for rel in original if rel not in SHELL})
    assert list(final["manifest"]["app"]["embedded_requirements"]) == SHELL


def _failed_run_shape(splitter, root: Path) -> dict:
    """The manifest of the run the service refused: 329 backend files (169 arm64,
    149 x64, 11 node) and the 8 shell entries, 136 KB. Paths are cut to the same
    length class as the real ones; the shell names are the real ones."""
    gen = _load(GENERATOR)
    electron = "Contents/Frameworks/Electron Framework.framework"
    helper = "Contents/Frameworks/KiroCrew Nightly Helper"
    shell_files = [
        f"{electron}/Versions/A/Helpers/app_shim_loader",
        f"{electron}/Versions/A/Helpers/chrome_crashpad_handler",
        "Contents/Frameworks/Squirrel.framework/Versions/A/Resources/ShipIt",
    ]
    shell_dirs = [electron, f"{helper} (GPU).app", f"{helper} (Plugin).app"]
    shell_dirs += [f"{helper} (Renderer).app", f"{helper}.app"]
    python = "Contents/Resources/backend-dist/kirocrew-backend-{}/lib/python3.12/site-packages"
    node = "Contents/Resources/backend-dist/kas/0.66.33/bundle/node_modules/native-addon"
    backend = []
    for base, count in ((python.format("arm64"), 169), (python.format("x64"), 149), (node, 11)):
        for i in range(count):
            backend.append(f"{base}/pkg{i:03d}/_x{i:03d}.cpython-312-darwin.so")
    entries = {}
    for rel in backend + shell_files:
        entries[rel] = {
            "full_identifier": gen.identifier_for(rel),
            "signing_args": dict(gen._ENTITLEMENTS),
        }
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(MACHO)
    for rel in shell_dirs:
        entries[rel] = {
            "full_identifier": "com.github.Electron.framework",
            "signing_args": dict(gen._ENTITLEMENTS),
        }
        (root / rel).mkdir(parents=True, exist_ok=True)
    doc = json.loads((SIGNING_DIR / "manifest-template.json").read_text())
    doc["manifest"]["app"]["embedded_requirements"] = dict(
        sorted(entries.items(), key=lambda kv: (-kv[0].count("/"), kv[0]))
    )
    return doc


def test_a_manifest_in_the_shape_of_the_failed_run_plans_two_parts_and_a_final(splitter, tmp_path):
    doc = _failed_run_shape(splitter, tmp_path)
    entries = doc["manifest"]["app"]["embedded_requirements"]
    assert len(entries) == 337
    assert 130_000 < len(splitter.encode(doc)) < 140_000, "not the size class that failed"
    shell = [rel for rel in entries if not rel.startswith("Contents/Resources/")]
    assert len(shell) == 8

    parts, final = splitter.split_manifest(
        doc, str(tmp_path), PART_LIMIT, "k.part-", "m.tar.gz", "in.part-"
    )

    assert len(parts) == 2
    assert len(splitter.encode(final)) < 5_000
    for request in [*parts, final]:
        assert len(splitter.encode(request)) < PART_LIMIT
        assert [
            r for r in request["manifest"]["app"]["embedded_requirements"] if r in shell
        ] == shell
    assert sum(len(p["manifest"]["app"]["embedded_requirements"]) - 8 for p in parts) == 329


def test_each_request_reads_and_writes_the_keys_the_flow_expects(splitter, tmp_path):
    app = _make_app(tmp_path, 340)
    doc = json.loads(_manifest(app))

    parts, final = splitter.split_manifest(
        doc, str(app), PART_LIMIT, "k.part-", "m.tar.gz", "in.part-"
    )

    assert [p["s3ArtifactLocations"]["outputKey"] for p in parts] == [
        f"k.part-{n}.zip" for n in range(1, len(parts) + 1)
    ]
    # Each part reads its own input, never the upload of the whole app.
    assert [p["s3ArtifactLocations"]["inputKey"] for p in parts] == [
        f"in.part-{n}.tar.gz" for n in range(1, len(parts) + 1)
    ]
    assert final["s3ArtifactLocations"]["inputKey"] == "m.tar.gz"
    assert final["s3ArtifactLocations"]["outputKey"] == ENV["OUTPUT_KEY"]
    for request in [*parts, final]:
        assert request["manifest"]["name"] == APP
        assert request["s3ArtifactLocations"]["bucket"] == ENV["SIGNING_BUCKET"]


def test_a_file_that_cannot_fit_a_request_fails_the_plan(splitter, tmp_path):
    app = _make_app(tmp_path, 3)
    doc = json.loads(_manifest(app))
    with pytest.raises(ValueError, match="alone does not fit"):
        splitter.split_manifest(doc, str(app), 400, "k.part-", "m.tar.gz", "in.part-")


# ── a part's own input ──────────────────────────────────────────────────────

BACKEND = f"{APP}/Contents/Resources/backend"


def _package(root: Path, files: int) -> "tuple[Path, Path]":
    """sign.sh's package dir: the app plus SIGNING_METADATA, with the layout
    details a part input has to keep: framework symlinks, a read-only
    directory, and a backend binary directly under Contents/Resources, outside
    the backend directory."""
    package = root / "package"
    package.mkdir()
    app = _make_app(package, files)
    framework = app / "Contents" / "Frameworks" / "Demo Framework.framework"
    (framework / "Versions" / "Current").symlink_to("A")
    (framework / "Helpers").symlink_to("Versions/Current/Helpers")
    (app / "Contents" / "Resources" / "en.lproj").chmod(0o555)
    (app / "Contents" / "Resources" / "loose.so").write_bytes(MACHO + b"loose")
    (package / "SIGNING_METADATA").mkdir()
    (package / "SIGNING_METADATA" / "Entitlements.entitlements").write_bytes(b"<plist/>")
    return package, app


def _tree(root: Path) -> dict:
    """Every path under root: a symlink's target, a file's bytes and mode, or a
    directory's mode."""
    out = {}
    for top, dirs, files in os.walk(root):  # never follows a directory symlink
        for name in dirs + files:
            path = Path(top) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir", path.stat().st_mode & 0o7777)
            else:
                out[rel] = ("file", path.read_bytes(), path.stat().st_mode & 0o7777)
    return out


def _is_backend_path(rel: str, backend: "set[str]") -> bool:
    return rel.startswith(BACKEND + "/") or rel == BACKEND or rel in backend


def _staged_parts(splitter, tmp_path: Path, files: int, limit: int = PART_LIMIT):
    package, app = _package(tmp_path, files)
    doc = json.loads(_manifest(app))
    parts, _ = splitter.split_manifest(doc, str(app), limit, "k.part-", "m.tar.gz", "in.part-")
    staged = []
    for number, part in enumerate(parts, 1):
        stage = tmp_path / f"stage-{number}"
        splitter.stage_part_input(doc, part, str(package), str(stage))
        staged.append(stage)
    return package, doc, parts, staged


def test_a_part_input_is_the_app_with_only_that_parts_backend_files(splitter, tmp_path):
    package, doc, parts, staged = _staged_parts(splitter, tmp_path, 340)
    assert len(parts) > 1
    full = _tree(package)
    entries = doc["manifest"]["app"]["embedded_requirements"]
    backend = {f"{APP}/{rel}" for rel in entries if rel.startswith("Contents/Resources/")}
    assert f"{APP}/Contents/Resources/loose.so" in backend

    seen = Counter()
    for part, stage in zip(parts, staged):
        tree = _tree(stage)
        listed = {f"{APP}/{rel}" for rel in part["manifest"]["app"]["embedded_requirements"]}
        mine = backend & listed
        assert mine, "every part signs some backend files"
        # Everything the app pass works on, byte for byte: the main executable,
        # the frameworks with their symlinks, the app's own resources, the
        # entitlements the request points at.
        shell = {rel: v for rel, v in full.items() if not _is_backend_path(rel, backend)}
        assert {rel: tree[rel] for rel in shell if rel in tree} == shell
        for rel in (
            f"{APP}/Contents/Resources/app.asar",
            f"{APP}/Contents/Resources/en.lproj/locale.pak",
            f"{APP}/Contents/Frameworks/Demo Framework.framework/Versions/Current",
            "SIGNING_METADATA/Entitlements.entitlements",
        ):
            assert rel in tree, rel
        # Of the backend, this part's binaries and the directories above them,
        # nothing else: no other part's binary, no source, no symlink.
        files = {rel for rel, v in tree.items() if v[0] != "dir"}
        assert {rel for rel in files if _is_backend_path(rel, backend)} == mine
        for rel in mine:
            assert tree[rel] == full[rel]
            assert os.path.samefile(stage / rel, package / rel), "staged by hard link"
        seen.update(mine)
    assert seen == Counter({rel: 1 for rel in backend})


def test_a_part_input_leaves_the_package_as_it_was_and_starts_from_nothing(splitter, tmp_path):
    package, app = _package(tmp_path, 20)
    doc = json.loads(_manifest(app))
    parts, _ = splitter.split_manifest(doc, str(app), 6_000, "k.part-", "m.tar.gz", "in.part-")
    assert len(parts) > 1
    before = _tree(package)
    stage = tmp_path / "stage"

    splitter.stage_part_input(doc, parts[0], str(package), str(stage))

    assert _tree(package) == before
    # A leftover input must never leak one part's files into the next.
    with pytest.raises(FileExistsError):
        splitter.stage_part_input(doc, parts[1], str(package), str(stage))


def test_a_part_input_copies_a_file_it_cannot_link(splitter, tmp_path):
    package, app = _package(tmp_path, 4)
    doc = json.loads(_manifest(app))
    parts, _ = splitter.split_manifest(doc, str(app), PART_LIMIT, "k.part-", "m.tar.gz", "in.part-")
    stage = tmp_path / "stage"

    def refuse(src, dst, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link", src)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(splitter.os, "link", refuse)
        count, _ = splitter.stage_part_input(doc, parts[0], str(package), str(stage))

    full, tree = _tree(package), _tree(stage)
    files = [rel for rel, v in tree.items() if v[0] == "file"]
    assert len(files) == count > 4
    for rel in files:
        assert tree[rel] == full[rel], rel
        assert not os.path.samefile(stage / rel, package / rel)


def test_a_part_input_keeps_a_listed_path_that_is_not_a_backend_file(splitter, tmp_path):
    """A listed entry under Contents/Resources that is not a regular file is in
    every request, so every part input carries it whole."""
    package, app = _package(tmp_path, 20)
    doc = json.loads(_manifest(app))
    kept = "Contents/Resources/backend/bin/python3"
    doc["manifest"]["app"]["embedded_requirements"][kept] = {"full_identifier": "org.example.py"}
    parts, _ = splitter.split_manifest(doc, str(app), 6_000, "k.part-", "m.tar.gz", "in.part-")
    assert len(parts) > 1

    for number, part in enumerate(parts, 1):
        stage = tmp_path / f"stage-{number}"
        splitter.stage_part_input(doc, part, str(package), str(stage))
        assert os.readlink(stage / APP / kept) == "python3.12"


# ── the merge ───────────────────────────────────────────────────────────────


def _signed_zip(path: Path, part: dict, app: Path, sign) -> None:
    """What the service returns: every listed file, the shell files included."""
    with zipfile.ZipFile(path, "w") as archive:
        for rel in part["manifest"]["app"]["embedded_requirements"]:
            if (app / rel).is_dir():
                continue
            data = sign((app / rel).read_bytes())
            if data is not None:
                archive.writestr(f"{APP}/{rel}", data)


def _one_part(splitter, app: Path) -> dict:
    doc = json.loads(_manifest(app))
    parts, _ = splitter.split_manifest(doc, str(app), PART_LIMIT, "k.part-", "m.tar.gz", "in.part-")
    return parts[0]


def test_merge_copies_the_backend_files_and_leaves_the_shell_for_the_final_request(
    splitter, tmp_path
):
    app = _make_app(tmp_path, 4)
    part = _one_part(splitter, app)
    zip_path = tmp_path / "part.zip"
    _signed_zip(zip_path, part, app, lambda data: data + b"\0SIGNED")
    shell_before = {rel: (app / rel).read_bytes() for rel in SHELL if (app / rel).is_file()}
    assert len(shell_before) == 2

    assert splitter.merge_signed(part, str(zip_path), str(app)) == 4

    for rel in part["manifest"]["app"]["embedded_requirements"]:
        if rel in SHELL:
            assert not (app / rel).is_file() or (app / rel).read_bytes() == shell_before[rel]
        else:
            assert (app / rel).read_bytes().endswith(b"\0SIGNED")
    odd = app / "Contents/Resources/backend/lib/python3.12/site-packages/crypto_pkg_0001"
    assert oct((odd / "_native_0001.cpython-312-darwin.so").stat().st_mode & 0o777) == "0o755"


@pytest.mark.parametrize(
    ("sign", "message"),
    [
        (lambda data: None, "signed zip has no"),
        (lambda data: data, "returned it unsigned"),
        (lambda data: b"\0\0\0\0" + data, "do not look like the same binary"),
    ],
    ids=["missing", "unsigned", "different-binary"],
)
def test_merge_refuses_a_file_the_service_did_not_sign(splitter, tmp_path, sign, message):
    app = _make_app(tmp_path, 2)
    part = _one_part(splitter, app)
    zip_path = tmp_path / "part.zip"
    _signed_zip(zip_path, part, app, sign)

    with pytest.raises(ValueError, match=message):
        splitter.merge_signed(part, str(zip_path), str(app))


# ── the whole flow: sign.sh against a fake service ─────────────────────────

FAKE_AWSCURL = r"""#!__PYTHON__
import hashlib, io, json, os, sys, tarfile, zipfile

D = os.environ["FAKE_DIR"]
BUCKET = os.path.join(D, "bucket")
LIMIT = 102400
SIGNED = b"\0SIGNED"
args = sys.argv[1:]
method = args[args.index("-X") + 1]
state_path = os.path.join(D, "tasks.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"tasks": {}}


def save():
    json.dump(state, open(state_path, "w"))


def say(obj):
    print(json.dumps(obj))
    sys.exit(0)


if method == "GET":
    task = state["tasks"][args[-1].rsplit("/", 1)[1]]
    task["polls"] += 1
    save()
    if task["fail"]:
        say({"status": "failure", "message": task["error"]})
    say({"status": "inProgress" if task["polls"] == 1 else "success"})

assert args[args.index("-d") + 1] == "@-", "the body must come on stdin"
body = sys.stdin.buffer.read()
number = len(state["tasks"]) + 1
if len(body) > LIMIT:
    say({"message": "request entity too large", "errors": {
        "expected": len(body), "length": len(body), "limit": LIMIT, "type": "entity.too.large"}})
if os.environ.get("FAKE_REJECT_POST") == str(number):
    say({"message": "User is not authorized"})
os.makedirs(os.path.join(D, "bodies"), exist_ok=True)
open(os.path.join(D, "bodies", f"{number}.json"), "wb").write(body)

doc = json.loads(body)
where = doc["s3ArtifactLocations"]
app = doc["manifest"]["name"]
embedded = doc["manifest"]["app"]["embedded_requirements"]
tree = {}
with tarfile.open(os.path.join(BUCKET, where["inputKey"])) as tar:
    for member in tar.getmembers():
        if member.isfile():
            tree[member.name] = (tar.extractfile(member).read(), member.mode)
os.makedirs(os.path.join(D, "inputs"), exist_ok=True)
json.dump(sorted(tree), open(os.path.join(D, "inputs", f"{number}.json"), "w"))
signed = set()
for rel in embedded:
    prefix = f"{app}/{rel}"
    hit = [n for n in tree if n == prefix or n.startswith(prefix + "/")]
    if not hit:
        say({"message": f"embedded requirement {rel} is not in the artifact"})
    for name in hit:
        if name not in signed:  # a bundle entry does not sign its listed files twice
            signed.add(name)
            tree[name] = (tree[name][0] + SIGNED, tree[name][1])
# The app pass signs every framework under Contents/Frameworks, and codesign
# refuses one whose nested executable is still unsigned.
error = "fake signing failure"
for name, (data, _) in sorted(tree.items()):
    if "/Contents/Frameworks/" in name and ".framework/" in name:
        if data[:4] == b"\xfe\xed\xfa\xcf" and not data.endswith(SIGNED):
            framework = name.split(".framework/")[0] + ".framework"
            error = f"{framework}: code object is not signed at all"
            break
# The app pass: sign the main executable, then seal every file of the tree.
main = f"{app}/Contents/MacOS/Demo"
tree[main] = (tree[main][0] + b"\0APP", tree[main][1])
seal = hashlib.sha256()
for name in sorted(tree):
    seal.update(name.encode() + b"\0" + tree[name][0])
tree[f"{app}/Contents/_CodeSignature/CodeResources"] = (seal.hexdigest().encode(), 0o644)
out = os.path.join(BUCKET, where["outputKey"])
os.makedirs(os.path.dirname(out), exist_ok=True)
with zipfile.ZipFile(out, "w") as archive:
    for name, (data, mode) in sorted(tree.items()):
        info = zipfile.ZipInfo(name)
        info.external_attr = (0o100000 | mode) << 16
        archive.writestr(info, data)
fail = str(number) in os.environ.get("FAKE_FAIL_TASK", "").split(",") or (
    os.environ.get("FAKE_FAIL_FINAL") and ".part-" not in where["outputKey"])
state["tasks"][f"task-{number}"] = {
    "fail": bool(fail or error != "fake signing failure"), "polls": 0, "error": error}
save()
say({"signTaskId": f"task-{number}"})
"""

FAKE_AWS = r"""#!/usr/bin/env bash
# aws s3 cp <src> <dst> [--quiet]: s3://bucket/key maps to $FAKE_DIR/bucket/key.
src="$3"; dst="$4"
case "$src" in s3://*) src="$FAKE_DIR/bucket/${src#s3://*/}" ;; esac
case "$dst" in s3://*) dst="$FAKE_DIR/bucket/${dst#s3://*/}" ;; esac
mkdir -p "$(dirname "$dst")"
cp "$src" "$dst"
"""

FAKE_NOOP = "#!/usr/bin/env bash\nexit 0\n"


def _sign(tmp_path: Path, files: int, **env: str):
    """Run sign.sh on a synthetic app; returns (result, bucket, bodies)."""
    work = tmp_path / "work"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {
        "awscurl": FAKE_AWSCURL.replace("__PYTHON__", sys.executable),
        "aws": FAKE_AWS,
        "sleep": FAKE_NOOP,
        "codesign": FAKE_NOOP,
        "spctl": FAKE_NOOP,
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    work.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    app = _make_app(src, files)
    done = subprocess.run(
        ["bash", str(SIGN_SH), str(app), "nightly", "1.0.0"],
        capture_output=True,
        encoding="utf-8",
        cwd=work,
        timeout=300,  # flake-ok: bounds a hung child process, not a test boundary
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DIR": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "AWS_SIGNING_BUCKET": "signing-bucket",
            "AWS_SIGNER_ROLE_ARN": ENV["SIGNER_ACCESS_ROLE_ARN"],
            "CDSIGNER_API_ENDPOINT": "https://signer.invalid",
            **env,
        },
    )
    bodies_dir = tmp_path / "bodies"
    bodies = (
        [p.read_bytes() for p in sorted(bodies_dir.glob("*.json"), key=lambda p: int(p.stem))]
        if bodies_dir.exists()
        else []
    )
    return done, app, work, bodies


def _check_signed_once(work: Path, app: Path, embedded: dict) -> None:
    """Every listed file is signed once, and the seal covers the tree it ships."""
    with zipfile.ZipFile(work / "signed" / "Demo.zip") as archive:
        tree = {n: archive.read(n) for n in archive.namelist()}
    seal_name = f"{APP}/Contents/_CodeSignature/CodeResources"
    seal = hashlib.sha256()
    for name in sorted(n for n in tree if n != seal_name):
        seal.update(name.encode() + b"\0" + tree[name])
    assert tree[seal_name] == seal.hexdigest().encode()
    for rel in embedded:
        listed = (app / rel).rglob("*") if (app / rel).is_dir() else [app / rel]
        for path in (p for p in listed if p.is_file()):
            name = f"{APP}/{path.relative_to(app).as_posix()}"
            assert tree[name] == path.read_bytes() + b"\0SIGNED", name


def _inputs(tmp_path: Path) -> "list[set[str]]":
    """The files of each input the fake service read, in submission order."""
    found = sorted((tmp_path / "inputs").glob("*.json"), key=lambda p: int(p.stem))
    return [set(json.loads(p.read_text())) for p in found]


def _app_files(app: Path) -> "set[str]":
    """The regular files of the app and its entitlements, named as in the tar."""
    files = {f"{APP}/{rel}" for rel, v in _tree(app).items() if v[0] == "file"}
    return files | {"SIGNING_METADATA/Entitlements.entitlements"}


def test_a_manifest_under_the_limit_is_still_one_request_byte_for_byte(tmp_path):
    done, app, work, bodies = _sign(tmp_path, 100)

    assert done.returncode == 0, done.stderr
    expected = _manifest(app)
    assert len(expected) < 100_000
    assert bodies == [expected]
    assert _inputs(tmp_path) == [_app_files(app)]
    pre_signed = tmp_path / "bucket" / "pre-signed" / "nightly" / "1.0.0"
    assert sorted(p.name for p in pre_signed.iterdir()) == ["Demo.tar.gz"]
    assert not (tmp_path / "bucket" / "signed" / "nightly" / "1.0.0" / "Demo.part-1.zip").exists()
    _check_signed_once(work, app, _entries(expected))


@pytest.mark.parametrize("files", [340, 700])
def test_an_over_limit_manifest_is_signed_in_parts_then_as_an_app(tmp_path, files):
    done, app, work, bodies = _sign(tmp_path, files)

    assert done.returncode == 0, done.stderr
    original = _entries(_manifest(app))
    assert len(_manifest(app)) > SERVICE_LIMIT
    assert len(bodies) > 2
    assert all(len(body) < PART_LIMIT for body in bodies)
    keys = Counter(rel for body in bodies for rel in _entries(body))
    assert keys == Counter(
        {rel: 1 if rel not in SHELL else len(bodies) for rel in original}
    ), "a backend file was dropped or sent twice, or a request lacks the shell"
    assert json.loads(bodies[-1])["s3ArtifactLocations"]["outputKey"] == ENV["OUTPUT_KEY"]
    assert list(_entries(bodies[-1])) == SHELL
    _check_signed_once(work, app, original)

    # Each part is signed from its own input: the app without the backend files
    # the part does not list.
    inputs = _inputs(tmp_path)
    assert len(inputs) == len(bodies)
    every = _app_files(app)
    backend = {name for name in every if name.startswith(f"{BACKEND}/")}
    assert len(backend) > 2 * files, "sources and binaries"
    pre_signed = "pre-signed/nightly/1.0.0"
    for number, (body, names) in enumerate(zip(bodies[:-1], inputs[:-1]), 1):
        assert json.loads(body)["s3ArtifactLocations"]["inputKey"] == (
            f"{pre_signed}/Demo.part-{number}.tar.gz"
        )
        listed = {f"{APP}/{rel}" for rel in _entries(body) if rel not in SHELL}
        assert names == (every - backend) | listed
    # The final request still signs the whole app, merged.
    assert json.loads(bodies[-1])["s3ArtifactLocations"]["inputKey"] == (
        f"{pre_signed}/Demo.merged.tar.gz"
    )
    assert inputs[-1] == every
    # Nothing reads the upload of the whole unsigned app, so it is not made.
    assert not (tmp_path / "bucket" / pre_signed / "Demo.tar.gz").exists()


def test_a_rejected_part_fails_the_job_and_prints_the_service_answer(tmp_path):
    done, _, _, bodies = _sign(tmp_path, 340, FAKE_REJECT_POST="2")

    assert done.returncode == 4
    assert "submission returned no signTaskId" in done.stderr
    assert "User is not authorized" in done.stderr
    assert len(bodies) == 1


def test_a_part_that_fails_signing_fails_the_job_before_the_app_is_signed(tmp_path):
    done, _, work, bodies = _sign(tmp_path, 340, FAKE_FAIL_TASK="1")

    assert done.returncode == 4
    assert "Signing failed" in done.stderr and "fake signing failure" in done.stderr
    assert all(".part-" in json.loads(b)["s3ArtifactLocations"]["outputKey"] for b in bodies)
    assert not (work / "signed").exists()


def test_every_part_answer_is_reported_before_the_job_fails(tmp_path):
    done, _, work, bodies = _sign(tmp_path, 340, FAKE_FAIL_TASK="1,2")

    assert done.returncode == 4
    assert "part 1 of 2 did not sign" in done.stderr
    assert "part 2 of 2 did not sign" in done.stderr
    assert len(bodies) == 2, "the app request must not go out after a failed part"
    assert not (work / "signed").exists()


def test_a_failed_app_signing_fails_the_job_and_prints_the_service_answer(tmp_path):
    done, _, work, _ = _sign(tmp_path, 340, FAKE_FAIL_FINAL="1")

    assert done.returncode == 4
    assert "Signing failed" in done.stderr and "fake signing failure" in done.stderr
    assert not (work / "signed").exists()
