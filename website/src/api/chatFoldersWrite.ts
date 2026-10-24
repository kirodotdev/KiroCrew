import type { QueryClient } from '@tanstack/react-query'
import type { ChatFolder } from '../types'

/** Mutation key carried by the folder-tree optimistic writes the idle gate
 *  covers — the `useFolderMutations` update/create/delete writes.
 *
 *  One optimistic writer deliberately does NOT carry it: the drag-reorder in
 *  `dnd/useSidebarDrag.ts` writes `['chat-folders']` through `setQueryData`
 *  outside any mutation, so `isMutating` cannot see it and a refetch can still
 *  land inside a reorder's window. Reorder is out of this PR's goal (it is
 *  being rewritten by #15041); a future writer that wants the gate's protection
 *  must run as a mutation carrying this key.
 *
 *  While a keyed write is pending, the cache holds a value the server may not
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

/** Put a just-created folder into the `['chat-folders']` cache, then refetch.
 *
 *  The server's own record goes in before the refetch, so the new folder is in
 *  the tree on the next render: a caller that reveals the folder, or moves a
 *  session into it, right after the create resolves looks the folder up in
 *  this list, and without the seed it would find nothing until the refetch
 *  landed. The refetch still runs and replaces the seed with the authoritative
 *  list.
 *
 *  Only into a list that already loaded. When the first load failed the cache
 *  holds no data, and seeding one folder into it would read as a successful
 *  load: the error notice and its Retry would vanish and the tree would show
 *  that one folder alone.
 *
 *  The refetch goes through the idle gate, not a direct invalidate: a refetch
 *  here would land inside a still-pending update's optimistic window. */
export function seedCreatedChatFolder(queryClient: QueryClient, created: unknown): void {
  const row = created as ChatFolder | null
  if (row && typeof row.id === 'string' && row.id) {
    queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old =>
      old && !old.some(f => f.id === row.id) ? [...old, row] : old)
  }
  invalidateFoldersWhenIdle(queryClient)
}
