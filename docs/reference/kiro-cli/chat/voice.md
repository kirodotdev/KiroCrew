# Voice Input & Output

Kiro Crew supports hands-free interaction through voice input (speech-to-text)
and voice output (text-to-speech). Both work in the dashboard and Slack.

## Voice Input (Speech-to-Text)

### Dashboard Chat Box

The chat input bar has a microphone button. Here's how it works:

1. Click the mic button, and your browser asks for microphone permission.
2. Speak your message. Words appear in the message box as you talk.
3. Stop by clicking the mic button again, or (on the default local provider)
   just by pausing: a silence of `stt.silence_ms` ends the utterance for you.
4. Review and edit the text, then press Enter to send.

Recognition runs on your own machine by default, on a model Kiro Crew keeps
loaded, so no audio leaves your device and a finished phrase becomes text in
tens of milliseconds. Turning `stt.streaming` off transcribes once at the end
instead, and the button shows a spinner while it works. If recognition fails or
returns nothing, nothing is inserted.

**Browser requirements:** Chrome, Edge, Firefox, or Safari with microphone
access. Live transcription needs `getUserMedia`, `AudioWorklet` and `WebSocket`;
a browser missing any of them falls back to recording the whole utterance with
`MediaRecorder` and transcribing it on release (WebM/Opus preferred, MP4/OGG
fallback).

### Slack Voice Memos

When STT is enabled, voice memos sent in Slack threads are automatically
transcribed. Kiro Crew processes the audio and responds to the transcribed text
as if you had typed it.

### Setup

Speech-to-text is on by default, and the default provider runs on this machine.
Desktop releases already contain the recognizer and audio decoder; users only
download their selected model. For a source install, add the recognizer to the environment that runs the
gateway:

```bash
pip install 'pywhispercpp>=1.5,<2'
```

Source environments also need a system FFmpeg for WebM, M4A, and ogg/Opus; Kiro
Crew deliberately does not execute packaged binaries from an agent-writable
project venv. Desktop installers instead carry and verify their pinned decoder,
so desktop users never install Homebrew, Winget, Apt, or FFmpeg. Prebuilt
recognizer wheels cover Apple silicon
macOS, glibc and musl Linux on x86_64 and arm64, and Windows. An Intel Mac has
none, so `pip` builds from source there and needs a C++ toolchain plus CMake.
Settings reports that as its own state, not as a missing extra.

Then open **Settings > Voice**. The Speech-to-Text card reports whether the
recognizer loaded and names the reason when it did not, and picks the model:

| Model | One-time download | Use it when |
|-------|-------------------|-------------|
| `tiny` | 78 MB | The machine is short of memory |
| `base` | 148 MB | The default, and right for most dictation |
| `small` | 488 MB | Accents or jargon are being misheard |
| `large-v3-turbo` | 1.6 GB | You want the accuracy ceiling |

Choose the model and click **Download now**. The download is verified against a
pinned sha256 digest before it is used and is reused from disk after that.
Nothing else needs installing by hand: there is no separate transcription
program, provider-specific runtime, or system FFmpeg dependency. `kirocrew
doctor` reports the recognizer, model, and bundled decoder.

The other two providers, the full setting list and the retired providers are in
Kiro Crew's own [configuration reference](../../../../src/kiro_crew/docs/configuration.md).

### CPU threads (many-core hosts)

Kiro Crew derives the recognizer's thread count from the host: **half the
available cores**, capped at 16. The count comes from `sched_getaffinity` where
available, so a CPU-restricted container gets its real budget rather than the
whole machine's.

Why not use every core: Whisper decodes one output step at a time, and each step
is a small matmul that ends in a thread barrier. Wide thread pools therefore cost
latency per step instead of buying throughput, and on a host that is doing other
work (a Kiro Crew host runs the gateway and agent sessions alongside) the workers
get time-sliced, so each barrier waits on threads the scheduler has not run yet.

Measured on a 32-vCPU Graviton3 host with an 11-second clip, 16 threads beat 31
(`base` 4.9s vs 7.3s, `large-v3-turbo` 20.8s vs 26.9s), and restricted to 16
cores with `taskset`, 8 threads beat 16 (5s vs 7s). The headroom buys
predictability more than raw speed: 8 threads measured 4.9-5.0s across repeats,
while taking all 32 ranged 8.1-68.4s depending on how busy the machine was.

