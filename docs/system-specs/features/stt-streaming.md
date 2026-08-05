# Streaming STT — Design Document

## Overview

Live speech-to-text for the dashboard chat input. The browser streams
16 kHz Int16 PCM audio over a WebSocket; the backend forwards to AWS
Transcribe Streaming and relays partial + final transcripts back so the
user sees text appear in the input box as they speak.

This complements the existing batch path at `POST /api/stt/transcribe`,
which remains the default and the only option for the `whisper` provider.

## Architecture

```
mic → AudioWorklet (PCM 16 kHz Int16) → WebSocket /api/ws/stt
    → TranscribeStreamingClient → partial/final events
    → WebSocket → ChatPage input (live preview)
```

### Components

| Component | File | Role |
|-----------|------|------|
| WS endpoint | `src/kiro_crew/dashboard/stt_stream.py` | One Transcribe stream per WS connection |
| Config field | `src/kiro_crew/config/loader.py` | `stt.streaming: bool` (default `False`) |
| Worklet | `frontend/public/pcm-worklet.js` | Float32 → Int16 PCM downsampler (48 kHz → 16 kHz) |
| Streaming hook | `frontend/src/hooks/useStreamingStt.ts` | Opens WS, wires worklet, emits partial/final |
| Voice hook | `frontend/src/hooks/useVoiceInput.ts` | Delegates to streaming hook when enabled |
| Input wiring | `frontend/src/pages/ChatPage.tsx` | Overwrites the partial tail of the input box |
| Settings toggle | `frontend/src/pages/overview/SlackTab.tsx` | "Streaming" toggle (provider=transcribe only) |

## WebSocket Protocol

Client → server:
- Binary frames: raw 16 kHz mono Int16 PCM (little-endian).
- Text frame `{"type":"stop"}`: clean shutdown request.

Server → client (JSON):
- `{"type":"ready"}` — stream started, client may begin sending audio.
- `{"type":"partial","text":"..."}` — in-progress hypothesis; replaces
  prior partials. Redacted via `redact_credentials()` +
  `redact_exfiltration_urls()` before emit — same as finals — so a
  user-spoken credential never flashes unredacted in the browser.
- `{"type":"final","text":"..."}` — stable segment. Redacted via
  `redact_credentials()` + `redact_exfiltration_urls()` before emit.
- `{"type":"error","message":"..."}` — start failure or missing dep.

## Activation Rules

The endpoint returns **503** unless all are true:

1. `stt.enabled == true`
2. `stt.provider == "transcribe"`
3. `stt.streaming == true`

If the `amazon-transcribe` package is not importable, the WebSocket is
accepted but the server immediately sends
`{"type":"error","message":"amazon-transcribe not installed"}` and
closes the socket.

Whisper users fall back to batch silently. The frontend hook degrades to
batch when any of `AudioContext`, `AudioWorklet`, or `WebSocket` is
absent in the browser.

## Lifecycle & Cleanup

- `start()`: request mic → open WS → load worklet module → begin posting
  PCM ArrayBuffers as soon as worklet emits them.
- `stop()`: send `{"type":"stop"}`; server ends Transcribe stream.
- WS `onclose`: hook joins all `final` texts with spaces, calls
  `onFinal(combined)`, stops mic tracks, closes `AudioContext`.
- Component unmount: same cleanup; guarantees no leaked Transcribe
  session (billable).

### SEL audit pairing — emit before closing

Every accepted connection logs `stt_stream_start`, and **every** exit path must
log a matching `stt_stream_end` (`error`, `timeout`, or `ok`) or the audit trail
shows an unmatched start.

`stt_stream_end` is emitted **before** `await ws.close()`, never after — on the
four early-return paths (via `_close_and_end_audit`) and on the normal cleanup
path. `WebSocketResponse.close()` awaits the *peer's* close acknowledgement under
its own timeout (10s by default), so a client that has already gone away (abrupt
disconnect, closed tab) parks the handler inside `close()`; with the audit after
the close, the end event is withheld for up to that timeout. Emitting first makes
the pairing independent of the peer. The close still runs and is still awaited
immediately after, and still tolerates a broken transport (logged, not raised).

Tests asserting on the audit pair must **wait** for the end event
(`_wait_for_operation`): neither receiving the error frame nor exiting the
`TestClient` context orders the assertion after the server handler's remaining
steps, so asserting straight after either is a race.

## Frozen-prefix Behaviour

`ChatPage.tsx` snapshots the current input on the first `partial` event
into `frozenInputRef`. Subsequent partials replace only the suffix after
that snapshot, so anything the user typed before speaking is preserved.
On `final` (via `onText`), the ref is cleared so the next utterance
begins from the newly committed text.

## Security

- Origin check (`check_origin(require=True)`) rejects cross-origin WS.
- `max_msg_size` caps per-frame audio payloads (128 KiB).
- Both partials and finals pass through the shared `redact_*` helpers
  before hitting the browser — matching the batch endpoint's behaviour.
- Partials are ephemeral (replaced) and never persisted server-side, but
  redaction still runs because the live dashboard display counts as an
  external surface per the `security-controls` guideline.

## Non-goals (Phase 1)

- Streaming Whisper support (requires `faster-whisper` or equivalent).
- Voice-activity-detected auto-submit (Phase 2).
- Parallel fan-out to multiple agents (Phase 3, TalkStream-style).
