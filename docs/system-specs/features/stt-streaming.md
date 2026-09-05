# Streaming speech-to-text

## Overview

Live speech-to-text for the dashboard composer. The browser streams 16 kHz mono Int16 PCM over a WebSocket and the server relays partial hypotheses, one or more final transcripts, and (when enabled) an auto-submit signal.

All selectable providers implement streaming (`stt_stream._STREAMING_PROVIDERS`): `local` processes audio in this process, `apple` processes it on-device, and `transcribe` sends it to AWS Transcribe Streaming.

| `stt.provider` | Where recognition runs | Cost | Precondition |
|---|---|---|---|
| `local` (default) | this process, whisper.cpp held loaded by [`kiro_crew.stt`](../../../src/kiro_crew/stt/__init__.py) | free | desktop builds include the runtime; select a model and click **Download now** |
| `apple` | the OS, on-device SpeechAnalyzer | free | macOS 26 or later, and a Swift toolchain to build the helper |
| `transcribe` | AWS Transcribe Streaming | billed per audio-second | the `voice` extra, and a recorded AWS consent |

The batch path at `POST /api/stt/transcribe` (`transcribe.transcribe_audio`)
serves whole files instead: a Slack voice memo, a channel voice note, an upload.
Both paths read one provider setting and apply the same redaction, and on `local`
both go through the same resident model, so a voice memo decodes on the weights a
dictation just warmed. `stt.hallucinations.filter_hallucinations` also runs on both
of `local`'s outputs (whisper emits caption boilerplate on near-silence, and an
emptied transcript is reported as nothing heard rather than written into an agent's
notes). It is the recogniser's own artefact, so it is not applied to `apple` or
`transcribe`.

Compressed files are decoded by the FFmpeg executable in the pinned
`imageio-ffmpeg` wheel. It is part of the desktop voice runtime, not a desktop
system prerequisite. The release gate resolves that exact packaged resource and
executes `ffmpeg -version`; a supported artifact cannot publish with only
dependency metadata or an ambient PATH copy satisfying the check. Source
environments do not read their own site-packages, because a project venv is
agent-writable executable storage; they resolve a system FFmpeg from the fixed
platform paths, and failing that the digest-verified decoder store below.

**The store is a third location, not a third rule.** A source or Toolbox install on
a distribution that packages no FFmpeg (Amazon Linux, RHEL without EPEL) previously
had no decoder it could ever reach, so batch voice was permanently broken with a
shell command as the only remedy — and on such a host that command was an `echo`
of the ffmpeg.org URL. `stt.decoder` closes that: it downloads the platform's
pinned `imageio-ffmpeg==0.6.0` **wheel** from PyPI (filename, URL, size and sha256
pinned per platform), verifies the wheel's own digest before opening it, extracts
only `imageio_ffmpeg/binaries/<artifact>` by exact member name through a descriptor
it opened itself (never `ZipFile.extract`, whose destination is attacker-controlled
name data), re-verifies the extracted bytes against the SAME
`transcribe._PACKAGED_FFMPEG_ARTIFACTS` pin, and renames it atomically into
`<data home>/models/ffmpeg/`. Both digest tables are one table, derived in
`stt.decoder`: two copies would fail as a decoder that downloads successfully and
is then refused at exec, with nothing to say which copy was wrong.

Resolution order is bundled site-packages → the trusted system directories → the
store. The store adds no directory to `transcribe._ffmpeg_candidate_dirs`, and that
distinction is the whole point: that list is searched by NAME, so an entry there is
a claim that a path is trustworthy, while the store is reached only as a pinned
FILENAME whose bytes match its pinned digest, re-verified on every open and kept
bound to the descriptor that is spawned. A store file that fails is ignored and
logged. The directory is user-writable and vouches for nothing; it is also inside
the `models/` tree the agent's file and shell gates already write-protect.
Platforms with no pinned artifact (32-bit ARM Linux, Windows on ARM, win32) report
`unsupported` and start no download.

