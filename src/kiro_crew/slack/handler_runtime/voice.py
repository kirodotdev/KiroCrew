"""Slack voice replies: loading the ``voice_reply`` settings into the live voice state, and
the fire-and-forget reply a turn hands to ``voice_reply``. The state object ``_vc`` is
module state of :mod:`kiro_crew.slack.handler`; the dashboard reads and writes the same
object.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        PROVIDER_PIPER,
        PROVIDER_SYSTEM,
        KiroCrewConfig,
        SlackClientOps,
        _tts_available,
        _validate_length_scale,
        _vc,
        _voice_reply_fn,
        config_path,
        logger,
        read_config_text,
        resolve_configured_provider,
        validated_config_string,
    )


def _str_or_default(value: object, default: str = "") -> str:
    """Return *value* stripped when it is a string, else *default*.

    The tolerant READ half of :func:`validated_config_string`, which is the strict
    WRITE half. The asymmetry is deliberate: the dashboard PUT rejects a
    wrong-typed field with 400 because a caller can still fix it, whereas this
    runs at boot against whatever is already on disk, where refusing would mean
    failing to start over a hand-edited typo. So the boundary rejects and the
    loader falls back.

    Every string field in the ``voice_reply`` block is hand-editable JSON, so a
    dict, list or number can arrive where a string is expected, and all of them
    reach the dashboard's config GET where a non-string crashes the React panel
    that renders it. Falling back beats ``str(value)``, which would keep ``"{}"``
    as a voice name or a binary path and push the failure into synthesis.

    *default* is per-field on purpose: an unset voice or engine has a real
    fallback, whereas an unset profile or path means "not configured". Coercing
    ``rate``/``pitch`` here as well as in their synthesis-time validators is not
    redundant — the validators protect synthesis, this protects the GET.
    """
    validated = validated_config_string(value)
    return default if validated is None else validated


def load_voice_reply_config(cfg: "KiroCrewConfig | None" = None) -> None:
    """Populate the live voice state (``_vc``) from config's ``voice_reply``.

    Callable without a Slack orchestrator: ``set_orch_cfg`` runs only on the
    Slack startup path, so the dashboard app builders call this directly at
    boot. Without that call a dashboard-only gateway (no Slack tokens) never
    restores persisted voice settings — every restart silently resets TTS to
    disabled while the dashboard's settings PUT keeps reporting success.
    """
    # Load voice_reply defaults from config
    _vr: dict = cfg.raw.get("voice_reply", {}) if (cfg is not None and hasattr(cfg, "raw")) else {}
    if not _vr:
        try:
            _vr = json.loads(read_config_text(config_path())).get("voice_reply", {})
        except Exception:
            _vr = {}
    if not isinstance(_vr, dict):
        _vr = {}
    _enabled = bool(_vr.get("enabled", False))
    if _enabled:
        _vc.global_enabled = True
    _vc.auto_speak = bool(_vr.get("auto_speak", False))
    # All ten string reads in this block go through the same coercion: each is
    # hand-editable JSON that reaches the dashboard's config GET verbatim.
    _vc.default_voice = _str_or_default(_vr.get("voice_id"), "Ruth")
    _vc.default_engine = _str_or_default(_vr.get("engine"), "generative")
    _vc.default_rate = _str_or_default(_vr.get("rate"), "100%")
    _vc.default_pitch = _str_or_default(_vr.get("pitch"), "+0%")
    _vc.aws_profile = _str_or_default(_vr.get("aws_profile"))
    _vc.region = _str_or_default(_vr.get("region"))
    # ``auto_reply_to_voice`` defaults to ``enabled``'s value: users with
    # explicit ``enabled=false`` keep the existing zero-voice behavior, and
    # users who turn voice on globally also get symmetric voice-in/voice-out
    # without needing to set a second flag.
    _vc.auto_reply_to_voice = bool(_vr.get("auto_reply_to_voice", _enabled))
    # Resolution rules (validate-or-default, and keep an existing Piper install)
    # live in one shared resolver so this loader, the Telegram settings path, and
    # the dashboard cannot drift apart on them.
    _vc.provider = resolve_configured_provider(_vr)
    _vc.piper_binary = _str_or_default(_vr.get("piper_binary"))
    _vc.piper_model = _str_or_default(_vr.get("piper_model"))
    _vc.piper_model_config = _str_or_default(_vr.get("piper_model_config"))
    _vc.system_voice = _str_or_default(_vr.get("system_voice"))
    # Coerce to finite/positive — a config.json with inf/NaN (JSON accepts both)
    # would otherwise reach synthesis and be re-serialized as non-RFC JSON,
    # breaking the dashboard's config GET.
    _vc.piper_length_scale = _validate_length_scale(_vr.get("piper_length_scale", 1.0))


async def _safe_voice_reply(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    text: str,
    voice_id: str = "Ruth",
    engine: str = "generative",
    rate: str = "100%",
    pitch: str = "+0%",
) -> None:
    """Fire-and-forget voice reply.  Never raises."""
    try:
        await _voice_reply_fn(
            slack,
            channel,
            thread_ts,
            text,
            provider=_vc.provider,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
            piper_binary=_vc.piper_binary,
            piper_model=_vc.piper_model,
            piper_model_config=_vc.piper_model_config,
            length_scale=_vc.piper_length_scale,
            system_voice=_vc.system_voice,
        )
    except Exception:
        logger.debug("Voice reply failed", exc_info=True)


async def _reply_by_voice(
    slack: SlackClientOps,
    channel: str,
    reply_ts: str,
    user_id: str,
    session_key: str,
    accumulated: str,
    final_text: str,
    had_voice_input: bool,
) -> None:
    """Speak the answer when a voice reply is wanted (fire-and-forget, non-blocking).

    Triggers when: (a) user has opted in globally or per-thread via !voice,
    or (b) this message carried transcribed voice input and
    auto_reply_to_voice is enabled (symmetric voice conversation).

    ``auto_reply_to_voice`` defaults to ``enabled``'s value at config load
    (see ``set_orch_cfg``) so users with explicit ``enabled=false`` retain
    zero-voice behavior, and globally-enabled users automatically get
    symmetric voice-in/voice-out. Users who want voice ONLY in response to
    voice memos can set ``auto_reply_to_voice=true`` while leaving
    ``enabled=false``. See docs/reference/kiro-cli/chat/voice.md.
    """
    voice_auto_reply = had_voice_input and _vc.auto_reply_to_voice
    if _vc.global_enabled or session_key in _vc.sessions or voice_auto_reply:
        if len(accumulated) >= 50:
            # Off the loop: the probe stats fixed directories (and, for Polly,
            # searches PATH). A stat is unbounded — one on a stalled network or
            # fuse mount would freeze every session and heartbeat sharing this
            # loop — and the same rule governs ``resolve_system_tts_async``,
            # which this reaches for the built-in provider.
            _tts_ok = await asyncio.to_thread(
                _tts_available,
                provider=_vc.provider,
                piper_binary=_vc.piper_binary,
                piper_model=_vc.piper_model,
            )
            if not _tts_ok:
                # Voice reply requested via any opt-in path (global, per-thread,
                # or voice-auto-reply) but the configured TTS backend isn't
                # available. Post a one-shot ephemeral so the user knows the
                # response fell back to text only — silent fallback is worse
                # UX for users who explicitly opted in.
                if _vc.provider == PROVIDER_SYSTEM:
                    # Only reachable on a host whose built-in engine is absent,
                    # which in practice means a Linux box without espeak-ng.
                    hint = (
                        "Install the host speech engine (`espeak-ng`) or pick "
                        "another provider in Voice settings."
                    )
                elif _vc.provider == PROVIDER_PIPER:
                    hint = (
                        "Install piper (`pip install piper-tts`) and set "
                        "`voice_reply.piper_model` to your voice .onnx file."
                    )
                else:
                    hint = "Run `ada credentials update` and ensure `aws` CLI " "is on PATH."
                if voice_auto_reply:
                    intro = "🔇 Received your voice memo. Replying as text — "
                else:
                    intro = "🔇 Voice reply requested but "
                try:
                    await slack.post_ephemeral(
                        channel,
                        user_id,
                        f"{intro}TTS (provider={_vc.provider}) isn't " f"configured. {hint}",
                    )
                except Exception:
                    logger.debug("Failed to post TTS-unavailable ephemeral", exc_info=True)
            else:
                _vid = _vc.voices.get(session_key, _vc.default_voice)
                _eng = _vc.engines.get(session_key, _vc.default_engine)
                _rate = _vc.rates.get(session_key, _vc.default_rate)
                _pitch = _vc.pitches.get(session_key, _vc.default_pitch)
                asyncio.create_task(
                    _safe_voice_reply(
                        slack,
                        channel,
                        reply_ts,
                        final_text,
                        voice_id=_vid,
                        engine=_eng,
                        rate=_rate,
                        pitch=_pitch,
                    )
                )
