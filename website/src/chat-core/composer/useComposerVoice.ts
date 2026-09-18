/**
 * Voice dictation for ONE composer — chat-core P3-b (composer layer).
 *
 * Everything a host needs to give its `ChatInput` a working microphone: the STT
 * config read, the `useVoiceInput` engine, caret-anchored transcript splicing,
 * the disarm flags that keep a late partial/final out of a composer the user
 * already sent or cancelled, the slot-switch teardown, and the "voice needs
 * setting up" modal state. Extracted verbatim from `ChatPage` so a second host
 * (`ChatPane`: split panes, Crew Members DMs) mounts the same behaviour instead
 * of a copy.
 *
 * What stays with the host, injected through `ComposerVoiceHost`:
 *   - the composer's text (`inputRef` + `setInput`);
 *   - whether its composer is the one on screen for a session
 *     (`isComposerFor`) — ChatPage has one composer over many slots and answers
 *     with its draft-settlement refs, a pane's composer IS its slot's;
 *   - what to do with a batch transcript for a session whose composer is not on
 *     screen (`deliverOffScreen`) — ChatPage appends it to that slot's persisted
 *     draft; a host without a draft store omits it and the text is dropped, the
 *     legacy off-screen streaming behaviour;
 *   - the endpointer's auto-submit (`onAutoSubmit`);
 *   - whether to own the global push-to-talk key binding (`pushToTalk`) —
 *     exactly one host per document, or a keystroke opens N microphones.
 *
 * ONE MIC. The engine is per instance, so a second instance would happily open a
 * second capture. A module-level owner slot makes capture exclusive across every
 * mounted composer: `startVoice` refuses while another instance holds it, and
 * `micBusyElsewhere` lets the host render its controls as busy — the same
 * treatment `voiceTranscribeActive` already gives a transcription in flight
 * (which is global through the inbox, see voiceTranscriptInbox).
 */
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { providerLabel } from '../../lib/sttProviders'
import { dictationSeparator, spliceDictationText } from '../../lib/dictationText'
import { useVoiceInput, voiceInputSupported, type TranscriptOrigin } from '../../hooks/useVoiceInput'
import { usePushToTalk } from '../../hooks/usePushToTalk'
import { redeliverPending } from '../../hooks/voiceTranscriptInbox'

/** How long the "dictation added" cue stays after a held transcript lands. */
const HELD_LANDED_MS = 4000

// ---------------------------------------------------------------------------
// Global capture owner — one microphone across every mounted composer.
// ---------------------------------------------------------------------------
let micOwner: string | null = null
/** The owner's chat session, so a blocked composer can NAME the chat that
 *  holds the mic (a lone DM thread shows nothing else that is capturing). */
let micOwnerSession: string | null = null
const micSubs = new Set<() => void>()
function setMicOwner(next: string | null, session: string | null = null): void {
  if (micOwner === next && micOwnerSession === session) return
  micOwner = next
  micOwnerSession = next === null ? null : session
  for (const fn of micSubs) fn()
}
function subscribeMic(fn: () => void): () => void {
  micSubs.add(fn)
  return () => { micSubs.delete(fn) }
}
const readMicOwner = () => micOwner
const readMicOwnerSession = () => micOwnerSession
/** Test seam. */
export function _resetMicOwner(): void { setMicOwner(null) }

export interface ComposerVoiceHost {
  /** Slot whose composer this instance drives; snapshotted by the engine at record-start. */
  sessionId: string | null
  /** Live composer text, fresh every render. */
  inputRef: React.MutableRefObject<string>
  setInput: (value: string) => void
  /**
   * Is THIS host's composer the one on screen for `target`? Defaults to
   * `target === sessionId`, which is exact for a per-slot composer. ChatPage
   * overrides it with its draft-settlement predicate (see its call site).
   */
  isComposerFor?: (target: string | null) => boolean
  /**
   * A BATCH transcript for a session whose composer is not on screen. `append`
   * builds the new text from that session's current draft. Omit to drop.
   */
  deliverOffScreen?: (target: string, append: (base: string) => string) => void
  /** Streaming semantic endpointing judged the utterance complete: submit. */
  onAutoSubmit?: () => void
  /** Own the document-wide push-to-talk key binding. Default false. */
  pushToTalk?: boolean
  /** Host-owned caret refs (a host that also reads them, e.g. to splice a picked
   *  file token at the caret). Omit and the hook creates its own. */
  caretRef?: React.MutableRefObject<{ start: number; end: number } | null>
  pendingCaretRef?: React.MutableRefObject<number | null>
}

export type SttConfig = { streaming?: boolean; enabled?: boolean; dictation_panel?: boolean; available?: boolean; provider?: string }

