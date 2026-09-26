import { useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { store, useAppDispatch } from '../store'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { updateSlotPin, updateSlot, markSlotRead, markSlotUnread } from '../store/dashboardSlice'
import { emitSlotRead } from '../lib/slotReadRelay'
import { copySessionLink } from '../utils/shareUrl'
import { useMoveSlotToFolder } from './useMoveSlotToFolder'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { commitPinnedSessionOperations, commitPinnedSessionSnapshot, readPinnedSessionOrder, reconcilePinnedSessionOrder } from '../utils/pinnedSessionOrder'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import type { ChatSlot } from '../types'
import { compareBySort, readSessionSortKey } from '../pages/chat/sessionOrder'
import { clearActionFailure, reportActionFailure } from '../utils/actionFailure'
import { findReport } from '../utils/errorReport'

interface PinMutationEntry {
  key: string
  pinned: boolean
  succeeded: boolean | null
  pinGeneration: number
  slotsGeneration: number
}

interface PinMutationBatch {
  baseline: string[]
  storedBaseline: string[]
  entries: PinMutationEntry[]
  snapshotVersion: number
}

let activePinMutationBatch: PinMutationBatch | null = null

/** Keys whose optimistic pin membership has not reached authoritative reconciliation. */
export function pinMutationKeysInFlight(): string[] {
  return activePinMutationBatch
    ? [...new Set(activePinMutationBatch.entries.map(entry => entry.key))]
    : []
}
let pinReconcileRequestId = 0
const pinMutationTails = new Map<string, Promise<unknown>>()

/**
 * The failure key of a pin write over ONE session. A bulk revert is one banner
 * naming N sessions, reported under N of these: the first of them whose pin
 * lands takes the banner down, whether it lands alone, with the rest, or with
 * more — a set key would have matched only the retry that repeated the set
 * exactly, leaving "was undone" on screen over sessions that are pinned.
 */
const pinActionKey = (key: string) => `pin:${key}`

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
    // A rejected pin whose commit the server DID apply comes back pinned from the
    // authoritative frame, and "was undone" would then be false.
    const reportRevertedPins = (reconciled: Iterable<string>) => {
      const owned = new Set(reconciled)
      // One verdict per session, read off its newest entry: rapid toggles of one
      // session leave it several entries, and judging each would name that
      // session once per click and count it as that many.
      const latest = new Map<string, PinMutationEntry>()
      for (const candidate of batch.entries) latest.set(candidate.key, candidate)
      const reverted: string[] = []
      const revertedKeys: string[] = []
      const applied = new Set<string>()
      for (const [key, candidate] of latest) {
        if (!owned.has(key)) continue
        const slot = store.getState().dashboard.slots.find(s => s.key === key)
        if ((slot?.pinned ?? false) === candidate.pinned) {
          applied.add(key)
          continue
        }
        if (candidate.succeeded !== false) continue
        reverted.push(slot?.title ?? '')
        revertedKeys.push(key)
      }
      // The state these asked for now holds, so a banner saying their pin change
      // was undone describes nothing: the same write landing takes it down. One
      // key per session, the shape the report writes.
      for (const key of applied) clearActionFailure(pinActionKey(key))
      if (reverted.length === 0) return
      // One report for the batch: the store REPLACES rather than queues, so a call
      // per candidate would leave the reader told about only the last of N.
      const names = reverted.filter(Boolean)
      const opts = { actionKey: revertedKeys.map(pinActionKey) }
      if (names.length < 2) {
        reportActionFailure(i18nT('hooks.useSessionActions.pin_change_failed'), names[0], undefined, undefined, opts)
        return
      }
      // Several sessions get their own lead: the shared one has a single subject
      // slot, and N titles joined into it read as one session called "A, B". The
      // names move into the sentence, joined the way the language joins a list,
      // and the count is theirs: a session with no row to name is not counted
      // either, or the heading would promise more than the sentence delivers.
      const listed = fmtList(names)
      reportActionFailure(
        i18nT('hooks.useSessionActions.pin_change_failed_named', { names: listed }),
        listed,
        undefined,
        i18nT('hooks.useSessionActions.pin_change_failed_heading', { count: names.length }),
        opts,
      )
    }
    const snapshotVersion = ++batch.snapshotVersion
    try {
      let slots: ChatSlot[]
      for (let attempt = 0; ; attempt += 1) {
        const slotsGeneration = store.getState().dashboard.slotsGeneration ?? 0
        slots = await queryClient.fetchQuery<ChatSlot[]>({
          queryKey: ['chat-slots', 'pin-reconcile', ++pinReconcileRequestId],
          queryFn: () => api.chatSlots() as Promise<ChatSlot[]>,
          staleTime: 0,
          gcTime: 0,
        })
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
      reportRevertedPins(latest.keys())
      const currentSlots = store.getState().dashboard.slots
      const pinnedKeys = new Set(currentSlots.filter(slot => slot.pinned).map(slot => slot.key))
      const currentKeys = new Set(currentSlots.map(slot => slot.key))
      const baselineKeys = new Set(batch.storedBaseline)
      for (const slot of slots) {
        if (slot.pinned && baselineKeys.has(slot.key) && !currentKeys.has(slot.key)) pinnedKeys.add(slot.key)
      }
      const baselineMembership = new Set(batch.baseline)
      const sortableByKey = new Map<string, ChatSlot>()
      for (const slot of slots) sortableByKey.set(slot.key, slot)
      for (const slot of currentSlots) sortableByKey.set(slot.key, slot)
      const fallbackSort = readSessionSortKey()
      const newlyPinnedKeys = [...pinnedKeys]
        .filter(key => !baselineMembership.has(key))
        .sort((a, b) => compareBySort(
          sortableByKey.get(a) ?? { key: a },
          sortableByKey.get(b) ?? { key: b },
          fallbackSort,
        ))
      const authoritativePinnedOrder = [
        ...batch.baseline.filter(key => pinnedKeys.has(key)),
        ...newlyPinnedKeys,
      ]
      commitPinnedSessionSnapshot(
        authoritativePinnedOrder, batch.baseline, newlyPinnedKeys, batch.storedBaseline,
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
            && (dashboard.slotPinGenerations?.[key] ?? 0) === candidate.pinGeneration
            && (current?.pinned ?? false) === candidate.pinned
        })
        .map(([key]) => key))
      const successfulOperations = batch.entries
        .filter(candidate => candidate.succeeded && ownedKeys.has(candidate.key))
        .map(({ key, pinned }) => ({ key, pinned }))
      const expected = new Set(batch.baseline)
      for (const { key, pinned } of successfulOperations) {
        if (pinned) expected.add(key)
        else expected.delete(key)
      }
      const finalMembershipOperations = [...ownedKeys].map(key => ({
        key,
        pinned: expected.has(key),
      }))
      commitPinnedSessionOperations(
        [...successfulOperations, ...finalMembershipOperations],
        batch.baseline,
        batch.storedBaseline,
      )
      for (const key of ownedKeys) {
        const pinned = expected.has(key)
        const current = store.getState().dashboard.slots.find(slot => slot.key === key)?.pinned ?? false
        if (current !== pinned) dispatch(updateSlotPin({ key, pinned }))
      }
      // No server re-read is attempted here, on purpose: this branch IS the
      // failed re-read, and the authoritative pinned state arrives without one.
      // Every accepted `PATCH /api/chat/slots/{slot}/pin` ends in
      // `push_slots_update()`, and a websocket reconnect refetches the whole
      // list. The `invalidateQueries({ queryKey: ['chat-slots'] })` this branch
      // used to end with was never that retry -- no query is registered on that
      // key, so it refreshed nothing (#10204).
      reportRevertedPins(ownedKeys)
    }
  }, [dispatch, queryClient])

  const forkMutation = useMutation({
    mutationFn: (slot: string) => api.forkChatSlot(slot),
    onSuccess: (data, slot) => {
      if (data?.ok && data.key) {
        // Inside the `ok` branch: only a fork that created something makes "No new
        // session was created" false.
        clearActionFailure(`fork:${slot}`)
        queryClient.invalidateQueries({ queryKey: ['slots'] })
        dispatch(switchSlot(data.key))
      }
    },
    // A fork that fails switches to no session, so without this the click is
    // indistinguishable from one that never registered. Its own heading: nothing
    // was created, so the shared "Couldn't update" would name a change that did
    // not happen.
    onError: (err, slot) => reportActionFailure(
      i18nT('hooks.useSessionActions.fork_failed'),
      slotTitle(slot),
      findReport(err.message),
      i18nT('hooks.useSessionActions.fork_failed_heading', { name: slotTitle(slot) }),
      { actionKey: `fork:${slot}` },
    ),
  })

  const pinMutation = useMutation({
    mutationFn: ({ key, pinned }: { key: string; pinned: boolean }) => setSlotPinInOrder(key, pinned),
    onMutate: ({ key, pinned }) => {
      const dashboard = store.getState().dashboard
      const fallbackSort = readSessionSortKey()
      const naturalPinned = dashboard.slots
        .filter(slot => slot.pinned)
        .sort((a, b) => compareBySort(a, b, fallbackSort))
        .map(slot => slot.key)
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
        pinGeneration: 0,
        slotsGeneration: dashboard.slotsGeneration ?? 0,
      }
      batch.entries.push(entry)
      dispatch(updateSlotPin({ key, pinned }))
      entry.pinGeneration = store.getState().dashboard.slotPinGenerations?.[key] ?? 0
      return { batch, entry }
    },
    onSuccess: (_data, _vars, ctx) => ctx
      ? finishPinMutation(ctx.batch, ctx.entry, true)
      : undefined,
    onError: (_err, _vars, ctx) => ctx ? finishPinMutation(ctx.batch, ctx.entry, false) : undefined,
  })

  const slotTitle = (key: string) =>
    store.getState().dashboard.slots.find(s => s.key === key)?.title ?? ''

  // Orchestrator/Autopilot mode toggle (optimistic, server-persisted).
  const modeMutation = useMutation({
    mutationFn: ({ key, newMode }: { key: string; newMode: string }) => api.setSlotMode(key, newMode),
    onMutate: ({ key, newMode }) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === key)?.mode ?? ''
      dispatch(updateSlot({ key, mode: newMode }))
      return { key, prev, newMode }
    },
    // A retry that lands is what a still-showing failure for this slot's mode was
    // waiting on; the key keeps another slot's — or another action's — banner up.
    onSuccess: (_data, { key }) => clearActionFailure(`mode:${key}`),
    onError: (err, _vars, ctx) => {
      if (!ctx) return
      // Guarded rollback: don't clobber a superseding mode toggle.
      const current = store.getState().dashboard.slots.find(s => s.key === ctx.key)?.mode ?? ''
      if (current !== ctx.newMode) return
      dispatch(updateSlot({ key: ctx.key, mode: ctx.prev }))
      reportActionFailure(i18nT('hooks.useSessionActions.mode_change_failed'), slotTitle(ctx.key), findReport(err.message), undefined, { actionKey: `mode:${ctx.key}` })
    },
  })

  // Session reload (relaunch the agent process in place). No optimistic state:
  // the success confirmation is the feed notice the backend appends, arriving
  // over the websocket (and lighting the row's unread indicator for a
  // non-active slot). Failure must NOT be silent -- the user would proceed
  // believing their stale MCP config was refreshed, the exact confusion the
  // feature exists to fix. It reports to the shared in-page notice, which carries
  // the agent hand-off a native alert() cannot. The copy branches on the backend's
  // machine-readable code, because "try again when the session is idle" is a
  // dead end for a slot that LOOKS idle but has sub-agents still working.
  const reloadMutation = useMutation({
    mutationFn: (slot: string) => api.chatSlotReload(slot),
    onSuccess: (_data, slot) => clearActionFailure(`reload:${slot}`),
    onError: (err, slot) => {
      const responded = err instanceof ApiError
      const body = responded ? err.body : ''
      // 409 is the route's "not now" (a turn in flight, a session rebound under
      // it), the one answer waiting for idle can outlast. A 404 for a slot that
      // is gone, a 403, a 5xx also answered, and no amount of idle helps them.
      reportActionFailure(i18nT(body.includes('slot_subagents_running')
        ? 'hooks.useSessionActions.reload_failed_subagents'
        : !responded
          ? 'hooks.useSessionActions.reload_failed'
          : err.status === 409
            ? 'hooks.useSessionActions.reload_failed_busy'
            : 'hooks.useSessionActions.reload_failed_rejected'), slotTitle(slot), findReport(err.message), undefined, { actionKey: `reload:${slot}` })
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
    // Read direction relays to other windows (a deliberate "I've seen this"),
    // watermarked at the slot's newest known message ts. The unread direction
    // stays window-local — markSlotUnread's string form records the manual
    // sentinel, so no other window's relayed read can clear the reminder.
    if (isUnread) {
      const slotTs = store.getState().dashboard.slots.find(s => s.key === slotKey)?.last_ts
      emitSlotRead(slotKey, slotTs)
    }
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
