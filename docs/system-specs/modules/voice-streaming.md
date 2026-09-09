# Voice Streaming

## Overview

Dashboard text-to-speech has three providers: the host's built-in speech engine,
local Piper, and Amazon Polly. `voice_reply.DEFAULT_PROVIDER` selects the
built-in engine unless configuration selects a valid provider, because it is the
only one that needs nothing installed on macOS and Windows.
`chat_voice.api_voice_synthesize()` streams local Piper PCM as small WAV chunks,
sends built-in-engine output as one WAV chunk, and streams Polly sentences as MP3.
Every synthesis has a request identity so the browser can reject audio from a
stopped request.

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
  backend exists, and inert wherever the no-backend policy already permits the
  spawn — a declared `sandbox_allow_unsandboxed_exec=true`, or the platform
  default on Windows.

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
| Dashboard routes | `dashboard.routes.sessions.register()` | Registers synthesis, cancellation, configuration, both voice catalogues, and synthesis shutdown cleanup. |
| Voice endpoints | `dashboard.chat_voice.api_voice_config()`, `api_voice_synthesize()`, `api_voice_cancel()`, `api_voice_voices()`, and `api_voice_system_voices()` | Read and persist configuration, synthesize and interrupt dashboard speech, and return the Polly and built-in-engine catalogues. |
| Provider implementation | `voice_reply.synthesize_speech()`, `streaming_piper_reply()`, `streaming_voice_reply()`, and `stitch_mp3s()` | Redacts text, selects a provider, streams local PCM, and joins completed Polly chunks. |
| Resident local voice | `piper_runtime.PiperRuntime`, `piper_worker.serve()` | Owns one sandboxed Piper model and serial framed requests, with cancellation, idle, model-change, and shutdown cleanup. |
| Streaming playback | `website/src/hooks/useWebSocket.ts`, `website/src/lib/voicePlayback.ts` | Detects speech boundaries, coalesces pending requests, schedules PCM on one audio clock, and handles interruption. |
| Playback failures | `website/src/components/VoicePlaybackNotice.tsx` | Displays localized playback or provider failures and retains their machine code in the error report. |
| Settings | `website/src/pages/settings/VoicePanel.tsx` | Updates auto-speak, provider, and the selected provider's settings; fetches each provider's voice catalogue only while that provider is selected. |
| Slack reply | `slack.handler.handle_message()` and `_safe_voice_reply()` | Starts a background provider-aware voice reply when thread, global, or voice-input settings allow it. |

## Dashboard auto-speak

`useWebSocket` buffers `chat_chunk` text and, after it updates the Redux
streaming message, scans the active slot for completed sentence boundaries. It
submits only text beyond `voiceProgressRef.spokenLen` through
`enqueueVoiceSynthesis()`. The progress record is keyed by slot and message
identity: this prevents an old segment or a background slot from replaying text
or resetting the active response.

`voiceBoundary()` recognizes Chinese sentence punctuation without requiring a
following space, Latin sentence endings, newlines, and clause boundaries in
long text. It avoids splitting inside code fences or inline code and does not
split the decimal point in a number. `flushVoiceTail()` submits every nonempty
remaining tail at `chat_segment` and `chat_done`, including a short reply. It
marks the whole message consumed so a later completion event cannot retry it.

`enqueueVoiceSynthesis()` appends each request to `synthChainRef`. The first
eligible text starts immediately; while a request is synthesizing, subsequent
completed sentences merge into the next pending request up to a bounded text
size. This reduces model launches while retaining source order. An interruption
invalidates the chain's epoch, so an old promise cannot start its queued text
after a newer response starts.

