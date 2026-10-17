import { useRef, useEffect, useState, useCallback, useMemo, type MutableRefObject } from 'react'
import { useAppSelector, type AppDispatch } from '../../../store'
import { settingsPath } from '../../../components/settingsPath'
import { stageToMainComposer } from '../../../store/chatSlice'
import type { ComposerHandle, ComposerVoiceOptions } from '../../../chat-core/composer/Composer'
import { createComposerDraftStore, useComposerDraftSelector, type ComposerDraftStore, useComposerDraft } from '../../../chat-core/composer/draftStore'
import { mergeIntoDraft, chatPageShouldConsumeHandoff, setDraft } from '../../../utils/chatDrafts'
import { focusComposer } from '../composerFocus'

/** Draft facts ChatPage renders from; each re-renders the page only when it flips. */
const isNonBlank = (text: string) => text.trim() !== ''

/**
 * The per-commit side of the composer draft: mirrors the committed text into
 * ChatPage's `inputRef` during render and
 * hands each committed change to `onCommit` (persist the slot draft, feed the
 * history suggestions). A child of ChatPage, so its effect runs before the
 * page's own effects in the same commit. Renders nothing, and it is the only
 * part of the page that re-renders on a keystroke.
 */
export function ComposerDraftSync({ store, inputRef, onCommit }: {
  store: ComposerDraftStore
  inputRef: React.MutableRefObject<string>
  onCommit: (text: string) => void
}) {
  const text = useComposerDraft(store)
  inputRef.current = text
  useEffect(() => { onCommit(text) }, [text, onCommit])
  return null
}

interface ComposerDraftStateOptions {
  activeSlot: string | null
  drafts: MutableRefObject<Record<string, string>>
}

