# Voice Streaming — Design Document

## Overview

Real-time text-to-speech for dashboard chat responses. Two providers: **Piper**
(local, offline, the default — no AWS needed) and **Amazon Polly** (cloud). For
Polly, voice playback starts as soon as the first sentence finishes streaming —
no waiting for the full response. Piper produces a single local clip delivered
whole. Sending a new message interrupts playback immediately.

## Architecture

```
Polly:  chat_chunk (WS) → sentence detection (frontend) → POST /api/voice/synthesize
    → Polly TTS (per sentence) → voice_chunk (WS) → Audio playback (browser)
Piper:  POST /api/voice/synthesize → synthesize_speech() one WAV
    → single voice_chunk + voice_complete (WS) → Audio playback (browser)
```

### Components

| Component | File | Role |
|-----------|------|------|
| Sentence detector | `frontend/src/hooks/useWebSocket.ts` | Watches streaming chunks for sentence boundaries |
| Playback queue | `frontend/src/hooks/useWebSocket.ts` | Queues and plays audio chunks sequentially |
| Synthesize endpoint | `src/kiro_crew/dashboard/chat_voice.py` | `POST /api/voice/synthesize` — Polly: splits text into sentences + broadcasts chunks; Piper: `_synthesize_nonstreaming()` emits one clip (re-exported via `chat.py`) |
| Voice config endpoint | `src/kiro_crew/dashboard/chat_voice.py` | `GET/PUT /api/voice/config` — read/update voice settings incl. `provider` + `piper_*` (re-exported via `chat.py`) |
| TTS synthesis | `src/kiro_crew/voice_reply.py` | `synthesize_speech()` provider dispatcher (Polly `aws polly` / local `piper`), `streaming_voice_reply()` (Polly-only), `validate_length_scale()`, `stitch_mp3s()` |
| Settings UI | `website/src/pages/settings/VoicePanel.tsx` | Provider selector + auto-speak toggle; Polly fields (voice/engine/speed/profile/region) or Piper fields (model/binary/speed) |

## Streaming Auto-Speak Flow

1. User sends a message; `spokenLenRef` resets to 0
2. Backend streams `chat_chunk` events via WebSocket
3. Frontend accumulates text in a `streaming` message in Redux
4. On each chunk, regex scans for sentence boundaries (`[.!?]` followed by whitespace)
5. New complete sentences (≥10 chars) are sent to `POST /api/voice/synthesize`
6. Backend calls Polly per sentence, broadcasts `voice_chunk` (base64 MP3) via WS
7. Frontend decodes chunks into blob URLs, queues them, plays sequentially
8. On `chat_done`, any remaining unspoken tail text is synthesized
9. `voice_complete` event carries the stitched full MP3 for replay

## Interrupt Mechanism

Sending a new message while voice is playing triggers an interrupt:

1. `ChatPage.tsx` dispatches a `voice-stop` DOM event on send
2. `useWebSocket` listens for `voice-stop` and calls `stopVoice()`
3. `stopVoice()` pauses the active `Audio` element, clears the queue, and sets `voiceMutedRef = true`
4. Incoming `voice_chunk` events from the old response are dropped while muted
5. `chat_done` for the old response skips remaining-text synthesis when muted
6. When the new response's first sentence is detected (`spokenLenRef === 0`), `voiceMutedRef` resets to `false`

The DOM event pattern avoids prop drilling between `ChatPage` (where send lives) and `useWebSocket` (where audio state lives, called from `App.tsx`).

## Voice Configuration

Stored in `~/.kiro/crew/config.json` under `voice_reply`:

| Setting | Default | Range |
|---------|---------|-------|
| `provider` | piper | `piper` (local) or `polly` (AWS). Invalid values fall back to `polly` on load |
| `voice_id` | Ruth | Any Polly voice ID (Polly only) |
| `engine` | generative | generative, neural, long-form, standard (Polly only) |
| `rate` | 100% | 50%–200% (Polly only) |
| `pitch` | +0% | -20% to +20% (Polly only) |
| `enabled` | true | Controls auto-speak and Slack voice replies |
| `aws_profile` | _(empty)_ | AWS CLI profile name for Polly calls. Empty = use default credentials |
| `region` | _(empty)_ | AWS region for Polly. Empty = use CLI default |
| `piper_model` | _(empty)_ | Path to a Piper `.onnx` voice model. Required for Piper |
| `piper_binary` | _(empty)_ | Path to the `piper` executable. Empty = auto-detect on PATH / `~/piper-venv/bin/piper` |
| `piper_model_config` | _(empty)_ | Optional `.onnx.json` config (auto-detected next to the model if empty) |
| `piper_length_scale` | 1.0 | Piper speed (lower = faster); coerced finite/positive by `validate_length_scale()` |

### AWS Authentication

Voice synthesis calls `aws polly synthesize-speech` via the AWS CLI. Credentials
are resolved in standard AWS CLI order (env vars, default profile, instance role,
etc.). To use a specific profile, set `aws_profile` in the config:

```json
{
  "voice_reply": {
    "enabled": true,
    "aws_profile": "my-profile",
    "region": "us-east-1"
  }
}
```

### Sandbox requirement

Both synthesis paths spawn a subprocess (`aws polly synthesize-speech` /
`piper`), and both route it through `sandbox.wrap_argv(mode="standard")` because
the argv carries LLM-derived text. `wrap_argv` **fail-closes** where the host
offers no OS sandbox backend — every Windows host, and Linux without user
namespaces — so on those hosts synthesis returns `None` until the operator sets
`agent.sandbox_allow_unsandboxed_exec` in `config.json`.

