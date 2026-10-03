import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'

import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import type { RootState } from '../../../store'
import { DRAFT_SAVE_DEBOUNCE_MS, loadDrafts, saveDrafts as persistDrafts, setDraft, appendTypedText, mergeIntoDraft, typedDuringCreate } from '../../../utils/chatDrafts'
import { registerMainComposer } from '../../../utils/composerRestore'
import { loadFileDrafts, saveFileDrafts as persistFileDrafts, setFileDraft } from '../../../utils/chatFileDrafts'
import { loadFileTokenDrafts, saveFileTokenDrafts as persistFileTokenDrafts } from '../../../utils/chatFileTokenDrafts'
import { loadPasteDrafts, savePasteDrafts as persistPasteDrafts, setPasteDraft } from '../../../utils/chatPasteDrafts'
import { loadSessionRefDrafts, saveSessionRefDrafts as persistSessionRefDrafts, setSessionRefDraft } from '../../../utils/chatSessionRefDrafts'
import { PREFILL_STORAGE_KEY } from '../../../utils/navIntent'
import { type PasteBlock, carryPastes, pruneBlocks as pruneBlocksUtil } from '../../../utils/pasteTokens'
import type { SessionRef } from '../../../utils/sessionRefs'
import type { useKnowledgeFetch } from '../useKnowledgeFetch'
import type { ComposerStaging } from './composerStaging'

/**
 * Per-slot composer drafts: the four stores (text, staged files, collapsed
 * pastes, session references), their persistence, and the slot-switch
 * lifecycle that saves the outgoing slot's composer and restores the incoming
 * one.
 *
 * ChatPage calls these hooks at the positions the inline code held, because
 * React runs effects in declaration order and that order is the contract: the
 * draft commit (`ComposerDraftSync`, a child, so its effect runs first) writes
 * the text against the slot the composer still belongs to, then the
 * slot-change restore below runs, then the staged-resource persist effects
 * (`useStagedDraftPersistence`), then the composer-key advance.
 */

/** What the composer has staged besides text (file paths and session-ref keys),
 *  for the create-carry check below: only a text-only draft carries. */
const stagedIdentity = (files: readonly string[] | undefined, sessions: readonly SessionRef[] | undefined) =>
  JSON.stringify([files ?? [], (sessions ?? []).map(r => r.key)])
const NOTHING_STAGED = stagedIdentity(undefined, undefined)

