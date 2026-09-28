"""Optional in-process feature rows of ``kirocrew doctor``: vector memory, speech.

A prerequisite of a feature this install runs is an issue: the embedding runtime on a
platform that ships one, an unusable custom embedding model, and the recogniser and
audio decoder of an enabled speech-to-text provider (reported without counting on
Windows, where the voice extra is optional). An optional accelerator, a cloud extra,
or weights fetched on first use is a note.
"""

from __future__ import annotations

import os
import platform as _plat
import sys
from typing import TYPE_CHECKING

from kiro_crew import cli_doctor, platform_compat, stt

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


def _os_fix_hint(mac: str, linux: str, windows: str | None = None) -> str:
    """Return the OS-appropriate Fix hint (brew on macOS, winget on Windows,
    else Linux guidance).

    Without a Windows arm Windows would fall through to the Linux text, telling a
    Windows user to ``pipx``/drop a static build in ``~/.local/bin``, neither of
    which applies. When *windows* is omitted the Linux text is still used, so
    callers only pass it where a Windows-specific remedy exists.
    """
    if _plat.system() == "Darwin":
        return mac
    if windows is not None and _plat.system() == "Windows":
        return windows
    return linux


# The Linux arm of the missing-ffmpeg remedy, a module constant so the test can hold
# it against the resolver's real search set. It must not name ``~/.local/bin``, which
# ``transcribe._find_ffmpeg`` deliberately never searches (``_ffmpeg_candidate_dirs``
# documents leaving it out: a generic user-writable PATH dir would let agent-written
# code run as the gateway), because a user who follows that advice still ends at
# "not found".
# Name only remedies that actually resolve: the dashboard's decoder download
# installs into the digest-verified store ``_find_ffmpeg`` checks last and needs
# no PATH reasoning (the working fix on distros with no packaged ffmpeg, e.g.
# AL2023 — pinned artifacts exist for x86_64 and aarch64, the Linux ISAs the
# desktop matrix ships; on any other ISA the fetch is refused and the second
# clause is the remedy), and ``/usr/local/bin`` is both a real
# ``_FFMPEG_CANDIDATE_DIRS`` entry and the conventional manual-install prefix.
_FFMPEG_LINUX_HINT = (
    "download the audio decoder from the dashboard (Settings → Speech-to-Text), "
    "or install ffmpeg into /usr/local/bin"
)


