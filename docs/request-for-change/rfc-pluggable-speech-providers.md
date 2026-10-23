---
title: Pluggable speech providers
status: in-progress
author: iamwhatever
created: 2026-10-22
last-audited: 2026-10-22
audited-at: 88d73a83b0
doc-pr: 22983
implementation-prs: [22997]
tracking-issues: [22982]
supersedes: []
superseded-by: []
---
# RFC: Pluggable speech providers

- Status: in-progress
- Author: iamwhatever
- Created: 2026-10-22
- Related: issue [#22982](https://github.com/kirodotdev/KiroCrew/issues/22982), [`docs/system-specs/modules/platform-context.md`](../system-specs/modules/platform-context.md), [`stt-streaming.md`](../system-specs/modules/stt-streaming.md), [`voice-streaming.md`](../system-specs/modules/voice-streaming.md)

## Summary

Speech-to-text (STT) and text-to-speech (TTS) providers are closed lists in core. A composed edition that wants to offer its own hosted speech backend has no place to plug it in, short of patching core. This RFC proposes one seam on `PlatformContext` that lets an edition register extra STT and TTS providers and choose the edition default. The default implementation registers nothing, so a standalone install behaves exactly as it does today.

## Motivation

### Current state (measured at `ba5327702c`)

Provider names are fixed in five places, and each one must agree with the others:

| Concern | Where | Fixed set |
|---|---|---|
| STT config validation | `_VALID_STT_PROVIDERS` in `src/kiro_crew/config/sections_stt.py` | `local`, `apple`, `transcribe`, `off` |
| STT streaming grant | `_STREAMING_PROVIDERS` in `src/kiro_crew/dashboard/stt_stream.py` | `local`, `apple`, `transcribe` |
| STT dispatch | the provider checks in `transcribe_audio` and the readiness check in `src/kiro_crew/transcribe.py` | `if provider == "transcribe"` / `"apple"`, else local |
| TTS validation + default | `VALID_PROVIDERS` and `DEFAULT_PROVIDER` in `src/kiro_crew/voice_reply.py` | `polly`, `piper`, `system`; default `system` |
| TTS dispatch | `is_available` and the synthesis branches in `src/kiro_crew/voice_reply.py`; `_synthesize_request` in `src/kiro_crew/dashboard/chat_voice.py` | same three |

`PlatformContext` (`src/kiro_crew/platform/context.py`) has no speech slot, and no Protocol in `src/kiro_crew/platform/interfaces.py` covers speech. `CONTRACT_VERSION` in `context.py` is `1`.

### Problems

1. An edition with its own speech backend must either patch these five places or fork the module. Both rot on every upstream pull.
2. An edition cannot change the default. A standalone default of `system` TTS is right for a laptop with no account; it is wrong for an edition whose users already sign in to a hosted backend.
3. Each new built-in provider today edits the same five sites. A registry turns that into one registration.

## Goals

- An edition can add named STT and TTS providers without editing core files.
- An edition can set which provider is the default when the user has not chosen one.
- A user's explicit choice in config always wins over the edition default.
- Standalone behavior is byte-identical: same names, same default, same validation messages.
- Added providers flow through the existing capability check, so the UI shows "not available" with a reason instead of a broken control.

## Non-goals

- No new built-in provider ships with this RFC.
- No change to the browser audio pipeline, sentence splitting, WAV chunking or the sandboxed spawn rules for local engines.
- No credential handling in core. A provider that needs a credential obtains it through its own edition code.
- Not a general plugin loader. Providers come only from the composed `PlatformContext`, never from config strings, apps or agent-writable paths.

## Design

### One slot, one Protocol

Add a `speech: "SpeechProviderRegistry"` field to `PlatformContext`, with a `DefaultSpeechProviderRegistry` that returns empty tuples and `None` defaults.

```python
class SttProvider(Protocol):
    name: str                      # config value, e.g. "acme"
    streaming: bool                # may join stt_stream
    async def check(self, stt_config) -> "ProviderStatus": ...
    async def transcribe_file(self, audio_path: str, stt_config) -> str | None: ...
    def open_stream(self, stt_config) -> "SttStreamSession": ...  # only if streaming

class TtsProvider(Protocol):
    name: str
    async def check(self, voice_config) -> "ProviderStatus": ...
    async def synthesize(self, text: str, voice_config) -> "AsyncIterator[AudioChunk]": ...

class SpeechProviderRegistry(Protocol):
    def stt_providers(self) -> tuple[SttProvider, ...]: ...
    def tts_providers(self) -> tuple[TtsProvider, ...]: ...
    def default_stt(self) -> str | None: ...   # None = core default
    def default_tts(self) -> str | None: ...
```

`SttStreamSession` and `AudioChunk` reuse the shapes `stt_stream.py` and `chat_voice.py` already produce, so the existing partial/final events and chunked WAV/MP3 output need no new wire format.

Provider rule: core may call any provider method from any event loop (the gateway's, a channel's, or a short-lived loop for a blocking caller). A provider must not keep a loop-bound client, such as an aiohttp or httpx session, across calls. It creates one per call or keeps one per loop. Core bounds every `check()` with a timeout and cancels a check that overruns.

### How core reads it

- `_VALID_STT_PROVIDERS` and `VALID_PROVIDERS` become the built-in set unioned with registered names, read through `current_context()`.
- `_STREAMING_PROVIDERS` grants a registered provider only when `streaming` is true. Positive membership stays the rule, so `off` still cannot stream.
- Each dispatch site checks the registry first for a non-built-in name, then falls through to today's branches unchanged.
- `resolve_configured_provider()` and the STT loader use `default_tts()` / `default_stt()` only when the config omits the key. A stored value is never rewritten.
- Capability endpoints list registered providers with their `check()` status, so the dashboard selectors render them from data rather than a hardcoded list.

### ADD-only guarantees

- A registered name that collides with a built-in or with `off` is refused at boot, with a clear error. An edition cannot shadow `transcribe`, `polly` or the others.
- Registration happens once, at composition. Nothing at runtime can add or swap a provider.
- `CONTRACT_VERSION` bumps to `2` because `PlatformContext` gains a field. The bootstrap check already refuses a companion built against the wrong version.

## Migration plan

| Phase | Change | Exit criteria |
|---|---|---|
| 1 | Add the Protocols, the `speech` slot and `DefaultSpeechProviderRegistry`. Route validation and default resolution through it. | Full test suite unchanged. New test: the default registry yields the same valid sets and defaults as today. |
| 2 | Route STT and TTS dispatch and the capability endpoints through the registry. | New test: a fake provider registered on a test context is accepted by config, appears in capabilities, receives `transcribe_file` / `synthesize`, and a colliding name fails boot. |
| 3 | Dashboard selectors render the provider list from the capability response. | UI test: a fake registered provider appears in both selectors; standalone shows today's options in today's order. |
| 4 | Update `platform-context.md`, `stt-streaming.md`, `voice-streaming.md`. | Docs name the slot, the collision rule and the default precedence. |

Each phase is independently shippable. Phase 3 can be dropped if the selectors stay hardcoded and editions ship their own panel.

## Backward compatibility

- Standalone: no registered providers, so every set, default and message is unchanged.
- Stored configs keep working. An unknown name keeps today's handling (the STT loader fails closed to `off`; TTS falls back to the default with a warning).
- Companions built against contract version 1 are refused at boot until rebuilt, which is the existing contract-version behavior.

## Security considerations

- Providers come only from the composed context, which is built before any agent runs. Config can select a registered name but cannot load code.
- Core never sees a provider's credentials. A hosted provider's `check()` reports "not available" when its credential is missing or refused; core does not retry in a loop.
- Audio and transcripts for a registered provider are the provider's responsibility. Core keeps its current rule of not writing audio to disk outside the existing temp files, and the RFC asks registered providers to follow it.
- Existing consent prompts for billed cloud providers stay as they are. A registered billed provider is expected to declare it through `check()` so the same consent gate can apply.

## Alternatives considered

- **Patch core in each edition.** Works once, conflicts on every pull, and is forbidden by companion rules that keep vendored core read-only.
- **An app.** Apps run in their own sandbox and do not sit on the in-process audio path, so latency and the streaming session do not fit. Apps also should not be able to register code that receives every user's microphone audio.
- **A config-driven HTTP provider.** A URL in config would let any config writer redirect microphone audio. Rejected for the same reason providers must come from composition.

## Open questions

1. Should the edition default also apply to existing installs that never set a provider, or only to fresh configs? This RFC proposes "whenever the key is absent", matching how `DEFAULT_PROVIDER` works today.
2. Should Phase 3 ship, or should editions own their settings panel?
