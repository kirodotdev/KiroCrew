# Voice Streaming

## Overview

Dashboard text-to-speech has two providers: local Piper and Amazon Polly.
`voice_reply.DEFAULT_PROVIDER` selects Piper unless configuration selects a valid
provider. `chat_voice.api_voice_synthesize()` sends Piper output as one WAV
chunk and streams Polly sentence chunks as MP3; the browser queues either form
for sequential playback.

## Components

| Component | Code | Responsibility |
|---|---|---|
| Dashboard routes | `dashboard.routes.sessions.register()` | Registers the synthesis, configuration, and Polly voice-catalogue endpoints. |
| Voice endpoints | `dashboard.chat_voice.api_voice_config()`, `api_voice_synthesize()`, and `api_voice_voices()` | Read and persist configuration, synthesize dashboard speech, and return the Polly catalogue. |
| Provider implementation | `voice_reply.synthesize_speech()`, `streaming_voice_reply()`, and `stitch_mp3s()` | Redacts text, selects a provider, creates audio, and joins completed Polly chunks. |
| Streaming playback | `website/src/hooks/useWebSocket.ts` | Detects completed sentences, serializes synthesis requests, queues audio, and handles interruption. |
| Settings | `website/src/pages/settings/VoicePanel.tsx` | Updates auto-speak, provider, Polly, and Piper settings; fetches the Polly catalogue only while Polly is selected. |
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
uses `stitch_mp3s()` to broadcast `voice_complete`. For Piper,
`_synthesize_nonstreaming()` broadcasts one WAV `voice_chunk` and one
`voice_complete`. `useWebSocket` decodes `voice_chunk` audio into blob URLs and
plays the queue one item at a time. `voice_complete` also updates the Redux
`voiceAudio` field; `UseWebSocketCoverage.test.tsx` covers that state update.

## Interruption

`useChatPageActionsController.ts` dispatches `voice-stop` when it sends a
message, and `ChatPage`'s Speak handler dispatches the same event while audio is
playing. `useWebSocket` maps
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
| `provider` | Validated by `voice_reply.synthesis_settings()` and `slack.handler.load_voice_reply_config()`; invalid values fall back to `voice_reply.DEFAULT_PROVIDER`. |
| `enabled` | Enables global Slack voice replies. |
| `auto_speak` | Enables dashboard auto-speak; `api_voice_config()` exposes it as `autoSpeak`. |
| `voice_id`, `engine`, `rate`, `pitch` | Polly synthesis settings, also usable as request overrides for the dashboard synthesis endpoint. |
| `aws_profile`, `region` | Passed to the AWS CLI by the Polly provider. |
| `piper_binary`, `piper_model`, `piper_model_config`, `piper_length_scale` | Piper executable, model, optional model configuration, and validated speed setting. `validate_length_scale()` rejects invalid or non-positive values. |

`dashboard.routes.sessions.register()` registers:

* `GET` and `PUT /api/voice/config`
* `POST /api/voice/synthesize`
* `GET /api/voice/voices`

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