For Polly, `api_voice_synthesize()` iterates
`voice_reply.streaming_voice_reply()`, broadcasts each `voice_chunk`, then
uses `stitch_mp3s()` to broadcast `voice_complete`. For Piper,
`_synthesize_piper_stream()` broadcasts `voice_chunk` frames while the child
still runs and collects bounded PCM for one WAV `voice_complete` replay clip.
The built-in engine broadcasts its completed WAV. Only the explicit Polly
provider reaches the Polly branch; a future provider cannot silently reach a
paid AWS service. Both event types include `audioMime` and echo the synthesis `request_id`.
`voice_complete` also updates the Redux
`voiceAudio` field; `UseWebSocketCoverage.test.tsx` covers that state update.

`VoicePcmPlayer` decodes WAV chunks in order and schedules their sources on one
`AudioContext` clock. Consecutive ready chunks abut on that clock instead of
paying a separate media-element load for each short chunk. If synthesis falls
behind playback, the next chunk starts at the current clock with a small
scheduling margin. MP3 and browsers without Web Audio use a sequential media
element queue. Polly therefore retains media-element clip-boundary gaps; the
continuous PCM clock applies to local WAV playback. Polly also shares the
newline/CJK sentence splitter: multiline or CJK replies can produce more smaller
requests than the previous Latin-punctuation-only splitter. Text content and AWS
consent remain unchanged; this is not a claim of lower Polly cost or latency.
A playback generation prevents a decode that finishes after stop
from scheduling obsolete audio.

The API announces `voice-synthesis-start` synchronously. On a manual Read aloud
click this lets the hook unlock its audio context inside the user's gesture,
before the HTTP request or the first WebSocket audio frame. Sending a message
also unlocks the context when auto-speak is enabled. A suspended context's resume
wait is bounded; a playback failure cancels the affected speech stream and its
queued chunks in both the PCM and media-element paths. `VoicePlaybackNotice` displays the error,
including a distinct autoplay-blocked remedy: the browser blocked automatic
playback, and the user can start playback manually through More actions and
Read aloud, named with their current localized labels. This distinguishes the
browser's playback restriction from the manual action used to unlock audio.
Every nonblank completed reply offers
Read aloud in More actions, retaining its localized message context as an
accessible description while its visible label remains its accessible name.
While the blocked-playback notice is present, completed replies in the active
conversation reveal their existing action rows without requiring hover. This
includes an older reply that the user manually selected. Dismissal, synthesis
restart, slot changes and unmount clear the reveal state.
Where speech creates a new menu, Copy text moves into that
menu so the footer gains neither another action control nor another row.
Clipboard success stays visible in the open menu. A refused text copy closes
the menu and leaves an `ErrorNotice` with a manual text-copy remedy until
dismissal or a successful retry. Copy-link keeps its separate short failure label.
The notice avoids navigating away from an embedded composer's unsaved draft.
Other playback
failures name text-to-speech settings and group the existing guarded Settings
link with the error inside one visible container. That link uses
`highlight=voice.provider-2` to target the text-to-speech provider row's explicit
`settingId`, independent of the equally named dictation provider or their display
order. The primitive and generated registry carry the same UI identity. TTS is
persisted in a raw `voice_reply` JSON section outside the typed config schema, so
this row does not claim a schema-backed `configKey` or a `SettingRef` mapping.
If the settings query is still loading, the highlight waits for the exact
`data-setting-id` control to mount; it never substitutes a same-label row.
Navigation or unmount cancels that pending lookup. Actual schema-key links retain
their existing `key:` path and legacy unidentified label fallback, excluding
rows that carry a different config key or an explicit UI identity.
The local composition uses
the existing inline `ErrorNotice` variant; the shared component API is unchanged.
The notice retains a structured machine code for the
diagnostic handoff. It accepts events only while bound to
a conversation slot and only for that slot. An unbound slot (`null` or
`undefined`) renders no failure, ignores playback events, and clears any prior
conversation's failure before the slot is bound again. HTTP-only synthesis
failures use the same request-identity checks as WebSocket failures so they
cannot report against an already interrupted request.
`reportVoiceFailure()` records the localized error and code in the existing
error journal at the WebSocket playback owner before dispatching the UI event.
Thus failures are recorded even when ChatPage is unmounted. The notice consumes
the same report without creating a duplicate; it owns only page-local recovery.