/**
 * The main composer's live draft: the draft store the composer subscribes to (so a
 * keystroke re-renders the composer, not the page), the refs every slot-switch guard
 * keys on (outgoing flush key, live persist key, mounted flag, active-slot mirror,
 * committed text), the dashboard-reply insert, and the in-flight send flag.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useComposerDraftState({
  activeSlot, drafts,
}: ComposerDraftStateOptions) {
  // Outgoing-slot flush key, advanced inside the slot-change effect after it
  // flushes that slot's draft. Distinct from composerSlotRef (the live persist
  // key); both must trail their writes or the draft smear returns.
  const prevSlot = useRef<string | null>(null)
  // Latest-value ref for `activeSlot`, updated every render. Used by async
  // upload callbacks (takeScreenshot, uploadFiles) to detect when the user
  // has switched slots between the initial click and the promise resolving,
  // so the uploaded file lands in the original slot's draft instead of
  // silently appearing in whatever slot is now active.
  const activeSlotRef = useRef(activeSlot); activeSlotRef.current = activeSlot
  // The slot the live composer state belongs to; the per-composer persist
  // effects key off this, not `activeSlot`. Advanced by a dedicated effect
  // declared AFTER those effects so a batched keystroke+switch can't smear one
  // slot's draft onto another. See that advance effect for the full rationale.
  const composerSlotRef = useRef(activeSlot)
  // False once this page is gone. A send can fail after the user has left the
  // chat route; the recovery then has no stage to restage a quote onto (a
  // setState on an unmounted hook is a silent no-op), so the quote goes into
  // the parked draft's text instead. `composerSlotRef` alone cannot tell: it
  // still names the slot after unmount.
  const composerMountedRef = useRef(true)
  useEffect(() => {
    composerMountedRef.current = true
    return () => { composerMountedRef.current = false }
  }, [])
  // The composer text lives in a store, not in this component's state: a
  // keystroke re-renders the composer that subscribes to it (`ChatInput` under
  // `<Composer draft>`), not this page. `setInput` keeps the `useState` setter
  // shape; code that acts on the text reads `inputRef`, and render-time facts
  // (blank or not, staged `@dir/` tokens) are selected below so the page
  // re-renders only when that fact changes.
  const composerDraft = useState(() => createComposerDraftStore(activeSlot ? drafts.current[activeSlot] ?? '' : ''))[0]
  const setInput = composerDraft.set
  // A dashboard page's reply lands in this chat's composer, merged after anything
  // already typed; the person reviews and sends it.
  const insertDashboardReply = useCallback((text: string) => {
    setInput(prev => mergeIntoDraft(prev, text))
    focusComposer()
  }, [setInput])
  const inputNonBlank = useComposerDraftSelector(composerDraft, isNonBlank)
  // The composer text as of the last committed draft render. `ComposerDraftSync`
  // (rendered by ChatPage) advances it; the mid-switch guards below write it
  // directly, and the next draft commit overwrites them.
  const inputRef = useRef(composerDraft.get())

  const sendingRef = useRef(false)
  return {
    prevSlot, activeSlotRef, composerSlotRef, composerMountedRef, composerDraft, setInput, insertDashboardReply, inputNonBlank, inputRef, sendingRef,
  }
}

/**
 * The state every outside-the-keyboard intake writes into: the prefill hint and its
 * expiry, the auto-send and app-launch send requests, the new-session and signed-token
 * guards, the widget prefill tag, and the consumed-prefill token.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useComposerIntakeRefs() {
  const [prefillHint, setPrefillHint] = useState(false)
  // Whether the user has edited the seeded composer. The hint's expiry is armed
  // by that first edit, not by the seed's arrival: a hand-off's error report is
  // a dozen lines the user reads before touching anything, and a clock started
  // at the seed collapsed the box from the prefill cap to the six-line typing
  // cap under them mid-read, taking the "pre-filled" explanation with it.
  const [prefillEdited, setPrefillEdited] = useState(false)
  const raisePrefillHint = useCallback(() => { setPrefillHint(true); setPrefillEdited(false) }, [])
  const autoSendRef = useRef<string | null>(null)
  const appLaunchSendRef = useRef<{ slotKey?: string } | null>(null)
  const [autoSendTick, setAutoSendTick] = useState(0)
  const newSessionRef = useRef(false)
  // True while the challenge-redirect token effect is creating/linking its
  // session. Blocks the auto-select effect from switching to a different slot
  // (which would orphan the freshly slack-linked session and break mirroring).
  const tokenConsumingRef = useRef(
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('token'),
  )
  // Holds the exact text a widget action pre-filled into the composer, so the
  // eventual user-initiated send can be tagged meta.origin='widget' for
 // forensic attribution. Set on widget pre-fill, consumed
  // and cleared in send(). A genuine from-scratch turn never sets this.
  const widgetPrefillRef = useRef<string | null>(null)
  // Token (`${slotKey}:${ts}`) of the most recently consumed composer prefill.
  // Guards the per-slot draft-restore effect against React.StrictMode's mount
  // double-invoke: the first invoke consumes+removes PREFILL_STORAGE_KEY and
  // seeds the composer, so without this the second invoke would find no stored
  // prefill and reset the composer to the (empty) incoming draft — the artifact
  // companion panel mounts ChatPage fresh, so it hits this double-invoke every
  // first open. See the draft-restore effect in composerDrafts.ts.
  const consumedPrefillRef = useRef<string | null>(null)

  // Auto-dismiss the prefill hint 10 seconds after the user starts editing the
  // seed. Until then it holds: the band and the taller cap are what let the
  // seeded text be read, and reading has no deadline.
  useEffect(() => {
    if (!prefillHint || !prefillEdited) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint, prefillEdited])
  return {
    prefillHint, setPrefillHint, prefillEdited, setPrefillEdited, raisePrefillHint, autoSendRef, appLaunchSendRef, autoSendTick, setAutoSendTick, newSessionRef, tokenConsumingRef, widgetPrefillRef,
    consumedPrefillRef,
  }
}

interface MainComposerHandoffOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  splitMode: boolean
  drafts: MutableRefObject<Record<string, string>>
  saveDraftsDebounced: () => void
  composerSlotRef: MutableRefObject<string | null>
  setInput: (next: string | ((prev: string) => string)) => void
  inputRef: MutableRefObject<string>
  mainComposerAppend: { slot: string; text: string; } | null
  raisePrefillHint: () => void
}

/**
 * Side Chat -> main composer hand-offs staged for this slot, appended to the live
 * draft (or, mid-switch, to the incoming slot's stored draft).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useMainComposerHandoff({
  dispatch, activeSlot, splitMode, drafts, saveDraftsDebounced, composerSlotRef, setInput, inputRef, mainComposerAppend, raisePrefillHint,
}: MainComposerHandoffOptions) {
  // Consume Side Chat → main composer hand-offs staged for THIS slot. APPEND
  // against the live input, never replace: the merge runs here because
  // `inputRef` is the only holder of unsent text, mirroring the follow-up
  // card's "add to this session". The `slot` guard means a hand-off staged in
  // a Crew Member's Side Chat (consumed by that member's ChatPane) is left
  // untouched here rather than landing in the dashboard composer. Pre-fills and
  // stops — the user sends when they choose, so it never touches a live turn.
  //
  // In split view this composer is unmounted (SessionGridView renders a
  // ChatInput per cell) and the grid ChatPane whose `slotKey === activeSlot`
  // is the real consumer. Since the anchor pane's slot IS `activeSlot`, the
  // slot guard alone would let both this effect and that pane consume the same
  // hand-off in one flush — the pane shows it while this effect also persists
  // it into the hidden single-session draft, resurfacing as a duplicate when
  // split view collapses. Skip entirely while split view is active so the grid
  // pane is the sole consumer.
  //
  // Mid-switch: this effect is declared before the slot-change restore and the
  // `composerSlotRef` advance, so when a hand-off lands in the same commit as a
  // switch INTO its slot, `activeSlot` already names the incoming slot while
  // `inputRef` still holds the OUTGOING slot's unsent text. Appending to that
  // would write the outgoing text under the incoming slot, which the restore
  // then shows and `flushDrafts()` persists — the incoming slot's own draft is
  // gone. Append to the incoming slot's stored draft instead; the restore that
  // runs next in this commit puts it on screen, and the outgoing slot's flush
  // (which reads `inputRef`) still sees only its own text. The hand-off is
  // consumed here either way: an early return would park it in the store with
  // nothing to re-run this effect (refs do not re-render).
  useEffect(() => {
    if (!chatPageShouldConsumeHandoff(splitMode, activeSlot, mainComposerAppend)) return
    const text = mainComposerAppend!.text
    dispatch(stageToMainComposer(null))
    const base = composerSlotRef.current === activeSlot ? inputRef.current : (drafts.current[activeSlot!] ?? '')
    const merged = mergeIntoDraft(base, text)
    setDraft(drafts.current, activeSlot!, merged)
    saveDraftsDebounced()
    setInput(merged)
    raisePrefillHint()
  }, [splitMode, mainComposerAppend, activeSlot, dispatch, drafts, saveDraftsDebounced, raisePrefillHint, setInput, composerSlotRef, inputRef])
}

interface ComposerVoiceHostOptions {
  embedded?: boolean
  drafts: MutableRefObject<Record<string, string>>
  saveDrafts: () => void
  activeSlotRef: MutableRefObject<string | null>
  composerSlotRef: MutableRefObject<string | null>
  inputRef: MutableRefObject<string>
}

/**
 * What the main composer hands the voice layer: where a transcript lands when the
 * composer is off screen, the caret it writes at, and auto-submit.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useComposerVoiceHost({
  embedded, drafts, saveDrafts, activeSlotRef, composerSlotRef, inputRef,
}: ComposerVoiceHostOptions) {
  const isMac = useAppSelector(s => s.dashboard.status?.platform) === 'darwin'
  // Voice dictation is the Composer's Voice atom (chat-core P3-b): ChatPage no
  // longer runs the hook or wires 23 props. It supplies, through the `Composer`
  // root ComposerDockView renders, only what the atom cannot know on its own: which slot the
  // composer currently shows (the draft-settlement predicate), where an
  // off-screen batch transcript goes (that slot's persisted draft), the
  // endpointer's auto-submit, and that this is the surface owning the
  // document-wide push-to-talk key. `composerRef` reaches the atom's controls
  // from send().
  //
  // Forward ref to the Enter-equivalent submit (assigned in ChatPage, after send
  // and steer) so the streaming endpointer's auto-submit callback — handed to
  // the atom here, above both — can fire it. Assigned in render after steer.
  const voiceSubmitRef = useRef<(() => void) | null>(null)
  const composerRef = useRef<ComposerHandle>(null)
  // Live composer caret, kept current by ChatInput; the resources controller
  // splices a picked file token at it, and dictation splices the transcript at
  // it — so ChatPage owns the refs and hands them to the atom.
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voicePendingCaretRef = useRef<number | null>(null)
  // Splice into the LIVE composer only when the target slot is both the active
  // slot AND the slot the composer's `input` currently belongs to. On a slot
  // switch, activeSlotRef updates synchronously in render, but the composer's
  // draft-restore + composerSlotRef advance run in LATER effects — splicing in
  // that unsettled window would let the pending draft restore overwrite the
  // transcript.
  const voiceIsComposerFor = useCallback((target: string | null) => target === activeSlotRef.current && composerSlotRef.current === target, [activeSlotRef, composerSlotRef])
  // Off-screen batch transcript: append to the target slot's persisted draft
  // (recoverable, shown on return). Mirrors handleOptimizeResult's cross-slot
  // routing.
  const voiceDeliverOffScreen = useCallback((target: string, append: (base: string) => string) => {
    const next = append(drafts.current[target] ?? '')
    setDraft(drafts.current, target, next)
    // Mid-switch guard: if the composer still belongs to `target` (activeSlot
    // has advanced in render but the outgoing-slot persist effect hasn't run
    // yet), that effect will flush inputRef.current into drafts[target] and
    // would overwrite this transcript with the pre-transcript input. Carry the
    // appended value into inputRef too so the flush preserves the transcript.
    if (composerSlotRef.current === target) inputRef.current = next
    saveDrafts()
  }, [saveDrafts, drafts, composerSlotRef, inputRef])
  const voiceAutoSubmit = useCallback(() => { voiceSubmitRef.current?.() }, [])
  const composerVoiceOptions = useMemo<ComposerVoiceOptions>(() => ({
    isComposerFor: voiceIsComposerFor,
    deliverOffScreen: voiceDeliverOffScreen,
    onAutoSubmit: voiceAutoSubmit,
    pushToTalk: true,
    caretRef: voiceCaretRef,
    pendingCaretRef: voicePendingCaretRef,
    settingsRoute: embedded ? '/embed/settings' : settingsPath({ tab: 'voice' }),
  }), [voiceIsComposerFor, voiceDeliverOffScreen, voiceAutoSubmit, embedded])
  return {
    isMac, voiceSubmitRef, composerRef, voiceCaretRef, voicePendingCaretRef, composerVoiceOptions,
  }
}
