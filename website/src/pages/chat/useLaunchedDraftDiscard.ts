import { useCallback, useEffect, useRef, useState } from 'react'

import { useAppDispatch, useAppStore } from '../../store'
import { deleteSlot } from '../../store/chatSlice'
import { isUntouchedSession } from '../../lib/untouchedSession'
import { appPathname } from '../../lib/basePath'
import { isChatPath } from '../../hooks/notificationBanner'
import { getSnapshot as poppedOutSlots, subscribe as onPopoutsChange } from '../../utils/chatPopout'
import { pendingSlotSwitchTarget, type SlotSwitchField } from '../../lib/slotSwitch'
import type { ChatSlot } from '../../types'

/** A session the routed chat created for an app's draft launch, the message the
 *  launch staged in its composer, and the session's row as the create returned it. */
interface LaunchedDraft {
  key: string
  seed: string
  row: ChatSlot
}

/** Fields of a session's row that change with no user action: live turn, process
 *  and approval state, what the server resolved or reports about the session, its
 *  identity, list-wide order revisions, and the fields only one of the create
 *  answer and the session list carries. Every other field, a new one included,
 *  is treated as the user's, so a change in it keeps the session. */
const SERVER_STATE = new Set<string>([
  'turn', 'turn_gen', 'running', 'stopping', 'stop_state', 'stop_declined',
  'pending_approval', 'pending_approval_info', 'waiting_for_input', 'needs_input',
  'interrupted', 'wake_starting', 'compacting', 'preparing', 'preparing_text',
  'preparing_lang', 'wait_state', 'has_options', 'options', 'options_ts',
  'prompt_preview', 'admission_queued', 'last_ts', 'last_turn_ts', 'last_message',
  'last_activity_ts', 'last_agent_ts', 'effective_agent', 'served_model',
  'model_withheld', 'mcp_report', 'process', 'row_identity', 'incarnation',
  'history_key', 'linked_session_key', 'pin_rev', 'manual_rev', 'subagents_running',
  'subagents_queued', 'subagents_queued_reason', 'work_aggregate', 'crew_work',
  'code_channel', 'code_channel_pending', 'session_folder_notice', 'headroom',
  'work_item_id',
])

/** Every field a session switch registers, typed so a new field must be added. */
const SWITCH_FIELDS: Record<SlotSwitchField, true> = { model: true, project: true, agent: true, reasoning_effort: true }

/** Whether a settings switch for the session is staged or still on the wire: its
 *  pick reaches the session's row only once the request succeeds. */
function switchPending(slotKey: string): boolean {
  return (Object.keys(SWITCH_FIELDS) as SlotSwitchField[]).some((field) => pendingSlotSwitchTarget(field, slotKey) !== null)
}

/** An absent, null, false, empty-string, empty-list or empty-object value. */
function isUnset(value: unknown): boolean {
  if (value == null || value === false || value === '') return true
  if (Array.isArray(value)) return value.length === 0
  return typeof value === 'object' && Object.keys(value as object).length === 0
}

/** Whether a field outside SERVER_STATE differs between the two rows. An unset
 *  value compares equal to any other unset value, so a field one payload omits
 *  and the other sends empty is no change. */
function userFieldChanged(created: ChatSlot, now: ChatSlot): boolean {
  const a = created as unknown as Record<string, unknown>
  const b = now as unknown as Record<string, unknown>
  for (const field of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (SERVER_STATE.has(field)) continue
    const before = isUnset(a[field]) ? null : a[field]
    const after = isUnset(b[field]) ? null : b[field]
    if (JSON.stringify(before) !== JSON.stringify(after)) return true
  }
  return false
}

/**
 * Discards the session an app's draft launch created when the user leaves it
 * having done nothing in it.
 *
 * A draft launch (`openChat` with `autoSend: false`, a `memoryMode` or
 * `sessionFolder: false`) makes the `?new=1` intake create the session before the
 * user types, so its composer opens holding the app's message. When the page
 * stops showing that session (another session, a new chat, or a page other than
 * the chat), it is closed and its History row deleted through the discard route,
 * the same one the artifact companion chat uses, so a user who only read the
 * message leaves no empty session behind.
 *
 * Only a session the user never acted on goes. Any key press, pointer press,
 * input or change inside the chat pane while it shows the session (`interacted`,
 * which the pane calls; the pane's menus and pickers count too) keeps it for good,
 * and so does being shown in split view or a pop-out window, which keep their own
 * composer outside this window's stored drafts. On leaving, it also needs no
 * settings switch staged or in flight for it, its row unchanged since the create
 * outside the fields the server writes itself (SERVER_STATE), nothing sent, and the
 * composer holding the app's message and nothing else.
 *
 * The check waits a microtask because the page saves its composer draft as it
 * switches or unmounts, which can run after this cleanup. The server re-checks
 * that the session has no message, turn, queue or loop and refuses quietly
 * otherwise.
 *
 * Returns `track`, which the intake calls with the created session, and
 * `interacted`, which the chat pane calls on each user event inside it.
 */
export function useLaunchedDraftDiscard(activeSlot: string | null, splitMode: boolean): {
  track: (slot: ChatSlot, seed: string) => void
  interacted: () => void
} {
  const dispatch = useAppDispatch()
  const appStore = useAppStore()
  const [launched, setLaunched] = useState<LaunchedDraft | null>(null)
  const track = useCallback((slot: ChatSlot, seed: string) => {
    setLaunched({ key: slot.key, seed, row: { ...slot } })
  }, [])

  const launchedKey = launched?.key ?? null
  const shownKey = launched && activeSlot === launched.key ? launched.key : null
  const seed = launched?.seed ?? ''
  const createdRow = launched?.row ?? null
  const keptKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (splitMode && shownKey) keptKeyRef.current = shownKey
  }, [splitMode, shownKey])
  useEffect(() => {
    if (!launchedKey) return
    const keepIfPoppedOut = () => { if (poppedOutSlots().has(launchedKey)) keptKeyRef.current = launchedKey }
    keepIfPoppedOut()
    return onPopoutsChange(keepIfPoppedOut)
  }, [launchedKey])
  const shownKeyRef = useRef<string | null>(null)
  const interacted = useCallback(() => {
    if (shownKeyRef.current) keptKeyRef.current = shownKeyRef.current
  }, [])
  useEffect(() => {
    shownKeyRef.current = shownKey
    if (!shownKey || !createdRow) return
    return () => {
      shownKeyRef.current = null
      queueMicrotask(() => {
        // Re-run with the same key (a changed seed, StrictMode): still shown.
        if (shownKeyRef.current === shownKey) return
        if (keptKeyRef.current === shownKey || poppedOutSlots().has(shownKey)) return
        const state = appStore.getState()
        // A chat page mounted afresh can still be showing it.
        if (isChatPath(appPathname()) && state.chat.activeSlot === shownKey) return
        const slot = state.dashboard.slots.find((x) => x.key === shownKey)
        if (switchPending(shownKey)) return
        if (!slot || userFieldChanged(createdRow, slot)) return
        if (!isUntouchedSession(state, shownKey, seed)) return
        dispatch(deleteSlot({ key: shownKey, discard: true })).unwrap().catch(() => {})
      })
    }
  }, [shownKey, seed, createdRow, appStore, dispatch])

  return { track, interacted }
}
