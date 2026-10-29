/** Per-tab chunk watch: tell the gateway which slots this tab renders a stream for.
 *
 *  The socket declares `chunk_scope.1` in `?caps=` (see `connection.ts`). A
 *  gateway that knows it then sends a slot's `chat_chunk`, `chat_thinking`,
 *  `tool_call` and `tool_result` frames only while this tab's `chunk_watch` set
 *  names the slot. Every other slot reaches the tab as `chat_phase` and
 *  `tool_status` frames, which carry just what the session list's status line
 *  draws, beside the coarse frames every tab gets (`slots`, `chat_message`,
 *  `chat_status`, `chat_done`, ...).
 *
 *  The set is every slot with a live transcript on screen: the active slot, each
 *  mounted `ChatPane` (split panes, the Crew Members DM thread) and each app
 *  embed. A hidden tab keeps only the active slot: voice auto-speak reads its
 *  streaming row while the window is in the background. The frame is sent on every open and on every
 *  change; a gateway that predates the capability ignores it and keeps sending
 *  everything, so nothing waits on an answer.
 *
 *  A slot that joins the set mid-turn has missed the chunks the gateway held
 *  back. The gateway's `chunk_watch_ack` names such slots under `catchup`, after
 *  the new set took effect, so a transcript re-read started then overlaps every
 *  live chunk that follows (the chunk seq floor drops the overlap). */
import type { QueryClient } from '@tanstack/react-query'
import { observedPaneSlots } from '../../api/slotMessagesQuery'

/** The gateway keeps this many slots of one frame (`MAX_CHUNK_WATCH_SLOTS`). */
export const MAX_CHUNK_WATCH_SLOTS = 64

export interface ChunkWatchSources {
  queryClient: QueryClient
  activeSlot(): string | null
  appSlots(): readonly string[]
  hidden(): boolean
}

/** The `slots` of a `chunk_watch` frame: the active slot first (it survives the
 *  gateway's cap), then the rest sorted, without duplicates. A hidden tab keeps
 *  only the active slot. */
export function chunkWatchSlots(src: ChunkWatchSources): string[] {
  const active = src.activeSlot()
  if (src.hidden()) return active ? [active] : []
  const rest = new Set<string>([...observedPaneSlots(src.queryClient), ...src.appSlots()])
  if (active) rest.delete(active)
  const out = [...(active ? [active] : []), ...[...rest].filter(Boolean).sort()]
  return out.slice(0, MAX_CHUNK_WATCH_SLOTS)
}

export interface ChunkWatch {
  /** A new socket opened: send the current set on it. */
  onOpen(): void
  /** Something the set is built from may have changed. */
  sync(): void
  /** Stop following the query cache. */
  dispose(): void
}

/** Send `chunk_watch` through *send* on each open and whenever the set changes. */
export function createChunkWatch(src: ChunkWatchSources, send: (payload: object) => boolean | void): ChunkWatch {
  let lastSent: string | null = null

  const sync = () => {
    const slots = chunkWatchSlots(src)
    const signature = JSON.stringify(slots)
    if (signature === lastSent) return
    // A send on a closed socket is retried by the next open's `onOpen`.
    if (send({ type: 'chunk_watch', slots }) === false) return
    lastSent = signature
  }

  const unsubscribe = src.queryClient.getQueryCache().subscribe((event) => {
    if (event.type === 'observerAdded' || event.type === 'observerRemoved' || event.type === 'removed') sync()
  })

  return {
    onOpen() {
      lastSent = null
      sync()
    },
    sync,
    dispose() {
      unsubscribe()
    },
  }
}

/** The slots of a `chunk_watch_ack` frame's `catchup` list, validated. */
export function catchupSlots(data: unknown): string[] {
  const list = (data as { catchup?: unknown } | null)?.catchup
  return Array.isArray(list) ? list.filter((s): s is string => typeof s === 'string' && !!s) : []
}
