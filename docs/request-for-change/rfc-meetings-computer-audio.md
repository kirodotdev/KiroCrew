---
title: Meetings — capture computer audio alongside the microphone
status: draft
author: gbrunoo
created: 2026-11-09
last-audited: 2026-11-09
audited-at: e943a2ee
doc-pr: 33933
implementation-prs: []
tracking-issues: [2814]
supersedes: []
superseded-by: []
---

# RFC: Meetings — capture computer audio alongside the microphone

The Meetings app transcribes the microphone only, so in a headphone call the other
participants never reach the transcript, the notes, or the action items. Issue
[#2814](https://github.com/kirodotdev/KiroCrew/issues/2814) asks for an opt-in
"microphone + computer audio" source and leaves three questions to the
maintainers. This document proposes answers to those three questions, backed by
measurements made with a working implementation of the same capture outside the
Meetings app, and lays out a phased plan whose first phase changes the browser
audio graph only.

The three questions, and the answers this document argues for:

| # | Question from #2814 | Proposed answer |
|---|---|---|
| Q1 | Which platforms first? | Any Chromium browser that returns a system-audio track (Chrome/Edge 141+ on macOS 14.2+ and Windows, verified), plus the Electron shell on Windows (loopback). The Electron shell on macOS is **out of scope**: Chromium under Electron has no system-audio capture there. The app tells the user to open the dashboard in a browser instead. |
| Q2 | Hide, disable, or show-then-report on unsupported platforms? | Show the option wherever `getDisplayMedia` exists, and report capability **after** the picker runs, because the only reliable signal is whether the returned stream has an audio track. Hide it only where `getDisplayMedia` is absent. Where the platform is known not to deliver audio (Electron on macOS) show it disabled with the browser alternative inline. |
| Q3 | Per-start, non-persisted consent? | Yes. The source choice is made at every start, is never written to config or `localStorage`, and the recording state shows the active sources for the whole session. |

## Summary

Add a second, opt-in audio source to the Meetings transcription hook: the
computer's output, obtained with `getDisplayMedia({ audio: true, systemAudio:
"include" })`. Phase 1 mixes it with the microphone in the browser, ahead of the
existing worklet, so the `/api/ws/stt` protocol and the server are untouched.
The display video track that the API forces on us is stopped as soon as the
audio track is confirmed and is never read. No audio is written to disk. A
failed or cancelled picker, a stream without audio, and a share that ends
mid-meeting all move the meeting to `paused` with an actionable message; the
app never falls back to microphone-only after the user chose both sources.

Phase 2 (optional, independently abandonable) keeps the two sources apart so
every transcript line carries a `You` / `Others` label, which the current
single-socket protocol cannot express.

## Motivation

### Current state (main @ `e943a2ee`)

- The hook acquires the microphone and nothing else:
  `acquireMicStream()` in `website/src/apps/meetings/hooks/useMeetingTranscription.ts`,
  then feeds one `MediaStreamSource` into the `pcm-worklet` `AudioWorkletNode`.
- The worklet reads the first channel of its first input only:
  `process()` in `website/public/pcm-worklet.js` (`const channel = inputs[0]?.[0]`). Any mix
  therefore has to happen *before* the worklet, in the Web Audio graph.
- The Electron shell answers every `getDisplayMedia` with video only:
  `createDisplayMediaHandler` in `website/electron/display-media.js`
  (`callback({ video: source })`), after the `isTrustedRequest` identity check. A renderer asking for audio
  gets a stream with zero audio tracks, with no error.
- The Meetings manifest already lists macOS, Linux and Windows
  (`platform.os` in `src/kiro_crew/apps/builtins/meetings/app.json`).
- Two open PRs picked answers without a ruling: #5738 persists the mixed audio
  as `audio.wav` and degrades to microphone-only on a capture failure; #7196
  (stacked on #5738) gates loopback to Windows via `LOOPBACK_AUDIO_PLATFORMS`.
  Both conflict with `main` and with the premises of #2814 (no raw audio
  retained; no silent fallback). See the thread's 2026-09-02 and 2026-10-21 notes.

### What was measured

A standalone Kiro Crew app built by the author on 2026-11-09 captures the
microphone and the computer audio as two separate tracks from the dashboard
page, so the platform behaviour below is observed, not read from documentation.
Measured on macOS 26.7, Google Chrome 156, Kiro Crew desktop shell of the same
day:

| Surface | `getDisplayMedia` result | System audio |
|---|---|---|
| Chrome 156, "Entire Screen" + "Share system audio" ticked | video + audio track | **yes** (transcribed) |
| Chrome 156, "Entire Screen", toggle left off | video only, no error | no |
| Chrome 156, Chrome Tab | video + tab audio | tab only |
| Kiro Crew desktop shell (Electron) | video only (`callback({ video: source })` in `display-media.js`) | no |
| Chrome, before granting *Screen & System Audio Recording* in macOS Privacy settings | `NotAllowedError` | no, until Chrome is restarted after the grant |

Two consequences for the design:

1. The browser gives no capability signal before the picker: the same call
   returns audio or not depending on a toggle the user ticks inside the picker.
   Capability has to be reported after the fact (answers Q2).
2. Under Electron on macOS there is no path to system audio at all. Electron
   tracks this as [electron/electron#47490](https://github.com/electron/electron/issues/47490)
   (open; ScreenCaptureKit loopback not exposed). The native macOS picker
   (`useSystemPicker`) selects the *surface*; whether it yields an audio track is
   unverified and is Phase 0's question. Windows loopback is documented
   (`callback({ video, audio: "loopback" })`) and is in scope.

Chrome added the system-audio toggle on macOS in version 141 on macOS 14.2+
(where Apple added third-party system-audio capture); earlier Chrome on macOS
has no toggle, which is why the table above fixes the floor at 141.

## Goals

- Transcribe both local and remote participants of a call from a browser tab,
  with no virtual audio device and no extra daemon.
- Keep the STT protocol, the server, and the providers unchanged in Phase 1.
- Explicit, per-start consent; a persistent indicator of the active sources;
  explicit failure states that never silently degrade.
- No video processed, stored or transmitted; no raw audio retained.
- Localized and accessible controls, consistent with the Meetings UI.

## Non-goals

- System audio under the Electron shell on macOS (platform gap, see above).
- Speaker diarization. Phase 2 gives per-source labels, not per-person ones.
- Recording audio to disk (#5738's `audio.wav`). Explicitly excluded by #2814.
- Changing `pcm-worklet.js` to take two inputs. Mixing in the graph is simpler
  and keeps the worklet shared with dictation.

## Design

### Source selection (Q3)

The transcript panel's start control gains a source choice, rendered before
capture starts and re-rendered at every start:

- **Microphone** (default, current behaviour).
- **Microphone + computer audio**.

The choice lives in component state of the start flow only. It is not a
config key, not a `localStorage` key, and not remembered across meetings or
restarts. Choosing the second option is the consent; there is no further
confirmation dialog, because the OS/browser picker that follows is itself a
consent step the user cannot skip.

### Capture graph (Phase 1)

```
getUserMedia(mic) ──► MediaStreamSource ─┐
                                         ├─► GainNode (sum) ─► pcm-worklet ─► /api/ws/stt (unchanged)
