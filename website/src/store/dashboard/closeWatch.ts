import { createAction, type UnknownAction } from '@reduxjs/toolkit'
import { isUnsafeKey } from '../../utils/sanitize'
import type { ChatSlot } from '../../types'
import type { ChatState } from '../chat/state'

/** The "close didn't go through" notice for a close the server undid after this
 *  tab stopped waiting for it: a proxy-timed-out close, or one answered 404 (#31168). */

type CloseNotice = ChatState['closeRefused']

export interface CloseWatchState {
  /** Closes that ended with no verdict, by the deadline after which a re-listing is no longer theirs. */
  closeWatch?: Record<string, { title: string | null; until: number }>
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

export const watchCloseOutcome = createAction<{ key: string; title: string | null }>('dashboard/watchCloseOutcome')

export const isCloseWatchAction = (action: UnknownAction): boolean =>
  action.type === watchCloseOutcome.type || action.type === 'chat/setCloseRefused'
  || action.type === 'dashboard/sseSlotPatch' || LIST_ACTIONS.has(action.type) || REOPEN_ACTIONS.has(action.type)

/** Runs after the slice's own reducer, so `slots` is the reconciled list with close holds applied. */
export function closeWatchReducer(state: CloseWatchState & { slots: ChatSlot[] }, action: UnknownAction): void {
  if (watchCloseOutcome.match(action)) {
    const { key, title } = action.payload
    if (isUnsafeKey(key) || state.durablyRemoved?.[key]) return
    state.closeWatch ??= {}
    state.closeWatch[key] = { title, until: Date.now() + CLOSE_RESTORE_WATCH_MS }
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
    const { title, until } = watch[key]
    if (now >= until) delete watch[key]
    else if (state.slots.some(s => s.key === key)) {
      delete watch[key]
      state.closeRestored = { key, title, reason: 'failed' }
    }
  }
}

export const selectCloseNotice = (st: { chat: Pick<ChatState, 'closeRefused'>; dashboard: CloseWatchState }): CloseNotice =>
  st.dashboard.closeRestored ?? st.chat.closeRefused
