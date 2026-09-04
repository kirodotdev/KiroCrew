"""A desktop build must record WHICH Electron it was packaged against.

``website/electron/package.json`` pins a RANGE (``^43.x``), so two builds a
month apart can resolve to different Electron versions with nothing recording
which was which. That matters more than it sounds: symbolizing a crash against
the wrong Electron does not fail loudly. ``atos`` happily prints the nearest
neighbouring symbol for every frame, producing a stack that reads like a real
call chain and is not -- which is how one main-process crash got filed against a
component that never appeared in it.

``scripts/emit-symbols-manifest.mjs`` writes the resolved version plus the exact
symbol-archive names and URLs, and ``scripts/symbolize-crash.sh`` consumes it.
The three ways that plumbing silently rots are what this file pins:

1. The emit step running BEFORE the build, where ``node_modules/electron`` does
   not yet exist or still holds a previous resolution.
2. The manifest not being in the upload globs, so it is written and discarded.
3. The manifest reaching only the CI artifact, which expires in 14 days, while
   the crash reports it decodes arrive from released builds months later.

None of the three shows up as a red build -- the artifact, or the release, is
simply missing the one file that makes a future crash report readable.

A third way it rots is not silent but dangerous: the symbolizer TRUSTS the
manifest. It reads an asset name out of it and makes that a path under the
download cache, and reads a URL out of it and fetches that. A manifest arrives as
a downloaded CI artifact, so neither value is this script's own, and a name
carrying ``../`` would put attacker-chosen bytes on a writable host file. The
last group of tests runs the real script against hostile manifests.

The final group covers the other half of that trust problem: a ``.ips`` carries
its own uuid and slice, and the script must keep reading them even when the
caller supplies ``--electron`` -- otherwise the one check that makes a wrong-build
dSYM loud is silently switched off by an argument that looks like extra precision.

Offline: the static checks read the workflow YAML and the script source, and the
execution checks all exit before the script reaches the network -- the ``.ips``
runs by pre-seeding both cached slices, the manifest runs by failing their gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
EMITTER = ROOT / "scripts" / "emit-symbols-manifest.mjs"
SYMBOLIZER = ROOT / "scripts" / "symbolize-crash.sh"

MANIFEST_PATH = "website/electron/dist/symbols-manifest.json"


def _steps(workflow: Path, job: str) -> list[dict]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return list(doc["jobs"][job]["steps"])


def _build_desktop_steps() -> list[dict]:
    return _steps(WORKFLOW, "build-desktop")


def _index_of(steps: list[dict], predicate) -> int:
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    return -1


def test_manifest_is_emitted_after_the_build() -> None:
    """The resolved Electron version only exists once the build has installed it.

    Emitting earlier would read a stale ``node_modules/electron`` (or none), and
    a manifest naming the wrong version is worse than no manifest: it sends
    someone to download symbols that will confidently mis-symbolize.
    """
    steps = _build_desktop_steps()
    build = _index_of(steps, lambda s: s.get("name") == "Build desktop app")
    emit = _index_of(steps, lambda s: "emit-symbols-manifest.mjs" in str(s.get("run", "")))

    assert build != -1, "build-desktop lost its 'Build desktop app' step"
    assert emit != -1, (
        "no step runs scripts/emit-symbols-manifest.mjs, so the built artifacts "
        "record no Electron version and a crash from them cannot be symbolized"
    )
    assert emit > build, (
        "the symbols manifest must be emitted AFTER the build: the resolved "
        "Electron version comes from the installed node_modules/electron"
    )


def test_manifest_is_uploaded_with_the_artifacts() -> None:
    """Written but not uploaded is the same as not written."""
    steps = _build_desktop_steps()
    upload = _index_of(
        steps,
        lambda s: str(s.get("uses", "")).startswith("actions/upload-artifact"),
    )
    assert upload != -1, "build-desktop lost its upload step"

    emit = _index_of(steps, lambda s: "emit-symbols-manifest.mjs" in str(s.get("run", "")))
    assert emit < upload, "the manifest must be written before the upload reads it"

    paths = str(steps[upload]["with"]["path"]).split()
    assert MANIFEST_PATH in paths, (
        f"{MANIFEST_PATH} is missing from the upload globs, so every build "
        "writes it and then throws it away"
    )


def test_the_windows_leg_records_its_own_pin() -> None:
    """Windows is the leg most stable users are running, and it builds separately.

    ``build-windows.yml`` exists apart from ``build-desktop.yml`` because it
    Authenticode-signs during the build, and it carries no channel ``if:`` in
    release.yml -- so unlike the mac/Linux legs it runs on stable too. A ``.dmp``
    records no semver, so without this manifest a Windows crash report has nothing
    naming the Electron it must be symbolized against.
    """
    steps = _steps(WINDOWS_WORKFLOW, "build-windows")
    build = _index_of(steps, lambda s: s.get("name") == "Build desktop app")
    emit = _index_of(steps, lambda s: "emit-symbols-manifest.mjs" in str(s.get("run", "")))
    upload = _index_of(
        steps, lambda s: str(s.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert build != -1, "build-windows lost its 'Build desktop app' step"
    assert emit != -1, (
        "build-windows.yml runs no emit-symbols-manifest.mjs, so every released "
        "Windows build ships with no Electron pin and its .dmp files cannot be "
        "symbolized"
    )
    assert emit > build, (
        "the manifest must be emitted AFTER the build: the resolved Electron "
        "version comes from the installed node_modules/electron"
    )
    assert upload != -1, "build-windows lost its upload step"
    assert emit < upload, "the manifest must be written before the upload reads it"
    assert (
        MANIFEST_PATH in str(steps[upload]["with"]["path"]).split()
    ), f"{MANIFEST_PATH} is missing from build-windows' upload globs"


def test_the_release_gate_cannot_red_a_stable_release() -> None:
    """The fail-closed check must fire on exactly the runs that build a desktop leg.

    ``build-desktop`` is ``if: channel == 'insider' || rebuild == 'true'``, and an
    ordinary stable release now ships a BARE version, so it REBUILDS from source
    (``rebuild == 'true'``) and runs those legs just as an insider does. The
    manifest requirement must fire on that same condition: a fresh stable build
    whose emit step broke would otherwise ship a green release with undecodable
    crash reports. Byte-for-byte promotion (``promote_mode`` -- stable with
    ``rebuild != 'true'``) is the one exempt mode: it builds nothing and republishes
    an insider build whose pin already exists, so requiring a manifest there would
    red a release that built nothing and publish no Release page at all -- the
    v0.3.0 incident release.yml's own comment records. Pinned because both failures
    only appear on a real tag, which no PR run exercises.
    """
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    assert (
        jobs["build-desktop"]["if"].strip()
        == "needs.version.outputs.channel == 'insider' || needs.version.outputs.rebuild == 'true'"
    ), (
        "build-desktop's channel condition changed; re-derive which runs can "
        "satisfy the symbols-manifest requirement below"
    )

    steps = list(jobs["github-release"]["steps"])
    assemble = _index_of(steps, lambda s: "Assemble release assets" in str(s.get("name", "")))
    script = str(steps[assemble]["run"])

    assert (
        'if { [ "$CHANNEL" = "insider" ] || [ "$REBUILD" = "true" ]; } && [ "$manifests" -eq 0 ]; then'
        in script
    ), (
        "the symbols-manifest requirement is not scoped to the runs that build a "
        "desktop leg (insider, or a stable rebuild), so either a fresh stable build "
        "ships undecodable crash reports or a byte promotion reds a release that "
        "built nothing"
    )
    env = steps[assemble].get("env", {})
    assert env.get("CHANNEL") == "${{ needs.version.outputs.channel }}", (
        "the assemble step reads $CHANNEL, so it must receive the resolved channel "
        "and nothing else. Merely being non-empty is not enough: a literal "
        "'CHANNEL: stable' would leave this suite green while making the guard dead "
        "code on every channel, which is the failure it exists to catch"
    )
    assert env.get("REBUILD") == "${{ needs.version.outputs.rebuild }}", (
        "the guard now also keys off $REBUILD, so the assemble step must receive the "
        "resolved rebuild flag; without it $REBUILD is empty and an ordinary stable "
        "rebuild silently falls back to the insider-only scope it had before"
    )


def test_the_manifest_reaches_the_durable_release_channel() -> None:
    """A 14-day artifact cannot serve a "symbolize months later" promise.

    The CI artifact expires with the job. Real crash reports arrive from users on
    released builds, long after that, so the pin has to sit on a channel that does
    not expire -- and a GitHub Release asset does not. Asserted here because the
    failure is silent in the worst way: the release publishes, everything is
    green, and the loss only surfaces when someone finally needs the pin and it is
    gone.

    Also asserts the per-leg rename. ``release/`` is flat and every desktop
    artifact holds a file with the same basename, so a plain copy would leave one
    arbitrary winner and drop the rest without saying so.
    """
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = list(doc["jobs"]["github-release"]["steps"])
    assemble = _index_of(steps, lambda s: "Assemble release assets" in str(s.get("name", "")))
    assert assemble != -1, "release.yml lost its release-asset assembly step"

    script = str(steps[assemble]["run"])
    assert "symbols-manifest.json" in script, (
        "release.yml does not copy symbols-manifest.json into the release, so the "
        "Electron pin for every published build expires with the CI artifact"
    )
    assert "symbols-manifest-${slug}.json" in script, (
        "the manifests must be renamed per leg on the way into the flat release/ "
        "directory, or all but one are silently overwritten"
    )

    upload = _index_of(
        steps, lambda s: str(s.get("uses", "")).startswith("softprops/action-gh-release")
    )
    assert upload != -1, "release.yml lost its GitHub Release step"
    assert "release/*" in str(steps[upload]["with"]["files"]), (
        "the release upload no longer publishes everything in release/, so the "
        "assembled manifest may not be attached"
    )


def test_the_symbolizer_ships_alongside_the_manifest() -> None:
    """A manifest nobody can act on is a decoration.

    The manifest's own ``symbolize`` field names the consumer, so that path has
    to exist and be runnable.
    """
    assert EMITTER.exists(), f"missing {EMITTER.relative_to(ROOT)}"
    assert SYMBOLIZER.exists(), f"missing {SYMBOLIZER.relative_to(ROOT)}"

    # The mode recorded in git, not the one on this filesystem. NTFS carries no
    # execute bit, so `st_mode & 0o111` is unconditionally false on a Windows
    # checkout -- it fails there while the file is perfectly executable for every
    # clone that matters. The index mode is what a fresh clone and the release
    # tarball actually receive, so it is the thing worth asserting.
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "--", "scripts/symbolize-crash.sh"],
        capture_output=True,
        timeout=120,
        **UTF8_TEXT,
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        pytest.skip("not a git checkout, so the recorded file mode cannot be read")
    mode = listed.stdout.split()[0]
    assert mode == "100755", (
        f"scripts/symbolize-crash.sh is recorded as {mode}, not 100755: a clone "
        "gets a non-executable symbolizer and the manifest names a script "
        "nobody can run"
    )

    emitter_source = EMITTER.read_text(encoding="utf-8")
    assert (
        '"scripts/symbolize-crash.sh"' in emitter_source
    ), "the manifest's symbolize field must name a script that exists"


def test_the_emitter_reads_the_resolved_version_not_the_range() -> None:
    """The whole point: never the ``^43.x`` range from package.json.

    A range does not identify a binary. This is a source assertion rather than
    an execution one because running the emitter needs an installed Electron,
    which a bare checkout does not have.
    """
    source = EMITTER.read_text(encoding="utf-8")
    assert (
        'node_modules", "electron", "package.json"' in source
    ), "the emitter must read the RESOLVED version from the installed Electron"


def test_both_symbol_formats_are_recorded_for_macos() -> None:
    """The two archives are not interchangeable inputs.

    ``atos`` reads Mach-O DWARF out of a dSYM to symbolize a ``.ips`` report;
    ``minidump_stackwalk`` reads Breakpad ``.sym`` text to walk a Crashpad
    ``.dmp``. ``crash-collector.js`` surfaces both artifact kinds, so a manifest
    listing only one leaves half the crashes unreadable.
    """
    source = EMITTER.read_text(encoding="utf-8")
    assert "-dsym.tar.xz" in source, "no dSYM asset: a .ips report cannot be symbolized"
    assert "-symbols.zip" in source, "no Breakpad asset: a .dmp cannot be walked"

    symbolizer = SYMBOLIZER.read_text(encoding="utf-8")
    assert "-dsym.tar.xz" in symbolizer and "-symbols.zip" in symbolizer
    assert "dwarfdump --uuid" in symbolizer, (
        "the symbolizer must cross-check the dSYM UUID against the report's "
        "framework image -- a mismatched dSYM prints plausible, WRONG frames "
        "rather than failing, and that silent failure is the whole hazard"
    )


# --- The manifest is untrusted input ---------------------------------------
#
# These run the real script. It needs bash (for the script) and node (to parse
# the manifest), and exits at the validation gate well before any download, so
# there is no network dependency -- but there IS a toolchain one, hence the skip.

VERSION = "43.2.0"
OFFICIAL = f"https://github.com/electron/electron/releases/download/v{VERSION}"

# Windows is excluded by platform, not by tool discovery. `shutil.which("bash")`
# succeeds on a GitHub Windows runner and resolves to the WSL launcher, which
# answers every invocation with a UTF-16 "no installed distributions" notice and
# exit 1 -- so these tests failed with the gate's message simply absent from an
# empty stderr, which reads as "the gate is gone" rather than "no shell ran it".
# The symbolizer is a macOS/Linux tool by construction (it drives `atos`,
# `dwarfdump` and `minidump_stackwalk`); its contract is asserted on the hosts
# that can execute it.
requires_shell_and_node = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("node") is None,
    reason="the symbolizer is a posix bash script that parses its manifest with node",
)


def _manifest(tmp_path: Path, name: str, url: str) -> Path:
    """A one-asset darwin manifest in the shape the emitter produces."""
    path = tmp_path / "symbols-manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "appVersion": "0.0.0-test",
                "platform": "darwin",
                "arches": ["arm64"],
                "electron": {"version": VERSION},
                "assets": [{"arch": "arm64", "kind": "breakpad", "name": name, "url": url}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _emitter_asset(kind: str, platform: str, arch: str) -> dict[str, str]:
    """Ask the emitter itself what it would name a given asset.

    Deliberately not a hardcoded string. The symbolizer compares the manifest's
    asset name and URL for EQUALITY against the one it derives, and that gate is
    only lossless because ``assetsFor`` is the single formula both sides mean. A
    literal here would keep passing after the formula changed, while every real
    manifest started being refused as forged -- the exact drift the gate cannot
    detect for itself.
    """
    out = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "const [, url, kind, platform, arch, version] = process.argv;"
            "const m = await import(url);"
            "const a = m.assetsFor(platform, arch, version).find((x) => x.kind === kind);"
            "process.stdout.write(JSON.stringify(a || null));",
            EMITTER.resolve().as_uri(),
            kind,
            platform,
            arch,
            VERSION,
        ],
        capture_output=True,
        timeout=120,
        check=True,
        **UTF8_TEXT,
    )
    asset = json.loads(out.stdout)
    assert asset, f"the emitter publishes no {kind} asset for {platform}-{arch}"
    return asset


def _symbolize(tmp_path: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    artifact = tmp_path / "crash.dmp"
    artifact.write_bytes(b"")  # never parsed: the gate is upstream of the walker
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return subprocess.run(
        [
            "bash",
            str(SYMBOLIZER),
            str(artifact),
            "--manifest",
            str(manifest),
            "--arch",
            "arm64",
        ],
        capture_output=True,
        timeout=120,
        # Pin every temp write under tmp_path: symbolize-crash.sh runs `mktemp -d`,
        # and if a timeout or a worker kill bypasses its EXIT trap the residue must
        # land in the test's own dir, not the shared host temp. cwd covers a
        # relative mktemp; TMPDIR steers the absolute default it uses here.
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "KIROCREW_SYMBOL_CACHE": str(cache),
        },
        **UTF8_TEXT,
    )


@requires_shell_and_node
def test_a_manifest_asset_name_cannot_escape_the_symbol_cache(tmp_path: Path) -> None:
    """``$CACHE/$ASSET`` is a path, and the manifest is a downloaded file.

    The asset name becomes the download target and then the extraction source, so
    a name containing ``../`` writes attacker-chosen bytes to an attacker-chosen
    place. Refused outright rather than trimmed to its basename: a repaired path
    is still a manifest that lied about what this build published.
    """
    manifest = _manifest(
        tmp_path,
        "../../../../tmp/pwned-symbols.zip",
        f"{OFFICIAL}/../../../../tmp/pwned-symbols.zip",
    )
    result = _symbolize(tmp_path, manifest)

    assert result.returncode != 0, "a traversing asset name was accepted"
    assert "refusing asset name" in result.stderr
    assert not (tmp_path / "pwned-symbols.zip").exists()
    assert not (tmp_path / "pwned-symbols.zip.part").exists()


@requires_shell_and_node
@pytest.mark.parametrize(
    "injected",
    [
        "$(id)",  # command substitution: the payload
        "`id`",  # its older spelling, which the same expansion still honours
        " x",  # a space, which splits one argument into two
        '"',  # a quote, which ends the string it was interpolated into
    ],
    ids=["dollar-paren", "backtick", "space", "quote"],
)
def test_a_manifest_asset_name_cannot_carry_shell_syntax(tmp_path: Path, injected: str) -> None:
    """A traversal gate is not enough: this name reaches a command line, not just a path.

    The asset name is interpolated into a ``gh api --jq`` PROGRAM, and that
    program's output becomes ``$SIZE``, which is then fed to ``$(( ))`` --
    arithmetic expansion, which re-evaluates its operand. So a name of
    ``...arm64$(id)-symbols.zip`` is a live command-execution route with no path
    separator anywhere in it, and every name below passes the two SHAPE globs
    (``electron-v*-symbols.zip`` matches ``*`` against ``$``, a space and a quote
    alike). Only bounding the characters closes it.
    """
    name = f"electron-v{VERSION}-darwin-arm64{injected}-symbols.zip"
    manifest = _manifest(tmp_path, name, f"{OFFICIAL}/{name}")
    result = _symbolize(tmp_path, manifest)

    assert result.returncode != 0, f"an asset name containing {injected!r} was accepted"
    assert "refusing asset name" in result.stderr
    # Nothing may have been fetched or unpacked on the way to the refusal.
    assert not list((tmp_path / "cache").iterdir()), "the gate ran after touching the cache"


@requires_shell_and_node
def test_a_manifest_url_must_be_electrons_own_release(tmp_path: Path) -> None:
    """An honest-looking name with a chosen URL is the same attack, one step later.

    The cache filename is harmless here; the bytes are not. Symbols come from
    electron/electron's releases and nowhere else, so the URL is compared for
    equality against the one the name implies.
    """
    name = f"electron-v{VERSION}-darwin-arm64-symbols.zip"
    manifest = _manifest(tmp_path, name, f"https://example.invalid/{name}")
    result = _symbolize(tmp_path, manifest)

    assert result.returncode != 0, "an off-origin symbol URL was accepted"
    assert "refusing download URL" in result.stderr
    assert "example.invalid" in result.stderr, "the refusal should show what it read"


@requires_shell_and_node
def test_an_implausible_electron_version_is_refused(tmp_path: Path) -> None:
    """The version is interpolated into the URL, so it is checked too.

    It reaches the script from three places -- the crash report, the manifest, and
    ``--electron`` -- and none of them is this script's own.
    """
    name = f"electron-v{VERSION}-darwin-arm64-symbols.zip"
    manifest = _manifest(tmp_path, name, f"{OFFICIAL}/{name}")
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(f'"{VERSION}"', '"../../x"', 1),
        encoding="utf-8",
    )
    result = _symbolize(tmp_path, manifest)

    assert result.returncode != 0, "a traversing Electron version was accepted"
    assert "implausible Electron version" in result.stderr


@requires_shell_and_node
def test_a_manifest_from_a_future_schema_is_refused_by_name(tmp_path: Path) -> None:
    """The emitter stamps ``schema``; the symbolizer has to be the one reading it.

    A schema bump that relocates ``assets[].url`` would otherwise surface as
    "lists no breakpad asset for arch arm64" -- which reads as a broken build and
    sends the reader to the wrong file. The version the writer declares is the
    cheapest possible way to say "this reader is the stale one".
    """
    asset = _emitter_asset("breakpad", "darwin", "arm64")
    manifest = _manifest(tmp_path, asset["name"], asset["url"])
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('"schema": 1', '"schema": 2', 1),
        encoding="utf-8",
    )
    result = _symbolize(tmp_path, manifest)

    assert result.returncode != 0, "an unknown manifest schema was read anyway"
    assert "declares schema '2'" in result.stderr
    assert (
        "emit-symbols-manifest.mjs" in result.stderr
    ), "the refusal must name the writer, so the two can be updated together"


@requires_shell_and_node
def test_the_manifest_the_emitter_actually_writes_passes_the_gate(tmp_path: Path) -> None:
    """The gate has to be lossless for honest input, or it just breaks the tool.

    Exact matching is only safe because ``assetsFor`` derives every name and URL
    from (platform, arch, version) by one formula, so the name and URL here come
    from calling that function rather than from a literal: this is the one test
    that fails if the emitter's formula and the symbolizer's gate ever disagree.
    Pre-seeds the extracted directory so the run stops at the missing stackwalker
    rather than downloading 128 MB.
    """
    asset = _emitter_asset("breakpad", "darwin", "arm64")
    name, url = asset["name"], asset["url"]
    manifest = _manifest(tmp_path, name, url)
    (tmp_path / "cache" / name[: -len(".zip")]).mkdir(parents=True)

    result = _symbolize(tmp_path, manifest)

    combined = result.stdout + result.stderr
    assert "refusing" not in combined, f"the gate rejected honest input:\n{combined}"
    assert "implausible" not in combined
    assert f"Asset: {name}" in result.stdout
    assert "(cached)" in result.stdout, "the gate ran before the cache lookup"


# --- The .ips report's own evidence, which --electron must not suppress -------
#
# A `.ips` carries three separate facts about the crash: which Electron version,
# which framework UUID, and which slice. Only the first is something a caller can
# legitimately assert with `--electron`; the other two are evidence about the
# report in hand and have no substitute. Parsing them was once gated on
# `--electron` being absent, so passing it left `REPORT_UUID` empty and silently
# disabled the dwarfdump gate -- the one check that turns a wrong-build dSYM into
# an error instead of a plausible, wrong stack.
#
# Both tests below run the real script and stop long before the network: the
# messages they assert on are printed while the report is being parsed, upstream
# of every tool and platform check.


def _ips_report(tmp_path: Path, version: str, uuid: str, arch: str) -> Path:
    """A minimal `.ips` in the two-part shape macOS writes.

    Line 1 is a JSON summary header, the remainder is the payload -- which is why
    the script parses `slice(1)` rather than the whole file.
    """
    path = tmp_path / "Kiro Crew-2026-09-04-120000.ips"
    header = {"app_name": "Kiro Crew", "timestamp": "2026-09-04 12:00:00.00 +0000"}
    payload = {
        "usedImages": [
            {"name": "dyld", "path": "/usr/lib/dyld", "uuid": "0" * 36, "arch": arch},
            {
                "name": "Electron Framework",
                "path": "/Applications/Kiro Crew.app/Contents/Frameworks/"
                "Electron Framework.framework/Versions/A/Electron Framework",
                "CFBundleShortVersionString": version,
                "uuid": uuid,
                "arch": arch,
            },
        ]
    }
    path.write_text(
        json.dumps(header) + "\n" + json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _symbolize_ips(tmp_path: Path, report: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the symbolizer on a ``.ips``, with BOTH slices pre-seeded in the cache.

    Seeding both rather than the expected one is deliberate. Seeding only the
    expected slice makes a regression miss the cache and start a 1.4 GB download,
    so the test reports a 120-second timeout instead of the assertion that would
    name the defect -- and does real network I/O on the way. With both present the
    run never fetches, and the asset name it prints is what discriminates: the seed
    bounds the cost without deciding the outcome.
    """
    cache = tmp_path / "cache"
    for arch in ("arm64", "x64"):
        (cache / f"electron-v{VERSION}-darwin-{arch}-dsym").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(SYMBOLIZER), str(report), *args],
        capture_output=True,
        timeout=120,
        # Pin every temp write under tmp_path: symbolize-crash.sh runs `mktemp -d`,
        # and if a timeout or a worker kill bypasses its EXIT trap the residue must
        # land in the test's own dir, not the shared host temp. cwd covers a
        # relative mktemp; TMPDIR steers the absolute default it uses here.
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "KIROCREW_SYMBOL_CACHE": str(cache),
        },
        **UTF8_TEXT,
    )