export function useComposerVoice(host: ComposerVoiceHost) {
  const { sessionId, inputRef, setInput } = host
  const instanceId = useId()
  const sessionIdRef = useRef(sessionId); sessionIdRef.current = sessionId
  const isComposerForRef = useRef(host.isComposerFor); isComposerForRef.current = host.isComposerFor
  const deliverOffScreenRef = useRef(host.deliverOffScreen); deliverOffScreenRef.current = host.deliverOffScreen
  const onAutoSubmitRef = useRef(host.onAutoSubmit); onAutoSubmitRef.current = host.onAutoSubmit
  const isComposerFor = useCallback((target: string | null) =>
    isComposerForRef.current ? isComposerForRef.current(target) : target === sessionIdRef.current, [])

  const { data: sttCfg } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<SttConfig>,
  })
  const sttStreaming = !!sttCfg?.streaming
  const sttEnabled = !!sttCfg?.enabled
  // The backend probes for the provider's binary and reports `available`.
  // Default true so a not-yet-loaded config doesn't flash the modal; the
  // separate sttConfigLoaded guard already covers the pre-load case.
  const sttAvailable = sttCfg?.available !== false
  // The LOCALISED provider name, not the wire id: the modal puts it in a
  // sentence, and a bare id reads as a typo there ("local is not installed").
  const sttProvider = providerLabel(sttCfg?.provider || '')
  // Default true so the panel is the standard recording surface; the backend
  // sends an explicit boolean, so `undefined` here means "config not loaded yet"
  // rather than "off", and a pre-load recording would otherwise flash the bar.
  const sttDictationPanel = sttCfg?.dictation_panel !== false
  // Treat "config not loaded yet" as disabled so the guard never lets a
  // recording start before STT is confirmed on. Stable boolean so toggleVoice's
  // deps don't churn on every sttCfg object identity from a refetch.
  const sttConfigLoaded = !!sttCfg
  // Opened when the user clicks the mic while STT is disabled — points them at
  // the setting that turns it on instead of starting a recording that would
  // never be transcribed.
  const [voiceSetupOpen, setVoiceSetupOpen] = useState(false)
  // A transcript that the inbox HELD (its composer was off screen when it
  // settled) lands later with nothing else happening on screen. Flag its arrival
  // for a moment so the composer can say "dictation added" — otherwise a user
  // who assumed the utterance was lost re-dictates and finds it doubled.
  const [heldLanded, setHeldLanded] = useState(false)
  const redeliveringRef = useRef(false)
  const landedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (landedTimerRef.current) clearTimeout(landedTimerRef.current) }, [])
  const frozenInputRef = useRef<string | null>(null)
  // Caret snapshot taken alongside frozenInputRef, so a streaming partial (and
  // the final that replaces it) keeps inserting at the same spot. The batch
  // path leaves both null and reads the LIVE composer caret instead.
  const frozenCaretRef = useRef<{ start: number; end: number } | null>(null)
  // Live composer caret, kept current by ChatInput (onSelect / click / typing).
  // Dictation splices the transcript in HERE instead of always appending at end.
  const ownCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voiceCaretRef = host.caretRef ?? ownCaretRef
  // Caret offset ChatInput should restore after a dictation-driven value update
  // lands (set by the splice below, consumed + cleared inside ChatInput).
  const ownPendingCaretRef = useRef<number | null>(null)
  const voicePendingCaretRef = host.pendingCaretRef ?? ownPendingCaretRef
  // Drops late-arriving partials/finals for the CURRENT slot after a send.
  // `stop()` is async (up to 5s for backend close) — without this guard, a
  // delayed onFinal would repopulate the composer with text the user already
  // sent. Cross-SLOT safety is handled separately by session-scoped routing
  // (see applyVoiceText + voice.sessionOwner).
  const sttDisarmedRef = useRef(false)
  // Narrower sibling of `sttDisarmedRef`, for a MANUAL STOP of a streaming
  // recording that already put a hypothesis in the composer.
  //
  // One flag was doing two jobs, and a manual stop only wants one of them.
  // `applyVoiceText` APPENDS (`base + ' ' + text`), so the close-time final
  // landing on a composer that already holds the hypothesis duplicates the
  // utterance ("hello hello") — that has to stay suppressed. But `onPartial`
  // REPLACES the region at the frozen boundary, and the hook re-emits
  // `finals.join(' ')` through it on every `final` message while `stop()`
  // deliberately leaves the socket draining. Suppressing that too meant every
  // segment Transcribe stabilised AFTER the release was dropped, so the user
  // was left holding the last UNSTABLE hypothesis. On a push-to-talk hold that
  // is the common case, not a corner: the hold is short, so the tail of the
  // utterance is exactly the part still unstable at release.
  //
  // So: this flag suppresses the append only, and leaves the drain's own
  // corrections free to keep replacing the region until the socket closes.
  // Cancel, send and slot-switch still want EVERYTHING suppressed and keep
  // using `sttDisarmedRef` — the user discarded, already sent, or left.
  const sttAppendDisarmedRef = useRef(false)
  // The composer content UP TO the end of the region onPartial last inserted,
  // plus the whole value it wrote. Dictation splices at the caret, so it can sit
  // mid-draft with an existing tail after it — and typing after the release
  // lands at the restored caret, i.e. between the two. Anchoring on the PREFIX
  // (not the whole value) is what lets a drain-time update replace the corrected
  // region and keep everything after it verbatim; anchoring on the whole value
  // would fail its own startsWith check mid-draft and drop the correction.
  // The full value distinguishes "the user typed" from "nothing changed", which
  // decides whether the caret may be moved.
  const lastDictationAnchorRef = useRef<string | null>(null)
  const lastDictationValueRef = useRef<string | null>(null)
  // Sticky for the whole post-stop drain: once the user has typed, the caret is
  // theirs until dictation restarts. Recomputing "did they edit?" per update is
  // not enough — after the first correction carries the suffix across, the
  // composer matches what we wrote again, so a second correction would decide
  // nothing was edited and yank the caret back in front of the typed text.
  const postStopEditedRef = useRef(false)
  // Suppresses ONLY the auto-submit route, and unlike the append flag it is set
  // by EVERY manual stop of a streaming recording — including a cold-stream stop
  // where no partial landed. "Stop capturing" is never "send": without this, a
  // short press against a cold stream leaves the endpointer armed, and a
  // trailing final's endpoint verdict submits the turn the user never asked to
  // send. The append flag cannot carry this, because with no partial landed the
  // close-time final is the only copy of the utterance and must still land.
  const sttEndpointDisarmedRef = useRef(false)
  // A frozen caret is a position in the composer as it stood at the release. Once
  // the user edits after that, it can go stale in two ways, and both corrupt the
  // splice: a RANGE (dictating over a selection replaces it) whose selection they
  // have since typed over, and an OFFSET whose meaning shifts when they edit text
  // BEFORE it. Rebase it onto the current text instead of trusting or discarding
  // it wholesale — discarding it would put the transcript after text they wrote
  // later, trusting it would cut into text they wrote earlier.
  const rebaseFrozenCaret = useCallback(() => {
    if (!sttEndpointDisarmedRef.current) return
    const frozen = frozenCaretRef.current
    const released = lastDictationValueRef.current
    const cur = inputRef.current ?? ''
    // Untouched composer: a selection here is still a legitimate replacement
    // target, which is what dictating over a selection is supposed to do.
    if (!frozen || released === null || cur === released) return
    // Bound the edit to the region between the longest common prefix and suffix.
    let lcp = 0
    while (lcp < released.length && lcp < cur.length && released[lcp] === cur[lcp]) lcp++
    let lcs = 0
    while (
      lcs < released.length - lcp && lcs < cur.length - lcp &&
      released[released.length - 1 - lcs] === cur[cur.length - 1 - lcs]
    ) lcs++
    const start = frozen.start
    let next: number
    if (start <= lcp) next = start                                    // edit is after it
    else if (start >= released.length - lcs) next = start + (cur.length - released.length)
    else next = voiceCaretRef.current?.start ?? start                 // edit straddles it
    next = Math.max(0, Math.min(next, cur.length))
    frozenCaretRef.current = { start: next, end: next }
  }, [inputRef, voiceCaretRef])
  // The hook's EFFECTIVE streaming mode: streaming is only truly active when the
  // config asks for it AND the browser supports it (AudioWorklet/WS). Mirrored
  // from voice.streamEnabled (set by the effect below, once `voice` exists) so
  // the disarm + cross-slot-routing decisions gate on what the hook ACTUALLY
  // runs, not the raw config. Keying those on the config alone would, in a
  // browser without AudioWorklet, treat a batch-fallback session as streaming
  // and disarm/drop its (only) transcript.
  const streamEnabledRef = useRef(false)
  // Splice a dictation transcript into `base` at the caret (frozen snapshot
  // when streaming, else the live caret), returning the new value and the caret
  // offset to restore. Falls back to appending when no caret is known (e.g. the
  // composer was never focused).
  const spliceDictation = useCallback((base: string, text: string): { value: string; caret: number } =>
    spliceDictationText(base, text, frozenCaretRef.current ?? voiceCaretRef.current), [voiceCaretRef])
  // Deliver a finished transcript to the slot that INITIATED the recording,
  // using the session id useVoiceInput snapshotted at record-start (falling back
  // to this host's session for the ordinary same-slot case). Same-slot splices
  // into the live composer; a background slot is handed to the host's
  // `deliverOffScreen` (ChatPage: appended to its persisted draft — recoverable,
  // shown on return — instead of leaking into the active session or being
  // dropped).
  const applyVoiceText = useCallback((text: string, transcriptSession: string | null, origin: TranscriptOrigin) => {
    // Disarmed after a send (streaming) — the transcript was already sent, so
    // drop it for EVERY route. Checked FIRST (before the cross-slot branch) so a
    // late final can't slip the already-sent text back into the originating
    // slot's draft.
    //
    // `sttAppendDisarmedRef` covers the narrower case: a manual stop whose
    // hypothesis is already in the composer. This route APPENDS, so letting the
    // close-time final through there would duplicate the utterance.
    //
    // Both are STREAMING-only states — every site that arms them is gated on
    // streaming — so they are keyed on where the text came from, not on the mode
    // selected right now. A batch transcription can outlive the page that started
    // it and land after streaming was switched on, and its onstop transcript is
    // always the only copy: suppressing it would delete what the user said.
    if (origin === 'stream' && (sttDisarmedRef.current || sttAppendDisarmedRef.current)) return
    const target = transcriptSession ?? sessionIdRef.current
    const append = (base: string) => base + dictationSeparator(base, text) + text
    // Splice into the LIVE composer only when the host says its composer is the
    // one on screen for the target. Otherwise route to the host's off-screen
    // delivery.
    const onScreen = isComposerFor(target)
    if (!onScreen) {
      // Off-screen (or not-yet-settled) delivery is BATCH ONLY. Streaming splices
      // its live hypothesis into `input`, which is flushed into the draft on
      // switch, so a cross-slot append would double it — a streaming final that
      // lands off its slot is dropped (pre-existing behaviour). Batch has no
      // partial, so appending to the slot's draft is unambiguous. Keyed on the
      // text's origin rather than the live streaming setting, which is a proxy
      // that goes wrong for a batch transcript arriving after the mode changed.
      if (!target || origin === 'stream') return
      deliverOffScreenRef.current?.(target, append)
      return
    }
    // Foreground: streaming seeds frozenInputRef/frozenCaretRef in onPartial
    // (the pre-dictation snapshot); the batch path never fires onPartial so both
    // are null — fall back to the live composer text + caret so the transcript
    // inserts at the cursor instead of overwriting (or blindly appending to)
    // what the user typed.
    rebaseFrozenCaret()
    const spliced = spliceDictation(frozenInputRef.current ?? inputRef.current ?? '', text)
    // Only arm the caret restore when the value actually changes. If a streaming
    // final equals the last partial, setInput is a no-op and the restore effect
    // (keyed on `value`) never fires — leaving a stale pending caret that would
    // hijack the user's NEXT edit.
    if (spliced.value !== inputRef.current) {
      setInput(spliced.value)
      voicePendingCaretRef.current = spliced.caret
      if (redeliveringRef.current) {
        setHeldLanded(true)
        if (landedTimerRef.current) clearTimeout(landedTimerRef.current)
        landedTimerRef.current = setTimeout(() => setHeldLanded(false), HELD_LANDED_MS)
      }
    }
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
  }, [isComposerFor, spliceDictation, rebaseFrozenCaret, inputRef, setInput, voicePendingCaretRef])
  // Capture can end from a manual release or from the readiness-buffer ceiling.
  // Both release the composer for typing while the same socket still sends finals.
  const protectStoppedDictation = useCallback(() => {
    if (!streamEnabledRef.current || sttEndpointDisarmedRef.current) return
    sttEndpointDisarmedRef.current = true
    if (frozenInputRef.current !== null) {
      // Partials already own the region; close-time delivery would duplicate it.
      sttAppendDisarmedRef.current = true
    } else {
      // Freeze the release caret, but keep the live draft for a cold stream's first
      // result so typing before that result is preserved at its authored position.
      frozenCaretRef.current = voiceCaretRef.current
      lastDictationValueRef.current = inputRef.current
    }
  }, [inputRef, voiceCaretRef])
  const onPartial = useCallback((text: string, partialSession: string | null) => {
    // Streaming partials only fire while the originating slot is on screen
    // (switching slots stops the stream), so a partial attributed to any
    // other slot is a late straggler — drop it rather than smear a
    // half-word into the wrong session.
    if (partialSession && partialSession !== sessionIdRef.current) return
    // Deliberately NOT gated on `sttAppendDisarmedRef`: after a manual stop
    // the socket is still draining, and this is the route that carries the
    // stabilised text. It REPLACES the region at the frozen boundary rather
    // than appending, so letting it keep firing cannot duplicate anything —
    // it is what turns the last unstable hypothesis into the real transcript.
    if (sttDisarmedRef.current) return
    // Snapshot the pre-dictation text AND caret on the first partial
    // (before setInput, so the updater stays pure — no ref mutation inside a
    // function React may invoke twice) so every later partial and the final
    // insert at the same spot, replacing the growing hypothesis.
    if (frozenInputRef.current === null) {
      frozenInputRef.current = inputRef.current
      // Do not clobber a caret a cold-stream stop already froze: that one is
      // the release-time insertion point, and the live caret is now wherever
      // the user has typed since.
      frozenCaretRef.current = frozenCaretRef.current ?? voiceCaretRef.current
    }
    rebaseFrozenCaret()
    const spliced = spliceDictation(frozenInputRef.current ?? '', text)
    // Everything up to and including the dictated insertion. What follows it
    // in the composer (an existing tail, and anything typed after release) is
    // carried across untouched rather than rebuilt from the snapshot.
    const anchor = spliced.value.slice(0, spliced.caret)
    let next = spliced.value
    // Where the caret should end up. Defaults to the end of the dictated
    // region (the ordinary "we own the composer" case); the post-stop branch
    // overrides it when the text is the user's to steer.
    let caretTarget: number | null = spliced.caret
    if (sttEndpointDisarmedRef.current) {
      // POST-STOP DRAIN. The user has let go, so as far as they are concerned
      // dictation is over and they may already be typing — at the restored
      // caret, which for mid-draft dictation sits in the MIDDLE of the text.
      // Rebuilding from the frozen snapshot would delete that typing, so
      // verify our own prefix is still intact and splice the correction in
      // ahead of whatever now follows it. If the prefix cannot be verified
      // the user edited inside the dictated region; leave the composer alone
      // rather than guess — same policy as cancelVoice, for the same reason:
      // a heuristic here deletes user-authored text.
      //
      // Gated on the ENDPOINT flag, not the append flag: a cold-stream stop
      // deliberately leaves the append armed (the close-time final is the
      // only copy of the utterance), so keying off it would skip this branch
      // in exactly the case where it is still needed.
      //
      // During recording this does not apply: the region is being actively
      // rewritten and that behaviour is unchanged.
      const prev = lastDictationAnchorRef.current
      const cur = inputRef.current ?? ''
      // The composer now holds a copy of the utterance, which is the exact
      // condition the append flag encodes — so close the close-time route
      // here rather than at stop time. stopVoice could not decide this: with
      // frozenInputRef still null it had to leave the append armed, because
      // back then the close-time final really was the only copy. Once a drain
      // partial has landed that is no longer true, and letting the final
      // through would re-splice from the snapshot and delete whatever the
      // user typed after the release.
      sttAppendDisarmedRef.current = true
      // Checked OUTSIDE the anchor guard: on a cold stream the first drain
      // partial has no anchor yet, but the user may already have typed since
      // the release, and their caret must still be left alone.
      if (cur !== lastDictationValueRef.current) postStopEditedRef.current = true
      // A null anchor means no partial has landed yet — the cold-stream stop.
      // This IS the first write: there is nothing to preserve and nothing to
      // verify, and returning here would drop the utterance. Fall through to
      // the plain write, which establishes the anchor for the next update.
      // The typed text is inside the snapshot (taken from the LIVE composer)
      // and the insertion point is the caret stopVoice froze at the release,
      // so the transcript lands where the user was speaking rather than after
      // what they wrote afterwards.
      if (prev !== null) {
        if (!cur.startsWith(prev)) return
        next = anchor + cur.slice(prev.length)
        if (postStopEditedRef.current) {
          // Their caret is in their own text, so it must not be dragged to the
          // end of the dictation — but NOT arming it is not "leaving it
          // alone" either: React replaces the textarea value and the browser
          // resets the DOM caret to the end. Re-arm it at the same LOGICAL
          // spot, shifted by how much the region ahead of it grew or shrank.
          const live = voiceCaretRef.current
          caretTarget = live && live.start >= prev.length
            ? live.start + (anchor.length - prev.length)
            : null
        }
      } else if (postStopEditedRef.current) {
        // Cold-stream first write with typing already done: there is no old
        // anchor to measure a shift against, and the value commit leaves the
        // caret at the end — which is past their text, a sane place to be.
        caretTarget = null
      }
    }
    if (next !== inputRef.current) {
      setInput(next)
      if (caretTarget !== null) voicePendingCaretRef.current = caretTarget
    }
    lastDictationAnchorRef.current = anchor
    lastDictationValueRef.current = next
  }, [spliceDictation, rebaseFrozenCaret, inputRef, setInput, voiceCaretRef, voicePendingCaretRef])
  // Semantic endpointing (stt.endpointing) judged the utterance complete:
  // auto-submit. The composer already holds the streamed transcript via
  // onPartial, and the host's send reads its composer + stops the live capture
  // itself (via `disarmForSend`), so this is the same path as pressing Enter
  // mid-dictation — just triggered by the backend verdict.
  const onEndpoint = useCallback(() => {
    // A manual stop is the user saying "stop capturing", so a backend
    // endpoint verdict arriving during the drain must not turn that into an
    // unrequested send. The endpoint flag is what covers a COLD-stream stop,
    // where no partial landed and the append flag is deliberately left unset
    // so the close-time final can still deliver the utterance.
    if (sttDisarmedRef.current || sttAppendDisarmedRef.current || sttEndpointDisarmedRef.current) return
    onAutoSubmitRef.current?.()
  }, [])
  const voice = useVoiceInput(
    applyVoiceText,
    {
      streaming: sttStreaming,
      sessionId,
      onCaptureStop: protectStoppedDictation,
      onPartial,
      onEndpoint,
      ownsSession: isComposerFor,
      acceptsUnowned: !!host.deliverOffScreen,
    }
  )
  // Keep a ref to the latest `voice` so effects that intentionally omit
  // `voice` from their deps always invoke the current instance — otherwise
  // they'd capture a stale `toggle`/`recording` whenever `voice` identity
  // changes (e.g. when `sttStreaming` flips).
  const voiceRef = useRef(voice)
  useEffect(() => { voiceRef.current = voice }, [voice])
  // Keep streamEnabledRef in sync with the hook's EFFECTIVE streaming mode (see
  // its declaration above). The host's send / the slot-switch effect /
  // toggleVoice read it to decide whether a draining final should be disarmed —
  // which must reflect what the hook actually runs, not the raw config.
  useEffect(() => { streamEnabledRef.current = voice.streamEnabled }, [voice.streamEnabled])

  // ---- one mic across instances ------------------------------------------
  const micOwnerNow = useSyncExternalStore(subscribeMic, readMicOwner, readMicOwner)
  const micOwnerSessionNow = useSyncExternalStore(subscribeMic, readMicOwnerSession, readMicOwnerSession)
  const micBusyElsewhere = micOwnerNow !== null && micOwnerNow !== instanceId
  const micBusyElsewhereSession = micBusyElsewhere ? micOwnerSessionNow : null
  // Latch across the async start: the engine flips `recording` only once the
  // stream is live, and a second composer's click inside that window must
  // already see the mic as taken. Generation-scoped: only the start that set
  // the latch may clear it, so a same-instance re-entry (a double-click during
  // startup) cannot release ownership while the first start is still pending
  // and let another composer capture concurrently.
  const startingRef = useRef(false)
  const startGenRef = useRef(0)
  // Release when THIS instance's capture and transcription are both over. Runs
  // every render on purpose: a start that fails (permission denied) never flips
  // `recording`, so a deps-keyed effect would not fire and the owner slot would
  // stay taken with nothing recording.
  useEffect(() => {
    if (micOwner === instanceId && !startingRef.current && !voice.recording && !voice.transcribing) setMicOwner(null)
  })
  useEffect(() => () => { if (micOwner === instanceId) setMicOwner(null) }, [instanceId])

  /**
   * Start voice capture, with the gating and state resets every entry point
   * needs. Extracted from `toggleVoice` so the push-to-talk key driver
   * (`usePushToTalk`) goes through the SAME preamble — calling `voice.start()`
   * raw would skip the disarm reset and the frozen-snapshot clear, and a
   * key-started dictation would then be rebuilt from stale pre-dictation text.
   *
   * RETURNS the start promise. Load-bearing, not incidental: `usePushToTalk`
   * chains on it to stop a session whose async startup only finished after the
   * key was already released. Swallowing it here leaves that guard unreachable
   * and the microphone open with nothing holding it.
   *
   * `silent` suppresses the "voice needs setting up" modal. The key binding is a
   * PASSIVE trigger — a bare modifier is also an ordinary typing modifier — so a
   * keystroke that used to type a character must never throw an unsolicited
   * dialog. Clicking the mic button is a deliberate request and still explains
   * itself.
   */
  const startVoice = useCallback((opts?: { silent?: boolean }): Promise<void> | void => {
    // Starting a recording while server-side STT is disabled would capture
    // audio that never gets transcribed. Point the user at the enable setting
    // instead — unless this came from the keyboard (see `silent`).
    if (!sttConfigLoaded || !sttEnabled || !sttAvailable) {
      if (!opts?.silent) setVoiceSetupOpen(true)
      return
    }
    // Exclusive sessions: the mic is a single shared device, so refuse to
    // START a new recording while another session's transcription is still
    // in flight (voice.transcribing) or another composer holds the capture.
    // This is what keeps voice single-session — no two recordings/
    // transcriptions ever overlap — so the busy state needs only a single
    // owner and can never be misattributed.
    if (voice.transcribing) return
    if (micOwner !== null && micOwner !== instanceId) return
    // Same-instance re-entry while a start is in flight: the engine has its own
    // latch and would no-op, but a no-op here must not touch ours either.
    if (startingRef.current) return
    sttDisarmedRef.current = false
    sttAppendDisarmedRef.current = false
    sttEndpointDisarmedRef.current = false
    // Reset stale snapshot from a prior session that ended without
    // finals — otherwise onPartial sees a non-null ref, skips
    // re-snapshotting, and text typed between sessions is dropped.
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    setMicOwner(instanceId, sessionIdRef.current)
    startingRef.current = true
    const gen = ++startGenRef.current
    const started = voice.start()
    // The release effect re-checks once the engine has reported the outcome.
    // Only THIS start's settle clears the latch it set.
    return Promise.resolve(started).finally(() => { if (gen === startGenRef.current) startingRef.current = false })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.transcribing, voice.start, sttEnabled, sttConfigLoaded, sttAvailable, instanceId])

  /** Stop voice capture. Always allowed — only starting is gated. */
  const stopCapture = voice.stop
  const stopVoice = useCallback(() => {
    protectStoppedDictation()
    stopCapture()
  }, [protectStoppedDictation, stopCapture])

  const toggleVoice = useCallback(() => {
    if (voice.recording) stopVoice()
    else startVoice()
    // Depends on the individual member actually read (`voice.recording`), not the
    // whole `voice` object — `[voice]` would recreate this callback every render
    // and re-render every child that receives `toggleVoice`. No suppression is
    // needed here because the split into startVoice/stopVoice left this list
    // genuinely exhaustive.
  }, [voice.recording, startVoice, stopVoice])
  // Cancel (discard) the in-progress dictation — Esc. Batch simply drops the
  // pending audio (the hook's onstop skips transcription), so nothing lands in
  // the composer. Streaming additionally disarms the draining final AND removes
  // the live dictated region from the composer at the frozenInputRef boundary:
  // the region is recomputed with the same `spliceDictation` call onPartial used
  // (so it matches a mid-draft caret splice, not just an append), and we drop
  // exactly that region — preserving the pre-dictation text verbatim (including
  // its own trailing whitespace) AND any suffix typed after the dictation. When
  // the region can't be verified (the user replaced/edited it), leave the
  // composer unchanged rather than restoring the snapshot and losing that edit.
  // Uses voiceRef.current (not `voice`) so this prop stays referentially stable
  // and does not re-render the composer every render — matching toggleVoice.
  const cancelVoice = useCallback(() => {
    if (streamEnabledRef.current) {
      sttDisarmedRef.current = true
      // Remove the dictated region at the frozenInputRef boundary, preserving
      // the pre-dictation text EXACTLY (including its own trailing whitespace)
      // and any suffix the user typed after the dictation. onPartial rebuilt the
      // composer as `frozen [+ ' ' separator] + partial`, so reconstruct that
      // exact region and drop only it — never a blanket trailing-space strip.
      const cur = inputRef.current ?? ''
      const frozen = frozenInputRef.current
      const p = voiceRef.current.partial
      if (frozen !== null && p) {
        // Reconstruct the composer value through the SAME pure function that
        // wrote it. onPartial splices at the snapshotted caret, so for a
        // mid-draft caret the value is `before + lead + partial + trail + after`
        // — NOT `frozen + separator + partial`. Re-deriving the region with an
        // append-only formula failed `startsWith` for every mid-draft dictation
        // and fell through to the leave-unchanged branch, stranding the partial
        // in the draft. spliceDictation reads the same frozen caret, so this
        // reproduces the write exactly for both the append and mid-caret shapes.
        const written = spliceDictation(frozen, p).value
        if (cur.startsWith(written)) {
          // The composer still begins with exactly the region onPartial wrote.
          // Restore the pre-dictation text verbatim and keep any suffix the user
          // typed after it.
          setInput(frozen + cur.slice(written.length))
        }
        // else: the dictated region can't be verified exactly — the user edited
        // or replaced it (e.g. deleted the separator, or typed their own text
        // that merely ends in the same word as the partial). Leave the composer
        // UNCHANGED: a suffix-match heuristic here would delete user-authored
        // text ("say hello" -> "say"). The disarm above still drops the draining
        // final, so no dictation is committed; at worst the visible partial
        // lingers for the user to clear.
      }
      // (frozen===null, or no current partial: nothing verifiably removable —
      // leave the composer as-is rather than risk clobbering user text.)
      // Clear BOTH halves of the snapshot: they are written together in
      // onPartial and a surviving caret would aim the next session's first
      // splice at a position from the discarded one.
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      frozenCaretRef.current = null
    }
    voiceRef.current.cancel()
  }, [spliceDictation, inputRef, setInput])

  // Push-to-talk / tap-to-toggle keyboard binding (default: hold right ⌥ on
  // macOS, ⌥⇧Space elsewhere). Routed through startVoice/stopVoice rather than
  // voice.start/stop so a key-driven dictation gets the same gating and
  // snapshot resets as the mic button, and `cancelVoice` — NOT the hook's raw
  // cancel — for the discard. Since capture now opens on the keydown, a fast
  // partial can reach the composer before the press is revealed as a chord or a
  // sub-threshold tap, and the raw cancel would strand that text; `cancelVoice`
  // runs the streaming rollback that removes the dictated region (and no-ops
  // when nothing verifiably removable was written). No `prewarm`: the driver
  // opens capture on the keydown itself, so there is no warm-up step to
  // schedule. Only the host that owns the binding (`pushToTalk`) is armed: N
  // composers each listening would open N captures on one keystroke.
  usePushToTalk(
    {
      recording: voice.recording,
      // silent: a bare modifier is also an ordinary typing modifier, so a
      // keystroke must never raise the voice-setup modal on its own.
      start: () => startVoice({ silent: true }),
      stop: stopVoice,
      cancel: cancelVoice,
    },
    { disabled: !voiceInputSupported || !host.pushToTalk },
  )
  // Stop any in-flight recording and clear the streaming prefix when the host's
  // session changes. The mic is a single shared device, so a recording can't
  // follow the user to another session; a BATCH transcript is still delivered
  // to the originating slot via applyVoiceText's session-scoped routing (which
  // prevents cross-slot leakage precisely — no blanket disarm needed here).
  // Clearing frozenInputRef here means a streaming final that lands after a
  // switch-and-return rebases on the LIVE input, so edits made after returning
  // are preserved rather than clobbered by a stale snapshot.
  useEffect(() => {
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    // Drop the previous slot's caret so dictating in a freshly switched-to slot
    // (without touching its composer) appends to that slot's draft instead of
    // inserting at the old slot's offset.
    voiceCaretRef.current = null
    // Streaming ONLY: disarm so a delayed streaming final arriving after this
    // switch is dropped instead of appended. Its live partial was already
    // flushed into the outgoing slot's draft, so appending the full final on
    // return would duplicate the dictated text ("hello hello"). Batch is NOT
    // disarmed — its single final is routed to the originating slot's draft by
    // applyVoiceText. (Cross-slot streaming delivery is a follow-up; streaming
    // is opt-in and off by default.)
    if (streamEnabledRef.current) sttDisarmedRef.current = true
    if (voiceRef.current.recording) voiceRef.current.toggle()
    // A batch transcript that settled while no composer showed its session is
    // held by the inbox; this composer may be that session's now. Delivery is
    // synchronous, so the flag brackets exactly the held delivery.
    redeliveringRef.current = true
    try { redeliverPending() } finally { redeliveringRef.current = false }
  }, [sessionId, voiceCaretRef])
  // True when the current voice session (owned by the slot where recording
  // actually started — see useVoiceInput's sessionOwner) is the slot on screen.
  // Gates the recording/transcribing UI so a session transcribing in the
  // background never shows a busy/locked mic in the session the user switched to.
  const voiceOwned = voice.sessionOwner === sessionId

  /**
   * Sending while STREAMING dictation is live ends the dictation. The panel
   * advertises "Enter to send", so this path is reachable by design — and
   * without it, streaming STT keeps running past the send: `onPartial`
   * re-derives the composer value from `frozenInputRef`, which was snapshotted
   * BEFORE the send cleared it, so the next partial repopulates the composer
   * with text the user already sent. Disarm FIRST so any partial/final already
   * in flight is dropped, then stop capture (stop() is async — up to 5s for
   * the backend close).
   *
   * STREAMING ONLY, deliberately. In batch mode the transcription arrives
   * exactly once, from `MediaRecorder.onstop` AFTER capture ends, and it
   * arrives through `onText` — which honours `sttDisarmedRef`. Disarming here
   * would throw away the entire recording, which is the opposite of the bug
   * being fixed. Batch therefore keeps its pre-existing behaviour untouched:
   * capture continues, and the transcript lands when the user stops.
   *
   * The host calls this at the top of its send, before it reads the composer.
   */
  const disarmForSend = useCallback(() => {
    if (voiceRef.current.recording && streamEnabledRef.current) {
      sttDisarmedRef.current = true
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      voiceRef.current.toggle()
    }
  }, [])

  return {
    voice,
    voiceOwned,
    voiceCaretRef,
    voicePendingCaretRef,
    startVoice,
    stopVoice,
    toggleVoice,
    cancelVoice,
    disarmForSend,
    micBusyElsewhere,
    micBusyElsewhereSession,
    /** True for a moment after a HELD transcript landed in this composer. */
    heldLanded,
    /** `VoiceDisabledModal` state; the host renders the modal and routes its settings link. */
    setup: {
      open: voiceSetupOpen,
      setOpen: setVoiceSetupOpen,
      reason: (sttEnabled && !sttAvailable ? 'unavailable' : 'disabled') as 'unavailable' | 'disabled',
      provider: sttProvider,
    },
    sttDictationPanel,
  }
}