def _doctor_vector_memory(issues: list[str]) -> None:
    """Render the ``Vector Memory (in-process embeddings)`` section."""
    print("\nVector Memory (in-process embeddings)")

    # Read BEFORE _load_llama_class(): the loader `setdefault`s this var to its
    # OWN bundled libs dir, so after the call an unset var is indistinguishable
    # from an operator override pointing at the bundle.
    _lib_path_override = os.environ.get(cli_doctor._LIB_PATH_ENV, "")

    if cli_doctor._load_llama_class() is not None:
        print("  runtime:     ✅ vendored llama-cpp-python importable")
    elif cli_doctor._platform_libs_dirname() is None:
        # Designed degradation, not a defect: no vendored native libs exist for
        # this platform (e.g. darwin/x86_64) and embeddings.py documents the
        # keyword-search fallback. Nothing for the user to fix — don't fail.
        print(
            "  runtime:     ⏹ unsupported platform "
            f"({sys.platform}/{_plat.machine()}) — memory uses keyword search"
        )
    else:
        print("  runtime:     ❌ vendored runtime failed to load")
        # Distinguish an incomplete SHIPPED payload from a load failure on a
        # complete one. Both surface as the same ctypes "base name 'llama' not
        # found", but only the former is a packaging defect the user cannot fix
        # by configuration — and naming the absent files is what stops the
        # diagnosis from being misread as an unsupported architecture.
        #
        # Mirrors the loader's LLAMA_CPP_LIB_PATH exemption: under an override
        # the libs load from the operator's directory, so blaming the bundled
        # tree would send them to reinstall a package they are deliberately not
        # loading from, while saying nothing about the dir that actually failed.
        _plat_dir = cli_doctor._platform_libs_dirname()
        _absent = (
            [] if _lib_path_override else cli_doctor.verify_vendored_libs().get(_plat_dir or "", [])
        )
        if _absent:
            print(f"               Missing native libs for {_plat_dir}: {', '.join(_absent)}")
            print("               This install's vendored llama.cpp is incomplete (packaging")
            print("               defect, not an unsupported platform) — reinstall Kiro Crew")
            print("               from a current release to restore vector memory.")
        elif _lib_path_override:
            print(f"               {cli_doctor._LIB_PATH_ENV} is set — the libs load from")
            print(f"               {_lib_path_override}, not the bundled tree.")
            print("               Verify that directory holds a complete llama.cpp closure.")
        issues.append("embedding runtime")

    # FAISS is an optional accelerator — never a dependency, on any platform.
    # Without it, episodic recall uses the stdlib cosine fallback (correct, just
    # slower on a large store). Report it as an informational note, never an
    # issue, so the user knows the speed-up exists without doctor failing.
    try:
        import faiss  # noqa: F401

        print("  faiss:       ✅ vector-search accelerator installed")
    except ImportError:
        print(
            "  faiss:       ⏹ not installed (optional) — episodic recall uses "
            "the stdlib fallback; installing faiss-cpu accelerates it"
        )
        # The command names THIS interpreter, not a bare `pip`. On a packaged or
        # minimal install the gateway's python is not what a bare `pip` resolves
        # to -- it may not be on PATH under that name at all -- so the wheel
        # lands somewhere this process never imports from, and the next doctor
        # run prints the identical advice with no sign the install missed.
        #
        # Printed only where that command can actually run. On the bundled
        # desktop interpreter it would write into the code-signed bundle, which
        # breaks later launches and is discarded on the next app update, so
        # naming it there is worse advice than naming nothing. The dashboard's
        # install card offers no command in the same state.
        if cli_doctor.pip_install_channel_available():
            print(f"               Install: {cli_doctor.pip_install_command_for('faiss-cpu')}")

    _custom = cli_doctor.resolve_custom_model()
    if _custom is not None:
        # A custom model is configured. Never suggest the CDN here: the default
        # model is deliberately not downloaded in this mode, so its reachability
        # is irrelevant and pointing at it would be misleading advice.
        if _custom.error:
            print(f"  model:       ❌ custom model unusable — {_custom.error}")
            issues.append("custom embedding model unusable")
        elif cli_doctor.model_file_present():
            print(f"  model:       ✅ {_custom.path} (custom)")
            print(f"  vector space: {_custom.model_id} @ {_custom.dim}d")
        else:
            print(f"  model:       ❌ custom model not readable: {_custom.path}")
            issues.append("custom embedding model unreadable")
    elif cli_doctor.model_file_present():
        print(f"  model:       ✅ {cli_doctor.default_model_path()}")
    else:
        print("  model:       ⏹ not downloaded yet (downloads in background on gateway start)")
        cli_doctor._doctor_model_url_reachable(issues)

    print("  embeddings:  ✅ always-on")