@requires_shell_and_node
def test_explicit_electron_does_not_suppress_the_reports_own_uuid(tmp_path: Path) -> None:
    """``--electron`` asserts which build to FETCH, never that it matches the report.

    The two are independent: an operator naming a version is stating a belief, and
    the uuid comparison is what tests that belief. Suppressing the parse because a
    version was supplied removed the test and kept the belief -- so ``.ips`` plus
    ``--electron`` symbolized against whatever was fetched and printed frames with
    no warning. Asserted through the mismatch NOTE because it names both versions,
    which is only possible if the report was parsed with ``--electron`` in hand.
    """
    report = _ips_report(tmp_path, "43.9.9", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "arm64")

    result = _symbolize_ips(tmp_path, report, "--electron", VERSION)

    combined = result.stdout + result.stderr
    assert "43.9.9" in combined, (
        "the report's version never appeared, so the report was not parsed at all "
        f"when --electron was supplied:\n{combined}"
    )
    assert f"--electron {VERSION} overrides" in combined
    assert f"Fetching {VERSION}" in combined, "the explicit version must win, not the report's"
    assert "uuid gate" in combined, "the operator is not told the gate still applies"


@requires_shell_and_node
def test_the_reports_arch_beats_this_host_even_with_an_explicit_version(tmp_path: Path) -> None:
    """A universal app can crash in either slice, and only the report says which.

    ``--electron`` says nothing about the slice, so falling back to ``uname -m``
    here fetches the other arch's dSYM on any host that does not happen to match
    the report -- and a dSYM for the wrong slice is exactly the input the uuid gate
    exists to catch, reached by a route that had disabled it.

    The absent host-fallback line is the portable half of the assertion; the
    positive half is only observable where a ``.ips`` run gets as far as naming an
    asset, which is macOS by construction. Elsewhere the run stops at the
    macOS-artifact refusal, and asserting on that keeps the absence check honest --
    it proves the run reached asset selection rather than exiting before the arch
    was ever resolved.
    """
    report = _ips_report(tmp_path, VERSION, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "x86_64")

    result = _symbolize_ips(tmp_path, report, "--electron", VERSION)

    combined = result.stdout + result.stderr
    assert (
        "from this host; pass --arch" not in combined
    ), f"the arch was guessed from this host while the report named x86_64:\n{combined}"
    if sys.platform == "darwin":
        assert (
            f"electron-v{VERSION}-darwin-x64-dsym" in combined
        ), f"the report named x86_64, so the x64 dSYM is the one to fetch:\n{combined}"
    else:
        assert (
            ".ips report is a macOS artifact" in combined
        ), f"the run stopped before arch resolution, so the check above proves nothing:\n{combined}"


