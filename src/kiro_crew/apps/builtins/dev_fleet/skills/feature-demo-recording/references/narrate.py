#!/usr/bin/env python3
"""STEP 1 -- narration first, and measure it.

    python3 narrate.py script.json --out-dir assets/audio

Reads a script file, renders one clip per line through a local or own-account
speech provider, measures each with
ffprobe, and derives the timeline from the REAL measured durations. Writes
`narration.mp3` (the single mixed track) and `narr.json` (the timeline the
recorder and the compositor both read).

Why this runs FIRST: the recorder paces the capture off these durations, so voice
and picture are aligned by construction. Recording first and narrating after is
what produces drift you then cannot fix without a re-record.

Script file format (JSON):

{
  "voice": "en-US-AndrewMultilingualNeural",
  "gap": 0.4,
  "lines": [
    {"role":"intro","eyebrow":"THE FEATURE","title":"Command Bar",
     "sub":"one line under the title","say":"what is spoken","cap":"what is shown"},
    {"role":"footage","say":"spoken over the capture","cap":"subtitle"},
    {"role":"outro","title":"Command Bar","sub":"closing line",
     "say":"spoken","cap":"subtitle"}
  ]
}

`say` and `cap` are deliberately separate: a glyph like the command key reads
badly in TTS, and a subtitle must be shorter than a spoken sentence. `role` is
one of intro (a designed slide), footage (spoken over the screen capture), or
outro. The COUNT of footage lines must equal the number of beats the recorder
marks -- that is the contract between the two steps.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pathcheck import (  # noqa: E402
    open_media_input,
    read_json_input,
    safe_open_output,
    safe_output_path,
)

from kiro_crew.deploy.engine import resolve_aws_bin  # noqa: E402
from kiro_crew.security import (  # noqa: E402
    redact_credentials,
    redact_exfiltration_urls,
)

_AUTHORED_TEXT_FIELDS = ("say", "cap", "title", "sub", "eyebrow")
# The AWS CLI expands a parameter value beginning with file:// or fileb:// by
# READING that local path, so a line like `say: "file:///.../id_rsa"` would ship
# the file's contents to a cloud speech service as narration. Refuse the prefix
# for every field and provider rather than trusting one call site to be safe.
_CLI_FILE_PREFIXES = ("file://", "fileb://")


def _no_cli_file(label: str, value: str) -> str:
    """Refuse a value the cloud CLI would expand by reading a local path.

    Guarding the authored text alone was not enough: every argv value handed to
    that CLI has the same expansion, so a voice id or a profile name is the same
    hole with a different name.
    """
    if value and value.lstrip().lower().startswith(_CLI_FILE_PREFIXES):
        raise SystemExit(
            f"{label}: refusing a file:// value -- the cloud CLI would read that "
            "path and submit its contents"
        )
    return value


def _require_fields(lines: list[dict]) -> list[dict]:
    """Reject a script the compositor would only fail on much later.

    `compose.py` indexes `role` and `cap` directly, so a line missing either dies
    there with a bare KeyError and nothing naming the script that caused it. The
    count of footage lines is also the contract with the recorder, so an empty
    script is refused rather than producing an empty film.
    """
    if not lines:
        raise SystemExit("the script has no lines")
    for i, line in enumerate(lines):
        role = line.get("role")
        if role not in ("intro", "footage", "outro"):
            raise SystemExit(f"line {i}: role must be intro, footage or outro, got {role!r}")
        for field in ("say", "cap"):
            value = line.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"line {i} ({role}): {field} must be a non-empty string")
        if role in ("intro", "outro"):
            title = line.get("title")
            if not isinstance(title, str) or not title.strip():
                raise SystemExit(f"line {i} ({role}): title must be a non-empty string")
    return lines


def _scrub_script(lines: list[dict]) -> list[dict]:
    """Scrub credential-shaped text out of every authored field, once, at the source.

    An earlier revision scrubbed only the cloud-speech argv, which covered one of
    four destinations: the local synthesiser received the raw line, and `cap` /
    `title` were serialised into the timeline and burned into the film as
    subtitles. A credential in a script is worst when it is SHOWN on screen, so
    the scrub belongs where the text enters, not at one exit.
    """
    for i, line in enumerate(lines):
        for field in _AUTHORED_TEXT_FIELDS:
            raw = line.get(field)
            if not isinstance(raw, str) or not raw:
                continue
            if raw.lstrip().lower().startswith(_CLI_FILE_PREFIXES):
                raise SystemExit(
                    f"line {i} {field}: refusing a file:// value -- the cloud CLI "
                    "would read that path and send its contents as narration"
                )
            out, urls = redact_exfiltration_urls(raw)
            out, creds = redact_credentials(out)
            if out != raw:
                # Count only. Naming what was removed would print the secret into
                # the transcript and undo the redaction we just performed.
                n = len(urls) + len(creds)
                print(f"  ! line {i} {field}: redacted {n} credential-shaped span(s)")
                line[field] = out
    return lines


def _home() -> pathlib.Path:
    """Resolved on call, never bound at import.

    A module-level Path.home() is a host-isolation hazard: any test that
    imports this module and reaches the binding writes the operator's real
    home, which is why the repo's isolation floor rejects it.
    """
    return pathlib.Path.home()


def tool(name: str) -> str:
    found = shutil.which(name) or str(_home() / ".local/bin" / name)
    if pathlib.Path(found).exists():
        return found
    # The doctor accepts an imageio-provided ffmpeg, so this must too --
    # otherwise deps.py reports green and narration dies on the next line.
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and pathlib.Path(exe).exists():
                return exe
        except Exception:
            pass
    raise SystemExit(f"{name} not found -- run scripts/deps.py --install")


def measure(path: pathlib.Path) -> float:
    out = subprocess.run(
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return round(float(out.stdout.strip()), 3)


@contextlib.contextmanager
def _staged_output(path):
    """Yield a stage path for a SUBPROCESS to write, and publish it on success.

    ffmpeg and the speech binaries open their output themselves, so a no-follow
    handle cannot be handed to them. Two things must hold anyway. The child must
    not be able to write THROUGH a link, and a failed run must not have destroyed
    the previous good artifact -- deleting the destination first buys the second
    problem, which is why this stages beside it and renames only after the block
    completes.

    Order matters: the symlink test runs on the REQUESTED name, because
    `safe_output_path` resolves the path and would report a link's TARGET as an
    ordinary regular file.
    """
    raw = pathlib.Path(os.path.expanduser(str(path)))
    if raw.is_symlink():
        raise SystemExit(f"refusing to write through a symlink: {path}")
    final = pathlib.Path(safe_output_path(raw))
    # The CHILD creates this file, so we cannot hold it open with O_EXCL. A
    # predictable name in a shared directory therefore stays plantable: someone
    # who can write beside the output could leave a symlink at the stage name and
    # have ffmpeg or the speech binary follow it. Staging inside a private
    # mode-0700 directory removes the opportunity instead of making it unlikely --
    # nothing can be pre-created in a directory nobody else may write.
    # mkdtemp creates it readable/writable/searchable by the owner ONLY, which is
    # the property we need; an explicit chmod would be redundant.
    stage_dir = pathlib.Path(tempfile.mkdtemp(prefix=".stage-", dir=final.parent))
    stage = stage_dir / final.name
    try:
        yield str(stage)
        if not stage.exists():
            raise SystemExit(f"nothing was written to {stage}")
        os.replace(stage, final)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _ffmpeg(ff: str, argv: list, **kw) -> None:
    """The single ffmpeg entry point.

    Routing every invocation through here keeps the tainted-argv suppression on
    ONE line instead of three: argv is a fixed list, no shell is involved, and
    argv[0] is a known tool name resolved by shutil.which for this local script.
    """
    full = [ff, *argv]
    subprocess.run(
        full, check=True, capture_output=True, **kw
    )  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args


def _piper_ready(binary: str, model: str) -> bool:
    exe = shutil.which(binary or "piper")
    return bool(exe and model and pathlib.Path(os.path.expanduser(model)).is_file())


def resolve_provider(requested: str, piper_binary: str, piper_model: str) -> str:
    """Pick a speech provider, preferring the one that keeps text on this machine.

    These are exactly the providers the product's own voice-reply path supports --
    piper (local) and polly (the caller's own AWS account) -- driven through the
    same CLIs it uses. There is deliberately no third-party speech service here:
    narration text is product content, and a demo often narrates an internal
    surface, so the only two destinations are this machine and the caller's own
    cloud account.
    """
    if requested != "auto":
        return requested
    if _piper_ready(piper_binary, piper_model):
        return "piper"
    # Probe the same binary the polly spawn in synthesize() executes (the
    # shared deploy-engine resolver), so auto's answer holds under a
    # GUI-launched gateway's minimal PATH.
    if shutil.which(resolve_aws_bin()):
        return "polly"
    raise SystemExit(
        "no speech provider available. This pipeline speaks through piper or polly\n"
        "only -- there is no third-party speech fallback by design:\n"
        "  --provider piper --piper-model <voice.onnx>  local, nothing leaves\n"
        "  --provider polly                            your own AWS account\n"
        "  --silent                                    no speech at all"
    )


def synthesize(
    provider: str,
    text: str,
    dest_raw: pathlib.Path,
    *,
    voice: str,
    piper_binary: str,
    piper_model: str,
    aws_profile: str,
    aws_region: str,
) -> pathlib.Path:
    """Produce audio for one line. Returns the file actually written."""
    if provider == "piper":
        out = dest_raw.with_suffix(".wav")
        exe = shutil.which(piper_binary or "piper")
        if not exe:
            raise SystemExit("piper is not on PATH; pass --piper-binary")
        argv = [
            exe,
            "--model",
            piper_model,
            "--output_file",
            str(out),
        ]
        with _staged_output(out) as stage:
            argv[argv.index("--output_file") + 1] = stage
            proc = subprocess.run(argv, input=text, capture_output=True, text=True)
            if proc.returncode != 0 or not pathlib.Path(stage).exists():
                raise SystemExit(f"piper failed: {(proc.stderr or proc.stdout)[-400:]}")
        return out
    if provider == "polly":
        out = dest_raw.with_suffix(".mp3")
        argv = [
            # Resolved absolutely (shared deploy-engine resolver) so a
            # GUI-launched gateway's minimal PATH still finds the CLI.
            resolve_aws_bin(),
            "polly",
            "synthesize-speech",
            "--output-format",
            "mp3",
            "--voice-id",
            _no_cli_file("polly voice", voice),
            "--engine",
            "neural",
            "--text",
            text,
        ]
        aws_profile = _no_cli_file("aws profile", aws_profile)
        if aws_profile:
            argv += ["--profile", aws_profile]
        aws_region = _no_cli_file("aws region", aws_region)
        if aws_region:
            argv += ["--region", aws_region]
        with _staged_output(out) as stage:
            argv.append(stage)
            proc = subprocess.run(argv, capture_output=True, text=True)
            if proc.returncode != 0 or not pathlib.Path(stage).exists():
                raise SystemExit(f"polly failed: {(proc.stderr or proc.stdout)[-400:]}")
        return out
        return out
    raise SystemExit(f"unknown provider {provider!r}; expected piper or polly")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", help="path to the narration script JSON")
    ap.add_argument("--out-dir", default="assets/audio")
    ap.add_argument(
        "--silent",
        action="store_true",
        help="evidence clip: no speech, each line's `dur` is authored in "
        "the script, nothing is sent to a speech service",
    )
    ap.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "piper", "polly"],
        help="speech provider. auto prefers piper (local), then polly "
        "(your own AWS account). There is no third-party fallback",
    )
    ap.add_argument("--piper-binary", default="", help="piper executable, if not on PATH")
    ap.add_argument(
        "--piper-model",
        default=os.environ.get("KC_VIDEO_PIPER_MODEL", ""),
        help="piper .onnx voice model (defaults to KC_VIDEO_PIPER_MODEL)",
    )
    ap.add_argument("--polly-voice", default="Matthew", help="Polly voice id")
    ap.add_argument("--aws-profile", default="")
    ap.add_argument("--aws-region", default="")
    args = ap.parse_args()

    spec = read_json_input(args.script)
    gap = float(spec.get("gap", 0.4))
    if not math.isfinite(gap) or gap < 0:
        raise SystemExit(f"gap must be a finite value >= 0, got {gap!r}")
    lines = _scrub_script(_require_fields([dict(line) for line in spec["lines"]]))
    roles = {line.get("role") for line in lines}
    unknown = roles - {"intro", "footage", "outro"}
    if unknown:
        raise SystemExit(f"unknown role(s): {sorted(str(r) for r in unknown)}")

    # Resolved, because the concat step runs with cwd set to this directory: a
    # relative --out-dir would then no longer point at the list file.
    aud = pathlib.Path(safe_output_path(args.out_dir))
    aud.mkdir(parents=True, exist_ok=True)

    if args.silent:
        # A silent timeline is authored, so its numbers are unchecked input. A
        # negative or non-finite duration produces timestamps that run backwards,
        # and the renderer fails much later with nothing pointing back to here.
        for i, line in enumerate(lines):
            if line.get("dur") is None:
                continue
            d = float(line["dur"])
            if not math.isfinite(d) or d <= 0:
                raise SystemExit(f"line {i}: dur must be a finite value > 0, got {d!r}")
        missing = [i for i, line in enumerate(lines) if not line.get("dur")]
        if missing:
            raise SystemExit(
                "silent mode takes its timeline from the script, so every line needs a "
                f"`dur`; missing on line(s) {missing}"
            )
        t = 0.0
        for i, line in enumerate(lines):
            line["start"], line["end"] = round(t, 3), round(t + float(line["dur"]), 3)
            print(
                f"  line{i:02d} {line['role']:8s} {float(line['dur']):7.3f}s  "
                f"start={line['start']:8.3f}  (authored)"
            )
            t = round(t + float(line["dur"]) + gap, 3)
        total = round(t - gap, 3)
        out = {"silent": True, "gap": gap, "total": total, "measured_total": None, "lines": lines}
        with safe_open_output(aud / "narr.json", replace=True) as fh:
            fh.write(json.dumps(out, ensure_ascii=False, indent=2))
        foot = [line for line in lines if line["role"] == "footage"]
        print(f"\nsilent timeline total={total}s (no audio written)")
        print(f"footage beats={len(foot)} -- the recorder must mark exactly this many")
        print(f"wrote {aud/'narr.json'}")
        return 0

    spec_voice = spec.get("voice", "")
    provider = resolve_provider(args.provider, args.piper_binary, args.piper_model)
    # The voice model is read by a CHILD process, once per line. Pin it with a
    # descriptor-pinned private copy ONCE -- a validated pathname could be swapped
    # between the check and any of those opens, and copying per line would move a
    # large model file for every sentence.
    drop_model = None
    piper_model = args.piper_model
    if provider == "piper" and piper_model:
        pinned, drop_model = open_media_input(piper_model)
        piper_model = str(pinned)
        atexit.register(drop_model)
    voice = args.polly_voice if provider == "polly" else spec_voice
    ff = tool("ffmpeg")

    print(f"provider={provider} voice={voice} gap={gap}s lines={len(lines)}")

    parts, t = [], 0.0
    for i, line in enumerate(lines):
        raw = synthesize(
            provider,
            line["say"],
            aud / f"raw{i:02d}",
            voice=voice,
            piper_binary=args.piper_binary,
            piper_model=piper_model,
            aws_profile=args.aws_profile,
            aws_region=args.aws_region,
        )
        # Normalise every clip to one codec/rate so providers are interchangeable and
        # the concat below can stream-copy (piper emits WAV, the others MP3).
        mp3 = aud / f"line{i:02d}.mp3"
        with _staged_output(mp3) as stage:
            _ffmpeg(ff, ["-y", "-i", str(raw), "-ar", "24000", "-ac", "1", "-q:a", "4", stage])
        raw.unlink(missing_ok=True)
        dur = measure(mp3)
        line["start"], line["dur"], line["end"] = round(t, 3), dur, round(t + dur, 3)
        parts.append(mp3)
        t = round(t + dur + gap, 3)
        print(f"  line{i:02d} {line['role']:8s} {dur:7.3f}s  start={line['start']:8.3f}")

    total = round(t - gap, 3)

    sil = aud / "gap.mp3"
    with _staged_output(sil) as stage:
        _ffmpeg(
            ff,
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-t",
                str(gap),
                "-q:a",
                "9",
                stage,
            ],
        )
    listing = aud / "concat.txt"
    with safe_open_output(listing, replace=True) as fh:
        for i, p in enumerate(parts):
            fh.write(f"file '{p.name}'\n")
            if i != len(parts) - 1:
                fh.write(f"file '{sil.name}'\n")
    narration = aud / "narration.mp3"
    with _staged_output(narration) as stage:
        _ffmpeg(
            ff,
            ["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", stage],
            cwd=str(aud),
        )

    payload: dict[str, object] = {
        "provider": provider,
        "voice": voice,
        "gap": gap,
        "total": total,
        "measured_total": measure(narration),
        "lines": lines,
    }
    with safe_open_output(aud / "narr.json", replace=True) as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2))

    foot = [line for line in lines if line["role"] == "footage"]
    print(f"\ntimeline total={total}s  narration.mp3={payload['measured_total']}s")
    print(f"footage beats={len(foot)} -- the recorder must mark exactly this many")
    print(f"wrote {aud/'narr.json'} and {narration}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
