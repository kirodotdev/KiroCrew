import { useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { store, useAppDispatch } from '../store'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { updateSlotPin, updateSlot, markSlotRead, markSlotUnread } from '../store/dashboardSlice'
import { copySessionLink } from '../utils/shareUrl'
import { useMoveSlotToFolder } from './useMoveSlotToFolder'
import { loadChatConfig } from '../pages/chat/ChatSettings'
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
  /** Merge a fork's summary back into its parent, then archive the fork (#3816). */
  mergeBack: (slotKey: string) => void
  /** True while a merge-back is in flight — the summarization pass can take
   * 10–30s, so callers disable the affordance and show progress (UX review). */
  mergeBackPending: boolean
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

  const forkMutation = useMutation({
    mutationFn: (slot: string) => api.forkChatSlot(slot),
    onSuccess: (data) => {
      if (data?.ok && data.key) {
        queryClient.invalidateQueries({ queryKey: ['slots'] })
        dispatch(switchSlot(data.key))
      }
    },
  })

  // Merge a fork's summary back into its parent, then archive the fork (#3816).
  // On success we switch to the PARENT (where the merged block now lives), not
  // the fork, which is closed. Failure must not be silent — the machine-readable
  // code distinguishes the cases the user can act on (wait for a running turn,
  // already merged) from a generic failure, mirroring reload's error copy.
  const mergeBackMutation = useMutation({
    mutationFn: (slot: string) => api.mergeBackChatSlot(slot),
    onSuccess: (data) => {
      if (data?.ok && data.parent_key) {
        queryClient.invalidateQueries({ queryKey: ['slots'] })
        dispatch(switchSlot(data.parent_key))
      }
    },
    onError: (err) => {
      const body = err instanceof ApiError ? err.body : ''
      // archive_failed means the merge COMMITTED (the block is durably in the
      // parent) and only the fork's archive save failed — the generic "parent
      // may be gone" copy would misstate a merge that succeeded (UX review).
      // not_found is also in-hand (a second tab merging a fork the first
      // already merged and popped), so it gets the already-merged copy rather
      // than the hedged fallback (UX review).
      const keyName = body.includes('summary_turn_running')
        ? 'hooks.useSessionActions.merge_back_failed_running'
        : body.includes('merge_in_progress')
          ? 'hooks.useSessionActions.merge_back_failed_running'
          : body.includes('parent_busy')
            ? 'hooks.useSessionActions.merge_back_failed_parent_busy'
            : body.includes('already_merged') || body.includes('not_found')
              ? 'hooks.useSessionActions.merge_back_failed_already_merged'
              : body.includes('summary_unavailable')
                ? 'hooks.useSessionActions.merge_back_failed_no_summary'
                : body.includes('nothing_to_merge')
                  ? 'hooks.useSessionActions.merge_back_failed_nothing'
                  : body.includes('archive_failed')
                    ? 'hooks.useSessionActions.merge_back_failed_archive'
                    : body.includes('parent_save_failed')
                      ? 'hooks.useSessionActions.merge_back_failed_parent_save'
                      : body.includes('parent_missing')
                        ? 'hooks.useSessionActions.merge_back_failed_parent_missing'
                        : 'hooks.useSessionActions.merge_back_failed'
      alert(i18nT(keyName))
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
  const { mutate: mergeBackMutate, isPending: mergeBackPending } = mergeBackMutation
  const { mutate: pinMutate } = pinMutation
  const { mutate: modeMutate } = modeMutation
  const { mutate: reloadMutate } = reloadMutation

  const duplicate = useCallback((slotKey: string) => { forkMutate(slotKey) }, [forkMutate])

  const mergeBack = useCallback((slotKey: string) => { mergeBackMutate(slotKey) }, [mergeBackMutate])

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

  return { duplicate, mergeBack, mergeBackPending, toggleRead, togglePin, toggleMode, copyLink, move, reload, close }
}