# --- The dwarfdump UUID gate compares for EQUALITY, never as a substring ------
#
# The gate that turns a wrong-build dSYM into an error (rather than a plausible,
# wrong stack) compared the report's uuid against the whole `dwarfdump --uuid`
# blob with a shell `*"$NORM_REPORT"*` glob. A report uuid shorter than the full
# 128 bits then matched the MIDDLE of an unrelated dSYM uuid and printed a false
# `✓` -- the one outcome the gate exists to prevent. The fix requires a full
# 32-hex uuid and tests equality against each uuid dwarfdump actually parsed.
#
# Reaching the gate needs a cached dSYM tree (so no 1.4 GB download) and a
# `dwarfdump` that returns a controlled uuid. Both are macOS-only: on Linux the
# `.ips` run stops at the macOS-artifact refusal long before the gate.


def _symbolize_ips_at_uuid_gate(
    tmp_path: Path, report_uuid: str, dsym_uuid: str, arch: str = "arm64"
) -> subprocess.CompletedProcess[str]:
    """Run the symbolizer on a `.ips` far enough to hit the dwarfdump uuid gate.

    Seeds `$CACHE/electron-v<V>-darwin-<arch>-dsym` with a DWARF binary at the
    path the script's `find` looks for, so the cached branch is taken and no
    download happens, and puts a fake `dwarfdump` on PATH that reports
    `dsym_uuid`. The report carries `report_uuid`.
    """
    report = _ips_report(tmp_path, VERSION, report_uuid, arch)

    cache = tmp_path / "cache"
    dwarf = (
        cache
        / f"electron-v{VERSION}-darwin-{arch}-dsym"
        / "Electron Framework.dSYM"
        / "Contents"
        / "Resources"
        / "DWARF"
        / "Electron Framework"
    )
    dwarf.parent.mkdir(parents=True, exist_ok=True)
    dwarf.write_bytes(b"not a real macho; the fake dwarfdump answers for it")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    dwarfdump = fakebin / "dwarfdump"
    # dwarfdump --uuid prints one "UUID: <uuid> (<arch>) <path>" line per slice.
    dwarfdump.write_text(
        "#!/usr/bin/env bash\n" f'echo "UUID: {dsym_uuid} ({arch}) /Electron Framework"\n',
        encoding="utf-8",
    )
    dwarfdump.chmod(0o755)

    return subprocess.run(
        ["bash", str(SYMBOLIZER), str(report)],
        capture_output=True,
        timeout=120,
        cwd=tmp_path,
        env={
            "PATH": f"{fakebin}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "KIROCREW_SYMBOL_CACHE": str(cache),
        },
        **UTF8_TEXT,
    )


