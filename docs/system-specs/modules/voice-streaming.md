# Voice Streaming

## Overview

Dashboard text-to-speech has three providers: the host's built-in speech engine,
local Piper, and Amazon Polly. `voice_reply.DEFAULT_PROVIDER` selects the
built-in engine unless configuration selects a valid provider, because it is the
only one that needs nothing installed. `chat_voice.api_voice_synthesize()` sends
built-in-engine and Piper output as one WAV chunk and streams Polly sentence
chunks as MP3; the browser queues either form for sequential playback.

## The built-in engine

`voice_reply.resolve_system_tts()` returns `(engine, binary)` for the host:
`say` on macOS, `sapi` (Windows PowerShell 5.1 driving `System.Speech`) on
Windows, `espeak-ng` on everything else when it is installed. Resolution goes
through `platform_compat.trusted_system_bin`, not `PATH`, so a shim in an
agent-writable directory cannot be handed LLM text. Linux is the one platform
where the answer can be `None` — a stock Ubuntu Desktop ships the espeak-ng
library and data but not the CLI — and that is reported as unavailable rather
than papered over.

Resolution is a handful of directory stats, and a stat is not bounded: a fixed
directory on a stalled network or fuse mount blocks, and one loop serves every
session plus its heartbeats. So EVERY async path that reaches it offloads to a
worker thread — `resolve_system_tts_async` for the two synthesis paths and the
endpoint, and `asyncio.to_thread` around `is_available` at the Slack caller,
which reaches the same stats (and, for Polly, a PATH search). The sync
`resolve_system_tts` and `is_available` remain for sync callers; an async caller
using them directly is the defect.

The same rule covers the two SPAWN-PREPARATION steps and the OUTPUT CHECK, all
three of which stat the filesystem for different reasons and are easy to miss
because they read as pure argv rewriting or a cheap size test: the sandbox probe
walks `PATH`, `cgroup_scope_argv` ensures the parent slice's limits through the
cgroup filesystem, and validating the produced audio stats `TMPDIR`, which an
operator can point at a network or FUSE mount. `sandboxed_spawn_argv_async`
carries the first off-loop itself; `cgroup_scope_argv` is wrapped in
`asyncio.to_thread` at both spawn sites, the synthesis path and the voice-listing
probe; and `_produced_audio` exists so the existence and size checks are one
function, offloaded in a single thread hop rather than two.

Two properties are load-bearing:

- **Every engine is sandboxed; Windows uses the first-party carve-out.**
  `_run_tts_subprocess` always calls `sandboxed_spawn_argv_async`, and no
  provider skips it — each parses text it did not author. That entry point is
  chosen over a bare `wrap_argv` because it returns BOTH layers, the OS-level
  wrap and a credential-scrubbed environment, and the child is spawned with that
  `env`. The distinction is load-bearing rather than stylistic: on a host with no
  sandbox backend the wrap is inert, so the env scrub is the only control that
  still applies, and a TTS child has no use for the gateway's credentials on any
  platform. macOS and Linux confine normally (`say` was
  verified to produce audio under seatbelt, and standard mode leaves the system
  data directories espeak-ng reads). Windows has no sandbox backend, so instead
  of skipping the wrap the SAPI spawn passes
  `first_party_fixed_argv=engine == SYSTEM_ENGINE_SAPI`. On a backend-less host
  that carve-out runs the spawn loudly warned and SEL-audited with
  `outcome="unconfined"`, and a governance `sandbox.min_level` floor still
  refuses it — controls a plain skip forfeited. It is inert wherever a
  backend exists, and inert when `sandbox_allow_unsandboxed_exec` is set.

  What earns the claim is that the Windows argv is derived entirely inside this
  package: a System32 `powershell.exe` from `trusted_system_bin`, four
  module-constant flags, and a base64 `-EncodedCommand` whose script interpolates
  only `mkstemp` paths and an integer from `_validate_rate`. Both values a user
  or a model supplies — the reply text and `system_voice` — are spilled to files
  the script reads at runtime. `say`/`espeak-ng` carry the configured voice on
  argv as `-v`, so they evaluate False and cannot claim it; that costs nothing,
  since both platforms have a backend. The call site is allowlisted in
  `test_spawn_audit.py::FIRST_PARTY_SPAWNS` with that reasoning, and
  `test_sapi_claims_the_first_party_carve_out` asserts on the REAL argv that
  neither the voice nor the text appears in it.

  One consequence to keep in view: Piper keeps no carve-out, so fixing its
  `Scripts\piper.exe` resolution makes `is_available()` report it usable while a
  spawn still fails closed on Windows without the global opt-in — Piper there is
  found, not yet audible.
