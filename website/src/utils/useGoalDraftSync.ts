/**
 * The Set a goal editor's draft sync, owned by `AutoNudgePopover`: the
 * check/save/updated/failed state, the open-edge check against the shared
 * server copy, the debounced save and close flush, Retry, and the conflict
 * actions' refs. The popover calls `useGoalDraftSync` and `useGoalDraftAutosave`
 * where this code sat, so its effects run in the same order as before.
 */
import { type MutableRefObject, useEffect, useRef, useState } from 'react'
import { TAB_ID } from '../api/tabId'
import type { AutoNudgeLoop } from '../components/autoNudgeLoop'
import { DRAFT_SAVE_DEBOUNCE_MS } from './draftConstants'
import {
  adoptGoalDraft,
  clearPendingGoalDraft,
  confirmedGoalDraft,
  deleteLegacyGoalDraft,
  hasUnansweredGoalDraftClear,
  loadLegacyGoalDraft,
  loadPendingGoalDraft,
  loadRemoteGoalDraft,
  pendingGoalDraftClear,
  pendingRemoteGoalDraftWrite,
  resendUnansweredGoalDraftClear,
  sameGoalDraft,
  savePendingGoalDraft,
  syncGoalDraft,
  type GoalDraft,
  type GoalDraftSnapshot,
  type GoalDraftSyncOutcome,
} from './goalDrafts'

/**
 * `failed` and `clear-failed` are the same recovery (the edit stays pending and
 * Retry reruns the check) with different copy: one says a draft is still held
 * here, the other that a clear did not finish. The copy must follow WHAT failed
 * to sync, or a failed tombstone would tell the user a draft exists while the
 * editor shows the cleared template.
 */
export type DraftSyncState = 'idle' | 'checking' | 'saving' | 'updated' | 'failed' | 'conflict' | 'clear-failed'

function syncFailureFor(draft: GoalDraft | null | undefined): DraftSyncState {
  return draft === null ? 'clear-failed' : 'failed'
}

/** The editor's fields as `AutoNudgePopover` keeps them current every render. */
export interface GoalEditorFields {
  slotKey: string
  message: string
  idleInput: string
  maxCyclesInput: string
  loop: AutoNudgeLoop | null
  open: boolean
}

/** What the popover hands the hook: its slot, field refs and setters. */
export interface GoalDraftSyncInputs {
  slotKey: string
  latest: MutableRefObject<GoalEditorFields>
  hasEdited: MutableRefObject<boolean>
  applyDraft: (draft: GoalDraft | null) => void
  draftToPersist: (s: GoalEditorFields) => GoalDraft | null
  onOpenChange: (open: boolean) => void
}

/** The open edge's Retry intent, read once by `beginOpenDraftSync`. */
export interface GoalDraftOpening {
  generation: number
  replaceConflict: boolean
  adoptCanonical: boolean
}