def _doctor_speech_to_text(cfg: KiroCrewConfig, issues: list[str]) -> None:
    """Render the ``Speech-to-Text`` section: the recogniser, its model, the audio
    decoder, and the cloud or on-device provider's own prerequisites."""
    print("\nSpeech-to-Text")
    stt_active = cfg.stt.enabled

    if not stt_active:
        print("  status:      ⏹ disabled (enable from dashboard → Settings → Speech-to-Text)")
    else:
        print(f"  provider:    ✅ {cfg.stt.provider}")

    # Source installs may omit the optional voice extra. Preserve Windows's
    # historical non-fatal report for that case so an enabled-by-default feature
    # cannot block gateway startup; desktop releases gate both native components
    # at build time and should never reach the missing branches.
    stt_fatal = not platform_compat.IS_WINDOWS
    stt_mark = "❌" if stt_fatal else "⚠️ "

    if stt_active and cfg.stt.provider == "local":
        engine = cli_doctor.availability_detail(cfg.stt)
        if engine.ok:
            print("  engine:      ✅ local recogniser loadable (whisper.cpp, in-process)")
        else:
            print(f"  engine:      {stt_mark} {engine.detail}")
            if stt_fatal:
                issues.append(f"speech recogniser ({engine.code})")
        # The weights are fetched on first use, so "not downloaded" is the normal
        # first-run state and never an issue. Naming the size is the useful part,
        # because that transfer is what a first dictation waits on.
        model = stt.resolve_model(cfg.stt.model)
        if stt.is_present(model):
            print(f"  model:       ✅ {model.name} at {stt.models_dir() / model.filename}")
        else:
            print(
                f"  model:       ⏹ {model.name} not downloaded yet "
                f"({model.size_bytes // 1_000_000} MB, fetched on first use)"
            )

    cli_doctor.ensure_ffmpeg_in_path()
    # The same resolver the transcode path uses, so what doctor REPORTS is what would
    # actually be exec'd. A bare `which` here reported a PATH-chosen ffmpeg that
    # `_find_ffmpeg` would decline, which is the more misleading of the two failures.
    ffmpeg_bin = cli_doctor._find_ffmpeg()
    if ffmpeg_bin:
        # The resolved path can contain a username or a credential-bearing mount
        # name. Doctor only needs to confirm the exact resolver found a decoder.
        print("  ffmpeg:      ✅ available")
    elif stt_active:
        # A prerequisite of every provider, not of one of them: a Slack voice memo
        # arrives as ogg/Opus and the dashboard records webm, so the only input
        # that reaches a recogniser without ffmpeg is a 16 kHz mono WAV.
        print(f"  ffmpeg:      {stt_mark} not found")
        if platform_compat.is_bundled_interpreter():
            print("               Fix: reinstall Kiro Crew (the bundled audio decoder is missing)")
        else:
            print(
                "               Fix: "
                + _os_fix_hint(
                    "brew install ffmpeg",
                    _FFMPEG_LINUX_HINT,
                    windows="winget install Gyan.FFmpeg",
                )
            )
        if stt_fatal:
            issues.append("ffmpeg")
    else:
        print("  ffmpeg:      ⏭  not installed (not needed)")

    # Cloud transcription (AWS Transcribe) is an OPTIONAL feature requiring
    # user-provided AWS credentials and the `amazon-transcribe`/`boto3` extras.
    # It is never a hard failure on a standard install — report gracefully.
    if stt_active and cfg.stt.provider == "transcribe":
        try:
            import amazon_transcribe.client  # noqa: F401

            print("  transcribe:  ✅ amazon_transcribe importable (optional)")
        except ImportError:
            print("  transcribe:  ⏹ optional cloud STT not installed")
            # Same reasoning as the faiss line above: this process imports the
            # package, so the command has to name this interpreter, and it is
            # printed only where that command can actually run.
            if cli_doctor.pip_install_channel_available():
                print(f"               Install: {cli_doctor.pip_install_command('voice-aws')}")

        try:
            import boto3  # noqa: F401

            print("  boto3:       ✅ importable (optional)")
        except ImportError:
            print("  boto3:       ⏹ optional AWS SDK not installed")
            if cli_doctor.pip_install_channel_available():
                print(f"               Install: {cli_doctor.pip_install_command('voice-aws')}")

    # Apple's on-device speech is a host capability rather than an install, so the
    # only useful thing to print is the reason it cannot run. Reaching a not-ok
    # state here means the operator selected a provider this machine does not
    # support, which is a real configuration fault and not a first-run state.
    #
    # Deliberately fatal on EVERY platform, so it does not take the Windows
    # downgrade above. That carve-out exists for prerequisites a user can simply
    # install; this is a provider that cannot be made to work on the host at all,
    # and reporting it as a note would have `kirocrew doctor` exit 0 on a
    # configuration that can only ever fail at the first recording.
    if stt_active and cfg.stt.provider == "apple":
        apple = cli_doctor.availability_detail(cfg.stt)
        if apple.ok:
            print("  apple:       ✅ on-device SpeechAnalyzer available")
        else:
            print(f"  apple:       ❌ {apple.detail}")
            issues.append(f"apple speech ({apple.code})")
