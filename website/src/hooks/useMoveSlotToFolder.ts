import { useCallback, useSyncExternalStore } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { store, useAppDispatch } from '../store'
import { updateSlotFolder } from '../store/dashboardSlice'

/* The session the user last moved here, in memory only: it marks a move, so a
 * session created into a folder, or seen after a reload, is never marked. */
let movedSlot: string | null = null
const movedListeners = new Set<() => void>()
export function setMovedSlot(key: string | null) {
  if (movedSlot === key) return
  movedSlot = key
  movedListeners.forEach(l => l())
}
export function useMovedSlot(): string | null {
  return useSyncExternalStore(cb => { movedListeners.add(cb); return () => { movedListeners.delete(cb) } }, () => movedSlot)
}

/** Options for a single move. */
export type MoveSlotOptions = {
  /**
   * Called once the server has ACKNOWLEDGED this move.
   *
   * The optimistic write lands in the store immediately, which is not the same
   * fact: a caller that treats the store as proof (the drag-move undo bar) would
   * offer an undo before the original move is durable, and the two writes can
   * then race and cancel each other.
   */
  onCommitted?: () => void
}

/**
 * Single source of truth for moving a chat session into a folder (or to root).
 *
 * Both the sidebar row menus and the session-header dropdown — plus the
 * sidebar's drag-to-folder — assign a slot to a folder with the same optimistic
 * semantics: update Redux immediately (`onMutate`), fire `api.setSlotFolder`,
 * and roll back to the prior `folder_id` if the request fails (`onError`). This
 * hook collapses what were two near-identical implementations (`assignToFolder`
 * in ChatSidebar and `moveToFolder` in ChatHeaderMenu) into one, removing the
 * desync risk.
 *
 * Uses `useMutation` (the package's standard server-write pattern, cf.
 * `pinMutation` in useSessionActions) so it gets proper pending/error state.
 * The previous folder is read from the store at mutate time (not captured from
 * a passed-in slots array), so callers stay stateless — they just call
 * `move(slotKey, folderId)`.
 */
export function useMoveSlotToFolder(): (
  slotKey: string,
  folderId: string | null,
  opts?: MoveSlotOptions,
) => void {
  const { mutate } = useMoveSlotMutation()
  // `mutate` is referentially stable across renders, so the returned callback is too.
  return useCallback((slotKey: string, folderId: string | null, opts?: MoveSlotOptions) => {
    mutate({ slotKey, folderId }, {
      // Per-call callback, running in addition to the hook's own rollback
      // handler: the caller learns the server ACKNOWLEDGED the write without
      // taking over the rollback.
      onSuccess: () => { opts?.onCommitted?.() },
    })
  }, [mutate])
}

/**
 * The same move, as a promise that settles with the server's answer: resolves
 * once the write is acknowledged and rejects with its error after the rollback.
 *
 * For a caller that must not report success before the move is durable, or
 * must show the failure itself (the session menus' New folder dialog, which
 * has just created the destination). The promise is per call, so a second move
 * through the same hook cannot swallow this one's outcome the way per-call
 * `mutate` callbacks can. Callers own the rejection.
 */
export function useMoveSlotToFolderAsync(): (slotKey: string, folderId: string | null) => Promise<void> {
  // 'always', not the default 'online': the gateway is often local, and an
  // offline browser would PAUSE the mutation before mutationFn, so the promise
  // a caller awaits (and any dismissal it holds while waiting) would not settle
  // until the browser came back online. Running it lets the request reach the
  // gateway, or fail fast through the usual rollback.
  const { mutateAsync } = useMoveSlotMutation('always')
  return useCallback(
    async (slotKey: string, folderId: string | null) => { await mutateAsync({ slotKey, folderId }) },
    [mutateAsync],
  )
}

/** The optimistic move with its guarded rollback, shared by both hooks above.
 *  `networkMode` left unset keeps React Query's default for the existing hook. */
function useMoveSlotMutation(networkMode?: 'always') {
  const dispatch = useAppDispatch()
  return useMutation({
    networkMode,
    mutationFn: ({ slotKey, folderId }: { slotKey: string; folderId: string | null }) =>
      api.setSlotFolder(slotKey, folderId),
    onMutate: ({ slotKey, folderId }) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === slotKey)?.folder_id ?? ''
      const target = folderId || ''
      dispatch(updateSlotFolder({ key: slotKey, folderId: target }))
      return { slotKey, prev, target }
    },
    onSuccess: (_data, { slotKey }) => { setMovedSlot(slotKey) },
    onError: (_err, _vars, ctx) => {
      if (!ctx) return
      // Guarded rollback: only revert if a later move hasn't already changed
      // this slot's folder. Without this, a rapid move A→B where the A call
      // fails would clobber B's optimistic update even though B succeeded.
      const current = store.getState().dashboard.slots.find(s => s.key === ctx.slotKey)?.folder_id ?? ''
      if (current === ctx.target) dispatch(updateSlotFolder({ key: ctx.slotKey, folderId: ctx.prev }))
    },
  })
}
