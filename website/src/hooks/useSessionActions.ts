import { useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { store, useAppDispatch } from '../store'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { updateSlotPin, updateSlot, markSlotRead, markSlotUnread } from '../store/dashboardSlice'
import { copySessionLink } from '../utils/shareUrl'
import { useMoveSlotToFolder } from './useMoveSlotToFolder'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import { slotUnsentWorkSource, type UnsentWorkSource } from '../utils/slotComposerRegistry'
import { i18nT } from '../i18n/t'

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
    },
  ) => Promise<void>
}

export function useSessionActions(mode?: string): SessionActions {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const moveSlotToFolder = useMoveSlotToFolder()

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
    mutationFn: ({ key, pinned }: { key: string; pinned: boolean }) => api.setSlotPin(key, pinned),
    onMutate: ({ key, pinned }) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === key)?.pinned ?? false
      dispatch(updateSlotPin({ key, pinned }))
      return { key, prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx) dispatch(updateSlotPin({ key: ctx.key, pinned: ctx.prev }))
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    },
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
   * Resolves to NOTHING, matching the contract on the interface above: the
   * signature is `Promise<void>` and every `return` here is bare, so no caller
   * can learn the outcome from the promise. One that needs to know learns it
   * from its own `beforeDelete` — the seam this ordering exists to give it.
   */
  const close = useCallback(async (
    slotKey: string,
    opts?: {
      beforeDelete?: () => boolean | Promise<boolean>
      forceConfirm?: boolean
      /** Replaces the generic prompt. For a MODEL-authored affordance, whose own
       *  label is the only thing the user recognises at the moment of clicking. */
      confirmMessage?: string
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
    dispatch(deleteSlot(slotKey))
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
    return 'hooks.useSessionActions.resume_unsent_confirm'
  }
  if (at === 'here') return 'hooks.useSessionActions.close_unsent_confirm_here'
  if (at === 'other-window') return 'hooks.useSessionActions.close_unsent_confirm_window'
  return 'hooks.useSessionActions.close_unsent_confirm'
}