export function useGoalDraftSync({
  slotKey,
  latest,
  hasEdited,
  applyDraft,
  draftToPersist,
  onOpenChange,
}: GoalDraftSyncInputs) {
  const [draftSyncState, setDraftSyncState] = useState<DraftSyncState>('idle')
  const [draftSyncAttempt, setDraftSyncAttempt] = useState(0)
  // The conflict's canonical record is a Clear from another device.
  const [conflictCleared, setConflictCleared] = useState(false)
  // The canonical record the editor last confirmed or adopted. Start runs only
  // while the editor holds exactly this text and no save is in flight.
  const [confirmed, setConfirmed] = useState<GoalDraftSnapshot | null>(null)
  // Async clear completion must distinguish its own reset from text typed across
  // either request boundary even when React batches the parent loop update.
  const editRevision = useRef(0)
  const syncGeneration = useRef(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const closeAfterClearRetry = useRef(false)
  const replaceConflictOnRetry = useRef(false)
  const adoptCanonicalOnRetry = useRef(false)

  /** Enter the conflict state; `canonical` is the record it is against, when known. */
  function showConflict(canonical?: GoalDraftSnapshot | null) {
    setConflictCleared(canonical?.draft === null)
    setDraftSyncState('conflict')
  }

  function editorIsCurrent(targetSlot: string): boolean {
    return mounted.current && latest.current.open && latest.current.slotKey === targetSlot && !latest.current.loop
  }

  /** Save the editor's draft: kept here as the pending edit first, so a failed
   *  save leaves it recoverable, then sent behind the slot's earlier writes. */
  function persistDraft(targetSlot: string, draft: GoalDraft | null, keepalive = false): Promise<GoalDraftSyncOutcome> {
    const generation = syncGeneration.current
    savePendingGoalDraft(targetSlot, draft)
    if (!keepalive) setDraftSyncState(state => (state === 'idle' || state === 'updated' ? 'saving' : state))
    return syncGoalDraft(targetSlot, draft, { keepalive }).then(outcome => {
      if (keepalive || generation !== syncGeneration.current || !editorIsCurrent(targetSlot)) return outcome
      // The editor still shows what was sent: that edit is saved or pending, so
      // the debounce has nothing new to send until the next keystroke.
      const sentIsShown = sameGoalDraft(draftToPersist(latest.current), draft)
      if (sentIsShown) hasEdited.current = false
      if (outcome.result === 'persisted') {
        setConfirmed(outcome.canonical)
        // The store took this editor's draft, so a failure or conflict notice ends.
        setDraftSyncState(state => (state === 'checking' ? state : 'idle'))
      } else if (outcome.result === 'conflict') {
        // A conflict rejects the base this editor still writes from, so it shows
        // even when the text changed after the submit.
        showConflict(outcome.canonical)
      } else {
        // A plain failure describes only the text that was sent.
        setDraftSyncState(state => (state !== 'saving' ? state : sentIsShown ? syncFailureFor(draft) : 'idle'))
      }
      return outcome
    })
  }

  function persistCurrentDraft(s: GoalEditorFields, keepalive = false) {
    return persistDraft(s.slotKey, draftToPersist(s), keepalive)
  }

  /** Keep the editor's edit here at once, a save in flight or not; only the network save waits. */
  function keepCurrentDraft(s: GoalEditorFields) {
    savePendingGoalDraft(s.slotKey, draftToPersist(s))
  }

  /** The open edge's close half: end every async continuation and Retry intent. */
  function closeDraftSync() {
    syncGeneration.current += 1
    closeAfterClearRetry.current = false
    replaceConflictOnRetry.current = false
    adoptCanonicalOnRetry.current = false
    setDraftSyncState('idle')
  }

  /** The open edge's first half, before the popover seeds a loop or the draft. */
  function beginOpenDraftSync(): GoalDraftOpening {
    const generation = ++syncGeneration.current
    const replaceConflict = replaceConflictOnRetry.current
    replaceConflictOnRetry.current = false
    const adoptCanonical = adoptCanonicalOnRetry.current
    adoptCanonicalOnRetry.current = false
    hasEdited.current = false
    return { generation, replaceConflict, adoptCanonical }
  }

  /** The open edge with no loop: show what this browser holds, read-only, while
   *  the shared copy is checked, then show the canonical record. Returns the
   *  effect's cleanup. */
  function reconcileOnOpen({ generation, replaceConflict, adoptCanonical }: GoalDraftOpening) {
    const seen = confirmedGoalDraft(slotKey)
    const seedPending = loadPendingGoalDraft(slotKey)
    // Another tab's unconfirmed edit shown here becomes this tab's own, on the
    // version it was made on, so text typed over it is saved on that version too.
    if (seedPending && seedPending.tab !== TAB_ID) savePendingGoalDraft(slotKey, seedPending.draft, seedPending.base)
    // An unanswered Clear never shows the goal it cleared, not even while checking.
    applyDraft(seedPending ? seedPending.draft
      : hasUnansweredGoalDraftClear(slotKey) ? null
        : seen ? seen.draft : loadLegacyGoalDraft(slotKey))
    setConfirmed(null)
    setDraftSyncState('checking')
    const stillCurrent = () => syncGeneration.current === generation && editorIsCurrent(slotKey)
    /** Show the canonical record and make it the one Start runs. `read` marks
     *  a record the open-time read brought in rather than this tab's own save. */
    const showCanonical = (canonical: GoalDraftSnapshot, read = false) => {
      adoptGoalDraft(slotKey, canonical)
      applyDraft(canonical.draft)
      setConfirmed(canonical)
      if (closeAfterClearRetry.current && canonical.draft === null) {
        closeAfterClearRetry.current = false
        setDraftSyncState('idle')
        onOpenChange(false)
        return
      }
      setDraftSyncState(read && seen && !sameGoalDraft(seen.draft, canonical.draft) ? 'updated' : 'idle')
    }
    void (async () => {
      try {
        // A Clear on the loop this editor replaced is still settling: the
        // loop's removal remounted the editor, possibly before its DELETE
        // answered. Its outcome is this editor's, and the shared copy is read
        // only once the Clear has settled.
        const clearing = pendingGoalDraftClear(slotKey)
        if (clearing) {
          const cleared = await clearing
          if (!stillCurrent()) return
          const typedOverClear = loadPendingGoalDraft(slotKey)?.draft
          if (!typedOverClear) {
            applyDraft(null)
            if (cleared === 'persisted') {
              setConfirmed(confirmedGoalDraft(slotKey) ?? null)
              setDraftSyncState('idle')
              onOpenChange(false)
            } else {
              setDraftSyncState('clear-failed')
            }
            return
          }
          applyDraft(typedOverClear)
        }
        // A Clear whose loop removal got no answer holds the slot: the editor
        // shows the failed Clear, never the goal it cleared, so Start stays
        // disabled. Retry re-sends the removal, and only its answer ends that.
        if (hasUnansweredGoalDraftClear(slotKey)) {
          const answered = closeAfterClearRetry.current && await resendUnansweredGoalDraftClear(slotKey)
          if (!stillCurrent()) return
          if (!answered) {
            applyDraft(loadPendingGoalDraft(slotKey)?.draft ?? null)
            setDraftSyncState('clear-failed')
            return
          }
        }
        // A write this tab queued for the slot is still in flight, typically the
        // previous visit's close flush. Read earlier, the shared copy is the one
        // that write replaces.
        const queued = pendingRemoteGoalDraftWrite(slotKey)
        if (queued) {
          await queued
          if (!stillCurrent()) return
        }
        const canonical = await loadRemoteGoalDraft(slotKey)
        if (!stillCurrent()) return
        const legacy = loadLegacyGoalDraft(slotKey)
        if (legacy) deleteLegacyGoalDraft(slotKey)
        // "Use newer goal": the canonical record replaces this browser's edit,
        // and nothing is written.
        if (adoptCanonical) {
          clearPendingGoalDraft(slotKey)
          showCanonical(canonical)
          return
        }
        // A pre-sync browser's draft fills an empty shared slot, once.
        const pending = loadPendingGoalDraft(slotKey)
          ?? (legacy && canonical.updatedAt === 0 ? savePendingGoalDraft(slotKey, legacy, 0) : null)
        if (!pending) {
          showCanonical(canonical, true)
          return
        }
        // An unconfirmed edit is saved on the version it was made on, or with
        // "Replace with my text" on the record just read. A save the store
        // already took answers that record unchanged.
        applyDraft(pending.draft)
        const outcome = await syncGoalDraft(slotKey, pending.draft, {
          base: replaceConflict ? canonical.updatedAt : pending.base,
        })
        if (!stillCurrent()) return
        if (outcome.result === 'persisted') showCanonical(outcome.canonical)
        else if (outcome.result === 'conflict') showConflict(outcome.canonical ?? canonical)
        else setDraftSyncState(syncFailureFor(pending.draft))
      } catch {
        if (stillCurrent()) setDraftSyncState(syncFailureFor(loadPendingGoalDraft(slotKey)?.draft))
      }
    })()
    return () => {
      if (syncGeneration.current === generation) syncGeneration.current += 1
    }
  }

  /** A keystroke in the goal text or a number field. A failure or conflict
   *  stays in place until a save is confirmed; only the Updated line clears. */
  function editField() {
    editRevision.current += 1
    hasEdited.current = true
    closeAfterClearRetry.current = false
    setDraftSyncState(state => (state === 'updated' ? 'idle' : state))
  }

  /** Retry after a failure, or "Replace with my text" on a conflict. Both keep
   *  the editor's text as the pending edit and rerun the open-time check. Only a
   *  conflict this notice showed may be replaced: a retry after a plain sync
   *  failure re-reads first, and a newer goal saved elsewhere comes back as that
   *  conflict. */
  function retryDraftSync(replaceConflict: boolean) {
    const typed = draftToPersist(latest.current)
    const pending = loadPendingGoalDraft(slotKey)
    if (hasEdited.current && !(pending && sameGoalDraft(typed, pending.draft))) savePendingGoalDraft(slotKey, typed)
    closeAfterClearRetry.current = draftSyncState === 'clear-failed'
    replaceConflictOnRetry.current = replaceConflict
    adoptCanonicalOnRetry.current = false
    setDraftSyncState('checking')
    setDraftSyncAttempt(attempt => attempt + 1)
  }

  return {
    draftSyncState,
    setDraftSyncState,
    draftSyncAttempt,
    setDraftSyncAttempt,
    conflictCleared,
    confirmed,
    editRevision,
    mounted,
    closeAfterClearRetry,
    replaceConflictOnRetry,
    adoptCanonicalOnRetry,
    persistDraft,
    persistCurrentDraft,
    keepCurrentDraft,
    closeDraftSync,
    beginOpenDraftSync,
    reconcileOnOpen,
    editField,
    retryDraftSync,
    showConflict,
    hasEdited,
    latest,
  }
}

/** The close flush and the debounced save, at the popover's original place for them. */
export function useGoalDraftAutosave(
  { draftSyncState, hasEdited, latest, persistCurrentDraft, keepCurrentDraft }: ReturnType<typeof useGoalDraftSync>,
  { open, slotKey, message, idleInput, maxCyclesInput, loop }: GoalEditorFields,
) {
  // Flush a pending debounced edit when the popover closes or unmounts: kept
  // here at once, then a keepalive request for the shared copy.
  useEffect(() => {
    if (!open) return
    // The popover writes both refs on every edit; the cleanup reads them at close time.
    const edited = hasEdited
    const fields = latest
    return () => {
      if (!edited.current || fields.current.loop) return
      void persistCurrentDraft(fields.current, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stable cleanup reading the latest ref
  }, [open])

  // Persist genuine edits per slot, debounced with the same interval as chat
  // drafts. One save at a time: the next waits for the previous answer, which
  // is the base it is made on.
  const holdDraftSave = draftSyncState === 'checking' || draftSyncState === 'saving'
  useEffect(() => {
    if (!open || !hasEdited.current || loop) return
    keepCurrentDraft(latest.current)
    if (holdDraftSave) return
    const timer = setTimeout(
      () => { void persistCurrentDraft(latest.current) },
      DRAFT_SAVE_DEBOUNCE_MS,
    )
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `draftToPersist` is a pure transform of the ref snapshot it is handed, redeclared each render, so its identity carries no information the deps above miss. Depending on it would restart the debounce timer on every unrelated re-render — the coalescing this effect exists for.
  }, [open, slotKey, message, idleInput, maxCyclesInput, loop, holdDraftSave])
}
