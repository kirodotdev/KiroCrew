import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { queueEntryAttachments } from '../store/chatSlice'
import { restoreQueuedContent } from '../utils/fileTokens'
import { revealComposer } from '../pages/chat/composerFocus'
import type { MessageQuote } from '../chat-core/composer/messageQuote'
import { DRAFT_TTL_MS } from '../utils/draftConstants'
import { safeGetItem, safeRemoveItem } from '../utils/safeStorage'
import { createSlotDraftStore } from '../utils/slotDraftStore'
import type { ChatMessage } from '../types'

/** What the person asked to do with a steer the running turn has not read yet. */
export type SteerRetractAction = 'unsend' | 'edit' | 'queue'

/** Why a take-back did not happen, when the row itself does not already say so. */
export type SteerRetractRefusal = 'busy' | 'unsupported' | 'failed'

/** One row's refused take-back: why, and which action was picked, so the notice names that action. */
export interface SteerRetractRefusalNotice {
  refusal: SteerRetractRefusal
  action: SteerRetractAction
}

/** A take-back in flight: what was asked, and the row as it stood when asked, so
 *  an Edit can put the text back in the composer once the row says it was taken. */
interface PendingTakeBack {
  slot: string
  mid: string
  action: SteerRetractAction
  content: string
  meta: Record<string, unknown> | undefined
  observedRunning: boolean
  requestFailed: boolean
}

const TAKE_BACKS_STORAGE_KEY = 'mc-steer-take-backs'
const TAKE_BACKS_TIMESTAMPS_KEY = `${TAKE_BACKS_STORAGE_KEY}-ts`
const TAKE_BACK_ACTIONS = new Set<SteerRetractAction>(['unsend', 'edit', 'queue'])

function sanitizePendingTakeBack(value: unknown): PendingTakeBack | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (
    typeof record.slot !== 'string' || !record.slot ||
    typeof record.mid !== 'string' || !record.mid ||
    typeof record.action !== 'string' || !TAKE_BACK_ACTIONS.has(record.action as SteerRetractAction) ||
    typeof record.content !== 'string' ||
    typeof record.observedRunning !== 'boolean' ||
    typeof record.requestFailed !== 'boolean'
  ) return null
  if (record.meta !== undefined && (!record.meta || typeof record.meta !== 'object' || Array.isArray(record.meta))) return null
  return {
    slot: record.slot,
    mid: record.mid,
    action: record.action as SteerRetractAction,
    content: record.content,
    meta: record.meta ? { ...(record.meta as Record<string, unknown>) } : undefined,
    observedRunning: record.observedRunning,
    requestFailed: record.requestFailed,
  }
}

const takeBackStore = createSlotDraftStore<PendingTakeBack>({
  key: TAKE_BACKS_STORAGE_KEY,
  storage: 'local',
  ttlMs: DRAFT_TTL_MS,
  // A record is the only copy of a taken-back Edit until its row settles, so never evict it; a write that does not fit refuses the Edit before sending it.
  sanitize: sanitizePendingTakeBack,
})

/** One recovery record per slot, independent of the currently mounted chat. */
const pendingTakeBackBySlot = new Map<string, PendingTakeBack>()

/** Take-backs started here, unlike recovery records loaded from another tab. */
const locallyStartedTakeBackBySlot = new Map<string, PendingTakeBack>()

/** Recovery writes attempted in this page session. A reload may try them again. */
const attemptedRecovery = new Set<string>()
const recoveryKey = (taken: Pick<PendingTakeBack, 'slot' | 'mid' | 'action'>) =>
  JSON.stringify([taken.slot, taken.mid, taken.action])

/** Clear module recovery state between tests, preserving storage to simulate reload. */
export function __resetSteerTakeBacksForTests(): void {
  pendingTakeBackBySlot.clear()
  locallyStartedTakeBackBySlot.clear()
  attemptedRecovery.clear()
  takeBackStore.__resetForTests?.()
}

export interface SteerRetractOptions {
  /** The slot whose transcript `messages` is. */
  slot: string | null
  messages: ChatMessage[]
  /** Whether this slot's history fetch has settled. Defaults true for store-free hosts. */
  transcriptAuthoritative?: boolean
  slotRunning: boolean
  /** Merge a taken-back steer's text, attachments and quote into the composer. */
  restoreDraft: (text: string, files: string[], aliases?: Record<string, string[]>, quote?: MessageQuote) => boolean | void
  /** The paths the composer has staged right now (see `useQueuedMessageActions`). */
  stagedFiles?: () => readonly string[]
  /** The composer's project root (see `useQueuedMessageActions`). */
  project?: () => string | undefined
}

