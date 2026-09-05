import { useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { store, useAppDispatch } from '../store'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { updateSlotPin, updateSlot, markSlotRead, markSlotUnread } from '../store/dashboardSlice'
import { copySessionLink } from '../utils/shareUrl'
import { useMoveSlotToFolder } from './useMoveSlotToFolder'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { commitPinnedSessionOperations, commitPinnedSessionSnapshot, readPinnedSessionOrder, reconcilePinnedSessionOrder } from '../utils/pinnedSessionOrder'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'
import { compareBySort, readSessionSortKey } from '../pages/chat/sessionOrder'

interface PinMutationEntry {
  key: string
  pinned: boolean
  succeeded: boolean | null
  optimisticSlot?: ChatSlot
  slotsGeneration: number
}

interface PinMutationBatch {
  baseline: string[]
  storedBaseline: string[]
  entries: PinMutationEntry[]
  snapshotVersion: number
}

let activePinMutationBatch: PinMutationBatch | null = null
const pinMutationTails = new Map<string, Promise<unknown>>()

/** Preserve invocation order at the server for rapid toggles of one session. */
function setSlotPinInOrder(key: string, pinned: boolean) {
  const request = (pinMutationTails.get(key) ?? Promise.resolve())
    .catch(() => undefined)
    .then(() => api.setSlotPin(key, pinned))
  pinMutationTails.set(key, request)
  return request.finally(() => {
    if (pinMutationTails.get(key) === request) pinMutationTails.delete(key)
  })
}

/**
 * The surface-agnostic session actions — the ones that need only a slot key and
 * shared mutations/dispatch, with no per-surface UI state. Centralising them
 * here means every menu (and the sidebar's non-menu buttons) shares one
 * definition instead of re-declaring a lambda apiece, and callers no longer
 * hand the menu a wall of handlers.
 *
 * Actions read any prior state they need to roll back (pinned, folder_id) from
 * the store at call time, so they stay self-contained — the same pattern as
 * useMoveSlotToFolder.
 *
 * Surface-specific actions are intentionally NOT here: the sidebar's Rename
 * (drives inline row-edit state) and Tags (opens a per-row popover) stay owned
 * by ChatSidebar; the header's Reveal/MCP/Slack/colour stay in ChatHeaderMenu.
 */
export interface SessionActions {
  /** Fork/duplicate a session. */
  duplicate: (slotKey: string) => void
  /** Toggle read/unread. */
  toggleRead: (slotKey: string) => void
  /** Toggle pinned. */
  togglePin: (slotKey: string) => void
  /** Toggle orchestrator (Autopilot) mode on/off, with a confirm. */
  toggleMode: (slotKey: string) => void
  /** Copy the session's share link. */
  copyLink: (slotKey: string) => void
  /** Move to a folder (or root for null) — shared optimistic move + rollback. */
  move: (slotKey: string, folderId: string | null) => void
  /** Relaunch the slot's agent process in place (fresh MCP servers/env, conversation preserved). */
  reload: (slotKey: string) => void
  /** Close (delete) a session, honouring the confirm-close preference. */
  close: (slotKey: string) => void
}

export function useSessionActions(mode?: string): SessionActions {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const moveSlotToFolder = useMoveSlotToFolder()

  const finishPinMutation = useCallback(async (
    batch: PinMutationBatch, entry: PinMutationEntry, succeeded: boolean,
  ) => {
    entry.succeeded = succeeded
    if (batch.entries.some(candidate => candidate.succeeded === null)) return
    const snapshotVersion = ++batch.snapshotVersion
    try {
      let slots: ChatSlot[]
      for (let attempt = 0; ; attempt += 1) {
        const slotsGeneration = store.getState().dashboard.slotsGeneration ?? 0
        slots = await api.chatSlots() as ChatSlot[]
        // A newer mutation may have joined this batch while the snapshot was in flight.
        // Its own settlement will fetch again; only that newest request may reconcile.
        if (snapshotVersion !== batch.snapshotVersion
          || batch.entries.some(candidate => candidate.succeeded === null)) return
        if ((store.getState().dashboard.slotsGeneration ?? 0) === slotsGeneration) break
        // Continuous live frames must not create an unbounded GET loop. After
        // bounded retries, Redux itself is the newest accepted full-slot snapshot.
        if (attempt >= 2) {
          slots = store.getState().dashboard.slots
          break
        }
      }
      if (activePinMutationBatch === batch) activePinMutationBatch = null
      const latest = new Map<string, boolean>()
      for (const candidate of batch.entries) latest.set(candidate.key, candidate.pinned)
      const snapshotByKey = new Map(slots.map(slot => [slot.key, slot]))
      for (const key of latest.keys()) {
        // Snapshot request generations reject older local mutations above. Redux
        // divergence alone is not newer-writer evidence: a delayed pre-mutation
        // slots frame can arrive while this request is in flight.
        const pinned = snapshotByKey.get(key)?.pinned ?? false
        const current = store.getState().dashboard.slots.find(slot => slot.key === key)?.pinned ?? false
        if (current !== pinned) dispatch(updateSlotPin({ key, pinned }))
      }
      const currentSlots = store.getState().dashboard.slots
      const pinnedKeys = new Set(currentSlots.filter(slot => slot.pinned).map(slot => slot.key))
      const currentKeys = new Set(currentSlots.map(slot => slot.key))
      const baselineKeys = new Set(batch.storedBaseline)
      for (const slot of slots) {
        if (slot.pinned && baselineKeys.has(slot.key) && !currentKeys.has(slot.key)) pinnedKeys.add(slot.key)
      }
      const baselineMembership = new Set(batch.baseline)
      const newlyPinnedKeys = [...pinnedKeys]
        .filter(key => !baselineMembership.has(key))
      commitPinnedSessionSnapshot(
        [...pinnedKeys], batch.baseline, newlyPinnedKeys, batch.storedBaseline,
      )
    } catch {
      // A newer request (or an entry that has not settled yet) owns reconciliation.
      if (snapshotVersion !== batch.snapshotVersion
        || batch.entries.some(candidate => candidate.succeeded === null)) return
      if (activePinMutationBatch === batch) activePinMutationBatch = null
      const latest = new Map<string, PinMutationEntry>()
      for (const candidate of batch.entries) latest.set(candidate.key, candidate)
      const ownedKeys = new Set([...latest]
        .filter(([key, candidate]) => {
          const dashboard = store.getState().dashboard
          const current = dashboard.slots.find(slot => slot.key === key)
          return (dashboard.slotsGeneration ?? 0) === candidate.slotsGeneration
            && current === candidate.optimisticSlot
            && (current?.pinned ?? false) === candidate.pinned
        })
        .map(([key]) => key))
      const operations = batch.entries
        .filter(candidate => candidate.succeeded && ownedKeys.has(candidate.key))
        .map(({ key, pinned }) => ({ key, pinned }))
      const expected = new Set(commitPinnedSessionOperations(operations, batch.baseline, batch.storedBaseline))
      for (const key of ownedKeys) {
        const pinned = expected.has(key)
        const current = store.getState().dashboard.slots.find(slot => slot.key === key)?.pinned ?? false
        if (current !== pinned) dispatch(updateSlotPin({ key, pinned }))
      }
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    }
  }, [dispatch, queryClient])

  const forkMutation = useMutation({
    mutationFn: (slot: string) => api.forkChatSlot(slot),
    onSuccess: (data) => {
      if (data?.ok && data.key) {
        queryClient.invalidateQueries({ queryKey: ['slots'] })
        dispatch(switchSlot(data.key))
      }
    },
  })

  const pinMutation = useMutation({
    mutationFn: ({ key, pinned }: { key: string; pinned: boolean }) => setSlotPinInOrder(key, pinned),
    onMutate: ({ key, pinned }) => {
      const dashboard = store.getState().dashboard
      const byKey = new Map(dashboard.slots.map(slot => [slot.key, slot]))
      const renderedPinned = dashboard.sidebarOrder.filter(candidate => byKey.get(candidate)?.pinned)
      const renderedSet = new Set(renderedPinned)
      const fallbackSort = readSessionSortKey()
      const completePinnedOrder = dashboard.slots
        .filter(slot => slot.pinned)
        .sort((a, b) => compareBySort(a, b, fallbackSort))
        .map(slot => slot.key)
      const naturalPinned = renderedPinned.length === completePinnedOrder.length
        && renderedSet.size === completePinnedOrder.length
        ? renderedPinned
        : completePinnedOrder
      const storedPinnedOrder = readPinnedSessionOrder()
      const prevPinnedOrder = reconcilePinnedSessionOrder(storedPinnedOrder, naturalPinned)
      const batch = activePinMutationBatch ?? {
        baseline: prevPinnedOrder,
        storedBaseline: storedPinnedOrder,
        entries: [],
        snapshotVersion: 0,
      }
      activePinMutationBatch = batch
      const entry: PinMutationEntry = {
        key,
        pinned,
        succeeded: null,
        slotsGeneration: dashboard.slotsGeneration ?? 0,
      }
      batch.entries.push(entry)
      dispatch(updateSlotPin({ key, pinned }))
      entry.optimisticSlot = store.getState().dashboard.slots.find(slot => slot.key === key)
      return { batch, entry }
    },
    onSuccess: (_data, _vars, ctx) => ctx
      ? finishPinMutation(ctx.batch, ctx.entry, true)
      : undefined,
    onError: (_err, _vars, ctx) => ctx
      ? finishPinMutation(ctx.batch, ctx.entry, false)
      : undefined,
  })

  // Orchestrator/Autopilot mode toggle (optimistic, server-persisted).
  const modeMutation = useMutation({
    mutationFn: ({ key, newMode }: { key: string; newMode: string }) => api.setSlotMode(key, newMode),
    onMutate: ({ key, newMode }) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === key)?.mode ?? ''
      dispatch(updateSlot({ key, mode: newMode }))
      return { key, prev, newMode }
    },
    onError: (_err, _vars, ctx) => {
      if (!ctx) return
      // Guarded rollback: don't clobber a superseding mode toggle.
      const current = store.getState().dashboard.slots.find(s => s.key === ctx.key)?.mode ?? ''
      if (current === ctx.newMode) dispatch(updateSlot({ key: ctx.key, mode: ctx.prev }))
    },
  })

  // Session reload (relaunch the agent process in place). No optimistic state:
  // the success confirmation is the feed notice the backend appends, arriving
  // over the websocket (and lighting the row's unread indicator for a
  // non-active slot). Failure must NOT be silent -- the user would proceed
  // believing their stale MCP config was refreshed, the exact confusion the
  // feature exists to fix. alert() is the always-available surface (the
  // dashboard has no global toast); the copy branches on the backend's
  // machine-readable code, because "try again when the session is idle" is a
  // dead end for a slot that LOOKS idle but has sub-agents still working.
  const reloadMutation = useMutation({
    mutationFn: (slot: string) => api.chatSlotReload(slot),
    onError: (err) => {
      const body = err instanceof ApiError ? err.body : ''
      alert(i18nT(body.includes('slot_subagents_running')
        ? 'hooks.useSessionActions.reload_failed_subagents'
        : 'hooks.useSessionActions.reload_failed'))
    },
  })

  // Destructure the stable `mutate` fns so the action callbacks below aren't
  // recreated on every render (the mutation result objects are new each render).
  const { mutate: forkMutate } = forkMutation
  const { mutate: pinMutate } = pinMutation
  const { mutate: modeMutate } = modeMutation
  const { mutate: reloadMutate } = reloadMutation

  const duplicate = useCallback((slotKey: string) => { forkMutate(slotKey) }, [forkMutate])

  const toggleRead = useCallback((slotKey: string) => {
    const isUnread = store.getState().dashboard.unreadSlots.includes(slotKey)
    dispatch(isUnread ? markSlotRead(slotKey) : markSlotUnread(slotKey))
  }, [dispatch])

  const togglePin = useCallback((slotKey: string) => {
    const isPinned = store.getState().dashboard.slots.find(s => s.key === slotKey)?.pinned ?? false
    pinMutate({ key: slotKey, pinned: !isPinned })
  }, [pinMutate])

  const toggleMode = useCallback((slotKey: string) => {
    const cur = store.getState().dashboard.slots.find(s => s.key === slotKey)?.mode ?? ''
    const newMode = cur === 'orchestrator' ? '' : 'orchestrator'
    if (confirm(newMode === 'orchestrator'
      ? i18nT('hooks.useSessionActions.switch_to_autopilot_mode_future_messages_will_us')
      : i18nT('hooks.useSessionActions.switch_to_normal_chat_mode_future_messages_will'))) {
      modeMutate({ key: slotKey, newMode })
    }
  }, [modeMutate])

  const copyLink = useCallback((slotKey: string) => {
    const slot = store.getState().dashboard.slots.find(s => s.key === slotKey)
    copySessionLink(slotKey, slot?.title, undefined, mode)
  }, [mode])

  const move = useCallback((slotKey: string, folderId: string | null) => {
    moveSlotToFolder(slotKey, folderId)
  }, [moveSlotToFolder])

  const reload = useCallback((slotKey: string) => { reloadMutate(slotKey) }, [reloadMutate])

  const close = useCallback((slotKey: string) => {
    if (!loadChatConfig().confirmCloseSession || confirm(i18nT('hooks.useSessionActions.close_this_session'))) dispatch(deleteSlot(slotKey))
  }, [dispatch])

  return { duplicate, toggleRead, togglePin, toggleMode, copyLink, move, reload, close }
}
