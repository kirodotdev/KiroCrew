#!/usr/bin/env python3
"""Dependency doctor for the voiceover-driven-video pipeline.

    python3 deps.py            # check only, exit 1 if anything is missing
    python3 deps.py --install  # check, then install what can be installed
    python3 deps.py --json     # machine-readable report

Policy: only USER-LEVEL installs (pip --user, the playwright browser cache, the
npx cache). Nothing here runs sudo or touches system packages -- if a dependency
genuinely needs root, this exits non-zero and prints the exact command for a human
to run. A doctor that silently escalates is worse than one that stops.

It also REUSES tooling that is already on disk instead of reinstalling it: a
Playwright venv and a static ffmpeg are commonly already present, and rebuilding
them into a temp dir wastes minutes and inodes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

# The deploy engine owns the repo-wide ``aws``-CLI resolution chokepoint, and
# narrate.py's polly spawn resolves through it -- so this doctor must probe the
# same binary, or its verdict disagrees with what the pipeline actually runs
# under a GUI-launched gateway's minimal PATH. The doctor also has to stay
# runnable WITHOUT kiro_crew on sys.path (diagnosing that environment is part
# of its job), so the resolver is an optional dependency: when it is not
# importable the probe degrades to the bare-name PATH lookup so the remaining
# checks still run. (In that environment narrate.py cannot import either, so
# spawn-parity is moot there; the fallback keeps the doctor itself alive.)
try:
    from kiro_crew.deploy.engine import resolve_aws_bin
except ImportError:  # kiro_crew not importable for this interpreter

    def resolve_aws_bin() -> str:
        """Bare-name fallback matching the plain PATH-probe behaviour."""
        return "aws"


def _home() -> pathlib.Path:
    """Resolved on call, never bound at import.

    A module-level Path.home() is a host-isolation hazard: any test that
    imports this module and reaches the binding writes the operator's real
    home, which is why the repo's isolation floor rejects it.
    """
    return pathlib.Path.home()


# ~/.local/bin is the standard user bin dir, so it is worth looking in when a tool
# is not on PATH. Anything more specific than that is one machine's layout and does
# not belong in a shipped skill -- point at an existing interpreter with
# KC_VIDEO_PW_PYTHON instead of hardcoding somebody's venv.
def _ffmpeg_dirs() -> list[pathlib.Path]:
    """Resolved per call, not at import.

    A module-level list holding a home-derived path is still an import-time data
    home resolution, which the repo's isolation floor rejects (issue #874): a test
    that imports this module would bind the operator's real home.
    """
    return [_home() / ".local/bin"]


def _candidate_pw_pythons() -> list[pathlib.Path]:
    """Interpreters that might already have playwright, best first.

    Naming only KC_VIDEO_PW_PYTHON is not enough in practice: a first-time
    reader usually HAS a working interpreter and does not know which, so the
    doctor told them to pip-install into the wrong one. These are standard
    locations, never a hardcoded personal venv.
    """
    out: list[pathlib.Path] = []
    env = os.environ.get("KC_VIDEO_PW_PYTHON")
    if env:
        out.append(pathlib.Path(env))
    on_path = shutil.which("python3")
    if on_path and pathlib.Path(on_path).resolve() != pathlib.Path(sys.executable).resolve():
        out.append(pathlib.Path(on_path))
    for pattern in (
        ".local/share/mise/installs/python/*/bin/python3",
        ".pyenv/versions/*/bin/python3",
    ):
        out.extend(sorted(_home().glob(pattern), reverse=True))
    seen: set[str] = set()
    uniq: list[pathlib.Path] = []
    for cand in out:
        key = str(cand)
        if key not in seen:
            seen.add(key)
            uniq.append(cand)
    return uniq


HYPERFRAMES_VERSION = "0.7.109"
# The default npm registry here is an authenticated internal mirror that answers
# E401 for public packages. Every npx call must name the public registry.
PUBLIC_REGISTRY = "https://registry.npmjs.org"


class Result(dict):
    def __init__(self, name, ok, detail, fix=None, installable=False):
        super().__init__(name=name, ok=ok, detail=detail, fix=fix, installable=installable)


def run(cmd, **kw):
    """Run a probe and never raise: a doctor that dies is worse than a red row.

    A slow registry or a wedged binary would otherwise propagate TimeoutExpired
    out of one check and abandon every remaining one. The synthetic result keeps
    the caller's returncode/stdout contract so no probe needs to special-case it.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {exc.timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _imageio_ffmpeg() -> str | None:
    """pip install imageio-ffmpeg puts NO executable on PATH; it bundles one
    inside the package, so a doctor that only re-checks PATH stays red after a
    successful install."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and pathlib.Path(exe).is_file() else None
    except Exception:
        return None


def which_any(binary: str, extra_dirs: list[pathlib.Path]) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for d in extra_dirs:
        cand = d / binary
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def check_speech() -> Result:
    """Report the speech providers, in the order `narrate.py --provider auto` prefers.

    Two providers, both chosen so narration text has only two possible destinations:
    this machine (piper) or the caller's own cloud account (polly). Claiming "auto picks
    piper" from the BINARY alone would be a lie -- resolve_provider() also needs a voice
    model, so without one auto falls through to polly.
    """
    piper_bin = shutil.which("piper")
    piper_model = os.environ.get("KC_VIDEO_PIPER_MODEL", "")
    piper_ready = bool(
        piper_bin and piper_model and pathlib.Path(os.path.expanduser(piper_model)).is_file()
    )
    if piper_ready:
        return Result("speech", True, "auto picks piper -- local, nothing leaves the machine")
    # Probe the same binary narrate.py's resolved polly spawn executes, so the
    # doctor's verdict agrees with the pipeline under a minimal PATH.
    if shutil.which(resolve_aws_bin()):
        detail = "auto picks polly -- runs in YOUR AWS account (costs a little)"
        if piper_bin:
            detail += "; piper is installed but has no model, so auto skips it. Set "
            detail += "KC_VIDEO_PIPER_MODEL and pass --piper-model for the local path"
        return Result("speech", True, detail)
    return Result(
        "speech",
        False,
        "neither piper (local) nor the aws CLI (polly) is available, and this pipeline "
        "has no third-party speech fallback",
        fix="install piper plus a voice model, or the aws CLI with credentials, or "
        "render with narrate.py --silent",
        installable=False,
    )


def check_ffmpeg() -> Result:
    # An imageio-provided ffmpeg is NOT on PATH, so reporting a bare pass would
    # leave the documented transcode step failing on a green doctor. The resolved
    # path is reported, and the pipeline's own tool lookups fall back the same way.
    exe = which_any("ffmpeg", _ffmpeg_dirs()) or _imageio_ffmpeg()
    if not exe:
        return Result(
            "ffmpeg",
            False,
            "not found",
            fix=[sys.executable, "-m", "pip", "install", "--user", "imageio-ffmpeg"],
            installable=True,
        )
    enc = run([exe, "-hide_banner", "-encoders"])
    if "libx264" not in enc.stdout:
        # Playwright bundles a vp8/webm-only build with no libx264 and no mp4 muxer.
        return Result(
            "ffmpeg",
            False,
            f"{exe} has no libx264 encoder",
            fix=[sys.executable, "-m", "pip", "install", "--user", "imageio-ffmpeg"],
            installable=True,
        )
    return Result("ffmpeg", True, f"{exe} (libx264 present)")


def check_ffprobe() -> Result:
    exe = which_any("ffprobe", _ffmpeg_dirs())
    if exe:
        return Result("ffprobe", True, exe)
    # imageio-ffmpeg bundles ffmpeg ONLY, so pip cannot supply this one -- claiming it
    # is installable would leave --install reporting success against a still-red check.
    return Result(
        "ffprobe",
        False,
        "not found -- durations cannot be measured, and pip cannot supply it "
        "(imageio-ffmpeg ships ffmpeg only)",
        fix="install a full ffmpeg build yourself (static release or package manager); "
        "ffprobe ships alongside ffmpeg",
        installable=False,
    )


def _playwright_probe(py: str):
    """Import-only probe. Testing `playwright.__version__` is a FALSE NEGATIVE trap:
    the package does not define that attribute, so the probe fails with the import
    working fine -- and the doctor then reinstalls something already present."""
    return run(
        [
            py,
            "-c",
            "import playwright, importlib.metadata as m;"
            "print(m.version('playwright') if m else '?')",
        ]
    )


def check_playwright() -> Result:
    """Playwright must be importable by the interpreter that will RUN the recorder.

    Reporting success because some other venv has it is how a green doctor turns into
    a ModuleNotFoundError: the documented command is `python3 record.py`.
    """
    probe = _playwright_probe(sys.executable)
    if probe.returncode == 0:
        return Result("playwright", True, f"{sys.executable} (playwright {probe.stdout.strip()})")
    for py in _candidate_pw_pythons():
        if py.is_file() and _playwright_probe(str(py)).returncode == 0:
            return Result(
                "playwright",
                True,
                f"not importable by {sys.executable}, but present in {py} -- "
                f"run the recorder with that interpreter (KC_VIDEO_PW_PYTHON={py})",
            )
    return Result(
        "playwright",
        False,
        f"not importable by {sys.executable}",
        fix=[sys.executable, "-m", "pip", "install", "--user", "playwright"],
        # ...or point KC_VIDEO_PW_PYTHON at an interpreter that already has it.
        installable=True,
    )


def _recorder_python() -> str:
    """The interpreter the recorder will actually use, or "" if there is none."""
    if _playwright_probe(sys.executable).returncode == 0:
        return sys.executable
    for py in _candidate_pw_pythons():
        if py.is_file() and _playwright_probe(str(py)).returncode == 0:
            return str(py)
    return ""


def check_chromium() -> Result:
    # A cache glob is NOT the check: after a playwright upgrade the old revision
    # is still on disk while the one this playwright requires is missing, and the
    # crash surfaces only at chromium.launch() inside the recorder. The bundle
    # this replaced guaranteed the match by running `playwright install chromium`;
    # so ask the installed playwright which executable IT requires and whether
    # that file exists. Revision-accurate, and no browser has to be started.
    py = _recorder_python()
    if not py:
        return Result(
            "chromium",
            False,
            "cannot check: no interpreter with playwright (see the playwright row)",
            fix="install playwright first, then re-run this doctor",
            installable=False,
        )
    probe = run(
        [
            py,
            "-c",
            "from playwright.sync_api import sync_playwright;"
            "p=sync_playwright().start();"
            "print(p.chromium.executable_path);"
            "p.stop()",
        ]
    )
    exe = probe.stdout.strip().splitlines()[-1] if probe.stdout.strip() else ""
    if probe.returncode == 0 and exe and pathlib.Path(exe).exists():
        return Result("chromium", True, f"revision required by playwright present: {exe}")
    detail = (
        f"playwright requires {exe} which is missing"
        if exe
        else "playwright could not name a chromium build"
    )
    return Result(
        "chromium",
        False,
        detail,
        fix=[py, "-m", "playwright", "install", "chromium"],
        installable=True,
    )


def check_node() -> Result:
    exe = shutil.which("node")
    if not exe:
        return Result(
            "node",
            False,
            "not found",
            fix="install Node.js 20+ yourself (nvm, mise, or your package manager) "
            "-- this doctor will not install a language runtime",
            installable=False,
        )
    ver = run([exe, "-v"]).stdout.strip()
    major = 0
    try:
        major = int(ver.lstrip("v").split(".")[0])
    except ValueError:
        pass
    if major < 18:
        return Result(
            "node",
            False,
            f"{ver} is too old (need 18+)",
            fix="upgrade Node.js to 20+ yourself",
            installable=False,
        )
    return Result("node", True, f"{exe} {ver}")


def check_hyperframes() -> Result:
    """Ask the registry WHETHER the pinned version exists -- never run it.

    `npx --yes <pkg>` downloads AND executes, so using it as a dependency check
    means a project-local .npmrc pointing at another registry gets attacker code
    run by the thing whose whole job is to tell you the environment is safe. A
    metadata query answers the same question without execution.

    The registry is named explicitly on every call for the same reason, and the
    caller's configured one is still tried FIRST: force-overriding it would break a
    legitimately proxied setup, while falling back covers an authenticated mirror
    that answers E401 for public packages. Which one worked is reported, because the
    render step needs the same choice.
    """
    if not shutil.which("npm"):
        return Result(
            "hyperframes",
            False,
            "npm missing (comes with Node)",
            fix="install Node.js 20+ yourself",
            installable=False,
        )
    spec = f"hyperframes@{HYPERFRAMES_VERSION}"
    default_reg = run(["npm", "view", spec, "version"], timeout=120)
    if default_reg.returncode == 0 and default_reg.stdout.strip():
        return Result(
            "hyperframes",
            True,
            f"{spec} resolvable via the configured registry (metadata only, not executed)",
        )
    pub = run(
        ["npm", "view", spec, "version", f"--registry={PUBLIC_REGISTRY}"],
        timeout=120,
    )
    if pub.returncode == 0 and pub.stdout.strip():
        return Result(
            "hyperframes",
            True,
            f"{spec} resolvable only via {PUBLIC_REGISTRY} (metadata only, not "
            f"executed) -- pass --registry={PUBLIC_REGISTRY} when rendering",
        )
    detail = (default_reg.stderr or pub.stderr or "registry did not answer").strip()
    return Result(
        "hyperframes",
        False,
        f"{spec} not resolvable: {detail[-160:]}",
        fix=f"npm view {spec} version --registry={PUBLIC_REGISTRY}",
        installable=False,
    )


def check_cjk_font() -> Result:
    """Only needed for CJK narration captions, but a missing declaration fails the
    render gate rather than silently degrading, so report which family exists."""
    if not shutil.which("fc-list"):
        return Result("cjk-font", True, "fc-list absent; skipping (only needed for CJK captions)")
    out = run(["fc-list", ":lang=zh", "family"]).stdout
    families = sorted(
        {f.strip() for line in out.splitlines() for f in line.split(",") if f.strip()}
    )
    if families:
        return Result("cjk-font", True, f"available: {', '.join(families[:4])}")
    return Result(
        "cjk-font",
        False,
        "no CJK family installed -- Chinese captions will render as boxes",
        fix="install a CJK font (e.g. Noto Sans CJK) via your package manager",
        installable=False,
    )


CHECKS = [
    check_speech,
    check_ffmpeg,
    check_ffprobe,
    check_playwright,
    check_chromium,
    check_node,
    check_hyperframes,
    check_cjk_font,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true", help="install what is installable")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    results = [c() for c in CHECKS]

    if args.install:
        for r in results:
            if r["ok"] or not r["installable"]:
                continue
            fix = r["fix"]
            if not isinstance(fix, list):
                continue
            # In JSON mode stdout belongs to the document: progress and the child's
            # own output go to stderr, or the consumer that asked for JSON cannot
            # parse what it gets.
            stream = sys.stderr if args.json else sys.stdout
            print(f"installing {r['name']}: {' '.join(fix)}", file=stream, flush=True)
            proc = subprocess.run(
                fix,
                stdout=sys.stderr if args.json else None,
                stderr=sys.stderr if args.json else None,
            )
            if proc.returncode != 0:
                print(
                    f"  FAILED to install {r['name']} (exit {proc.returncode})",
                    file=stream,
                )
        results = [c() for c in CHECKS]  # re-check so the report reflects reality

    if args.json:
        # Nothing else may reach stdout after this: a trailing human summary
        # makes the document unparseable for the consumer that asked for JSON.
        print(json.dumps(results, indent=2))
        return 1 if any(not r["ok"] for r in results) else 0
    else:
        for r in results:
            print(f"{'ok  ' if r['ok'] else 'MISS'}  {r['name']:<12} {r['detail']}")
            if not r["ok"]:
                fix = r["fix"]
                print(f"        fix: {' '.join(fix) if isinstance(fix, list) else fix}")

    missing = [r for r in results if not r["ok"]]
    if missing:
        blocked = [r["name"] for r in missing if not r["installable"]]
        print(
            f"\n{len(missing)} missing"
            + (f"; needs a human: {', '.join(blocked)}" if blocked else "; re-run with --install")
        )
        return 1
    print("\nall dependencies present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