export type ComposerVoice = ReturnType<typeof useComposerVoice>

/**
 * The `ChatInput` voice props, built from a `useComposerVoice` result. Both
 * hosts spread this so the prop set cannot drift between them again — the
 * drift this slice exists to close.
 */
export function composerVoiceInputProps(cv: ComposerVoice) {
  const { voice, voiceOwned } = cv
  return {
    voiceRecording: voiceOwned && voice.recording,
    voiceTranscribing: voiceOwned && voice.transcribing,
    /* Ungated: `startVoice` refuses on `voice.transcribing` outright, so the
       voice controls have to read the same global fact. */
    voiceTranscribeActive: voice.transcribing,
    /* Another composer holds the capture. Distinct from transcribing: the mic
       is blocked, but nothing of this composer's is being transcribed, so
       ChatInput must not show a "Transcribing" spinner for it. */
    voiceBusyElsewhere: cv.micBusyElsewhere,
    /* Which chat holds it, when known — the host names it in the blocked copy. */
    voiceBusyElsewhereSession: cv.micBusyElsewhereSession,
    /* A held dictation just landed here: the composer shows a brief cue. */
    voiceHeldLanded: cv.heldLanded,
    voiceError: voice.error,
    voiceLevel: voiceOwned ? voice.level : 0,
    voiceDeviceLabel: voiceOwned ? voice.deviceLabel : '',
    voiceDeviceId: voiceOwned ? voice.deviceId : '',
    onSelectVoiceDevice: voice.switchDevice,
    voiceDeviceSwitchIsLive: voiceOwned && voice.deviceSwitchIsLive,
    onClearVoiceError: voice.clearError,
    voiceDictationPanel: cv.sttDictationPanel,
    voiceStreaming: voice.streamEnabled,
    voiceSampleRef: voice.sampleRef,
    voicePartial: voiceOwned ? voice.partial : '',
    voiceDownload: voiceOwned ? voice.download : null,
    voiceCaretRef: cv.voiceCaretRef,
    voicePendingCaretRef: cv.voicePendingCaretRef,
    onVoiceToggle: voiceInputSupported ? cv.toggleVoice : undefined,
    onVoiceCancel: voiceInputSupported ? cv.cancelVoice : undefined,
    onVoicePrewarm: voiceInputSupported && !cv.micBusyElsewhere ? voice.prewarm : undefined,
    onVoiceStart: voiceInputSupported ? cv.startVoice : undefined,
    onVoiceStop: voiceInputSupported ? cv.stopVoice : undefined,
    voiceCaptureActive: voice.recording,
  }
}