- **Text never reaches argv.** `say` and `espeak-ng` read it on stdin. SAPI
  reads it from a temp file whose path is interpolated into a base64
  `-EncodedCommand` payload, so no quoting decision is made about model output.
  On the SAPI path `system_voice` is spilled the same way, so nothing a user or
  a model supplied is on that command line at all. Every spill is unlinked in a
  `finally`.

Speed for this provider comes from the shared `rate` percentage:
`_system_wpm()` scales it against a 175 wpm baseline for `say`/`espeak-ng`, and
`_sapi_rate()` maps it onto SAPI's `-10..10`. `system_voice` is the engine's own
selector (a name for `say` and SAPI, a language code for `espeak-ng`); empty
means the OS default voice.

`list_system_voices()` enumerates the engine's voices and
`_parse_system_voices()` normalizes the three listing formats.
`chat_voice.api_voice_system_voices()` serves them at
`GET /api/voice/system-voices` as `{available, voices}`, cached for an hour, and
reports `available: false` for a host with no engine without spawning a probe.

The endpoint has three outcomes, and the panel renders each differently, so the
probe must not collapse two of them. No engine is `available: false`, shown as
neutral status with a re-check. A working engine is `available: true` plus its
voices. A probe that FAILS — spawn error, timeout, or nonzero exit — raises
`SystemVoiceProbeError`, which the handler turns into a 502 carrying
`code: "system_voices_probe_failed"`; returning an empty list instead would be
served as `available: true` and rendered as a picker holding only the OS
default, which reads as "this host has one voice" rather than as a retryable
failure. A failed probe is not cached.

## Resolving the configured provider

`voice_reply.resolve_configured_provider()` is the single reader of the raw
section's `provider`, shared by `slack.handler.load_voice_reply_config()` and
`voice_reply.synthesis_settings()` so the rules cannot drift between the Slack,
Telegram and dashboard paths. Three rules:

- A named, valid provider is kept.
- An invalid or non-string value warns and falls back to `DEFAULT_PROVIDER`,
  never to Polly: reaching a paid service because a key was misspelled is not a
  decision an operator made. The type is checked before the membership test,
  because `config.json` can hold a list or dict where a string belongs and
  `in VALID_PROVIDERS` would raise on those.
- An **unnamed** provider on a section that already carries `piper_model` keeps
  Piper. That section is a working Piper install from before the built-in engine
  became the default, and resolving it to the default would silently downgrade it
  to a lower-quality voice on upgrade.

## Components

| Component | Code | Responsibility |
|---|---|---|
| Dashboard routes | `dashboard.routes.sessions.register()` | Registers the synthesis, configuration, Polly voice-catalogue, and built-in-engine voice-catalogue endpoints. |
| Voice endpoints | `dashboard.chat_voice.api_voice_config()`, `api_voice_synthesize()`, `api_voice_voices()`, and `api_voice_system_voices()` | Read and persist configuration, synthesize dashboard speech, and return the Polly and built-in-engine catalogues. |
| Provider implementation | `voice_reply.synthesize_speech()`, `streaming_voice_reply()`, and `stitch_mp3s()` | Redacts text, selects a provider, creates audio, and joins completed Polly chunks. |
| Streaming playback | `website/src/hooks/useWebSocket.ts` | Detects completed sentences, serializes synthesis requests, queues audio, and handles interruption. |
| Settings | `website/src/pages/settings/VoicePanel.tsx` | Updates auto-speak, provider, and the selected provider's settings; fetches each provider's voice catalogue only while that provider is selected. |
| Slack reply | `slack.handler.handle_message()` and `_safe_voice_reply()` | Starts a background provider-aware voice reply when thread, global, or voice-input settings allow it. |

## Dashboard auto-speak

`useWebSocket` buffers `chat_chunk` text and, after it updates the Redux
streaming message, scans the active slot for completed sentence boundaries. It
submits only text beyond `voiceProgressRef.spokenLen` through
`enqueueVoiceSynthesis()`. The progress record is keyed by slot and message
identity: this prevents an old segment or a background slot from replaying text
or resetting the active response.

`flushVoiceTail()` handles the remaining eligible text at `chat_segment` and
`chat_done`. It marks the message consumed even when the tail does not meet the
speech floor, so a later completion event cannot retry it. The floor and
boundary rule are implemented in `useWebSocket.ts`; they are not duplicated
here.

`enqueueVoiceSynthesis()` appends each request to `synthChainRef`. This keeps
requests in source order even if a provider finishes them out of order, which is
load-bearing because the playback queue cannot reconstruct the intended
sentence order after receiving audio.

