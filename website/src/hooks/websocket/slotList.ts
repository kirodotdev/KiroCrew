/** The session list the gateway pushes: full `slots` frames with their side
 *  channels (yolo, channel trust, the folder tree and three process-local
 *  generations), and one-row `slot_patch` frames. */
import { useMemo, useRef } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { sseSlots, sseYolo, setChannelTrusted, sseSlotPatch, fetchSlots, type SlotPatchFrame } from '../../store/dashboardSlice'
import type { ChatSlot, ChatFolder } from '../../types'
import type { FrameData } from './frames'

export interface SlotListSync {
  /** A new connection: forget the last-seen generations and raw frame. */
  resetForConnection(): void
  onSlots(msg: FrameData, data: ChatSlot[], raw: string): void
  onSlotPatch(frame: SlotPatchFrame): void
}

export function useSlotListSync(dispatch: AppDispatch, queryClient: QueryClient): SlotListSync {
  const lastGitlabHostsGenRef = useRef<number | null>(null)
  const lastFoldersGenRef = useRef<number | null>(null)
  const lastGovernanceGenRef = useRef<number | null>(null)
  const lastSlotsRawRef = useRef<string | null>(null)
  const lastSlotsArrayRef = useRef<ChatSlot[] | null>(null)

  return useMemo<SlotListSync>(() => ({
    resetForConnection() {
      // Forget the last-seen allowlist generation: it is process-local to the
      // gateway, so after a restart an equal number can mean a different
      // allowlist. Clearing it makes the next generation frame refetch.
      lastGitlabHostsGenRef.current = null
      // Same process-local reasoning for the folder-tree generation.
      lastFoldersGenRef.current = null
      // …and for the governance-ceiling generation.
      lastGovernanceGenRef.current = null
      // Forget the last raw slots frame too, so a reconnect whose first frame
      // repeats the last one before it cannot swallow that first frame.
      lastSlotsRawRef.current = null
    },
    onSlots(msg, data, raw) {
      // An identical repeat carries identical values for every arm below, but
      // only while no other writer (fetchSlots) has since replaced the list.
      if (raw === lastSlotsRawRef.current
          && store.getState().dashboard.slots === lastSlotsArrayRef.current) return
      lastSlotsRawRef.current = raw
      // Query keys this frame has made stale; flushed once at the end.
      const staleKeys = new Set<'chat-folders' | 'dashboardConfig'>()
      dispatch(sseSlots(data))
      lastSlotsArrayRef.current = store.getState().dashboard.slots
      if (msg.yolo !== undefined) {
        dispatch(sseYolo(msg.yolo))
      }
      if (msg.channelTrusted !== undefined) {
        dispatch(setChannelTrusted(msg.channelTrusted))
      }
      // Seed the ['chat-folders'] query cache from the folder tree carried
      // on this frame so the sidebar groups sessions correctly on the FIRST
      // paint. Sessions arrive on this WS frame the instant the socket
      // connects; the folders otherwise come only from a separate HTTP GET,
      // so without this the sidebar renders every session ungrouped (Unfiled)
      // until that GET resolves, then visibly re-shuffles them into folders.
      //
      // Seed ONLY when the cache has no folder data yet (first paint). Two
      // reasons this must not run on later frames, both from the shipped
      // staleTime: Infinity on this query:
      //   1. A `slots` frame fires on routine session activity, so a frame
      //      landing inside an in-flight folder mutation's optimistic window
      //      (collapse / reorder / rename / move) would overwrite the
      //      optimistic cache value with backend state via a direct
      //      setQueryData — which the mutation's cancelQueries cannot cancel
      //      — snapping the folder back to its pre-action state until
      //      onSettled refetches.
      //   2. The WS payload omits per-folder `history_count` (the backend
      //      computes it via a synchronous session scan that must not run on
      //      this hot path). Seeding count-less data marks the query fresh,
      //      so a mount-time query would skip GET /api/chat/folders and the
      //      counts (the "hide when empty" filter's input) would never load.
      // So seed the tree once, then invalidate to let the HTTP GET backfill
      // counts; after the cache is populated, live frames leave it alone and
      // folder create/rename/move propagate through their own mutation +
      // invalidate path as before.
      //
      // Guard on `existing === undefined` (cache NEVER populated), NOT on
      // `!existing || length === 0`: a user with genuinely zero folders has
      // the HTTP GET cache the empty array `[]`, and `[].length === 0` would
      // then re-match on EVERY subsequent slots frame — re-seeding `[]` and
      // re-invalidating in a loop, hammering the session-scanning
      // GET /api/chat/folders. `undefined` fires exactly once, on first paint.
      if (Array.isArray(msg.folders)) {
        const existing = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])
        if (existing === undefined) {
          queryClient.setQueryData<ChatFolder[]>(['chat-folders'], msg.folders as ChatFolder[])
          // Backfill history_count (omitted from the WS payload) — the seed
          // marked the query fresh, so nudge the real GET to run.
          staleKeys.add('chat-folders')
        }
      }
      // Refetch the folder tree when the STORE changed, for every source of
      // change — an agent, another tab, another device — not just this tab's
      // own mutation. The seed above deliberately fires once, so without
      // this a folder created anywhere else stays invisible until a reload:
      // ['chat-folders'] carries the app-wide staleTime: Infinity, and the
      // sessions do arrive (their folder_id points at a folder this tab has
      // never heard of, so they render as Unfiled and the folder looks like
      // it was never created).
      //
      // invalidate, never setQueryData: a refetch is what brings back the
      // `history_count` the WS payload omits, and the generation only moves
      // on a real store write, so the value a refetch resolves to already
      // includes the mutation whose optimistic window it might land in.
      //
      // Same process-local trap as gitlabHostsGeneration below: the counter
      // resets with the gateway, so a restart can hand back a number equal
      // to the one this client last saw over a different tree. The first
      // generation frame of each connection is therefore "unknown, refetch",
      // and comparison only happens within one connection.
      if (typeof msg.foldersGeneration === 'number') {
        const prevFoldersGen = lastFoldersGenRef.current
        lastFoldersGenRef.current = msg.foldersGeneration
        if (prevFoldersGen === null || prevFoldersGen !== msg.foldersGeneration) {
          staleKeys.add('chat-folders')
        }
      }
      // Refresh the cached GitLab-hosts allowlist when it may have changed.
      // The generation is PROCESS-local, so a gateway restart can hand out a
      // number equal to the one this client last saw even though the
      // allowlist on disk changed. Treat the first generation frame of each
      // connection as "unknown, refetch" and only compare within a
      // connection — one extra fetch per connect, never a stale allowlist.
      if (typeof msg.gitlabHostsGeneration === 'number') {
        const prevGen = lastGitlabHostsGenRef.current
        lastGitlabHostsGenRef.current = msg.gitlabHostsGeneration
        if (prevGen === null || prevGen !== msg.gitlabHostsGeneration) {
          staleKeys.add('dashboardConfig')
        }
      }
      // Same contract for the governance ceiling: a centrally pushed policy
      // swaps it mid-session and bumps this generation, and the config
      // endpoint derives `social_share_enabled` from that ceiling. Without
      // this the cached answer would keep offering the Share entry for the
      // rest of its stale window after the fleet withdrew it.
      if (typeof msg.governanceGeneration === 'number') {
        const prevGovGen = lastGovernanceGenRef.current
        lastGovernanceGenRef.current = msg.governanceGeneration
        if (prevGovGen === null || prevGovGen !== msg.governanceGeneration) {
          staleKeys.add('dashboardConfig')
        }
      }
      // One invalidation per key per frame. Several arms above can each
      // ask for the same key on one frame (the first frame of a connection
      // trips both generation arms, and the folder seed plus its generation),
      // and every invalidate cancels the refetch the previous one started
      // and issues another, so separate calls put two identical GETs on the
      // wire where one answers them all.
      for (const key of staleKeys) queryClient.invalidateQueries({ queryKey: [key] })
    },
    onSlotPatch(frame) {
      const dashboard = store.getState().dashboard
      const removed = new Set(frame.removed ?? [])
      for (const slot of removed) queryClient.resetQueries({ queryKey: ['dashboard-card', slot] })
      const hasUnknownRow = (frame.slots ?? []).some(row =>
        typeof row?.key === 'string'
        && !removed.has(row.key)
        && !Object.prototype.hasOwnProperty.call(dashboard.closingSlots ?? {}, row.key)
        && !dashboard.slots.some(slot => slot.key === row.key))
      // A stale list can remove a newly created row; its later patch cannot
      // restore that row, so the authoritative list repairs the omission.
      if (hasUnknownRow) dispatch(fetchSlots())
      dispatch(sseSlotPatch(frame))
    },
  }), [dispatch, queryClient])
}
