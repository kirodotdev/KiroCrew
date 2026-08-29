import { useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { store, useAppDispatch } from '../store'
import { appendSlotMessage, deleteSlot, switchSlot } from '../store/chatSlice'
import { updateSlotPin, updateSlot, markSlotRead, markSlotUnread } from '../store/dashboardSlice'
import { emitSlotRead } from '../lib/slotReadRelay'
import { copySessionLink } from '../utils/shareUrl'
import { useMoveSlotToFolder } from './useMoveSlotToFolder'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { commitPinnedSessionOperations, commitPinnedSessionSnapshot, readPinnedSessionOrder, reconcilePinnedSessionOrder } from '../utils/pinnedSessionOrder'
import { beginSlotQuiesce, slotHasUnsentWorkHere, slotUnsentWorkSource, type UnsentWorkSource } from '../utils/slotComposerRegistry'
import {
  anotherWindowHoldsComposer,
  awaitClosingAcks,
  clearClosingIntent,
  closingIntentVetoed,
  publishClosingIntent,
} from '../utils/slotClosingIntent'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'
import { compareBySort, readSessionSortKey } from '../pages/chat/sessionOrder'

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
  /**
   * Close a session behind the confirm-on-close preference.
   *
   * `beforeDelete` runs AFTER the confirm and BEFORE the delete; returning
   * `false` aborts — which is how a caller sequences work on the close without
   * giving up the right to abort it.
   *
   * Resolves to NOTHING. It used to answer whether the slot was deleted, and
   * every counted caller threw that away: the menu `void`s it, the shared option
   * dispatcher discards the await, and `ChatSidebar`'s prop is declared
   * `(key: string) => void`. A caller that needs to know already learns it from
   * its own `beforeDelete`.
   */
  close: (
    slotKey: string,
    opts?: {
      beforeDelete?: () => boolean | Promise<boolean>
      /**
       * Confirm even when the user's `confirmCloseSession` preference is off.
       *
       * For a close whose affordance was authored by a MODEL rather than by the
       * product: an `[OPTION-ACTIONS: close=That's all]` chip is one click, its
       * label is arbitrary model prose, and `confirmCloseSession` defaults to
       * `false` — so without this the tab goes away with neither a stated
       * consequence nor a confirm. A caller that put the affordance on screen
       * itself (the session menu, a keyboard shortcut) has no such problem and
       * leaves this unset.
       */
      forceConfirm?: boolean
      // Replaces the generic prompt, so a forced confirm can name the label the
      // user just clicked and say where the transcript goes.
      confirmMessage?: string
      /**
       * Where a failed DELETE is reported. A caller owning an `ErrorNotice`
       * passes its own setter; without one the failure lands as an error row in
       * the session that survived, because the alternative was discarding it.
       */
      onError?: (message: string) => void
    },
  ) => Promise<void>
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

  /**
   * Close a session, honouring the confirm-on-close preference.
   *
   * `beforeDelete` runs AFTER the confirm and BEFORE the delete, and a `false`
   * return aborts the close. That ordering exists for one caller and one reason:
   * the option-action dispatch writes a PERMANENT `inject` breadcrumb row, and
   * writing it before the confirm left the transcript asserting a close that the
   * user then cancelled. Moving the write into this window keeps both properties
   * at once — nothing is written when the user declines, and the close is still
   * refused if the write does not land.
   *
   * Resolves to NOTHING regardless of outcome, matching the contract on the
   * interface above: the signature is `Promise<void>` and every `return` here is
   * bare. A caller needing the outcome learns a REFUSAL from its own
   * `beforeDelete` and a FAILED delete from `onError`.
   */
  const close = useCallback(async (
    slotKey: string,
    opts?: {
      beforeDelete?: () => boolean | Promise<boolean>
      forceConfirm?: boolean
      /** Replaces the generic prompt. For a MODEL-authored affordance, whose own
       *  label is the only thing the user recognises at the moment of clicking. */
      confirmMessage?: string
      /** Reports a failed DELETE; unset routes it to the surviving session. */
      onError?: (message: string) => void
    },
  ): Promise<void> => {
    // `confirmCloseSession` governs the HABITUAL "are you sure"; silencing it is not consent
    // to lose the only copy of a draft, so unsent work summons its own confirm on every route.
    const prefConfirm = loadChatConfig().confirmCloseSession || opts?.forceConfirm === true
    // The SOURCE, not just the boolean: discarding it sent the user hunting through windows
    // that may not exist, over a draft in front of them.
    const unsentAt = slotUnsentWorkSource(slotKey)
    const unsent = unsentAt !== null
    const mustConfirm = prefConfirm || unsent
    const base = opts?.confirmMessage ?? i18nT('hooks.useSessionActions.close_this_session')
    const prompt = unsentAt
      ? i18nT(unsentConfirmKey(unsentAt), { base })
      : base
    if (mustConfirm && !confirm(prompt)) return
    if (opts?.beforeDelete && !(await opts.beforeDelete())) return
    // `unsent` above is a SNAPSHOT, and both gates just passed are windows another window
    // can write in: `confirm` blocks only this thread, `beforeDelete` awaits the network.

    // Re-asked on EVERY route, not only where a dialog was already due: work that appeared
    // during the await is exactly the case a preference about routine confirms cannot speak to.
    const lateAt = unsent ? null : slotUnsentWorkSource(slotKey)
    if (lateAt) {
      if (!confirm(i18nT(unsentConfirmKey(lateAt), { base }))) return
    }
    // ASK before deleting: the registry publishes a claim in a layout effect, so a keystroke
    // inside the DELETE round trip lands after this thread already read the tier.
    const holderElsewhere = anotherWindowHoldsComposer()
    const intent = holderElsewhere ? publishClosingIntent(slotKey) : null // nobody to answer -> no wait
    try {
      // A FAILED publish while another window HOLDS a composer is a refusal: that window
      // cannot be asked, and every read below is LOCAL and cannot see its draft.
      if (holderElsewhere && intent === null) {
        const notice = i18nT('hooks.useSessionActions.close_vetoed_unsent')
        if (opts?.onError) opts.onError(notice)
        else {
          try {
            dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'error', content: notice, cls: '' } }))
          } catch {
            /* the refusal already stopped the delete; the notice is best effort */
          }
        }
        return
      }
      // Consent covers the draft the user was WARNED about, so it gates only the LOCAL
      // re-read. A veto is another window: `storage` never fires in the writing one.
      const consentedAt = unsentAt ?? lateAt
      if (intent) {
        await awaitClosingAcks()
        if (closingIntentVetoed(intent) || (consentedAt === null && slotUnsentWorkSource(slotKey) !== null)) {
          const notice = i18nT('hooks.useSessionActions.close_vetoed_unsent')
          if (opts?.onError) opts.onError(notice)
          else {
            try {
              dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'error', content: notice, cls: '' } }))
            } catch {
              /* the refusal already stopped the delete; the notice is best effort */
            }
          }
          return
        }
      }
      // COMMIT BOUNDARY, and NOT gated on `intent`: a null intent means no OTHER window holds
      // a composer, so the single-window close had no pre-commit re-read at all.
      if ((intent && closingIntentVetoed(intent))
        || (consentedAt === null && slotHasUnsentWorkHere(slotKey))) {
        const notice = i18nT('hooks.useSessionActions.close_vetoed_unsent')
        if (opts?.onError) opts.onError(notice)
        return
      }
      // QUIESCED for the whole round-trip and released on SETTLE: a composer taking a
      // keystroke now can see a close is committing and leave a durable trace for it.
      const workBefore = slotHasUnsentWorkHere(slotKey)
      const releaseQuiesce = beginSlotQuiesce(slotKey)
      try {
        await dispatch(deleteSlot(slotKey)).unwrap()
      } finally {
        releaseQuiesce()
      }
      // APPEARED, not merely present, and storage-free so it reaches the single-window path.
      // Its OWN copy, never the pre-commit veto text: the delete has already succeeded, so
      // "not closed" beside a vanishing tab asserts the one thing the code knows is false.
      const appeared = !workBefore && slotHasUnsentWorkHere(slotKey)
      if (appeared || (intent && closingIntentVetoed(intent))) {
        const late = i18nT('hooks.useSessionActions.close_committed_late_draft')
        if (opts?.onError) opts.onError(late)
      }
    } catch (err) {
      // A rejected DELETE leaves the session alive, so silence here reads as a
      // close that worked until the tab comes back or shows up twice.
      // `instanceof Error` alone: `ApiError` extends it, so naming both added nothing and
      // made this line depend on that export existing on every mock of the api module.
      const message = err instanceof Error
        ? err.message || i18nT('hooks.useSessionActions.close_failed')
        : i18nT('hooks.useSessionActions.close_failed')
      if (opts?.onError) opts.onError(message)
      // The surviving slot is the one place guaranteed still on screen; a caller
      // owning an ErrorNotice passes `onError` and renders there instead.
      else {
        try {
          dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'error', content: message, cls: '' } }))
        } catch (rowErr) {
          // Third tier, below the sink and the row, reached only by a store that cannot
          // hold one -- the real store always can. DEV-only, like `safeStorage`.
          if (import.meta.env.DEV) {
            // eslint-disable-next-line no-console
            console.warn('useSessionActions: close failed and its notice could not be shown', message, rowErr)
          }
        }
      }
    } finally {
      // Every exit: a stranded intent would make the next close in another window read a
      // veto that no live composer stands behind.
      clearClosingIntent(slotKey, intent)
    }
  }, [dispatch])

  return { duplicate, toggleRead, togglePin, toggleMode, copyLink, move, reload, close }
}

