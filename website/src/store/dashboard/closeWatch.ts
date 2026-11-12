import { createAction, type UnknownAction } from '@reduxjs/toolkit'
import { isUnsafeKey } from '../../utils/sanitize'
import type { ChatSlot } from '../../types'
import type { ChatState } from '../chat/state'

/** The "close didn't go through" notice for a close the server undid after this
 *  tab stopped waiting for it: a proxy-timed-out close, or one answered 404 (#31168). */

type CloseNotice = ChatState['closeRefused']

export interface CloseWatchState {
  /** Closes that ended with no verdict, by the deadline after which a re-listing
   *  is no longer theirs. `quietUntil` is set only when the close armed the watch
   *  with its own DELETE still in flight (a 404: another close popped the key and
   *  is still saving). Until that in-flight cap passes, a re-listing is that slow
   *  save finishing, not the server undoing the close, so the watch stays quiet
   *  and only an actual restore AFTER the cap is reported (#31538). */
  closeWatch?: Record<string, { title: string | null; until: number; incarnation?: string; quietUntil?: number }>
  /** A watched session a server list put back. Newer than any chat notice, so shown first. */
  closeRestored?: CloseNotice
  durablyRemoved?: Record<string, true>
}

const CLOSE_RESTORE_WATCH_MS = 120_000
const LIST_ACTIONS = new Set(['dashboard/sseSlots', 'dashboard/fetchSlots/fulfilled'])
// This tab opening the key again (a resume, a create) is not the close coming undone.
const REOPEN_ACTIONS = new Set([
  'dashboard/addSlotOptimistic', 'chat/createSlot/fulfilled', 'chat/resumeFromHistory/pending', 'chat/resumeFromHistory/fulfilled',
])

// A History row resumes by its history key; the server maps it back the same way (`_history_key_for`).
const slotKeyOfHistoryKey = (key: string): string => {
  let slot = key.startsWith('dashboard:') ? key.slice('dashboard:'.length) : key
  while (slot.startsWith('dashboard_')) slot = slot.slice('dashboard_'.length)
  return slot
}

/** Whether a listed row is the session this tab tried to close, not one reopened under its key.
 *  The failed close's restore puts the same server slot back, so its `incarnation` matches;
 *  a reopen in another tab or device builds a new one. Without both ids (an older
 *  gateway) the key alone decides. A gateway restart inside the watch also rebuilds
 *  the slot with a new id, so a close that really failed across a restart goes
 *  unreported: the notice fires in fewer cases than before, never in more. */
export const isClosedIncarnation = (closed: string | undefined, row: ChatSlot): boolean =>
  !closed || !row.incarnation || row.incarnation === closed

export const watchCloseOutcome = createAction<{ key: string; title: string | null; incarnation?: string; quietUntil?: number }>('dashboard/watchCloseOutcome')

export const isCloseWatchAction = (action: UnknownAction): boolean =>
  action.type === watchCloseOutcome.type || action.type === 'chat/setCloseRefused'
  || action.type === 'dashboard/sseSlotPatch' || LIST_ACTIONS.has(action.type) || REOPEN_ACTIONS.has(action.type)

/** Runs after the slice's own reducer, so `slots` is the reconciled list with close holds applied. */
export function closeWatchReducer(state: CloseWatchState & { slots: ChatSlot[] }, action: UnknownAction): void {
  if (watchCloseOutcome.match(action)) {
    const { key, title, incarnation, quietUntil } = action.payload
    if (isUnsafeKey(key) || state.durablyRemoved?.[key]) return
    state.closeWatch ??= {}
    state.closeWatch[key] = { title, until: Date.now() + CLOSE_RESTORE_WATCH_MS, incarnation, quietUntil }
    return
  }
  if (action.type === 'chat/setCloseRefused') {
    state.closeRestored = null
    return
  }
  if (action.type === 'dashboard/sseSlotPatch') {
    for (const key of (action.payload as { removed?: string[] }).removed ?? []) {
      if (state.closeRestored?.key === key) state.closeRestored = null
      if (!isUnsafeKey(key)) delete state.closeWatch?.[key]
    }
    return
  }
  if (REOPEN_ACTIONS.has(action.type)) {
    const reopen = action as { payload?: { key?: unknown }; meta?: { arg?: { key?: unknown } } }
    const raw = reopen.payload?.key ?? reopen.meta?.arg?.key
    if (typeof raw !== 'string') return
    for (const key of new Set([raw, slotKeyOfHistoryKey(raw)])) {
      if (isUnsafeKey(key)) continue
      delete state.closeWatch?.[key]
      if (state.closeRestored?.key === key) state.closeRestored = null
    }
    return
  }
  const watch = state.closeWatch
  if (!watch) return
  const now = Date.now()
  for (const key of Object.keys(watch)) {
    const { title, until, incarnation, quietUntil } = watch[key]
    if (now >= until) {
      delete watch[key]
      continue
    }
    const row = state.slots.find(s => s.key === key)
    if (!row) continue
    // A 404 close (quietUntil set) completed for this tab while the popping
    // close kept saving. A re-listing once that close's in-flight hold has hit
    // its cap is the slow save, not a restore, so it stays quiet; only a
    // re-listing while the hold was still live counts as the server undoing it.
    // A genuinely failed close (504, dropped connection) has no quietUntil and
    // reports every re-listing as before.
    if (quietUntil !== undefined && now >= quietUntil) continue
    delete watch[key]
    // A session reopened elsewhere means the close went through: end the watch quietly.
    if (isClosedIncarnation(incarnation, row)) state.closeRestored = { key, title, reason: 'failed' }
  }
}

export const selectCloseNotice = (st: { chat: Pick<ChatState, 'closeRefused'>; dashboard: CloseWatchState }): CloseNotice =>
  st.dashboard.closeRestored ?? st.chat.closeRefused