For Polly, `api_voice_synthesize()` iterates
`voice_reply.streaming_voice_reply()`, broadcasts each `voice_chunk`, then
uses `stitch_mp3s()` to broadcast `voice_complete`. For the built-in engine and
Piper, `_synthesize_nonstreaming()` broadcasts one WAV `voice_chunk` and one
`voice_complete`; it is selected by `provider != PROVIDER_POLLY` so a provider
added later cannot fall into the Polly branch and reach a paid AWS service. `useWebSocket` decodes `voice_chunk` audio into blob URLs and
plays the queue one item at a time. `voice_complete` also updates the Redux
`voiceAudio` field; `UseWebSocketCoverage.test.tsx` covers that state update.

## Interruption

`ChatPage` dispatches `voice-stop` when it sends a message, and its Speak
handler dispatches the same event while audio is playing. `useWebSocket` maps
the event to `stopVoice()`, which pauses the active audio element, revokes
queued blob URLs, clears the queue, and sets `voiceMutedRef`.

While muted, `voice_chunk` frames are discarded and the `chat_segment`/
`chat_done` tail paths do not synthesize more text. `voiceProgressFor()` clears
the muted state only when it sees a different message identity. This identity
boundary is load-bearing: it prevents late audio from an interrupted response
from being played as though it belonged to the next response.

## Configuration and API

Configuration is stored under `voice_reply` in the Crew configuration file.
`slack.handler.load_voice_reply_config()` loads the live `_VoiceConfig`, and
`api_voice_config()` merges a partial update back into that section rather than
replacing it. The merge preserves voice settings owned by other channels.

| Setting | Meaning |
|---|---|
| `provider` | Resolved by `voice_reply.resolve_configured_provider()` for every reader; invalid values fall back to `voice_reply.DEFAULT_PROVIDER`, and an unnamed provider beside a configured `piper_model` keeps Piper. |
| `enabled` | Enables global Slack voice replies. |
| `auto_speak` | Enables dashboard auto-speak; `api_voice_config()` exposes it as `autoSpeak`. |
| `voice_id`, `engine`, `pitch` | Polly synthesis settings, also usable as request overrides for the dashboard synthesis endpoint. |
| `rate` | Speech rate as a percentage. Shared by Polly and the built-in engine, which converts it to words per minute or to SAPI's `-10..10`. |
| `system_voice` | The built-in engine's own voice selector; empty means the OS default voice. |
| `aws_profile`, `region` | Passed to the AWS CLI by the Polly provider. |
| `piper_binary`, `piper_model`, `piper_model_config`, `piper_length_scale` | Piper executable, model, optional model configuration, and validated speed setting. `validate_length_scale()` rejects invalid or non-positive values. |

`dashboard.routes.sessions.register()` registers:

* `GET` and `PUT /api/voice/config`
* `POST /api/voice/synthesize`
* `GET /api/voice/voices`
* `GET /api/voice/system-voices`

`api_voice_voices()` caches a successful Polly catalogue in process, sorts it by
language code and name, and does not cache the empty result produced when the
AWS CLI is unavailable. It checks that Polly is the active provider and that
`aws_consent.refuse_and_log()` grants consent before it invokes
`aws polly describe-voices`. Those gates keep a direct API request from
silently using ambient AWS credentials for a provider the operator did not
select or authorize.

## Provider safety

`voice_reply.synthesize_speech()` redacts credentials and suspicious URLs before
provider selection. `text_to_ssml()` and `strip_markdown()` then produce
speakable text. `strip_markdown()` replaces fenced code, diff blocks, widgets,
tables, path-like inline code, and links with spoken placeholders or labels and
removes option markers, emoji, formatting markers, and diff hunk headers. The
thresholds and pattern details remain in `voice_reply.strip_markdown()`.

`_synthesize_polly()` calls `aws_consent.refuse_and_log()` before resolving or
spawning the AWS CLI. It returns no audio when consent is absent, which lets its
callers retain their text response rather than spending through an unattended
path.

`_synthesize_polly()` and `_synthesize_piper()` run their commands through
`wrap_argv_async(..., _prepare=wrap_argv)` and catch
`SandboxUnavailableError` separately from provider failures. They log the
sandbox error kind and its own message. The distinction is load-bearing because
only the sandbox layer can distinguish a missing backend from transient
pressure or an existing outer sandbox, and therefore provides the applicable
remedy.

## Slack voice replies

`slack.handler` accepts `!voice` thread commands for enabling and disabling a
thread, toggling global replies, and choosing a voice, engine, speed, or pitch.
`handle_message()` starts `_safe_voice_reply()` as a background task when a
thread or global setting enables replies, or when voice-input reply settings
allow a transcribed voice message to receive audio. `_safe_voice_reply()` calls
the provider-aware `voice_reply.voice_reply()` path, so Slack replies follow
the selected provider rather than assuming Polly.