_full_dsym_uuid = "12345678-9ABC-DEF0-1234-56789ABCDEF0"


@requires_shell_and_node
@pytest.mark.skipif(
    sys.platform != "darwin", reason="the .ips dwarfdump uuid gate is only reached on macOS"
)
def test_a_truncated_report_uuid_cannot_substring_match_a_dsym(tmp_path: Path) -> None:
    """A partial uuid that is a PREFIX of the dSYM uuid must be refused, not ✓'d.

    `"12345678"` normalizes to a strict prefix of the fake dSYM uuid, so the old
    `*"$NORM_REPORT"*` glob matched and printed a false `✓`. The gate must now
    reject it as not a full 128-bit uuid instead of verifying against it.
    """
    result = _symbolize_ips_at_uuid_gate(tmp_path, "12345678", _full_dsym_uuid)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"a partial uuid was accepted:\n{combined}"
    assert "not a full 128-bit UUID" in combined, combined
    assert (
        "matches the report's framework image" not in combined
    ), f"a truncated uuid substring-matched the dSYM and printed a false verify:\n{combined}"


@requires_shell_and_node
@pytest.mark.skipif(
    sys.platform != "darwin", reason="the .ips dwarfdump uuid gate is only reached on macOS"
)
def test_a_full_but_mismatched_report_uuid_is_refused(tmp_path: Path) -> None:
    """A full uuid that does not equal the dSYM's is a wrong-build dSYM -> error."""
    result = _symbolize_ips_at_uuid_gate(
        tmp_path, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", _full_dsym_uuid
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"a mismatched dSYM was accepted:\n{combined}"
    assert "does not match the crash report's framework image" in combined, combined


@requires_shell_and_node
@pytest.mark.skipif(
    sys.platform != "darwin", reason="the .ips dwarfdump uuid gate is only reached on macOS"
)
def test_a_full_matching_report_uuid_verifies(tmp_path: Path) -> None:
    """The report uuid equal to the dSYM's (case/dash-insensitive) verifies with ✓."""
    result = _symbolize_ips_at_uuid_gate(
        tmp_path, "12345678-9abc-def0-1234-56789abcdef0", _full_dsym_uuid
    )

    combined = result.stdout + result.stderr
    assert (
        "matches the report's framework image" in combined
    ), f"an exact uuid match was not verified:\n{combined}"