export interface SteerRetract {
  onSteerAction: (mid: string, action: SteerRetractAction) => void
  /** The action in flight for each row `mid`. */
  pending: Readonly<Record<string, SteerRetractAction>>
  /** Why the last take-back of each row `mid` did not happen. */
  refusals: Readonly<Record<string, SteerRetractRefusalNotice>>
  /** Rows whose take-back request failed before its outcome was known. */
  requestFailures: Readonly<Record<string, true>>
}

const REFUSAL_BY_CODE: Readonly<Record<string, SteerRetractRefusal | null>> = {
  // The turn read it, or is about to say so on the row.
  steer_not_pending: null,
  steer_retract_busy: 'busy',
  steer_retract_unsupported: 'unsupported',
}

/** A 4xx is the route's final answer. Transport failures and 5xx leave the outcome unknown. */
function definitiveRefusalOf(err: unknown): SteerRetractRefusal | null | undefined {
  if (!(err instanceof ApiError) || err.status < 400 || err.status >= 500) return undefined
  if (err.body) {
    try {
      const code = (JSON.parse(err.body) as { code?: unknown }).code
      if (typeof code === 'string' && code in REFUSAL_BY_CODE) return REFUSAL_BY_CODE[code]
    } catch {
      // Not the gateway's JSON: an ordinary refusal.
    }
  }
  return 'failed'
}

function takeBackLocks(): LockManager | undefined {
  return typeof navigator === 'undefined' ? undefined : navigator.locks
}

function withTakeBackLock(callback: () => void): void {
  const locks = takeBackLocks()
  if (!locks) {
    callback()
    return
  }
  // One store-wide Web Lock makes whole-blob recovery updates atomic across tabs.
  void locks.request(TAKE_BACKS_STORAGE_KEY, callback)
}

function clearStoredTakeBacks(): void {
  safeRemoveItem(TAKE_BACKS_STORAGE_KEY)
  safeRemoveItem(TAKE_BACKS_TIMESTAMPS_KEY)
}

function loadStoredTakeBacks(): Record<string, PendingTakeBack> {
  const raw = safeGetItem(TAKE_BACKS_STORAGE_KEY)
  let parsed: Record<string, unknown> | null = null
  if (raw !== null) {
    try {
      const value = JSON.parse(raw) as unknown
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid take-back store')
      parsed = value as Record<string, unknown>
    } catch {
      clearStoredTakeBacks()
      return {}
    }
  }
  const stored = takeBackStore.load()
  let needsRewrite = parsed !== null && Object.keys(parsed).length !== Object.keys(stored).length
  for (const [slot, taken] of Object.entries(stored)) {
    if (taken.slot === slot) continue
    delete stored[slot]
    needsRewrite = true
  }
  if (raw !== null && Object.keys(stored).length === 0) clearStoredTakeBacks()
  else if (needsRewrite) takeBackStore.save(stored)
  return stored
}

function storedTakeBackForSlot(slot: string): PendingTakeBack | undefined {
  const taken = loadStoredTakeBacks()[slot]
  if (!taken) return undefined
  if (taken.slot === slot) return taken
  removeStoredTakeBack(slot)
  return undefined
}

function peekStoredTakeBackForSlot(slot: string): PendingTakeBack | undefined {
  const taken = takeBackStore.peek()[slot]
  return taken?.slot === slot ? taken : undefined
}

function persistTakeBack(taken: PendingTakeBack): boolean {
  pendingTakeBackBySlot.set(taken.slot, taken)
  const stored = loadStoredTakeBacks()
  takeBackStore.set(stored, taken.slot, taken)
  return takeBackStore.save(stored)
}

function removeStoredTakeBack(slot: string, expected?: Pick<PendingTakeBack, 'mid' | 'action'>): void {
  const stored = loadStoredTakeBacks()
  const current = stored[slot]
  if (!current) return
  if (expected && (current.mid !== expected.mid || current.action !== expected.action)) return
  delete stored[slot]
  takeBackStore.save(stored)
  if (Object.keys(stored).length === 0) clearStoredTakeBacks()
}

function takeBackForSlot(slot: string | null): PendingTakeBack | undefined {
  if (!slot) return undefined
  const inMemory = pendingTakeBackBySlot.get(slot)
  if (inMemory) return inMemory
  const stored = peekStoredTakeBackForSlot(slot)
  if (stored && attemptedRecovery.has(recoveryKey(stored))) return undefined
  if (stored) pendingTakeBackBySlot.set(slot, stored)
  return stored
}