getDisplayMedia(system audio) ─► MediaStreamSource ─┘
```

- `getDisplayMedia` is called with `video: { frameRate: 1, width: 320, height: 180 }`
  (video is mandatory per the specification), `audio: { echoCancellation: false,
  noiseSuppression: false, autoGainControl: false }`, `systemAudio: "include"`,
  `selfBrowserSurface: "exclude"`, `surfaceSwitching: "exclude"`,
  `monitorTypeSurfaces: "include"`. The video track is stopped immediately after
  the audio track is confirmed present and is never attached to any node, canvas,
  or element.
- Microphone constraints keep `echoCancellation: true`: the mix would otherwise
  contain the remote voices twice when the user listens on speakers (once from
  the system track, once leaking into the microphone).
- Both sources connect to one `GainNode`; Web Audio sums them. A second
  `GainNode` per source allows a fixed -3 dB on each to avoid clipping on
  overlap. The worklet is unchanged.
- Nothing new is written to disk, the transcript JSONL stays the only artifact.

### Indicator and failure states

While capture is active, the compact caption and the transcript panel header
show the active sources, as text with an icon, not colour alone (for example
"Transcribing: microphone + computer audio"). The indicator stays for the
whole meeting.

| Event | Behaviour |
|---|---|
| Picker cancelled (`NotAllowedError` with a user-dismissal message) | meeting stays `idle` (if starting) or moves to `paused`; message "Computer audio was not shared"; actions: *Share again*, *Continue with microphone only* (explicit, logged as a new choice) |
| OS permission refused (`NotAllowedError`, "permission denied by system") | same, with the platform hint (macOS: *System Settings › Privacy & Security › Screen & System Audio Recording*, then restart the browser) |
| Stream returned without an audio track | video track stopped; same as cancelled, with "the share had no audio: choose Entire Screen and tick Share system audio" |
| Audio track `ended` mid-meeting (user clicked *Stop sharing*) | meeting moves to `paused`; message names the lost source; *Share again* resumes |
| `getDisplayMedia` absent | second option hidden |
| Known-unsupported shell (Electron on macOS) | second option shown disabled, with "open the dashboard in Chrome or Edge" and the URL |

In no case does the app continue microphone-only after the user chose both
sources without the user choosing it again.

### Electron (Q1, Windows only)

`website/electron/display-media.js` returns `callback({ video: source, audio:
"loopback" })` on Windows when the request asks for audio and passes
`isTrustedRequest`. On other platforms the handler is unchanged (video only).
The renderer detects the shell (`navigator.userAgent` contains `Electron`) and
the OS from the user agent string, and applies the Q2 rule above.

### Phase 2: source provenance (optional)

Keep the two `MediaStreamSource`s on separate worklets and separate STT
sessions, label finals `You` (microphone) and `Others` (computer), and merge by
timestamp in the transcript. This is what the author's standalone app does and
it produces usable attribution without any diarization model. Costs: a second
provider session per meeting (billed for Transcribe; it counts against the
concurrent-session cap in `dashboard/stt_stream.py`), and a `source` field on the
`dispatch` payload. Independently abandonable; Phase 1 does not depend on it.

## Phases

### Phase 0: probe the Electron native picker on macOS

Exit criteria: a written verdict (PR description plus a line in this RFC)
stating whether `setDisplayMediaRequestHandler` with `useSystemPicker: true` on
macOS 15+ ever yields an audio track to the renderer. Expected answer, from
electron#47490: no. Blocked on nothing; one afternoon.

### Phase 1: browser capture, mixed before the worklet

Scope: `useMeetingTranscription.ts` (second source, mix, failure handling),
`TranscriptPanel` start control and indicator, i18n keys, vitest coverage of the
state machine (cancelled picker, no-audio stream, track ended). No server change.

Exit criteria:
- In Chrome or Edge 141+ on macOS 14.2+ or Windows, a meeting started with both
  sources transcribes speech played by another application.
- Choosing both sources and cancelling the picker leaves the meeting not
  transcribing, with the message visible; no microphone audio reaches the socket.
- Stopping the share mid-meeting moves the meeting to `paused` within 2 s.
- The choice is not present in config or `localStorage` after a restart.
- No file other than the existing per-meeting files is created.

### Phase 2: Electron Windows loopback

Scope: `display-media.js` audio grant on Windows behind `isTrustedRequest`;
unit test in `website/electron/test/display-media.test.js`. Exit criteria: the
desktop shell on Windows transcribes computer audio; macOS and Linux shells are
byte-for-byte unchanged in behaviour (video only).

### Phase 3: source provenance (optional)

As described above. Blocked on a maintainer decision about the second provider
session.

## Migration and compatibility

No stored data changes. Meetings started with the microphone only behave as
today. The STT WebSocket protocol is unchanged in Phases 1 and 2.

## Security considerations

- Capturing other people's voices is the sensitive part. The consent is
  explicit, per start, and visible for the whole meeting; the browser or OS
  picker is a second consent the app cannot bypass.
- The display video track is a capability we do not want. It is stopped before
  any node sees it and never leaves the renderer. A test asserts that no
  `MediaStreamTrack` of kind `video` is connected to the audio graph.
- No raw audio is retained; redaction of transcripts at the `dispatch`
  boundary is unchanged.
- The Electron change widens the display-media grant to include loopback audio
  on Windows only, still behind `isTrustedRequest`.

## Alternatives considered

- **Virtual audio device (BlackHole, VB-Cable).** Works everywhere but requires
  a per-machine driver install and routing; rejected by #2814 as well.
- **Native helper spawned by the backend** (Core Audio process tap on macOS
  14.2+, WASAPI loopback on Windows). The only route that would make the
  Electron shell work on macOS. Larger surface (a signed native binary per
  platform, TCC prompt attributed to the app), so not the first step; it is the
  logical follow-up if the macOS shell matters more than the browser path.
- **Mixing inside the worklet with a second input** (#5738). Changes a worklet
  shared with dictation and needs #9246's resampler to handle two channels;
  summing in the graph is smaller.
- **Separate STT sessions from the start** (Phase 2 here). Better provenance,
  more cost; #2814 prefers the mixed stream first, and this document agrees.

## Open questions

1. Phase 0: does the Electron native macOS picker yield audio? (Expected: no.)
2. Phase 3: is a second provider session per meeting acceptable for Transcribe
   users, or should provenance be limited to local providers?
3. Should "Continue with microphone only" after a failed share be offered at
   all, or should the only way out be to start the meeting again? #2814 reads
   as the latter; the design above offers the explicit button because a second
   full start loses the typed broadcasts already made.

## Provenance

Measurements and the two-track reference implementation: gbrunoo, 2026-11-09,
macOS 26.7, Google Chrome 156.0.8078.81, Kiro Crew desktop shell of the same
date. Upstream code citations are against `e943a2ee` (main, 2026-11-09).
Thread context: #2814 comments of 2026-09-02, 2026-09-25, 2026-10-21 and
2026-11-05 (operator bolichen97 asked for a design through this process).