## Local synthesis pipeline

When no custom `piper_binary` is configured and the gateway interpreter has a
compatible Piper Python API, `streaming_piper_reply()` uses the dashboard's
`PiperRuntime`. API capability probing runs off the event loop and never loads
a voice in the gateway. The owned `python -m kiro_crew.piper_worker` subprocess
loads `PiperVoice` once and serves serial requests over stdin/stdout. Every
bounded frame has a type and request identity; PCM additionally carries its
sample rate. Explicit `ready`, `done`, and `error` frames establish lifecycle and
request boundaries without relying on quiet periods or process exit.

The dashboard owns at most one loaded Piper model. Changed model/config paths,
sizes, or modification times retire the old worker before a replacement starts.
The dashboard admits at most two syntheses: the resident path runs one and can
queue one, with its 180-second deadline covering both the wait and synthesis.
Each browser already serializes its sentence requests; the waiter serves
concurrent clients. Further requests receive HTTP 429 with `voice_busy`, which is
retryable after the outstanding work finishes. Cancelled work continues to occupy
capacity until its subprocess and sandbox resources have been reaped. Two clients
interrupting together can therefore receive 429 for their immediate replacements;
they can retry Read aloud once cancellation finishes. Releasing admission before
reaping would let repeated interrupts exceed the model/process bound.
Successful requests retain it for up to 120 seconds of inactivity. Cancellation,
an early generator close, malformed output, or a request timeout kills and reaps
the active worker; a cancelled waiter does not kill another request's worker.
Application shutdown closes the runtime after cancelling active HTTP requests.
The first request after startup, eviction, or cancellation still pays the model's
cold-load and initial phonemizer cost; residency improves subsequent requests,
not cold inference. Cleanup runs in a tracked, shielded task so repeated
cancellation cannot abandon the child or its sandbox launcher; the next start
and shutdown join any outstanding reap before proceeding.

An explicit `piper_binary` or a missing/incompatible Python API retains the CLI
fallback with `--output-raw`. It loads one model for all phrases in that request.
Its raw PCM has no reliable per-line end marker, so EOF defines completion. The
CLI is not reused across requests by guessing that silence means completion.
Its request retains and shields the bounded reap and sandbox-file cleanup task
until completion, including when another stop or shutdown cancels it again.

A successful gateway API probe does not guarantee that the isolated worker can
import and run Piper. If that worker fails before sending any PCM, a synthesis
or protocol failure, or a non-permission OS failure, can use the discovered CLI
after the worker is reaped. No available CLI preserves the original failure.
After any PCM, failure is propagated without replay. Cancellation, timeout,
sandbox or permission refusal, invalid request/model settings and output limits
never trigger fallback. One outer 180-second deadline covers both attempts,
including capability probing and queueing; fallback does not reset the budget.
Both timeout scopes use `piper_runtime.REQUEST_TIMEOUT_SECONDS`; the runtime
retains its own bound when called directly. The worker, resident reader, and CLI
reader share `piper_worker.MAX_AUDIO_BYTES`.

Both paths use `split_sentences()` for CJK/Latin punctuation and newlines, and
`_piper_phrases()` bounds unpunctuated phrases while preserving their tails.

The model JSON declares the actual sample rate. Invalid or absent audio format
is a coded failure before spawn; a guessed rate would change pitch and playback
speed. PCM is signed little-endian mono Int16. Pipe reads are bounded by
`_PIPER_CHUNK_SECONDS`, and an odd trailing byte carries into the next read.
`pcm_to_wav()` wraps complete samples for immediate browser decoding. Replay
encoding runs off the event loop. Piper's own inference still determines when
the first samples become available; chunk size is not a promised synthesis
latency.

