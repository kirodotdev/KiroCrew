import type { QueryClient } from '@tanstack/react-query'

/** Mutation key shared by every OPTIMISTIC write to the `['chat-folders']` cache.
 *
 *  While one of these is pending, the cache holds a value the server may not
 *  have yet, and a refetch that resolves inside that window paints the older
 *  server tree over it: a folder the person just expanded collapses again, a
 *  dragged folder snaps back, and the next settle-time refetch moves it
 *  forward once more. The live `slots` frame asks for such a refetch whenever
 *  the folder store's generation moves, which is every time an agent files a
 *  session through the dashboard MCP, so during an agent burst the window is
 *  hit constantly. The frame handler therefore leaves the refetch to the
 *  mutation's own settle (see `useFolderMutations` and `useSlotListSync`). */
export const CHAT_FOLDERS_WRITE_KEY = ['chat-folders', 'optimistic-write'] as const

/** Refetch the folder tree once no optimistic folder write is pending.
 *
 *  Called from each write's `onSettled`. A write still counts as pending while
 *  its own `onSettled` runs, and writes fired together (expanding every
 *  collapsed ancestor of a folder) settle together, so a check made inline
 *  would see its siblings pending in every callback and none would refetch.
 *  The check therefore runs on the next task, after every write that settled
 *  in this round has left the pending set. A write still genuinely in flight
 *  runs this again from its own settle, so the last one to finish refetches,
 *  and that refetch also covers any generation move the frame handler
 *  deferred meanwhile. */
export function invalidateFoldersWhenIdle(queryClient: QueryClient): void {
  // One scheduled check per client: writes settling in the same round would
  // otherwise each schedule one and refetch the tree once apiece.
  if (scheduled.has(queryClient)) return
  scheduled.add(queryClient)
  setTimeout(() => {
    scheduled.delete(queryClient)
    if (queryClient.isMutating({ mutationKey: CHAT_FOLDERS_WRITE_KEY }) > 0) return
    void queryClient.invalidateQueries({ queryKey: ['chat-folders'] })
  }, 0)
}
const scheduled = new WeakSet<QueryClient>()