The cap is where both model shapes stop gaining. On the same clip in-process,
`base` runs 0.96s at 8 threads, 1.13s at 16 and 1.18s at 24, while the
encoder-heavy `large-v3-turbo` keeps improving (6.26s / 5.13s / 4.81s), so 16 is
the width that leaves both within a few percent of their own best rather than
extrapolating onto a 64-core host nobody measured.

## Voice Output (Text-to-Speech)

Kiro Crew can speak responses aloud, through local Piper by default or through
Amazon Polly. Two modes are available:

### Auto-Speak (Non-Interruptive Streaming)

When enabled, responses are spoken **as they stream in** — you don't wait for
the full response. The system detects sentence boundaries in real time and
synthesizes each sentence as soon as it's complete.

**How it works:**
1. The assistant starts streaming a response.
2. As each sentence completes (detected by `.` `!` `?` boundaries), it's sent
   to Amazon Polly for synthesis.
3. Audio chunks arrive via WebSocket and play sequentially.
4. When the response finishes, any remaining text is spoken.

**Non-interruptive behavior:** Sending a new message while voice is playing
immediately stops playback. The old response's remaining audio is discarded,
and voice output resumes from the new response's first sentence. This means
you can interrupt at any time by typing or speaking your next message.

**Enable it:**
1. Open **Settings > Voice**.
2. Toggle **Auto-speak Responses** on.
3. Configure your AWS profile if needed (Polly requires AWS credentials).

### Manual Replay

Hover over any assistant message (≥50 chars) and click the **Speak** button
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

Settings are in **Settings > Voice**, or directly in
`~/.kiro/crew/config.json`. The `voice_reply` section is a loose dictionary
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
| `enabled` | `false` | Turn on voice replies for **every** Kiro Crew response (text-triggered). Also seeds the `auto_reply_to_voice` default — see below. |
| `provider` | `"piper"` | TTS backend: `"piper"` (local, offline) or `"polly"` (AWS, cloud, and billed). An unrecognized value falls back to `piper` with a warning logged: reaching for a paid service is not a choice a typo may make for you. |
| `auto_reply_to_voice` | _follows `enabled`_ | **Voice-triggered**: when the user sends a voice memo, auto-respond with voice. Defaults to whatever `enabled` is — set explicitly to override. |
| **Polly-specific** | | ignored when `provider="piper"` |
| `voice_id` | `Ruth` | Any [Amazon Polly voice](https://docs.aws.amazon.com/polly/latest/dg/voicelist.html) |
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
model for Piper), Kiro Crew posts a one-shot **ephemeral** explaining why and
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

### Prerequisites — Amazon Polly (`provider: "polly"`)

- **AWS credentials** with `polly:SynthesizeSpeech` permission. Kiro Crew
  calls the AWS CLI (`aws polly synthesize-speech`) under the hood, so any
  credential method the CLI supports will work:

  1. Run `aws configure --profile polly` (or your credential provider) in your
     terminal to set up a named profile.
  2. In **Settings > Voice**, enter `polly` in the
     **AWS Profile** field (or set `"aws_profile": "polly"` in config.json).
  3. Leave the profile blank to use your default AWS CLI credentials
     (`~/.aws/credentials` default profile or environment variables).

### Prerequisites — Piper (`provider: "piper"`)

Piper is a local, offline neural TTS — no credentials, no network. Good when
you can't or don't want to use Amazon Polly.

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

3. **Set the config** in `~/.kiro/crew/config.json`:
   ```json
   "voice_reply": {
     "enabled": true,
     "provider": "piper",
     "piper_model": "/home/<you>/piper/en_US-lessac-medium.onnx"
   }
   ```

4. **ffmpeg is NOT required for Piper** (it outputs WAV directly that Slack
   plays natively). ffmpeg is still needed for voice-memo *input*, whichever
   speech-to-text provider is selected.

- **ffmpeg** for audio stitching (replay/Slack uploads). Not needed for
  streaming playback in the dashboard.