Model and configuration paths pass the shared sensitive-path gate before the
gateway reads configuration or starts a worker. Protected paths and paths whose
resolution stalls fail closed with `voice_model_path_forbidden`; the refusal
does not include file contents.

The provider bounds total output and runtime and drains stderr concurrently;
the CLI retains only a bounded diagnostic tail and the resident worker discards
native diagnostics instead of retaining user text. CLI failure logs pass that
tail through `redact_log_via_context`, preserving an installed credential policy
and withholding diagnostic text when its composition fails. Cancellation, timeout, output
overflow, and early generator closure kill and reap the child before removing
sandbox resources. The worker uses the same standard sandbox and subprocess
resource ceilings as the CLI, without an unconfined first-party exception.
Both dashboard Piper spawn paths remove credential-bearing environment keys
with the shared `scrub_env` helper and prepare cgroup argv off the event loop,
matching the upstream built-in engine's controls without changing sandbox policy.
Unsupported sandbox environments still fail closed. The dashboard explicitly
closes the generator even when a request is interrupted while it is suspended
at a yield. `test/test_voice_streaming.py` verifies first PCM before child exit
through real OS pipes, odd-byte alignment, full replay, errors, and cancellation.
`test/test_piper_runtime.py` additionally exercises real framed pipes, reuse,
model replacement, active and waiting cancellations, protocol limits, idle
eviction, and shutdown. Malformed requests and invalid provider PCM retire the
worker without a success frame or processing the next request; entrypoint tests
also verify that model-load failures return a bounded error without native text.

## Interruption

`ChatPage` dispatches `voice-stop` when it sends a message and before a manual
Read aloud request. This replaces a prior synthesis even during model loading,
before any audio has started. `useVoiceInput` also dispatches it when an actual
batch or streaming recording starts, before microphone acquisition, so the
recognizer does not capture ongoing synthesized speech. Hover prewarming does
not interrupt playback. Clicking Read aloud while audio plays stops it.
`useWebSocket` maps the event to `stopVoice()`, which stops scheduled PCM sources,
pauses an active media element, revokes queued blob URLs, invalidates pending
decodes and synthesis requests, and sets `voiceMutedRef`.

While muted, `voice_chunk` frames are discarded and the `chat_segment`/
`chat_done` tail paths do not synthesize more text. A new assistant message
identity alone does not unmute: an interrupted turn stays interrupted across
tool-use segments. A new user, non-passive inject, or subagent turn, an explicit
manual synthesis start, or a slot-focus change establishes a new playback
context. Focus changes stop the departing slot's speech first. Disabling
auto-speak also stops current speech. Every accepted `voice_chunk`,
`voice_complete`, and `voice_error` must match a known `request_id` and the
active slot; clearing the known IDs makes late frames inert even after the
muted flag is reset for another response.

The backend keys active synthesis by `(slot, request_id)` in an application-owned
registry. `POST /api/voice/cancel` cancels the matching handler, which closes its
provider stream and releases its child. A bounded, expiring cancellation record
also rejects a synthesis POST that arrives after its cancellation. Unknown IDs
cannot cancel another request in the same slot. `register_voice_lifecycle()`
cancels active work during dashboard shutdown, and the registry bounds concurrent
synthesis so simultaneous local requests cannot load unbounded models.

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
* `POST /api/voice/cancel`
* `GET /api/voice/voices`
* `GET /api/voice/system-voices`

Synthesis accepts `text`, `slot`, and an optional `request_id`; older clients
receive a generated identity. `voice_chunk`, `voice_complete`, `voice_error`,
and synthesis HTTP results echo that ID. Cancellation requires `slot` and
`request_id`. The synthesis endpoint validates body types, text size, and ID
size before provider work. Provider failures produce `voice_error` and a non-2xx
response with a stable `code`; empty audio is a failure, not successful silence.

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