/** The per-slot draft stores, loaded once per mount, and their persistence. */
export function useComposerDraftStores() {
  const drafts = useRef<Record<string, string>>(null!)
  if (drafts.current === null) drafts.current = loadDrafts()
  const fileDrafts = useRef<Record<string, string[]>>(null!)
  if (fileDrafts.current === null) fileDrafts.current = loadFileDrafts()
  // Per-slot collapsed-paste blocks backing the `[ Paste #N · M lines ]` tokens
  // in `input`. Persisted (localStorage, same TTL as text drafts) so the chip
  // survives slot switches / refresh instead of degrading to literal text.
  const pasteDrafts = useRef<Record<string, PasteBlock[]>>(null!)
  if (pasteDrafts.current === null) pasteDrafts.current = loadPasteDrafts()
  // Per-slot session references staged by dragging a session onto this pane.
  // Persisted (sessionStorage) so a slot switch restores the refs belonging to
  // the slot being shown — which is also what stops one slot's staged refs from
  // smearing onto another.
  const sessionRefDrafts = useRef<Record<string, SessionRef[]>>(null!)
  if (sessionRefDrafts.current === null) sessionRefDrafts.current = loadSessionRefDrafts()
  const saveDraftsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveDrafts = useCallback(() => { persistDrafts(drafts.current); persistFileDrafts(fileDrafts.current); persistPasteDrafts(pasteDrafts.current); persistSessionRefDrafts(sessionRefDrafts.current); persistFileTokenDrafts(pickedFileTokens.current) }, [])
  const saveDraftsDebounced = useCallback(() => {
    if (saveDraftsTimer.current) clearTimeout(saveDraftsTimer.current)
    saveDraftsTimer.current = setTimeout(() => { saveDraftsTimer.current = null; saveDrafts() }, DRAFT_SAVE_DEBOUNCE_MS)
  }, [saveDrafts])
  const flushDrafts = useCallback(() => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    saveDrafts()
  }, [saveDrafts])
  // Exact `@rel` composer token recorded per PICKER-PICKED file, so the file
  // chip's remove control can strip precisely the token the pick inserted —
  // the same remove contract folder chips have. Uploaded/dropped files never
  // get an entry (they have no token), so their remove stays state-only. A
  // ref, not state: it never drives rendering. Entries die with their chip.
  //
  // Keyed by SLOT first, then absolute path, then an ARRAY of every `@rel`
  // alias ever recorded for that path in this slot (`pickedFileTokens.current
  // [slot][absPath] = ['@src/main.ts', '@main.ts']`) -- five review rounds on
  // progressively narrower shapes each found a different way the previous
  // shape lost information:
  //  1-3. A flat, path-only `Record<absPath, token>` let a DIFFERENT slot's
  //     pick/send/staging corrupt this slot's entry through the shared key.
  //     Moot once each slot owns its own sub-map, reachable ONLY through
  //     composerSlotRef -- a foreign slot's entry can never be read as this
  //     slot's own.
  //  4-5. Even slot-scoped, a single STRING per path only remembers the LAST
  //     alias recorded. If the slot's project changes and the same file gets
  //     a SECOND `@rel` alias (a fresh pick, or an already-typed mention in
  //     the new form), the second pick overwrote the first alias outright --
  //     deleting the NEW alias then unstaged the file even though the text
  //     still carried the ORIGINAL alias untouched. An array keeps every
  //     alias ever recorded; the file counts as mentioned if ANY of them is
  //     still in the text, and unstages only once NONE are.
  // Producer/consumer seam table (every site that writes or reads this map;
  // keep it current -- it is the one place the full population is enumerated):
  //   record:   recordSlotToken (file pick / typed-mention adoption)
  //   restore:  mergeSlotTokens <- transport-failure, queued-cancel stash,
  //             create-failure (all three recovery arms restore aliases)
  //   clear:    send-clear (captures sentSlotTokens first), slot teardown
  //   read:     reconciliation effect, insertion clamp,
  //             remove-chip strip, send-boundary replaceTokens
  //   persist:  saveDrafts writes it beside fileDrafts (chatFileTokenDrafts,
  //             sessionStorage) and mount / the slot-switch rehydrate load it,
  //             so a reloaded chip keeps its aliases
  //   outside:  steer
  //             (attachments discarded by design), split-view pane (no
  //             alias consumer -- see ChatPane's restoreDraft adapter)
  const pickedFileTokens = useRef<Record<string, Record<string, string[]>>>(null!)
  if (pickedFileTokens.current === null) pickedFileTokens.current = loadFileTokenDrafts()
  return { drafts, fileDrafts, pasteDrafts, sessionRefDrafts, pickedFileTokens, saveDraftsTimer, saveDrafts, saveDraftsDebounced, flushDrafts }
}

export type ComposerDraftStores = ReturnType<typeof useComposerDraftStores>

interface ComposerDraftLifecycleOptions {
  activeSlot: string | null
  stores: ComposerDraftStores
  /** Declared just before this hook: its state and refs, never its effects. */
  staging: ComposerStaging
  /** Outgoing-slot flush key, advanced inside the slot-change effect. */
  prevSlot: MutableRefObject<string | null>
  /** The slot the live composer state belongs to (advanced by `useStagedDraftPersistence`). */
  composerSlotRef: MutableRefObject<string | null>
  inputRef: MutableRefObject<string>
  setInput: ComposerDraftStore['set']
  /** StrictMode guard for the prefill hand-off; see ChatPage's note on it. */
  consumedPrefillRef: MutableRefObject<string | null>
  foregroundCreateId: RootState['chat']['foregroundCreateId']
  foregroundCreateIdRef: MutableRefObject<RootState['chat']['foregroundCreateId']>
  lastCreatedActivationRef: MutableRefObject<RootState['chat']['lastCreatedActivation']>
  knowledgeFetchRef: MutableRefObject<ReturnType<typeof useKnowledgeFetch>>
  raisePrefillHint: () => void
  setPrefillHint: Dispatch<SetStateAction<boolean>>
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
  updateHistoryQuery: (text: string) => void
}