That gate reports **two independent verdicts** and the build treats them
differently (`transcribe.PackagedDecoderProbe`). Whether the resolved bytes
**authenticate** is a property of the artifact, holds on every host, and failing
it stops the build. Whether they then **execute** is a property of the build
machine: an image lacking an OS library the executable load-time imports makes
the loader refuse it before its entry point runs, while the identical bytes run
correctly for a user. Windows Server Core is the case that forced the
distinction — it carries no Video for Windows components, so it cannot load a
Windows ffmpeg that imports `AVICAP32.dll`, and every CodeBuild Windows image is
built on it. So an authenticated payload that will not run **warns and ships**;
only an unauthenticated one fails. Collapsing the two reported a build-host
limitation as a corrupt artifact and withheld a correct release over it.

That executable ships **uncompressed** inside the macOS bundle, and must keep
doing so: the Apple notary service decompresses archive members and scans what is
inside them, so a decoder sealed as a compressed payload is rejected as an
unsigned nested executable and fails the entire macOS release. Shipping it plain
means the app signer rewrites its Mach-O signature, so its released bytes cannot
match the pinned upstream digest. The runtime therefore accepts the macOS decoder
on **either** cryptographic anchor: the pinned upstream digest (a local or
unsigned build, and the pre-signing release gate above) or a valid Developer ID
signature from the release team, evaluated by `codesign` against the exact
private snapshot staged for execution. Neither anchor is a path or a
filesystem-permission claim, and a payload satisfying neither is refused.
`packaging/signing/generate-manifest.py` additionally fails the sign when any
compressed member of the bundle contains a Mach-O, so this class of defect
surfaces at sign time rather than as an opaque notarization `Invalid`.

