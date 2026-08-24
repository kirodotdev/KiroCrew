# Design — local meeting transcription (post-meeting batch)

Branch: `feat/local-streaming-stt` (to be renamed conceptually; the branch name
predates this decision — see "Naming" below).

## Problem

Meetings need on-device transcription using the already-installed openai-whisper,
without AWS Transcribe or Apple SpeechAnalyzer. The original attempt made Whisper a
*streaming* provider behind `/api/ws/stt` (pseudo-streaming: window-chunk + decode +
incremental word-commit). That word-commit step failed adversarial correctness review
three times (suffix-dedup, sliding-window, LocalAgreement-2) on the same
anchor-by-first-word primitive — dropping repeated words and duplicating revised ones.

The user does not need mid-meeting (live) transcription. Post-meeting processing is
the accepted standard. That requirement removes the entire reason the streaming
machinery existed.

## Decision

**Do not run Whisper as a streaming provider.** Record the meeting audio in the
browser, upload it once when the meeting stops, and transcribe the whole file with the
app's EXISTING batch path. No windowing, no incremental commit, no live-decode loop —
so the failure class cannot exist.

This is "approach A": the browser owns capture; the server owns batch transcription at
stop.

## Provider scope (which cohort this serves)

Batch capture runs **only when the configured STT provider is a batch/local one**
(`whisper`, `mlx`, `parakeet`) — never for a live *streaming* provider
(`transcribe`, `apple`, `whisper_stream`). A streaming provider already transcribes
the meeting live over `/api/ws/stt` and persists finals via agent dispatch; also
running the post-meeting batch pass would transcribe the same speech twice and
append a duplicate transcript, and would cost a second (billed, for AWS Transcribe)
transcription. The frontend gates the recorder on
`batchTranscriptionActive = config && !STREAMING_STT_PROVIDERS.includes(config.stt_provider)`
(`STREAMING_STT_PROVIDERS` mirrors the backend `_STREAMING_PROVIDERS`), so the
streaming cohort — for whom meeting transcription already works — is completely
untouched by this change.

Scope of what improves for the batch/local cohort: this PR adds the **durable
transcript**. It does NOT re-run the meeting agents (note-taker / sketch-artist /
task-extractor) over the post-meeting transcript — by the time transcription
finishes the live `MeetingSession` is torn down and the meeting is `ended`. So for a
local-whisper user, notes and tasks are not produced from the batch transcript in
v1; the transcript itself is the artifact. (Agents continue to run live for the
streaming cohort, unchanged.)

## Known limitations (v1)

- The recording lives in browser (`MediaRecorder` chunk array) memory until the
  single upload at stop; a tab reload/crash before stop loses it. A failed upload
  still lets the meeting stop (the user is never trapped). A gateway restart
  mid-transcribe marks the meeting `failed` and deletes the audio rather than
  retrying. Durable/resumable capture is follow-on work.

## What already exists (reused, not rebuilt)

- `src/kiro_crew/transcribe.py::transcribe_audio(audio_path, stt_config) -> str | None`
  — provider-dispatching batch entrypoint. The default path (`_transcribe_native`)
  runs the local `whisper` CLI over a file as an isolated subprocess with bounded
  intra-op threads. Already redacts the transcript. **Reuse verbatim.**
- `store.append_transcript()` + `transcript.jsonl` — durable, locked, fsync'd
  transcript layer. The batch result is written here.
- `meeting_lifecycle.handle_stop_meeting` — the single, `START_LOCK`-guarded
  session-end seam. The transcription hook fires here.
- `website/src/hooks/useStreamingStt.ts` + `website/public/pcm-worklet.js` — the
  shared browser mic path. Left intact for chat dictation / push-to-talk.

## What changes

### Backend
1. **New endpoint** `POST …/meetings/{id}/audio` (register in
   `backend/routes/__init__.py`; handler in `meeting_lifecycle.py` or a new
   `recording.py`). Accepts one uploaded audio blob (webm/opus from the browser).
   - Size cap (reuse the spirit of `_TRANSCRIBE_MAX_BYTES` = 25 MB, tune for meeting
     length; reject over-cap with a clear code).
   - Write to a containment-checked path under the meeting dir via a new
     `store.recording_path(meeting_id)` (mirror `transcript_path`'s
     `MeetingsPathError` guards; reject symlinks; `[A-Za-z0-9._-]` id only).
