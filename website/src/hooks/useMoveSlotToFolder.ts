import { useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { store, useAppDispatch } from '../store'
import { updateSlotFolder } from '../store/dashboardSlice'

/**
 * Single source of truth for moving a chat session into a folder (or to root).
 *
 * Both the sidebar row menus and the session-header dropdown — plus the
 * sidebar's drag-to-folder — assign a slot to a folder with the same optimistic
 * semantics: update Redux immediately (`onMutate`), fire `api.setSlotFolder`,
 * and roll back to the prior `folder_id` if the request fails (`onError`). One
 * shared hook for `assignToFolder` (ChatSidebar) and `moveToFolder`
 * (ChatHeaderMenu) keeps the two call sites from desyncing.
 *
 * Uses `useMutation` (the package's standard server-write pattern, cf.
 * `pinMutation` in useSessionActions) so it gets proper pending/error state.
 * The previous folder is read from the store at mutate time (not captured from
 * a passed-in slots array), so callers stay stateless — they just call
 * `move(slotKey, folderId)`.
 */
export function useMoveSlotToFolder(): (slotKey: string, folderId: string | null) => void {
  const dispatch = useAppDispatch()
  const { mutate } = useMutation({
    mutationFn: ({ slotKey, folderId }: { slotKey: string; folderId: string | null }) =>
      api.setSlotFolder(slotKey, folderId),
    onMutate: ({ slotKey, folderId }) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === slotKey)?.folder_id ?? ''
      const target = folderId || ''
      dispatch(updateSlotFolder({ key: slotKey, folderId: target }))
      return { slotKey, prev, target }
    },
    onError: (_err, _vars, ctx) => {
      if (!ctx) return
      // Guarded rollback: only revert if a later move hasn't already changed
      // this slot's folder. Without this, a rapid move A→B where the A call
      // fails would clobber B's optimistic update even though B succeeded.
      const current = store.getState().dashboard.slots.find(s => s.key === ctx.slotKey)?.folder_id ?? ''
      if (current === ctx.target) dispatch(updateSlotFolder({ key: ctx.slotKey, folderId: ctx.prev }))
    },
  })
  // `mutate` is referentially stable across renders, so the returned callback is too.
  return useCallback((slotKey: string, folderId: string | null) => {
    mutate({ slotKey, folderId })
  }, [mutate])
}