That refusal arrives as the typed `SandboxUnavailableError` and is caught ahead
of the generic handler in both `_synthesize_polly` and `_synthesize_piper`. The
generic handler logs "Polly synthesis error" / "piper synthesis error" with a
stack trace, which misreads as an AWS-credential or piper-model fault and sends
the operator to the wrong place.

Both handlers log `exc.kind` plus **`str(exc)`** — the remedy prose the sandbox
layer selected — and never compose their own remedy text. Only `kind ==
"no_backend"` names the `agent.sandbox_allow_unsandboxed_exec` opt-in;
`"transient"` means momentary resource pressure where the caller must *not*
advise disabling the sandbox, and `"foreign_sandbox"` means this host's sandbox
works and the fix is a kiro-cli setting. A hardcoded remedy would give the wrong
instruction for two of the three kinds.

### API

- `GET /api/voice/config` — returns current settings + `autoSpeak` flag
- `PUT /api/voice/config` — update settings (partial patch), persists to config.json
- `POST /api/voice/synthesize` — `{ slot, text, voice?, engine?, rate?, pitch? }`
- `GET /api/voice/voices` — list available Polly voices via `aws polly
  describe-voices` (respects `aws_profile`/`region`), cached in-process for 1
  hour. Each entry: `{ id, name, language, languageCode, gender, engines }`,
  sorted by `languageCode` then `name`. If the `aws` CLI is not resolvable on
  the gateway's PATH (it is optional — the default Piper provider doesn't
  need it), the endpoint returns `{ "voices": [] }` with a 200 instead of
  erroring; the empty result is not cached, so the list recovers once `aws`
  becomes available

## Content Filtering for Speech

`strip_markdown()` in `voice_reply.py` transforms response text into natural
speakable content. Non-speakable elements are replaced with brief spoken
placeholders so listeners know content exists without hearing raw syntax.

| Content Type | Handling |
|-------------|----------|
| Fenced code blocks | Replaced with "(code block)" |
| Diff blocks | Replaced with "(diff block)" |
| `<mcwidget>` blocks | Replaced with "(widget)" |
| Residual HTML/XML tags | Stripped (after Slack-link handling) |
| Markdown tables | Replaced with "(table with N rows)" |
| Long inline code (>30 chars or paths) | Replaced with "(file path)" |
| Short inline code (≤30 chars, no `/`) | Kept as spoken text |
| Bare URLs | Replaced with "(link)" |
| Slack URLs `<url\|label>` | Kept label only |
| Markdown links `[label](url)` | Kept label only |
| `[OPTIONS: ...]` lines | Stripped silently |
| Unicode emoji | Stripped silently |
| Slack shortcodes (`:emoji:`) | Stripped silently |
| Bold/italic/strikethrough markers | Stripped (text kept) |
| Diff hunk headers (`@@`) | Stripped |

## Segment Handling (Tool Call Boundaries)

When the assistant calls a tool mid-response, the backend emits a `chat_segment`
event that finalizes the current streaming message and starts a new one.

The frontend resets `spokenLenRef` to 0 on `chat_segment` so the new segment's
text is spoken from the beginning. Without this reset, the offset from segment 1
would cause segment 2 to be skipped until its length exceeded the old offset.

## Synthesize Serialization

Synthesize calls are chained via a promise (`synthChainRef`) to prevent
out-of-order audio. Each `voiceSynthesize` call waits for the previous one to
complete before firing. This guarantees `voice_chunk` events arrive in sentence
order regardless of per-sentence Polly synthesis time. The chain resets on new
user messages.

## WebSocket Events

| Event | Direction | Payload |
|-------|-----------|---------|
| `voice_chunk` | server → client | `{ slot, index, sentence, audio }` (base64 MP3) |
| `voice_complete` | server → client | `{ slot, audio, chunks }` (stitched MP3) |

## Slack Voice Reply

Separate from dashboard streaming. Uses `!voice` commands in Slack threads:

- `!voice on/off` — toggle voice replies per thread
- `!voice <name>` — switch Polly voice (e.g., `!voice Matthew`)
- `!voice engine <type>` — change engine
- `!voice speed <percent>` — adjust speed
- `!voice pitch <percent>` — adjust pitch

Handler integration in `src/kiro_crew/slack/handler.py` with fire-and-forget
async pipeline: markdown strip → SSML → Polly → Slack file upload.

## Frontend State

| Ref | Type | Purpose |
|-----|------|---------|
| `voiceQueueRef` | `string[]` | Blob URLs queued for playback |
| `voicePlayingRef` | `boolean` | True while an Audio element is playing |
| `activeAudioRef` | `HTMLAudioElement` | Currently playing audio (for pause on interrupt) |
| `autoSpeakRef` | `boolean` | Cached auto-speak preference from server |
| `spokenLenRef` | `number` | Character offset of text already sent to TTS |
| `voiceMutedRef` | `boolean` | Suppresses incoming voice chunks after interrupt |
| `synthChainRef` | `Promise` | Serializes synthesize calls to prevent out-of-order audio |

Redux state in `chatSlice`:
- `voicePlaying: boolean` — drives UI indicators
- `voiceAudio: string | null` — base64 stitched MP3 for replay via 🔊 button

## Manual Replay

The 🔊 Speak button appears on hover over assistant messages ≥50 chars.
Clicking it calls `api.voiceSynthesize(slot, content)` which streams the
full message through the same pipeline. This works independently of auto-speak.