function pendingForSlot(slot: string | null): Readonly<Record<string, SteerRetractAction>> {
  const taken = takeBackForSlot(slot)
  return taken ? { [taken.mid]: taken.action } : {}
}

function requestFailuresForSlot(slot: string | null): Readonly<Record<string, true>> {
  const taken = takeBackForSlot(slot)
  return taken?.requestFailed ? { [taken.mid]: true } : {}
}

function without<T>(record: Readonly<Record<string, T>>, key: string): Readonly<Record<string, T>> {
  if (!(key in record)) return record
  const next = { ...record }
  delete next[key]
  return next
}

/**
 * Unsend, Edit and Queue instead on a steer the running turn has not read yet.
 *
 * The route answers before anything is known: kiro-cli is asked to drop the steer,
 * and the turn then reports whether it did. That verdict lands on the steer's own
 * row, so this reads it there rather than from the request:
 *
 * - `retracted`: taken back. Edit puts the text back in the composer, from the row
 *   as it stood when the person asked, the way a cancelled queue card does.
 * - `consumed`: the turn read it first. The row's badge says so.
 * - `requeued`: the turn ended first and the steer runs as its own message, which
 *   is what Queue instead asked for.
 * - `steerRetract: false`: the agent refused to take a steer back, unless the row is
 *   also `steerRetractExpired`: a later turn withdrew the offer of a steer whose own
 *   turn ended without settling it, which reads as the next case.
 * - the turn gone with the row still `written`: a hard stop discarded the steer, so
 *   an Edit or a Queue instead gets its text back in the composer rather than lose it.
 *
 * Reading the row rather than a reply also covers a dropped socket: the row is
 * re-read with the transcript.
 */