Legacy provider values and the loader behavior for persisted values are in
[Legacy provider values](#legacy-provider-values).

## Architecture

```
mic -> AudioWorklet (16 kHz mono Int16 PCM) -> WebSocket /api/ws/stt
    -> provider session (local | apple | transcribe)
    -> status / partial / final / endpoint frames
    -> composer (partial tail replaced in place)
```

### Components

| Component | File | Role |
|---|---|---|
| WS endpoint | `src/kiro_crew/dashboard/stt_stream.py` | One provider session per connection, plus the caps and the SEL audit pair |
| Local recogniser | `src/kiro_crew/stt/engine.py` | One resident whisper.cpp context, serialised decodes, idle eviction |
| Local session | `src/kiro_crew/stt/session.py` | Turns a PCM stream into partials and a final |
| Endpointing VAD | `src/kiro_crew/stt/vad.py` | Adaptive-RMS speech detection and end-of-utterance |
| Model catalog | `src/kiro_crew/stt/models.py` | The offered models, their sizes, and the sha256-pinned download |
| Apple helper | `src/kiro_crew/apple_speech/` | Swift `AppleTranscribe.swift` plus its Python driver |
| Config fields | `src/kiro_crew/config/loader.py` | `SttConfig`, and the degradation rules for a stored provider or model |
| Worklet | `website/public/pcm-worklet.js` | Float32-to-16 kHz mono Int16 PCM downsampler |
| Streaming hook | `website/src/hooks/useStreamingStt.ts` | Opens the WS, wires the worklet, emits partial and final |
| Voice hook | `website/src/hooks/useVoiceInput.ts` | Chooses streaming or batch, owns mic and device selection |
| Composer wiring | `website/src/pages/ChatPage.tsx` | Splices the live region into the input box |
| Recording UI | `website/src/components/VoiceDictationPanel.tsx`, `VoiceStatusBar.tsx` | The animated panel, and the thin bar it falls back to |
| Settings UI | `website/src/pages/settings/SttSettings.tsx` | Enable, provider, model, language, and the streaming knobs |

## WebSocket protocol

Client to server:

- Binary frames: raw 16 kHz mono, little-endian Int16 PCM; `test/test_stt_stream.py` pins the transport format and frame limits.
- Text frame `{"type":"stop"}`: the user released the mic. The server finishes
  the utterance and closes, so trailing finals still arrive.

Server to client, JSON. `stt.session.SttEvent.kind` supplies the local provider's `partial` and `final` frame types; `dashboard.stt_stream` owns the complete wire contract:

- `{"type":"ready"}`: the session is live and the client may send audio. Capture begins before this arrives, so `useStreamingStt` buffers PCM locally and flushes it in order after readiness. The buffer is capped and drops **oldest-first** so an unavailable server cannot grow browser memory without bound.
- `{"type":"status","stage":...,"downloaded_bytes":N,"total_bytes":N,"code":...}`
  where `stage` is `downloading` or `ready`. A first-ever local session has to
  fetch weights before it can recognise anything, and a silent transfer is
  indistinguishable from a hang, so the transport emits the notice itself *before*
  starting the fetch and `LocalSession.prepare()`'s own copy of it is dropped on
  return: re-sending it with a zero byte count would walk a progress reading
  backwards. Live byte progress is polled from `GET /api/stt/status` rather than
  pushed. A session with nothing to report emits no status frame at all.
- `{"type":"partial","text":"..."}`: an in-progress hypothesis that replaces the
  previous one.
- `{"type":"final","text":"..."}`: the committed transcript for the utterance.
- `{"type":"endpoint","complete":true}`: the semantic endpointer judged the
  utterance a finished request, so the composer may submit without a keypress.
  Only when `stt.endpointing` is on.
- `{"type":"error","message":"...","code":"..."}`: a setup failure, a refusal or
  a cap. The English `message` is advisory and the `code` is the contract, because
  the dashboard renders localised text and cannot key off a sentence. Codes the
  `stt` package already owns travel through unchanged rather than being remapped;
  the transport adds `_CODE_MAX_DURATION` and `_CODE_SESSION_FAILED` for the two
  conditions only it can see. Only the FIRST fatal claimant sends a frame
  (`_claim_fatal`): otherwise the duration cap and a concurrent failure each emit
  one in the window before the other's close lands, and the client shows two
  contradictory errors for a single failure. `useStreamingStt` resolves the code
  through `sttProviders.streamErrorMessage`, which prefers a stream-specific
  catalog key, falls back to the availability vocabulary the settings panel
  already renders, and only then to `message`.

Partials and finals both pass `security.redact_credentials` and
`security.redact_exfiltration_urls` before emit. A partial is ephemeral and never
persisted, but it is written into the browser DOM, which makes it an external
surface: a spoken credential must not flash unredacted.

## Activation

The endpoint answers **503** unless all three hold:

1. `stt.enabled`
2. `stt.streaming`
3. `stt.provider` is in `stt_stream._STREAMING_PROVIDERS`

The third is positive membership in a named tuple, never an inequality or a
negation against one provider. Adding a name to that tuple grants it the
endpointer, the caps and the `stt_stream_*` audit identity in one step, so the
grant has to be an explicit edit to the set rather than a side effect of not
matching some other provider. `handlers/core.py` serves the same tuple to the
settings page as `streaming_providers`, so the UI gates its streaming controls on
that capability instead of on a hardcoded name.

After the three gates, each provider has its own precondition and failure frame:

- **local**: the recogniser must import (`stt.engine.probe`) and the configured
  model must be on disk. Supported desktop releases bundle the recogniser and
  fail their build if it is missing; macOS Intel is the unsupported exception.
  The model remains an explicit one-click download, and first dictation can join
  the same transfer. Source/PyPI installs can still add the `voice` extra without
  a gateway restart. What cannot be fixed by waiting arrives as an `error` frame
  carrying `stt_extra_missing`, `stt_no_wheel_for_platform` or
  `stt_import_failed`.
- **apple**: `apple_speech.availability()` decides, and separates "this macOS
  cannot run it" from "the Swift toolchain is missing", because only the second
  has a fix.
- **transcribe**: `amazon_transcribe` must be importable, and
  `aws_consent.authorize(SERVICE_TRANSCRIBE, profile, region)` must grant.

### The AWS consent gate is an authorization, not a preference

Transcribe bills per second of audio, so the socket is refused before the client
is constructed and before any audio is read, and the refusal is reported over the
same `error` frame as every other setup failure so the audit pair stays balanced.
The grant is recorded per profile, per region and per resolved account in
`aws_service_consent.json` under the data home, which sits on the read and write
keystone floor, so the agent can neither read the record nor grant itself
permission to spend. The authenticated dashboard is the only writer: there is
deliberately no CLI verb, because a terminal command that records a grant on
request is a grant an automated caller can take.

Moving that check later, adding a CLI verb that records a grant, or reporting the
refusal over some other channel each break one of those three properties.

## The local provider's pipeline

whisper.cpp is not a streaming recogniser: it decodes a buffer. Live text is
therefore produced by decoding repeatedly as audio arrives, and the interesting
question is *what* to re-decode.

**Endpointing.** `stt.vad.Endpointer` consumes the same PCM as the recogniser and tracks an adaptive noise floor rather than a fixed dBFS threshold. A frame must clear `SPEECH_MARGIN_DB`, speech must persist for `MIN_SPEECH_FRAMES`, and quiet for `stt.silence_ms` ends an utterance; `DEFAULT_MAX_UTTERANCE_MS` bounds a session that never becomes quiet. `test/test_stt_vad.py` pins the frame, threshold, and endpointing behavior. The floor falls quickly and rises slowly so sustained speech does not raise it enough to terminate the speaker mid-sentence.

**Partials.** The detector that decides when the utterance ended also decides
where to cut it. On a pause too short to end the utterance, the audio so far is
decoded once and its text is *committed*, and the phrase buffer resets. A partial
is then the committed text plus a decode of the current phrase, so its cost
tracks the current phrase rather than the whole recording. Decoding the entire
utterance on every partial makes each update grow with the recording and can fall behind the speaker. Committed text never regresses under the speaker. Cadence is `stt.partial_interval_ms`, pinned by `test/test_stt_session.py`.

**The final.** One decode of the entire buffer, so the text that reaches the
message box has the full context the model would have had if it had never been
streamed, followed by `filter_hallucinations`. Partials are fast and approximate
on purpose; the final is the accurate one.

**A failed decode is not silence, and the engine no longer says it is.** whisper.cpp
reports a failure through `whisper_full`'s return code and pywhispercpp discards it:
`Model._transcribe` calls the binding as a bare statement, then reads
`whisper_full_n_segments`, which is 0 after a failed encode — so `transcribe` answers
the empty list for a failure and the empty list for a quiet room, with the difference
visible only in whisper.cpp's own stderr (`whisper_full_with_state: failed to encode`).
`stt.engine` therefore reads that status itself, from the same extension module the
library calls, and `WhisperEngine.decode` RAISES `engine.DecodeFailed` carrying
`stt_decode_failed` for a native failure, an exception out of the call, or a decode
that outran `DEFAULT_TIMEOUT_SECS`. It still returns `""` for the three non-events a
caller already handles — no resident model, a superseded partial, and an `expect`
mismatch — so an empty transcript means nothing was heard and nothing else.

`LocalSession` splits the two failure classes by what the user keeps. A FINAL decode
failure becomes an `error` event carrying that code, which the transport relays as an
`error` frame before closing; a partial or a phrase-commit failure is logged and
skipped, because the next partial is moments away and one bad decode must not end a
session the speaker is still talking into. Returning an empty final instead is what
made this invisible: the transport drops an empty final, so a failed decode after the
user pressed stop discarded the whole utterance with nothing on screen to say why.
`transcribe_pcm` reports the same code through the `Availability` it already returns,
so the batch path names the reason rather than reporting a memo it could not hear.

The detector, not the client, normally ends an UTTERANCE: `feed()` returns the
final, drops that utterance's audio and committed text, and installs a fresh
`Endpointer` for the next one.

**The chunk that ends an utterance is split, not filed whole.** A client chunk can contain both the silence that ends one utterance and speech that starts the next. `Endpointer.push` stops at the frame that closed the utterance and returns
everything after it as `VadUpdate.pending`; `feed()` buffers only the head, finalises,
and then seeds the re-armed buffer and detector with that tail. Filing the chunk whole
attributed resumed speech to the utterance that just closed, where it sits behind a
hangover of silence and contributes nothing, and clipped that word's onset off the
utterance it belongs to. `pending` is empty unless `ended`, because otherwise it would
be the sub-frame carry `push` retains internally and a caller re-feeding it would
duplicate audio. It does **not** end the session, and
`LocalSession.ended` is not set — only a client `stop`, a close, or the session
audio ceiling does that. A session spans many utterances here exactly as it does on
`apple` and `transcribe`, and `useStreamingStt` accumulates finals rather than treating the first as the end. Ending the socket on a recognizer utterance would make continuous transcription stop after the first pause.

An utterance finishing is also a different event from the `endpoint` frame (a
judgment about whether the finished text is a complete request). The transport
skips `finish()` entirely on a session with no deliverable transcript, whose client
went away, or whose socket is closed. That is not tidiness: `finish()` is a decode
of the whole tail, real work on the shared model that a live session behind this one
would queue behind. It is gated on `LocalSession.has_pending_audio` rather than on
"a final was already sent", because over a multi-utterance session both are true at
once and reading the latter discarded whatever was said after the last detected
pause. The endpointer is closed AFTER the final, because the final is the one
segment its judgment is about.

**Residency.** The model is loaded once and reused. A warm decode is tens of
milliseconds against seconds for anything that loads a model per utterance, which
is the whole reason this path is worth having. `stt.idle_evict_secs` releases the
weights after a quiet spell, because a reload from a warm OS cache is a fraction
of a second and the resident footprint is not something to hold for the life of a
gateway that transcribed one voice memo this morning. Decodes run on
`executors.stt_executor()` and hold `WhisperEngine._decode_lock`: `whisper_full`
mutates the context, so two concurrent decodes on one context corrupt each other,
and a superseded partial aborts rather than queueing.

`stt.engine`'s docstring carries the two properties that make this safe inside
the gateway process: whisper.cpp releases the GIL for the duration of a decode,
and it writes nothing to stdout with `print_progress=False` and
`print_realtime=False`. The second matters because the MCP servers import this
module and their stdout *is* their protocol. Neither argument may be removed.
stderr is not quiet, so no test may assert it empty.

`redirect_whispercpp_logs_to` stays at its `False` default. Its binding governs stderr rather than the log callback, and `None` redirects process-wide fd 2 during model loading, silencing unrelated threads while leaving stdout behavior unchanged.

## Model download

`stt.models` holds the catalog: name, byte size and a sha256 digest per entry.
Four endpoints expose it, all four refused to an app token by `_deny_app_token`
because they start a download and warm a resident model inside the gateway, which
is operator setup rather than something an app earns by naming a path (the
transcription surfaces are deliberately open to an app token):

- `GET /api/stt/status`: the availability code and prose, the resolved model with
  `model_present` and its size, whether a model is resident right now, and the
  live transfer state. Separate from `GET /api/config/stt`, which serves settings.
  It also carries `ffmpeg: {present, source, auto_fetch, os, arch, download}`.
  `source` is `bundled` | `system` | `store` | `null` and names WHICH decoder the
  transcode path would run, because each one is repaired differently — reinstall
  the app, the host's package manager, or the fetch below. `auto_fetch` is
  `available` | `unsupported` | `bundled`, so the panel offers a button rather than
  a shell command, and `unsupported` is not a failure: no retry can make a pinned
  artifact exist. `os`/`arch` are the GATEWAY's, since a dashboard may be open
  against another machine and the agent hand-off has to name the host that needs
  fixing. `ffmpeg_missing` on `GET /api/config/stt` is kept for compatibility.
- `POST /api/stt/prepare`: starts or joins the transfer and returns its current state. Concurrent callers share one transfer behind the store's lock.
- `POST /api/stt/prewarm`: starts local-provider preparation without waiting for completion. `useVoiceInput` calls it while the user reaches for the microphone so local initialization can overlap capture.
- `POST /api/stt/ffmpeg/download`: starts or joins the decoder fetch, 202 then
  poll, the same contract as `prepare`. Refused 409 on a bundled interpreter
  (`stt_decoder_bundled`) and on a platform with no pinned artifact
  (`decoder_unsupported_platform`) rather than answering 202 for work that can
  never help. A completed whisper-model download also starts this in the
  background when the interpreter is not bundled and no decoder resolves — that is
  the one moment a second transfer reads as part of the same setup, instead of
  surfacing as a hang during a first voice memo.

Desktop installers intentionally contain no speech-model weights. Settings shows
the selected model's exact size and a **Download now** action; that is the only
setup action a desktop user needs because the recogniser and its dependencies are
already in the application. First use can start or join the same download, and
every later session loads the verified model from disk. The digest is the trust
anchor for that fetch: bytes are streamed to a
staging file inside the target directory and renamed into place only after the
computed digest matches, so a tampered mirror, a truncated transfer or a
captive-portal HTML body can only fail verification. The pinned **size** is enforced
as a ceiling during the transfer rather than compared afterwards: nothing about an
HTTPS response bounds its length, `Content-Length` is the server's claim rather than
the pin, and the operator can point `KIROCREW_WHISPER_MODEL_BASE_URL` at any host, so
streaming to EOF first let a hostile or misconfigured mirror fill the disk before
anything was checked. The refusal precedes the write, which caps the overshoot at one
read; the post-loop size comparison is then only reachable for a *short* response,
which is the common failure and keeps its own message. The staging file comes from
`tempfile.mkstemp`, written through the descriptor it returns: the name is
unpredictable and the create is exclusive, so a symlink pre-planted at a guessable
staging path cannot redirect the write.

A file already on disk is verified against the pin too, on every model LOAD. Not once
per session, because `WhisperEngine.ensure_loaded` settles residency before asking the store. It is not memoised against size and mtime because
`os.utime` is available to anything that can write the file.

The digest is the second line of defence, not the first. Verifying and then handing a
PATH to a native loader leaves a window in which the bytes can be swapped, and
re-hashing cannot close it because the loader re-opens by name. What closes it is that
`<data home>/models` is **write-protected from the agent on both gates**
(`security._WRITE_PROTECTED_HOME_PATHS` for the file tools,
`_WRITE_PROTECTED_BASH_LEAVES` for the shell), so the verified bytes are the loaded
bytes. Reads stay allowed — the weights hold no secret and the settings surface reports
what is installed — and Kiro Crew's own downloader writes directly without routing
through those gates, so a first fetch and a re-download after a failed check both work.
The same directory holds the embedding GGUF, which the one entry covers.

`is_present` also checks the file's size, which is what makes an interrupted
download visible: a staging file never occupies the final path, so a wrong size
there means a replaced or truncated file, and reporting it as absent lets the next
download overwrite it. `MODEL_URL_ENV` repoints the base URL for a mirrored or
air-gapped install without weakening the pin, and `SKIP_DOWNLOAD_ENV` is the same
switch the embedding downloader honours, so one setting means "this process must
not pull model weights".

## Caps and limits

Every limit is a named constant in the module that owns it. The values are not
restated here, because a copied constant goes stale silently.

| Constant | Module | Bounds |
|---|---|---|
| `_MAX_CONCURRENT_SESSIONS` | `dashboard/stt_stream.py` | Sessions per gateway process |
| `_MAX_STREAM_DURATION_SECS` | `dashboard/stt_stream.py` | Wall-clock life of one connection |
| `_MAX_WS_MSG_SIZE` | `dashboard/stt_stream.py` | One inbound audio frame |
| `_MAX_TEXT_FRAME_BYTES` | `dashboard/stt_stream.py` | One inbound control frame |
| `_MAX_MODEL_PREPARE_SECS` | `dashboard/stt_stream.py` | The one-time model fetch a first-ever `local` session waits on |
| `heartbeat` on `WebSocketResponse` | `dashboard/stt_stream.py` | Idle liveness ping interval |
| `MAX_SESSION_SECS` | `stt/session.py` | Audio one local session buffers |
| `MAX_PHRASE_SECS` | `stt/session.py` | Phrase length before a commit is forced |
| `MIN_DECODE_SECS`, `MIN_COMMIT_SECS` | `stt/session.py` | Floors below which a decode or a commit is not worth doing |
| `THREAD_CEILING` | `stt/engine.py` | Extrapolation ceiling on the derived thread count |
| `DEFAULT_TIMEOUT_SECS` | `stt/engine.py` | One decode or one model load |
| `DEFAULT_MAX_UTTERANCE_MS`, `MIN_SILENCE_MS` | `stt/vad.py` | Utterance backstop, and the floor on `stt.silence_ms` |

The duration and concurrency caps exist for a different reason per provider and
for an unbounded cost in every case. On `transcribe` an abandoned socket bills per
audio-second and counts against the account's concurrent-stream quota; on `apple`
it holds a helper process and an OS recognition session; on `local` it accumulates
buffered audio and keeps queueing decodes onto the one shared model. The
concurrency cap is shared by the free providers because all still consume bounded local capacity.

The model fetch gets its own ceiling rather than borrowing the session's because it transfers a catalog entry rather than a dictation. `stt.models` uses a request timeout, while `_prepare_local_with_progress` also bounds how long a WebSocket waits. On the WebSocket timeout the transfer is shielded and left running:
cancelling it would release the model store's transfer lock while its worker thread
is still writing the staging file, and the next session would start a second write
to the same path. Only the socket gives up; the bytes land for the next attempt.

`test/test_stt_stream.py` pins the transport caps, `test/test_stt_session.py` the
session ones, `test/test_stt_vad.py` the detector's thresholds and
`test/test_stt_engine.py` the thread derivation and the availability codes.

## SEL audit pairing: emit before closing

Every accepted connection logs `stt_stream_start`, and **every** exit path must
log a matching `stt_stream_end` (`error`, `refused`, `timeout` or `ok`) or the
audit trail shows an unmatched start. A rejection before the socket is prepared
logs `stt_stream_rejected` instead.

`stt_stream_end` is emitted **before** `await ws.close()`, never after, on the
early-return paths (via `_close_and_end_audit`) and on the normal cleanup path.
`WebSocketResponse.close()` awaits the *peer's* close acknowledgement under its
own timeout, so a client that has already gone away (an abrupt disconnect, a
closed tab) parks the handler inside `close()`, and with the audit after the close
the end event is withheld for as long as that takes. Emitting first makes the
pairing independent of the peer, which is the property a balanced trail actually
needs. The close still runs, is still awaited immediately after, and still
tolerates a broken transport (logged, not raised).

A claimed fatal cause outranks the read loop's own outcome: the loop can exit
cleanly because the cap or the relay closed the socket under it, and recording
that as `ok` would report a session that died as a session that finished.

Tests asserting on the audit pair must **wait** for the end event: neither
receiving the error frame nor exiting the `TestClient` context orders the
assertion after the server handler's remaining steps, so asserting straight after
either one is a race.

## Frozen-prefix behaviour

`ChatPage.tsx` snapshots the composer's contents and the caret on the first
`partial` of an utterance. Later partials replace only the live region after that
snapshot, so anything the user typed before speaking survives, and the caret does
not jump. The snapshot clears on the final, so the next utterance starts from the
newly committed text.

## Legacy provider values

`_validated_stt_provider` in `config/loader.py` accepts only `local`, `apple`, and `transcribe`. Persisted `whisper`, `mlx`, `parakeet`, or `faster` values degrade to `local` and log the replacement rather than preventing the gateway from loading a voice setting. `stt.models` resolves legacy model aliases to a catalog entry; unknown models fall back through the loader's validation path.

Legacy config fields such as `whisper_path`, `mlx_model`, `parakeet_model`, and `device` are ignored by `KiroCrewConfig.load` because `SttConfig` does not consume them. `config/superseded_defaults.py` records migrated defaults for the config surface.

## Deliberately not built

- **Speaker diarisation and word-level timestamps.** Neither has a consumer in
  the composer, and both change the frame shape every client conforms to.
- **A neural VAD.** It would add a second model download and native dependency; `stt.vad.Endpointer` supplies the current adaptive-RMS decision.
- **Fan-out of one utterance to several agents.** One session drives one
  composer.