`_synthesize_polly()`, `_synthesize_piper()`, and `streaming_piper_reply()` run their commands through
`wrap_argv_async(..., _prepare=wrap_argv)` and catch
`SandboxUnavailableError` separately from provider failures. They log the
sandbox error kind and its own message, then **re-raise**. The distinction is
load-bearing because only the sandbox layer can distinguish a missing backend
from transient pressure or an existing outer sandbox, and therefore provides the
applicable remedy.

Re-raising rather than returning `None` is what lets that remedy reach a person.
A refusal collapsed into the generic "no audio" result is indistinguishable from
a broken engine, a rejected voice name or a muted device, so the one fact that
resolves it would live only in the gateway log. `synthesize_speech()` therefore
propagates the error rather than swallowing it, and each caller decides what its
surface can show:

- The dashboard synthesis endpoint relays `str(exc)` to the client and over
  `voice_error`, prefixed but never rewritten — the endpoint must not compose a
  remedy of its own, because only `exc.kind` distinguishes the three cases and
  advising `sandbox_allow_unsandboxed_exec` is actively wrong for two of them.
  The 502 body also carries `code` = `sandbox_<exc.kind>`, derived mechanically
  from the closed kind set so no mapping table can drift: relayed prose is
  untranslatable on its own, and the kind is what decides which remedy it states.
  Neither the 502 nor the `voice_error` broadcast carries the remedy to a person.
  The dashboard does handle `voice_error`, but it keys a generic failure off
  `code` and never renders `error`, and both auto-speak call sites discard the
  rejected request — so the handler also raises one notification-centre
  note carrying the remedy. That handler also drops any event without a
  `request_id`, so the broadcast carries the request identity rather than the slot
  alone; without it the event is discarded before reaching that surface.
  That note is throttled per sandbox KIND, never per
  request: the refusal is a host-level property with an identical remedy for every
  slot, and a key carrying caller-chosen data would grow for the process lifetime
  on a host that refuses every sentence. Three kinds means three entries, bounded
  by construction rather than by a size cap. The note is persisted and its `meta`
  is stored verbatim — the payload validator checks title, body, actions, url and
  ttl, never `meta` values — so what keeps caller junk out of it is the endpoint's
  own request validation, which refuses a non-string `slot` with a 400 before any
  synthesis runs. No downstream normalization is needed, and adding one would be
  unreachable.
  Both branches that reach this helper report through it, so they cannot drift.
  Those are the default `system` engine's single-WAV path and Polly's
  sentence-chunked one; Polly needs its own clause because without it the refusal
  would fall to the generic handler — a 500 with no note — and stay invisible on a
  Polly host, which is the defect itself rather than a cosmetic difference.
  The `piper` provider takes neither: it has its own streaming path whose runtime
  converts the refusal into `VoiceSynthesisError("voice_sandbox_unavailable")`,
  which carries the sandbox's own prose to the HTTP caller but raises no
  notification. Recovering the note there means reading the preserved cause, and
  is deliberately not part of this change.
- `synthesize_and_deliver()` has no channel for prose, so it still reports "no
  audio" and catches the refusal explicitly so it cannot escape as an unhandled
  error on a voice reply. Both current callers then drop that signal — Slack's
  `_safe_voice_reply` discards the returned bool and Telegram only logs it — so a
  refusal is still silent on those surfaces. Closing that is a separate change to
  those callers, not to this function.

## Slack voice replies

`slack.handler` accepts `!voice` thread commands for enabling and disabling a
thread, toggling global replies, and choosing a voice, engine, speed, or pitch.
`handle_message()` starts `_safe_voice_reply()` as a background task when a
thread or global setting enables replies, or when voice-input reply settings
allow a transcribed voice message to receive audio. `_safe_voice_reply()` calls
the provider-aware `voice_reply.voice_reply()` path, so Slack replies follow
the selected provider rather than assuming Polly.