export function useSteerRetract({ slot, messages, transcriptAuthoritative = true, slotRunning, restoreDraft, stagedFiles, project }: SteerRetractOptions): SteerRetract {
  const [pending, setPending] = useState<Readonly<Record<string, SteerRetractAction>>>(() => pendingForSlot(slot))
  const [refusals, setRefusals] = useState<Readonly<Record<string, SteerRetractRefusalNotice>>>({})
  const [requestFailures, setRequestFailures] = useState<Readonly<Record<string, true>>>(() => requestFailuresForSlot(slot))
  const slotRef = useRef(slot)
  slotRef.current = slot
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const slotRunningRef = useRef(slotRunning)
  slotRunningRef.current = slotRunning
  const restoreDraftRef = useRef(restoreDraft)
  restoreDraftRef.current = restoreDraft
  const stagedFilesRef = useRef(stagedFiles)
  stagedFilesRef.current = stagedFiles
  const projectRef = useRef(project)
  projectRef.current = project

  const settle = useCallback((taken: PendingTakeBack, refusal?: SteerRetractRefusal | null, keepStored = false) => {
    if (pendingTakeBackBySlot.get(taken.slot) !== taken) return
    pendingTakeBackBySlot.delete(taken.slot)
    if (locallyStartedTakeBackBySlot.get(taken.slot) === taken) {
      locallyStartedTakeBackBySlot.delete(taken.slot)
    }
    if (!keepStored) {
      withTakeBackLock(() => removeStoredTakeBack(taken.slot, taken))
    }
    if (slotRef.current !== taken.slot) return
    setPending(prev => without(prev, taken.mid))
    setRefusals(prev => (refusal ? { ...prev, [taken.mid]: { refusal, action: taken.action } } : without(prev, taken.mid)))
    setRequestFailures(prev => without(prev, taken.mid))
  }, [])

  const restore = useCallback((taken: PendingTakeBack) => {
    // The row holds the wire form (attachment markers, image lines and quote
    // block). Attachment markers return to composer form while the quote stays durable.
    const { text, files, aliases } = restoreQueuedContent(
      taken.content,
      queueEntryAttachments(taken.meta).files,
      stagedFilesRef.current?.(),
      projectRef.current?.(),
    )
    const stored = restoreDraftRef.current(text, files, aliases)
    revealComposer()
    return stored
  }, [])

  const restoreAndSettle = useCallback((taken: PendingTakeBack) => {
    const stored = restore(taken)
    if (stored === false) attemptedRecovery.add(recoveryKey(taken))
    settle(taken, undefined, stored === false)
  }, [restore, settle])

  const onSteerAction = useCallback((mid: string, action: SteerRetractAction) => {
    if (!slot) return
    // Without a cross-tab lock another tab can overwrite the record that holds the only copy of an Edit's text.
    if (action === 'edit' && !takeBackLocks()) {
      setPending(prev => without(prev, mid))
      setRefusals(prev => ({ ...prev, [mid]: { refusal: 'failed', action } }))
      setRequestFailures(prev => without(prev, mid))
      return
    }
    withTakeBackLock(() => {
      if (slotRef.current !== slot) return
      const active = pendingTakeBackBySlot.get(slot)
      if (active && locallyStartedTakeBackBySlot.get(slot) === active) return
      const row = messagesRef.current.find(m => m.meta?.mid === mid)
      if (!row) return
      const outstanding = active ?? storedTakeBackForSlot(slot)
      if (outstanding) {
        // A stored take-back may hold the only copy of an Edit's text, so refuse before replacing it:
        // busy while it settles, failed once this page could not save that text to the message box.
        const refusal: SteerRetractRefusal = attemptedRecovery.has(recoveryKey(outstanding)) ? 'failed' : 'busy'
        setPending(prev => without(prev, mid))
        setRefusals(prev => ({ ...prev, [mid]: { refusal, action } }))
        setRequestFailures(prev => without(prev, mid))
        return
      }
      const taken: PendingTakeBack = {
        slot,
        mid,
        action,
        content: row.content,
        meta: row.meta,
        observedRunning: slotRunningRef.current,
        requestFailed: false,
      }
      const initiallyStored = persistTakeBack(taken)
      if (action === 'edit' && !initiallyStored) {
        pendingTakeBackBySlot.delete(slot)
        setPending(prev => without(prev, mid))
        setRefusals(prev => ({ ...prev, [mid]: { refusal: 'failed', action } }))
        setRequestFailures(prev => without(prev, mid))
        return
      }
      locallyStartedTakeBackBySlot.set(slot, taken)
      setPending({ [mid]: action })
      setRefusals(prev => without(prev, mid))
      setRequestFailures(prev => without(prev, mid))
      api.retractSteer(slot, mid, action).catch((err: unknown) => {
        const refusal = definitiveRefusalOf(err)
        if (refusal !== undefined) {
          settle(taken, refusal)
          return
        }
        withTakeBackLock(() => {
          if (pendingTakeBackBySlot.get(taken.slot) !== taken) return
          const current = storedTakeBackForSlot(taken.slot)
          if (current && (current.mid !== taken.mid || current.action !== taken.action)) return
          if (!current && initiallyStored) return
          taken.requestFailed = true
          persistTakeBack(taken)
          if (slotRef.current === taken.slot) setRequestFailures({ [taken.mid]: true })
        })
      })
    })
  }, [slot, settle])

  useEffect(() => {
    setPending(pendingForSlot(slot))
    setRefusals({})
    setRequestFailures(requestFailuresForSlot(slot))
  }, [slot])

  useEffect(() => {
    if (!slot) return
    const taken = pendingTakeBackBySlot.get(slot)
    if (!taken) return
    const row = messages.find(message => message.meta?.mid === taken.mid)
    if (!row && transcriptAuthoritative && messages.length > 0) {
      // A row missing from the loaded page may only be older than it. If the take-back
      // went through, the server cleared that row's text and this record is the only
      // copy, so an Edit gets its text back: a duplicate is recoverable, a lost text is not.
      if (taken.action === 'edit') restoreAndSettle(taken)
      else settle(taken)
      return
    }
    if (slotRunning && !taken.observedRunning) taken.observedRunning = true
    const state = row?.meta?.steerState
    if (state === 'retracted') {
      if (taken.action === 'edit') restoreAndSettle(taken)
      else settle(taken)
    } else if (state === 'consumed' || state === 'requeued') {
      settle(taken)
    } else if (row?.meta?.steerRetract === false && row.meta.steerRetractExpired === true) {
      // A later turn withdrew the offer because this steer's turn ended without
      // settling it. Nothing refused it, so it settles as the turn-ended case below.
      if (taken.action !== 'unsend') restoreAndSettle(taken)
      else settle(taken)
    } else if (row?.meta?.steerRetract === false) {
      settle(taken, 'unsupported')
    } else if (transcriptAuthoritative && messages.length > 0 && taken.observedRunning && !slotRunning) {
      if (taken.action !== 'unsend') restoreAndSettle(taken)
      else settle(taken)
    }
  }, [messages, transcriptAuthoritative, slotRunning, slot, restoreAndSettle, settle])

  return { onSteerAction, pending, refusals, requestFailures }
}