2. **Stop hook.** In `handle_stop_meeting`, AFTER agents are drained and BEFORE/adjacent
   to `end_meeting_meta`: if a recording exists, `await transcribe_audio(path, cfg)`,
   then `append_transcript()` the result, then dispatch the agents over it.
   - Runs under the existing lock discipline; the whisper subprocess is already
     off-loop inside `transcribe_audio`.
   - Long transcription (minutes for a long meeting) must not block the stop response
     unboundedly: stop returns `status=ended` immediately and transcription runs as a
     tracked background task that updates meta (`transcription: pending|done|failed`)
     the frontend polls — OR stop awaits with a generous cap. **Open question T1 below.**
3. **Config.** `whisper` (batch) is already a valid provider. Confirm meetings can
   select it; no new provider string needed. `whisper_stream` is removed (below).

### Frontend (meetings-scoped)
4. In `website/src/apps/meetings/hooks/useMeetingTranscription.ts` /
   `useMeetingSession.ts`: when a meeting is `active`, capture mic with
   `MediaRecorder` (opus). On stop, POST the recorded blob to `…/{id}/audio`, then call
   the existing stop endpoint. Show a "transcribing…" state until meta reports done.
   - Does NOT touch the shared `useStreamingStt` dictation path.
   - No live transcript panel during the meeting (accepted).

### Deletions (all code authored on THIS branch — nothing pre-existing)
5. Delete `src/kiro_crew/whisper_stream.py`.
6. Revert `src/kiro_crew/dashboard/stt_stream.py` to `origin/main` (remove
   `_run_whisper_stream_session`, the `whisper_stream` entry in `_STREAMING_PROVIDERS`,
   and `_WHISPER_FINISH_TIMEOUT_SECS`).
7. Delete `test/test_whisper_stream.py`.
8. Revert the `whisper_stream` addition in `src/kiro_crew/config/loader.py`
   (`_VALID_STT_PROVIDERS`).
   The off-loop-executor / backpressure / finish-quiesce work in `4028d7a` was correct
   but solved streaming-only problems; it has nothing to protect in batch and goes too.

## Retention (privacy)

A meeting recording is sensitive audio. **Default: transcribe-then-delete** — the audio
file is removed as soon as `transcribe_audio` returns (success or failure), regardless of
outcome. The transcript (already redacted by `transcribe_audio`) is the only retained
artifact. Not configurable in v1 unless the user asks.

## Open questions to resolve before/while coding

- **T1 — stop latency.** Await transcription inside stop (simple, but a long meeting
  makes stop slow) vs. background task + polled status (better UX, more moving parts).
  Recommend background task with meta status.
- **T2 — capture format & sample rate.** MediaRecorder opus/webm → ffmpeg (already
  located by `transcribe.py`) handles decode for whisper. Confirm the whisper CLI path
  accepts webm directly or needs a transcode step.
- **T3 — no-recording meetings.** Typed-only meetings (broadcast bar, no mic) have no
  audio; stop hook must no-op cleanly and keep the typed transcript.
- **T4 — long-meeting size.** A 1-hour opus recording can exceed a 25 MB cap; size the
  cap to realistic meeting length or chunk the upload.

## Tests & gate plan

- Backend: upload endpoint (happy, over-cap, path-traversal id, symlink, no-recording);
  stop hook (transcribe called, transcript appended, agents dispatched, audio deleted,
  failure path leaves a usable meeting); reuse existing `transcribe_audio` tests.
- Frontend: meeting record→upload→stop sequence; transcribing state; typed-only meeting.
- Gates unchanged: pytest, isort/flake8/mypy `--platform linux` on changed .py,
  vitest/tsc for the frontend.
- Then the adversarial pre-merge review on the new (much smaller, no-algorithm) diff.

## Naming

The branch is `feat/local-streaming-stt`; the feature is no longer streaming. Either
rename the branch or make the PR title/description state it is post-meeting batch. The
commit message and PR must not claim streaming.