/**
 * The composer's per-commit and per-switch draft bookkeeping: the draft-commit
 * sink, the create-carry, the slot-switch save/restore (which also consumes the
 * keyed prefill hand-off), and the unmount / tab-close flushes.
 */
export function useComposerDraftLifecycle({
  activeSlot,
  stores,
  staging,
  prevSlot,
  composerSlotRef,
  inputRef,
  setInput,
  consumedPrefillRef,
  foregroundCreateId,
  foregroundCreateIdRef,
  lastCreatedActivationRef,
  knowledgeFetchRef,
  raisePrefillHint,
  setPrefillHint,
  setActionError,
  updateHistoryQuery,
}: ComposerDraftLifecycleOptions) {
  const { drafts, fileDrafts, pasteDrafts, sessionRefDrafts, pickedFileTokens, saveDraftsTimer, saveDraftsDebounced, flushDrafts } = stores
  const {
    stagedNowRef, pendingFilesRef, pasteBlocksRef, pendingSessionsRef,
    setPendingFiles, setPasteBlocks, setPendingSessions, setUploadError, setUploadHint,
  } = staging
  // Persist the composer text against the slot it BELONGS to (composerSlotRef),
  // not the live activeSlot (see the composerSlotRef note in ChatPage).
  // The draft key is composerSlotRef, which a ref does not need to be a
  // dependency of; the slot-change effect below handles the transition.
  // Runs from `ComposerDraftSync`'s effect, a child of this page, so it lands
  // before this page's own effects in the same commit: ahead of the slot-change
  // and composerSlotRef-advance effects, which is what keeps a keystroke batched
  // with a switch on the slot it was typed in.
  // Set by the file-chip reconciliation (composerFileMentions'
  // useFileMentionActions); called on every committed draft text.
  const reconcileFileChipsRef = useRef<((text: string) => void) | null>(null)
  const onComposerDraftCommit = useCallback((text: string) => {
    inputRef.current = text
    const s = composerSlotRef.current
    if (s) { setDraft(drafts.current, s, text); saveDraftsDebounced() }
    updateHistoryQuery(text)
    reconcileFileChipsRef.current?.(text)
  }, [saveDraftsDebounced, updateHistoryQuery, inputRef, composerSlotRef, drafts])
  // Create-carry. The composer stays bound to the old slot until a create
  // resolves, so anything typed in that window (a fast typist after the new-chat
  // shortcut, or a click into the composer while the POST is slow) lands in the
  // OLD slot's draft, and the activation then restores the new slot's empty draft
  // over it: the text vanishes. Snapshot the composer when a create starts; the
  // slot-change effect below moves only what was typed since into the new slot
  // and puts the old slot's draft back as it was. Declared after the persist
  // effect above, so `inputRef` already reflects a composer cleared in the same
  // commit (the send path clears it before its create), and before the
  // slot-change effect, which consumes the snapshot.
  // Snapshots are kept per create requestId, so overlapping creates (two quick
  // shortcut presses) each keep their own, and an activation consumes only the
  // one its own create took. The snapshot also records the staged attachments
  // and session refs: if any are staged when the create starts or when it
  // activates, the carry is skipped and the whole draft stays together in the
  // old session, as it did before the carry existed, rather than moving the
  // text without its files.
  type CreateCarry = { origin: string | null; baseline: string; staged: string }
  const createCarryRef = useRef<Map<string, CreateCarry>>(new Map())
  // Creates that write their own composer content and must never carry: the
  // Slack-link token flow sets its prompt with setInput and auto-sends it, so
  // carried text would be overwritten or sent. Typed text stays in the old
  // session's draft for those, as on main.
  const noCarryCreateIdsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (foregroundCreateId && !noCarryCreateIdsRef.current.has(foregroundCreateId)) {
      createCarryRef.current.set(foregroundCreateId, {
        origin: composerSlotRef.current, baseline: inputRef.current, staged: stagedIdentity(stagedNowRef.current.files, stagedNowRef.current.sessions),
      })
    }
  }, [foregroundCreateId, composerSlotRef, inputRef, stagedNowRef])
  // Per-slot draft: save current → restore target (persisted to localStorage)
  useEffect(() => {
    // Re-hydrate from localStorage — only pull in keys we don't already have
    // in-memory, so unflushed drafts from rapid slot switches aren't clobbered.
    const stored = loadDrafts()
    for (const [k, v] of Object.entries(stored)) { if (!(k in drafts.current)) drafts.current[k] = v }
    const storedFiles = loadFileDrafts()
    for (const [k, v] of Object.entries(storedFiles)) { if (!(k in fileDrafts.current)) fileDrafts.current[k] = v }
    const storedFileTokens = loadFileTokenDrafts()
    for (const [k, v] of Object.entries(storedFileTokens)) { if (!(k in pickedFileTokens.current)) pickedFileTokens.current[k] = v }
    const storedPastes = loadPasteDrafts()
    for (const [k, v] of Object.entries(storedPastes)) { if (!(k in pasteDrafts.current)) pasteDrafts.current[k] = v }
    const storedSessionRefs = loadSessionRefDrafts()
    for (const [k, v] of Object.entries(storedSessionRefs)) { if (!(k in sessionRefDrafts.current)) sessionRefDrafts.current[k] = v }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    const stagedNow = stagedNowRef.current
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, stagedNow.files)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, stagedNow.pastes)
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, stagedNow.sessions)
    const prevSlotVal = prevSlot.current
    prevSlot.current = activeSlot
    // Create-carry (see createCarryRef): only on the transition the create itself
    // made, from the slot the snapshot was taken on. A switch the user made while
    // the create was pending keeps the create from activating at all, so it never
    // matches here and a plain switch restores drafts exactly as before.
    const activation = lastCreatedActivationRef.current
    const carry = activation && activation.slot === activeSlot ? createCarryRef.current.get(activation.requestId) : undefined
    // A consumed snapshot is spent. The others survive ordinary switches, so
    // leaving and returning to the origin while a create is pending keeps the
    // baseline the create started from. The map stays small: an entry is
    // added per create, and the oldest are dropped past a handful.
    if (carry && activation) createCarryRef.current.delete(activation.requestId)
    while (createCarryRef.current.size > 8) createCarryRef.current.delete(createCarryRef.current.keys().next().value as string)
    let carried: string | null = null
    // Carry only a text-only draft: nothing staged when the create started and
    // nothing staged now. A file or session ref staged at either end belongs
    // with the caption, so the whole draft stays in the old session instead.
    const nothingStaged = !!carry && carry.staged === NOTHING_STAGED && stagedIdentity(stagedNow.files, stagedNow.sessions) === NOTHING_STAGED
    if (carry && activeSlot && prevSlotVal === carry.origin && nothingStaged) {
      const typed = typedDuringCreate(carry.baseline, inputRef.current)
      if (typed !== null) {
        // A large paste in the window became a `[ Paste #N ]` token whose block
        // sits in the old slot's paste list. The token and its block move
        // together, renumbered against the new slot's own blocks, or the send
        // would carry the bare token and the content would belong to no draft.
        const blocks = stagedNow.pastes
        const moved = carryPastes(typed, pruneBlocksUtil(typed, blocks), pasteDrafts.current[activeSlot] ?? [])
        carried = moved.text
        setPasteDraft(pasteDrafts.current, activeSlot, moved.pastes)
        if (prevSlotVal) {
          setDraft(drafts.current, prevSlotVal, carry.baseline)
          setPasteDraft(pasteDrafts.current, prevSlotVal, pruneBlocksUtil(carry.baseline, blocks))
        }
      }
    }
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    const storedDraft = activeSlot ? drafts.current[activeSlot] ?? '' : ''
    const draftFallback = carried !== null ? appendTypedText(storedDraft, carried) : storedDraft
    // What this switch put in the composer, for the create-carry re-arm below.
    let restoredInput: string | null = null
    const restoreInput = (value: string) => { restoredInput = value; setInput(value) }
    // The prefill hint describes THIS composer's seeded text. A switch that
    // restores a plain draft drops it; the hint no longer expires on its own
    // clock, so without this it would follow the user to an unrelated session.
    let seeded = false
    if (raw) {
      try {
        const { slotKey, prompt, ts } = JSON.parse(raw)
        if (Date.now() - (ts ?? 0) > 30_000) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); restoreInput(draftFallback) }
        else if (slotKey === activeSlot) {
          sessionStorage.removeItem(PREFILL_STORAGE_KEY)
          consumedPrefillRef.current = `${slotKey}:${ts}`
          // A launcher seed and text typed during its create are both the user's;
          // neither may overwrite the other.
          restoreInput(carried !== null ? appendTypedText(prompt, carried) : prompt)
          // Same hint the pendingInput and widget paths raise: it is what lifts the
          // composer from its ~6-line typing cap to the prefill cap. Without it a
          // hand-off's error report (13+ lines) sat in a 140px box showing only its
          // tail, and nothing on the page said the composer had been seeded at all.
          raisePrefillHint()
          seeded = true
        }
        else { restoreInput(draftFallback) }
      } catch { sessionStorage.removeItem(PREFILL_STORAGE_KEY); restoreInput(draftFallback) }
    } else if (prevSlotVal === activeSlot && !!activeSlot && consumedPrefillRef.current?.startsWith(`${activeSlot}:`)) {
      // (see the note below) -- the composer still holds the seed, so the hint
      // it arrived with stays too.
      seeded = true
      // StrictMode re-invoked this mount effect for the SAME active slot after
      // the first invoke already consumed+removed the prefill. The composer
      // already holds the staged prompt; a setInput(draftFallback) here would
      // wipe it back to the empty draft. Leave the composer as-is. (A genuine
      // slot switch changes activeSlot, so prevSlotVal !== activeSlot and this
      // branch cannot mask a real draft restore.)
    } else { restoreInput(draftFallback) }
    // The ?new=1 flow parks on a null slot while its create is in flight, so
    // the activation comes from no slot. Only that switch, onto no slot,
    // re-arms the snapshot against the composer it just restored; an ordinary
    // switch keeps the snapshot the create took.
    const pendingCreate = foregroundCreateIdRef.current
    if (!activeSlot && pendingCreate && !noCarryCreateIdsRef.current.has(pendingCreate)) {
      createCarryRef.current.set(pendingCreate, {
        origin: activeSlot,
        baseline: restoredInput ?? inputRef.current,
        // What this switch is about to stage, not what the outgoing slot had.
        staged: stagedIdentity(activeSlot ? fileDrafts.current[activeSlot] : undefined, activeSlot ? sessionRefDrafts.current[activeSlot] : undefined),
      })
    }
    if (!seeded) setPrefillHint(false)
    // Restore the incoming slot's staged file attachments (copy so the
    // live state array and the stored draft don't share a reference).
    setPendingFiles(activeSlot ? (fileDrafts.current[activeSlot] ?? []).slice() : [])
    // Staged folder references need no restore of their own: the chips derive
    // from `@rel/` tokens in the composer text, and the text draft restored
    // above is per-slot. A folder staged in slot A therefore reappears with
    // slot A's draft and never bleeds into slot B.
    // Restore the incoming slot's collapsed-paste blocks (deep copy so the live
    // state and the stored draft don't share references). Without this the
    // token text rehydrates from the text draft but its backing block is gone,
    // leaving a dead `[ Paste #N · M lines ]` literal in the input.
    setPasteBlocks(activeSlot
      ? (pasteDrafts.current[activeSlot] ?? []).map(b => ({ ...b }))
      : [])
    // Restore the incoming slot's staged session references (copy per record so
    // the live state and the stored draft never share a reference).
    setPendingSessions(activeSlot
      ? (sessionRefDrafts.current[activeSlot] ?? []).map(r => ({ ...r }))
      : [])
    knowledgeFetchRef.current.clearResults()
    setUploadError('')
    setUploadHint('')
    // A pane-level action failure ("Fork failed", "Could not read …") belongs to
    // the slot it happened in; carried over, it reads as the new slot's.
    setActionError(prev => prev?.preserveOnSwitch ? prev : null)
    flushDrafts()
  }, [activeSlot, flushDrafts, raisePrefillHint, setInput,
    // Refs and state setters: stable, so the effect still runs on a slot change only.
    drafts, fileDrafts, pasteDrafts, sessionRefDrafts, pickedFileTokens, prevSlot, stagedNowRef, lastCreatedActivationRef, inputRef,
    consumedPrefillRef, foregroundCreateIdRef, setPendingFiles, setPasteBlocks, setPendingSessions, knowledgeFetchRef,
    setUploadError, setUploadHint, setActionError, setPrefillHint])
  // Persist drafts on unmount (navigating away from chat page)
  useEffect(() => () => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
    flushDrafts()
  }, [flushDrafts, saveDraftsTimer, prevSlot, drafts, inputRef, fileDrafts, pendingFilesRef, pasteDrafts, pasteBlocksRef, sessionRefDrafts, pendingSessionsRef])
  // Flush pending draft save on tab close / refresh (debounce may not fire)
  useEffect(() => {
    const h = () => {
      if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
      if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
      if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
      if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
      flushDrafts()
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [flushDrafts, prevSlot, drafts, inputRef, fileDrafts, pendingFilesRef, pasteDrafts, pasteBlocksRef, sessionRefDrafts, pendingSessionsRef])
  // Answers a retired question card hands back land in this composer or in that slot's stored draft.
  useEffect(() => registerMainComposer((slot, text, showingOnly) => {
    if (slot === composerSlotRef.current) { setInput(prev => mergeIntoDraft(prev, text)); return true }
    if (showingOnly) return false
    setDraft(drafts.current, slot, mergeIntoDraft(drafts.current[slot], text))
    saveDraftsDebounced()
    return true
  }), [composerSlotRef, setInput, drafts, saveDraftsDebounced])
  return { reconcileFileChipsRef, onComposerDraftCommit, noCarryCreateIdsRef }
}

interface StagedDraftPersistenceOptions {
  activeSlot: string | null
  composerSlotRef: MutableRefObject<string | null>
  stores: ComposerDraftStores
  staging: ComposerStaging
}

/**
 * Live-persists each staged resource under the slot the composer belongs to,
 * then advances that key. Called after the slot-change restore in
 * `useComposerDraftLifecycle` and after everything else that stages: a change
 * batched with a switch is written against the OUTGOING slot first.
 */
export function useStagedDraftPersistence({ activeSlot, composerSlotRef, stores, staging }: StagedDraftPersistenceOptions) {
  const { fileDrafts, pasteDrafts, sessionRefDrafts, saveDraftsDebounced } = stores
  const { pendingFiles, pendingFilesRef, pasteBlocks, pasteBlocksRef, pendingSessions, pendingSessionsRef } = staging
  useEffect(() => {
    pendingFilesRef.current = pendingFiles
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setFileDraft(fileDrafts.current, s, pendingFiles)
      saveDraftsDebounced()
    }
    // Draft key is composerSlotRef; the slot-change effect handles that
    // transition.
  }, [pendingFiles, saveDraftsDebounced, pendingFilesRef, composerSlotRef, fileDrafts])
  useEffect(() => {
    pasteBlocksRef.current = pasteBlocks
    // Live-persist the composer's blocks so a slot switch / refresh restores
    // them alongside the text draft (mirrors the pendingFiles effect above).
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setPasteDraft(pasteDrafts.current, s, pasteBlocks)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
  }, [pasteBlocks, saveDraftsDebounced, pasteBlocksRef, composerSlotRef, pasteDrafts])
  useEffect(() => {
    pendingSessionsRef.current = pendingSessions
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setSessionRefDraft(sessionRefDrafts.current, s, pendingSessions)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
  }, [pendingSessions, saveDraftsDebounced, pendingSessionsRef, composerSlotRef, sessionRefDrafts])
  // Advance the composer draft key AFTER the three persist effects above. React
  // runs effects in declaration order, so on a slot switch each persist effect
  // has already written its changed value against the OUTGOING slot before this
  // repoints the key at the incoming one. Declared last on purpose. Moving it
  // earlier (or back into the slot-change effect) would let a file/paste change
  // batched with the switch smear onto the new slot.
  useEffect(() => { composerSlotRef.current = activeSlot }, [activeSlot, composerSlotRef])
}