/**
 * The confirm rider for each place the unsent work can be, so the user is sent to ONE place.
 *
 * Mirrors `useOptionActionDispatch`'s abort notices, for the same reason: the registry and
 * the claim already distinguish this window from another, and hedging across both made the
 * reader check two places when the code knew which. `elsewhere` keeps the hedge because
 * there the surface genuinely is not identifiable.
 *
 * The route picks the SUBJECT. A close dialog is about the session named in its own base
 * string, but resume names the session being OPENED, so there "this session" would point at
 * the wrong tab — the one whose draft is safe.
 */
export type ConfirmRoute = 'close' | 'resume'

export function unsentConfirmKey(at: UnsentWorkSource, route: ConfirmRoute = 'close'): string {
  if (route === 'resume') {
    if (at === 'here') return 'hooks.useSessionActions.resume_unsent_confirm_here'
    if (at === 'other-window') return 'hooks.useSessionActions.resume_unsent_confirm_window'
    if (at === 'unverifiable') return 'hooks.useSessionActions.resume_unsent_confirm_unverifiable'
    return 'hooks.useSessionActions.resume_unsent_confirm'
  }
  if (at === 'here') return 'hooks.useSessionActions.close_unsent_confirm_here'
  if (at === 'other-window') return 'hooks.useSessionActions.close_unsent_confirm_window'
  // Unreadable storage cannot support "it will be lost": the confirm still fires,
  // but it must not assert a draft nobody could read.
  if (at === 'unverifiable') return 'hooks.useSessionActions.close_unsent_confirm_unverifiable'
  return 'hooks.useSessionActions.close_unsent_confirm'
}
