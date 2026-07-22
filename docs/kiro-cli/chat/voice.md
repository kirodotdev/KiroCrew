# Voice Input & Output

KiroCrew supports hands-free interaction through voice input (speech-to-text)
and voice output (text-to-speech). Both work in the dashboard and Slack.

## Voice Input (Speech-to-Text)

### Dashboard Chat Box

The chat input bar has a 🎙️ microphone button. Here's how it works:

1. Click the mic button — your browser requests microphone permission.
2. Speak your message. The button pulses red while recording.
3. Click the mic button again to stop recording.
4. The audio is sent to the backend and transcribed locally using
   [OpenAI Whisper](https://github.com/openai/whisper).
5. The transcribed text appears in the input field. Review and edit it, then
   press Enter to send.

The button shows a spinner while transcribing. If transcription fails or
returns empty text, nothing is inserted.

**Browser requirements:** Chrome, Edge, or Firefox with microphone access.
The browser must support `getUserMedia` and `MediaRecorder`. Audio format is
auto-detected (WebM/Opus preferred, MP4/OGG fallback).

### Slack Voice Memos

When STT is enabled, voice memos sent in Slack threads are automatically
transcribed. KiroCrew processes the audio and responds to the transcribed text
as if you had typed it.

### Setup (Required for Both)

Whisper must be installed for voice input to work in both the dashboard and
Slack. Transcription runs entirely on your machine — no audio leaves your
device.

1. Open the **Overview** page → **Slack** tab (the 🎙️ Speech-to-Text card is
   here).
2. Toggle **Speech-to-Text** on.
3. Install system dependencies and build ffmpeg:

   **AL2023:**
   ```bash
   sudo dnf install -y python3.11 python3.11-pip python3.11-devel gcc gcc-c++
   sudo dnf install -y gcc make nasm diffutils
   bash /path/to/KiroCrew/scripts/build-ffmpeg.sh
   ```

   **macOS:**
   ```bash
   brew install python@3.11 ffmpeg
   ```

   **AL2/AL2023 (recommended):** Install via brew (avoids glibc/dependency issues):
   ```bash
   brew install openai-whisper ffmpeg
   ```
   The `dnf` instructions above are for building from source when brew is unavailable.

4. Choose a model size:

   | Model | Size | Speed | Accuracy |
   |-------|------|-------|----------|
   | tiny | 75 MB | Fastest | Lower |
   | base | 142 MB | Fast | Good (default) |
   | small | 466 MB | Medium | Better |
   | medium | 1.5 GB | Slow | High |

5. Click **📦 Install Whisper** in the Speech-to-Text card — this installs
   the `openai-whisper` Python package and downloads the selected model.

### MLX provider (Apple Silicon GPU)

On Apple Silicon (M-series) Macs, the `mlx` provider runs Whisper on the Metal
GPU via Apple's [MLX](https://github.com/ml-explore/mlx) framework — typically
~5× faster than the CPU-based `whisper` provider. The `mlx` provider is
selectable on every platform but only *available* on arm64 macOS; elsewhere the
status badge stays "not installed".

1. In the Speech-to-Text card, set **Provider** to `mlx`.
2. Click **📦 Install** — this runs `pipx install mlx-whisper` plus `ffmpeg`
   (the provider-aware install button installs the right runtime for whichever
   provider is selected).
3. The MLX model (`mlx_model`, default `mlx-community/whisper-large-v3-turbo`)
   downloads from Hugging Face on first transcription and is cached under
   `~/.cache/huggingface/hub/`.

`mlx-whisper` is installed out-of-band via `pipx` rather than as a package
dependency because the `mlx` wheel is arm64-only; KiroCrew invokes the
`mlx_whisper` CLI as a subprocess, exactly like the `whisper` provider.

## Voice Output (Text-to-Speech)

KiroCrew can speak responses aloud using AWS Polly. Two modes are available:

### Auto-Speak (Non-Interruptive Streaming)

When enabled, responses are spoken **as they stream in** — you don't wait for
the full response. The system detects sentence boundaries in real time and
synthesizes each sentence as soon as it's complete.

**How it works:**
1. The assistant starts streaming a response.
2. As each sentence completes (detected by `.` `!` `?` boundaries), it's sent
   to AWS Polly for synthesis.
3. Audio chunks arrive via WebSocket and play sequentially.
4. When the response finishes, any remaining text is spoken.

**Non-interruptive behavior:** Sending a new message while voice is playing
immediately stops playback. The old response's remaining audio is discarded,
and voice output resumes from the new response's first sentence. This means
you can interrupt at any time by typing or speaking your next message.

**Enable it:**
1. Open **Settings → Chat → Voice (TTS)**.
2. Toggle **Auto-speak Responses** on.
3. Configure your AWS profile if needed (Polly requires AWS credentials).

### Manual Replay

Hover over any assistant message (≥50 chars) and click the 🔊 **Speak** button
to hear it read aloud. This works independently of auto-speak.

### Slack Voice Replies

Use the `/kirocrew voice` slash command to open a settings modal where you can
configure voice, engine, speed, and pitch.

The legacy `!voice` inline commands still work but are deprecated:

| Command | Effect |
|---------|--------|
| `!voice on` | Enable voice replies in this thread |
| `!voice off` | Disable voice replies |
| `!voice Ruth` | Switch to a specific Polly voice |
| `!voice engine generative` | Change engine type |
| `!voice speed 120%` | Adjust speech rate |
| `!voice pitch +10%` | Adjust pitch (neural/standard engines only) |

Voice replies are uploaded to the Slack thread alongside the text response.
File format depends on the provider (MP3 for Polly, WAV for Piper).

### Configuration

Settings are in **Settings → Chat → Voice (TTS)**, or directly in
`~/.kirocrew/config.json`. The `voice_reply` section is a loose dictionary
(not part of the typed config schema), so you edit it by hand:

```json
{
  "voice_reply": {
    "enabled": true,
    "provider": "polly",
    "auto_reply_to_voice": true,

    "voice_id": "Ruth",
    "engine": "generative",
    "rate": "100%",
    "pitch": "+0%",
    "aws_profile": "",
    "region": "",

    "piper_binary": "",
    "piper_model": "",
    "piper_model_config": "",
    "piper_length_scale": 1.0
  }
}
```

| Setting | Default | Purpose |
|---------|---------|---------|
| `enabled` | `false` | Turn on voice replies for **every** Kirocrew response (text-triggered). Also seeds the `auto_reply_to_voice` default — see below. |
| `provider` | `"polly"` | TTS backend: `"polly"` (AWS, cloud) or `"piper"` (local, offline). Invalid values fall back to `polly` with a warning logged. |
| `auto_reply_to_voice` | _follows `enabled`_ | **Voice-triggered**: when the user sends a voice memo, auto-respond with voice. Defaults to whatever `enabled` is — set explicitly to override. |
| **Polly-specific** | | ignored when `provider="piper"` |
| `voice_id` | `Ruth` | Any [AWS Polly voice](https://docs.aws.amazon.com/polly/latest/dg/voicelist.html) |
| `engine` | `generative` | `generative`, `neural`, `long-form`, `standard` |
| `rate` | `100%` | 50%–200% |
| `pitch` | `+0%` | -20% to +20% (neural/standard only) |
| `aws_profile` | _(empty)_ | AWS CLI profile; empty = default credentials |
| `region` | _(empty)_ | AWS region for Polly; empty = CLI default |
| **Piper-specific** | | ignored when `provider="polly"` |
| `piper_binary` | _(auto-detect)_ | Path to `piper` CLI. Auto-detects `piper` on `PATH` and `~/piper-venv/bin/piper` |
| `piper_model` | _(required)_ | Absolute path to a piper voice `.onnx` model |
| `piper_model_config` | _(optional)_ | Path to `.onnx.json` config; piper auto-detects one next to the `.onnx` |
| `piper_length_scale` | `1.0` | Speech speed. `<1` faster, `>1` slower |

### Voice-in → voice-out (symmetric voice)

`auto_reply_to_voice` controls whether sending a Slack voice memo
automatically triggers a voice reply. By default it follows `enabled`:

| `enabled` | `auto_reply_to_voice` (unset) | Behavior |
|-----------|-------------------------------|----------|
| `false` | defaults to `false` | No voice anywhere — explicit opt-out is preserved. |
| `true`  | defaults to `true`  | Every reply is voice (incl. voice-memo replies). |

You can also set `auto_reply_to_voice: true` explicitly while leaving
`enabled: false` if you want voice **only** as a response to voice memos —
i.e. text replies stay text, voice memos get a spoken reply.

If TTS is **not configured** (missing `aws` CLI for Polly, missing binary or
model for Piper), KiroCrew posts a one-shot **ephemeral** explaining why and
replies with text only. The ephemeral fires for every opt-in path —
globally enabled, per-thread `!voice on`, or voice-memo auto-reply — so
silent fallback never surprises the user.

**Caveat — Polly credentials fail silently.** The availability check for
Polly only verifies that the `aws` CLI is on `PATH`, not that credentials are
valid. If your AWS credentials are expired or missing, the `aws polly`
invocation fails inside synthesis, is logged, and the reply falls back to
text — **no ephemeral is posted** in this case. If voice replies stop working
after your AWS credentials expire, refresh them (e.g. `aws configure` or your
credential provider) and try again.

### Content Handling

Responses are cleaned for natural speech before synthesis:

- Code blocks → "(code block)"
- Diff blocks → "(diff block)"
- Tables → "(table with N rows)"
- File paths → "(file path)"
- URLs → "(link)" or just the link label
- Emoji, markdown formatting → stripped
- Credentials → redacted

### Prerequisites — AWS Polly (`provider: "polly"`)

- **AWS credentials** with `polly:SynthesizeSpeech` permission. KiroCrew
  calls the AWS CLI (`aws polly synthesize-speech`) under the hood, so any
  credential method the CLI supports will work:

  1. Run `aws configure --profile polly` (or your credential provider) in your
     terminal to set up a named profile.
  2. In **Settings → Chat → Voice (TTS)**, enter `polly` in the
     **AWS Profile** field (or set `"aws_profile": "polly"` in config.json).
  3. Leave the profile blank to use your default AWS CLI credentials
     (`~/.aws/credentials` default profile or environment variables).

### Prerequisites — Piper (`provider: "piper"`)

Piper is a local, offline neural TTS — no credentials, no network. Good when
you can't or don't want to use AWS Polly.

1. **Install piper-tts** into a Python 3.11 venv (PyPI wheels don't yet
   support Python 3.12):
   ```bash
   # Using mise, pyenv, or system python3.11:
   python3.11 -m venv ~/piper-venv
   ~/piper-venv/bin/pip install 'numpy<2' piper-tts
   ```
   The `~/piper-venv/bin/piper` path is auto-detected.

2. **Download a voice model** from the
   [Piper voices on HuggingFace](https://huggingface.co/rhasspy/piper-voices/tree/main):
   ```bash
   mkdir -p ~/piper
   BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
   curl -fsSL "$BASE/en_US-lessac-medium.onnx" -o ~/piper/en_US-lessac-medium.onnx
   curl -fsSL "$BASE/en_US-lessac-medium.onnx.json" -o ~/piper/en_US-lessac-medium.onnx.json
   ```

3. **Set the config** in `~/.kirocrew/config.json`:
   ```json
   "voice_reply": {
     "enabled": true,
     "provider": "piper",
     "piper_model": "/home/<you>/piper/en_US-lessac-medium.onnx"
   }
   ```

4. **ffmpeg is NOT required for Piper** (it outputs WAV directly that Slack
   plays natively). ffmpeg is still needed for voice-memo input
   transcription via openai-whisper.

- **ffmpeg** for audio stitching (replay/Slack uploads). Not needed for
  streaming playback in the dashboard.
