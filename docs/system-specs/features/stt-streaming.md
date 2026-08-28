# Streaming speech-to-text

## Overview

Live speech-to-text for the dashboard composer. The browser streams 16 kHz mono
Int16 PCM over a WebSocket and the server relays partial hypotheses, one final
transcript, and (when enabled) an auto-submit signal.

All three providers stream, so streaming is a property of the endpoint rather
than of a provider:

| `stt.provider` | Where recognition runs | Cost | Precondition |
|---|---|---|---|
| `local` (default) | this process, whisper.cpp held loaded by [`kiro_crew.stt`](../../../src/kiro_crew/stt/__init__.py) | free | the `voice` extra, plus one model download on first use |
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

Retired providers, and what happens to a config that still names one, are in
[Retired providers](#retired-providers).

## Architecture

```
mic -> AudioWorklet (16 kHz Int16 PCM) -> WebSocket /api/ws/stt
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
| Worklet | `website/public/pcm-worklet.js` | Float32 to Int16 downsampler onto 16 kHz |
| Streaming hook | `website/src/hooks/useStreamingStt.ts` | Opens the WS, wires the worklet, emits partial and final |
| Voice hook | `website/src/hooks/useVoiceInput.ts` | Chooses streaming or batch, owns mic and device selection |
| Composer wiring | `website/src/pages/ChatPage.tsx` | Splices the live region into the input box |
| Recording UI | `website/src/components/VoiceDictationPanel.tsx`, `VoiceStatusBar.tsx` | The animated panel, and the thin bar it falls back to |
| Settings UI | `website/src/pages/settings/SttSettings.tsx` | Enable, provider, model, language, and the streaming knobs |

## WebSocket protocol

Client to server:

- Binary frames: raw 16 kHz mono Int16 PCM, little-endian.
- Text frame `{"type":"stop"}`: the user released the mic. The server finishes
  the utterance and closes, so trailing finals still arrive.

Server to client, JSON. The frame set is a rename of
`stt.session.SttEvent`, whose `kind` values are the frame `type` values, so a new
event shape cannot reach the browser without a matching field on that dataclass:

- `{"type":"ready"}`: the session is live, the client may send audio. Capture starts
  before this arrives, so the client buffers PCM locally until it lands and then
  flushes in order. That buffer is capped and drops **oldest-first**, so the cap is
  sized against the slowest readiness rather than one provider's connect time: a cold
  resident load compiles a GPU pipeline (measured 7.4 s) after verifying the weights'
  digest (up to ~4 s for the largest model), and a cap calibrated for Transcribe's
  ~2-3 s spin-up silently discarded the words the user opened with while the UI still
  showed a live mic. It is not sized against the transport's own 1800 s prepare
  ceiling, because that covers a model *download*, which the `downloading` status
  announces and which nobody expects to talk through.
- `{"type":"status","stage":...,"downloaded_bytes":N,"total_bytes":N,"code":...}`
  where `stage` is `downloading` or `ready`. A first-ever local session has to
  fetch weights before it can recognise anything, and a silent 148 MB transfer is
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
  contradictory errors for a single failure.

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

Past the three gates, each provider has its own precondition and its own failure
frame:

- **local**: the recogniser must import (`stt.engine.probe`) and the configured
  model must be on disk. Neither is checked as an activation gate, because both
  resolve themselves: a missing model downloads, and a failed import is not
  cached in `sys.modules`, so installing the extra takes effect without a gateway
  restart. What cannot be fixed by waiting arrives as an `error` frame carrying
  `stt_extra_missing`, `stt_no_wheel_for_platform` or `stt_import_failed`.
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

**Endpointing.** `stt.vad.Endpointer` consumes the same PCM the recogniser will,
in 20 ms frames, and tracks the noise floor rather than comparing against a fixed
dBFS threshold: no constant survives a built-in laptop microphone, a headset and
a noisy room at once. A frame counts as speech when it clears the floor by
`SPEECH_MARGIN_DB`, the utterance starts only after `MIN_SPEECH_FRAMES`
consecutive such frames (one loud frame is a key click, not a word), and it ends
after `stt.silence_ms` of quiet. `DEFAULT_MAX_UTTERANCE_MS` is the backstop that
makes a session terminate on its own when the room never goes quiet. The floor
falls quickly and rises very slowly, because a fast-rising floor is dragged up by
sustained speech until the speaker's own voice stops clearing the margin and the
utterance ends mid-sentence.

**Partials.** The detector that decides when the utterance ended also decides
where to cut it. On a pause too short to end the utterance, the audio so far is
decoded once and its text is *committed*, and the phrase buffer resets. A partial
is then the committed text plus a decode of the current phrase, so its cost
tracks the current phrase rather than the whole recording. Decoding the entire
utterance on every partial is the obvious design and the wrong one: at the
measured real-time factor of about 0.01 a 60 s dictation would spend hundreds of
milliseconds per partial and fall behind the speaker. Committed text also never
regresses under the speaker. Cadence is `stt.partial_interval_ms`.

**The final.** One decode of the entire buffer, so the text that reaches the
message box has the full context the model would have had if it had never been
streamed, followed by `filter_hallucinations`. Partials are fast and approximate
on purpose; the final is the accurate one.

The detector, not the client, normally ends an UTTERANCE: `feed()` returns the
final, drops that utterance's audio and committed text, and installs a fresh
`Endpointer` for the next one.

**The chunk that ends an utterance is split, not filed whole.** A client's chunk is
much longer than a detector frame — a browser sends about 100 ms against a 20 ms
frame — so the chunk that trips the endpoint can also carry the start of the next
utterance. `Endpointer.push` stops at the frame that closed the utterance and returns
everything after it as `VadUpdate.pending`; `feed()` buffers only the head, finalises,
and then seeds the re-armed buffer and detector with that tail. Filing the chunk whole
attributed resumed speech to the utterance that just closed, where it sits behind a
hangover of silence and contributes nothing, and clipped that word's onset off the
utterance it belongs to. `pending` is empty unless `ended`, because otherwise it would
be the sub-frame carry `push` retains internally and a caller re-feeding it would
duplicate audio. It does **not** end the session, and
`LocalSession.ended` is not set — only a client `stop`, a close, or the session
audio ceiling does that. A session spans many utterances here exactly as it does on
`apple` and `transcribe`, and both clients accumulate finals rather than treating
the first as the end. Ending the session on the detector broke the continuous
consumer outright: the socket closed on the speaker's first pause,
`useMeetingTranscription`'s `onclose` reported `disconnected` and ran its cleanup,
and because its session binding keys only on `status` nothing restarted it, so
transcription stayed dead for the rest of the meeting while the UI still showed
Live.

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

`redirect_whispercpp_logs_to` must be left at its `False` default. Despite the
name it governs stderr rather than the log callback, and `None` makes the binding
`os.dup2` `/dev/null` over **fd 2** for the whole model load — process-wide, so
every other thread is silenced with it. Measured across one load: 693 of 862
concurrent stderr writes destroyed, against zero at the default, with stdout clean
either way.

## Model download

`stt.models` holds the catalog: name, byte size and a sha256 digest per entry.
Three endpoints expose it, all three refused to an app token by `_deny_app_token`
because they start a download and warm a resident model inside the gateway, which
is operator setup rather than something an app earns by naming a path (the
transcription surfaces are deliberately open to an app token):

- `GET /api/stt/status`: the availability code and prose, the resolved model with
  `model_present` and its size, whether a model is resident right now, and the
  live transfer state. Separate from `GET /api/config/stt`, which serves settings.
- `POST /api/stt/prepare`: starts or JOINS the transfer, 202 with the current
  state. Concurrent callers share one transfer behind the store's own lock.
- `POST /api/stt/prewarm`: 202, fire-and-forget, called when the user reaches for
  the mic rather than when they release it. A first-ever load compiles a GPU
  pipeline (measured at 7.4 s) and the first decode after any load allocates its
  graph (154-528 ms), so both are paid while the user is still speaking.

The first use of voice input fetches one model and every later session loads it
from disk. The digest is the trust anchor for that fetch: bytes are streamed to a
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
per session, because `WhisperEngine.ensure_loaded` settles residency before asking the
store, so the hash costs 0.5 s (default) to ~5 s (largest) once per load rather than on
the path this feature exists to make fast. Not memoised against size and mtime either:
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
concurrency cap is deliberately NOT widened for the free providers: the number
that would justify a higher one is a measured number, and nothing has measured it.

The model fetch gets its own ceiling rather than borrowing the session's, because
it is a transfer of the catalog entry's whole size rather than a dictation, and
the fetch applies no read timeout, so a stalled mirror would otherwise hold a concurrency slot for the
life of the gateway. On that timeout the transfer is SHIELDED and left running:
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

## Retired providers

`whisper`, `mlx`, `parakeet` and `faster` are gone. Each of them needed an
out-of-band install the user had to perform themselves (a whisper CLI on `PATH`,
or an `mlx-whisper` / `parakeet-mlx` / `faster-whisper` wheel), which is exactly
the cost the resident local engine removes while recognising the same speech.
Nothing needs installing by hand any more, and the Metal acceleration that was
`mlx`'s reason to exist is already in the whisper.cpp wheel on Apple silicon.

A `config.json` that still names one keeps working: `_validated_stt_provider`
degrades it to `local` and logs which value it replaced and why. It never raises,
because the value arrives from a file on disk and failing the load that read it
would take the whole gateway down over a voice setting. `stt.model` degrades the
same way, except that the catalog's alias table maps a superseded name onto the
entry that best honours what was asked for, so a stored `turbo` keeps the accuracy
ceiling instead of silently dropping to the default.

The deleted config fields (`whisper_path`, `mlx_model`, `parakeet_model`,
`device`) are ignored when present. `config/superseded_defaults.py` records the
two defaults that moved, so an install that materialized the old value is told
about the drift rather than left on it.

## Deliberately not built

- **Speaker diarisation and word-level timestamps.** Neither has a consumer in
  the composer, and both change the frame shape every client conforms to.
- **A neural VAD.** It would work, and it costs a second model download plus
  another native dependency for a decision the measured 36 dB separation between
  speech and noise floor already makes unambiguous.
- **Fan-out of one utterance to several agents.** One session drives one
  composer.
