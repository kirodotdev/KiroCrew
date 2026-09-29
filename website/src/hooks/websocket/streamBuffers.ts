/** The socket's three bounded coalescing pipelines.
 *
 *  Streaming frames arrive per token; dispatching each one would recompute
 *  the O(N) displayItems / index maps (and re-rank the sidebar) per token.
 *  Each pipeline buffers instead and lands its burst in one flush per
 *  animation frame:
 *   - the chat stream: per-slot content chunks plus reasoning text, one
 *     dispatch per kind per slot;
 *   - subagent streams: per-(slot, agent) text, one batch dispatch;
 *   - slot recency: the newest activity timestamp, one dispatch per slot.
 *  A UI animation can hold all three (lib/streamHold.ts). In a hidden window,
 *  where requestAnimationFrame never runs, the chat and subagent pipelines
 *  are bounded by their 50k-char overflow flushes; slot recency keeps one
 *  entry per slot. */
import { useEffect, useMemo, useRef } from 'react'
import { store, type AppDispatch } from '../../store'
import { sseChatMessage, sseThinkingChunk, sseSubagentBatchChunks } from '../../store/chatSlice'
import { touchSlotActivity } from '../../store/dashboardSlice'
import { streamingFlushHoldMs } from '../../lib/streamHold'
import { registerPendingChunkDrain } from '../../lib/pendingChunkDrain'
import type { SocketConnection } from './connection'

/** One buffered chunk: its text (gap marker included) and the seq it carried,
 *  kept apart so the reducer can hold each part against the slot's replay
 *  floor and drop exactly the chunks a snapshot already covers. */
type ChunkPart = { seq: number | undefined; text: string }
export type ChunkBufEntry = { parts: ChunkPart[]; lastSeq: number | undefined; gen: string | undefined; thinking: string; chars: number }
const newChunkBufEntry = (): ChunkBufEntry => {
  return { parts: [], lastSeq: undefined, gen: undefined, thinking: '', chars: 0 }
}
const bufferedText = (entry: ChunkBufEntry): string => entry.parts.map((p) => p.text).join('')

/** Buffered chars (content + thinking) per slot above which the chunk buffer
 *  is flushed synchronously instead of waiting for the next animation frame.
 *  requestAnimationFrame is suspended while the window is hidden, so a
 *  backgrounded renderer streaming a long turn would otherwise buffer the
 *  entire turn. The subagent buffer (bufferSubagentChunk) flushes at this
 *  same 50 KB threshold for the same reason. */
const CHUNK_BUF_FLUSH_CHARS = 50_000

/** A pipeline's pending flush: at most one animation frame or timer per burst. */
type PendingFlush = {
  scheduled: boolean
  raf: number | null
  timer: ReturnType<typeof setTimeout> | null
}

const newPendingFlush = (): PendingFlush => ({ scheduled: false, raf: null, timer: null })

/** Schedule `flush` for the next frame unless one is already pending. While a
 *  UI animation holds the pipelines (see lib/streamHold.ts), defer the flush
 *  to the hold's end instead of the next frame. The buffer keeps absorbing
 *  meanwhile, and the guard flag stays set, so the whole burst lands as ONE
 *  flush when the hold lapses. A flush already scheduled when a hold STARTS
 *  still fires — at most one frame leaks into the slide. */
function scheduleFlush(pending: PendingFlush, flush: () => void): void {
  if (pending.scheduled) return
  pending.scheduled = true
  const hold = streamingFlushHoldMs()
  if (hold > 0) { pending.timer = setTimeout(() => flush(), hold + 16); return }
  if (typeof requestAnimationFrame === 'function') pending.raf = requestAnimationFrame(() => flush())
  else pending.timer = setTimeout(() => flush(), 16)
}

/** Cancel the pending frame or timer, leaving the bookkeeping as it is. */
function cancelFlush(pending: PendingFlush): void {
  if (pending.raf != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(pending.raf)
  if (pending.timer != null) clearTimeout(pending.timer)
}

/** Cancel the pending frame or timer and forget it. A synchronous flush does
 *  this first: nulling the ids without cancelling would orphan a frame
 *  (uncancellable by unmount/reconnect cleanup, firing a stale flush). From
 *  the frame callback itself the id has already fired, so the cancel is a
 *  no-op. */
function resetFlush(pending: PendingFlush): void {
  cancelFlush(pending)
  pending.scheduled = false
  pending.raf = null
  pending.timer = null
}

export interface StreamBuffersDeps {
  dispatch: AppDispatch
  socket: SocketConnection
  /** Called after a flush landed content in the active slot. */
  onActiveSlotFlushed: (activeSlot: string) => void
}

export interface StreamBuffers {
  /** Land every buffered chat chunk now: one batched dispatch per slot. Runs
   *  once per animation frame, and synchronously before a `chat_message` row,
   *  a steer echo, a segment or a turn end so buffered text lands above it.
   *  An approval's permission row does not flush first. */
  flushChunks(): void
  /** Buffer one `chat_chunk` for its slot; null for a repeated delivery. */
  bufferChunk(slot: string, seq: number | undefined, text: string, gen: unknown): ChunkBufEntry | null
  /** Drain now past the overflow threshold, else schedule the frame flush. */
  drainOrScheduleChunks(entry: ChunkBufEntry): void
  /** Buffer `chat_thinking` text in the slot's shared entry. */
  bufferThinking(slot: string, text: string): void
  /** Drop a slot's buffered stream text (content + thinking). */
  dropSlotChunks(slot: string): void
  bufferSlotActivity(slot: string, ts: string, settled: boolean): void
  flushSlotActivity(): void
  bufferSubagentChunk(slot: string, id: string, text: string): void
  flushSubagentChunks(): void
  /** Land one agent's buffered text ahead of a frame that must follow it. */
  flushSubagentKey(slot: string, id: string): void
  /** Forget one agent's buffered text: a snapshot already includes it. */
  dropSubagentKey(slot: string, id: string): void
  /** Reconnect: drop pre-disconnect partial buffers; the catch-up refetch
   *  recovers them, except reasoning, which is salvaged first. */
  dropForReconnect(): void
  /** Unmount: cancel pending frames, then land what the store must keep. */
  flushForUnmount(): void
}

export function useStreamBuffers({ dispatch, socket, onActiveSlotFlushed }: StreamBuffersDeps): StreamBuffers {
  // Streaming-chunk coalescing: accumulate per-slot chunk text and flush once
  // per animation frame. lastSeq is carried across flushes so cross-batch gap
  // detection mirrors the reducer's per-chunk "N chunk(s) missed" marker.
  // `thinking` buffers reasoning-stream text (chat_thinking) in the SAME entry
  // so both content types share one flush cycle and one lifecycle (reconnect
  // clear, chat_done delete, unmount cancel); the flush dispatches thinking
  // before content, matching a turn's thought-then-answer arrival order.
  const chunkBufRef = useRef<Map<string, ChunkBufEntry>>(new Map())
  // A fresh entry (first frame of a turn, or the first after a reconnect cleared
  // the buffer) starts with no seq. The buffer's lastSeq is about WS delivery
  // only: a repeated delivery of the same seq and a forward gap between two
  // deliveries. The snapshot replay floor (`lastChunkSeq`) is the reducer's;
  // each flush hands it the buffered parts with their seqs and the reducer drops
  // the ones a snapshot already holds. The hook has no view of that floor and
  // needs none.
  const chunkFlushRef = useRef<PendingFlush>(newPendingFlush())

  // Subagent-chunk coalescing: buffer per-agent text, flush once per rAF frame.
  const subagentChunkBufRef = useRef<Map<string, { slot: string; id: string; text: string }>>(new Map())
  const subagentFlushRef = useRef<PendingFlush>(newPendingFlush())

  // Slot-recency coalescing: last ts seen per slot, flushed once per frame, plus
  // whether the burst contained a SETTLING row (a prompt) — one settled event
  // anywhere in the burst settles the flush, since the reducer's settled bump is
  // additive rather than a toggle.
  // Last-seen wins — the reducer is last-write-wins, so this is the burst's end state.
  const slotActivityBufRef = useRef<Map<string, { ts: string; settled: boolean }>>(new Map())
  const slotActivityFlushRef = useRef<PendingFlush>(newPendingFlush())

  const buffers = useMemo<StreamBuffers>(() => {
    const flushChunks = () => {
      resetFlush(chunkFlushRef.current)
      const buf = chunkBufRef.current
      const activeSlot = store.getState().chat.activeSlot
      let dispatchedActive = false
      for (const [slot, entry] of buf) {
        // Everything buffered for this slot lands below, so the overflow counter
        // restarts here regardless of which branches dispatch.
        entry.chars = 0
        // Thinking first: within a turn the reasoning stream precedes the answer
        // stream, so a frame holding both must land them in that order.
        if (entry.thinking) {
          dispatch(sseThinkingChunk({ slot, content: entry.thinking }))
          entry.thinking = ''
        }
        // The reducer holds each part against the slot's replay floor and drops
        // what a snapshot already holds; the hook only batches.
        const text = bufferedText(entry)
        if (!text) continue
        dispatch(sseChatMessage({ slot, role: 'chunk', content: text, seq: entry.lastSeq, gen: entry.gen, batched: true, parts: entry.parts }))
        entry.parts = []
        if (slot === activeSlot) dispatchedActive = true
      }
      if (dispatchedActive && activeSlot) onActiveSlotFlushed(activeSlot)
    }

    const scheduleChunkFlush = () => scheduleFlush(chunkFlushRef.current, flushChunks)

    /** Salvage buffered reasoning before the chunk buffer is dropped. Buffered
     *  CONTENT may be discarded — refreshSlot recovers it from the server — but
     *  reasoning is client-only (the backend never persists it), so anything
     *  still buffered when the buffer is cleared (reconnect) or the hook unmounts
     *  would be permanently lost. A hidden tab widens that window:
     *  requestAnimationFrame is suspended there, so the scheduled flush never
     *  runs; the overflow flush (CHUNK_BUF_FLUSH_CHARS) bounds how much can sit
     *  here meanwhile, and this salvages the sub-threshold remainder. */
    const flushBufferedThinking = () => {
      for (const [slot, entry] of chunkBufRef.current) {
        if (entry.thinking) {
          dispatch(sseThinkingChunk({ slot, content: entry.thinking }))
          entry.chars -= entry.thinking.length
          entry.thinking = ''
        }
      }
    }

    /** Flush buffered slot-recency bumps: one touchSlotActivity per slot, not per
     *  event. */
    const flushSlotActivity = () => {
      resetFlush(slotActivityFlushRef.current)
      const buf = slotActivityBufRef.current
      if (buf.size === 0) return
      // A frame firing during reconnect backoff must drop, not dispatch: the on-open
      // refetch is authoritative. Unmount sets closing and still flushes deliberately.
      const ws = socket.wsRef.current
      if (!socket.isClosing() && (!ws || ws.readyState !== WebSocket.OPEN)) { buf.clear(); return }
      // Every buffered bump is dispatched: the "never move a timestamp backwards"
      // rule lives in the reducer, which holds both fields. It has to be per-field —
      // mid-turn `last_ts` runs ahead of `last_turn_ts`, so one shared check would
      // drop a settling bump whose ts is older than the newest streamed row.
      for (const [key, { ts, settled }] of buf) {
        dispatch(touchSlotActivity({ key, ts, settled }))
      }
      buf.clear()
    }

    /** Flush buffered subagent chunks into the store: one sseSubagentBatchChunks
     *  dispatch per frame, keyed by (slot, id) since several subagents can
     *  stream concurrently. */
    const flushSubagentChunks = () => {
      resetFlush(subagentFlushRef.current)
      const buf = subagentChunkBufRef.current
      if (buf.size === 0) return
      // Collect all buffered chunks and dispatch as a single batch. The reducer
      // iterates internally, so this is one React batch instead of O(agents).
      const chunks: { id: string; slot: string; text: string }[] = []
      for (const entry of buf.values()) {
        if (entry.text) chunks.push({ id: entry.id, slot: entry.slot, text: entry.text })
      }
      buf.clear()
      if (chunks.length > 0) dispatch(sseSubagentBatchChunks({ chunks }))
    }

    return {
      flushChunks,
      bufferChunk(slot, seq, text, gen) {
        const buf = chunkBufRef.current
        let entry = buf.get(slot)
        if (!entry) { entry = newChunkBufEntry(); buf.set(slot, entry) }
        // Idempotency guard: drop a repeated WS delivery (seq <= lastSeq).
        // WS delivery is at-least-once (reconnect replay, retry re-stream), so a
        // chunk can arrive twice. The reducer's gap markers only flag FORWARD
        // gaps (curSeq - prevSeq - 1 > 0), so a repeat would slip through and its
        // content be appended a second time with no marker — the silent
        // mid-stream "stutter".
        // chat_done deletes the buffer entry; seqs are the slot's and never
        // restart, so a later turn's chunks are never suppressed. This guard is about WS
        // delivery only; a chunk a slot SNAPSHOT already holds is dropped
        // by the reducer, which owns that floor and receives every part's
        // seq at flush.
        if (entry.lastSeq !== undefined && seq !== undefined && seq <= entry.lastSeq) {
          return null
        }
        // No gap marker here: the reducer derives markers from the seqs
        // of the parts it keeps, after filtering against the snapshot
        // floor, so a gap the snapshot filled in is not flagged.
        entry.parts.push({ seq, text })
        entry.chars += text.length
        if (seq !== undefined) entry.lastSeq = seq
        // The gateway generation that numbered the seqs (see floorForGen).
        if (typeof gen === 'string') entry.gen = gen
        return entry
      },
      drainOrScheduleChunks(entry) {
        // A hidden window never runs the scheduled frame; past the
        // threshold, drain now so the buffer cannot hold a whole turn.
        if (entry.chars > CHUNK_BUF_FLUSH_CHARS) flushChunks()
        else scheduleChunkFlush()
      },
      bufferThinking(slot, text) {
        const buf = chunkBufRef.current
        let entry = buf.get(slot)
        if (!entry) { entry = newChunkBufEntry(); buf.set(slot, entry) }
        entry.thinking += text
        entry.chars += text.length
        // Same hidden-window guard as chat_chunk; reasoning streams are
        // the long ones, so this branch is the one that usually trips it.
        if (entry.chars > CHUNK_BUF_FLUSH_CHARS) flushChunks()
        else scheduleChunkFlush()
      },
      dropSlotChunks(slot) {
        chunkBufRef.current.delete(slot)
      },
      bufferSlotActivity(slot, ts, settled) {
        // Keeps the NEWEST ts of the burst, and `settled` is sticky: one prompt
        // anywhere in a burst settles the flush, so the settling row surviving the
        // agent output it triggered does not depend on arrival order.
        const prev = slotActivityBufRef.current.get(slot)
        const newest = prev && Date.parse(prev.ts) > Date.parse(ts) ? prev.ts : ts
        slotActivityBufRef.current.set(slot, { ts: newest, settled: settled || !!prev?.settled })
        // Same hold as the chat stream — a recency bump reorders sidebar rows,
        // which is main-thread layout work mid-slide.
        scheduleFlush(slotActivityFlushRef.current, flushSlotActivity)
      },
      flushSlotActivity,
      bufferSubagentChunk(slot, id, text) {
        const key = `${slot}:${id}`
        const prev = subagentChunkBufRef.current.get(key)
        if (prev) {
          prev.text += text
          // Flush through reducer on overflow: the reducer's 50KB→40KB truncation
          // preserves the marker. A hidden tab suspends rAF, so flush synchronously.
          if (prev.text.length > 50_000) {
            dispatch(sseSubagentBatchChunks({ chunks: [{ id: prev.id, slot: prev.slot, text: prev.text }] }))
            subagentChunkBufRef.current.delete(key)
            return
          }
        } else {
          subagentChunkBufRef.current.set(key, { slot, id, text })
        }
        // Same hold as the chat stream — several subagents streaming at once is
        // exactly the reported worst case for the drawer slide.
        scheduleFlush(subagentFlushRef.current, flushSubagentChunks)
      },
      flushSubagentChunks,
      flushSubagentKey(slot, id) {
        const key = `${slot}:${id}`
        const entry = subagentChunkBufRef.current.get(key)
        if (entry?.text) {
          dispatch(sseSubagentBatchChunks({ chunks: [{ id: entry.id, slot: entry.slot, text: entry.text }] }))
        }
        subagentChunkBufRef.current.delete(key)
      },
      dropSubagentKey(slot, id) {
        subagentChunkBufRef.current.delete(`${slot}:${id}`)
      },
      dropForReconnect() {
        // Cancel any in-flight flush before dropping the buffer, so a chunk
        // arriving right after reconnect can't race a stale scheduled frame
        // into a second concurrent flush (mirrors the unmount cleanup).
        resetFlush(chunkFlushRef.current)
        // Reasoning first: it is client-only, so unlike content the refresh
        // cannot recover it — land it in the store before the drop.
        flushBufferedThinking()
        chunkBufRef.current.clear()  // drop pre-disconnect partial chunks; refreshSlot recovers state
        // Same for subagent chunks: pre-disconnect text must not cross a reconnect.
        resetFlush(subagentFlushRef.current)
        subagentChunkBufRef.current.clear()
        // Same for pending recency bumps: the slot refetch carries authoritative last_ts.
        resetFlush(slotActivityFlushRef.current)
        slotActivityBufRef.current.clear()
      },
      flushForUnmount() {
        cancelFlush(chunkFlushRef.current)
        cancelFlush(subagentFlushRef.current)
        // Flush rather than drop: the store outlives the hook, so a pending bump would
        // otherwise leave a stale sidebar tint. The flush also cancels the scheduled frame.
        flushSlotActivity()
        flushSubagentChunks()
        // Same for buffered reasoning: it is client-only and unrecoverable, unlike
        // buffered content (which the next mount's refresh restores from the server).
        flushBufferedThinking()
      },
    }
  }, [dispatch, socket, onActiveSlotFlushed])

  // Expose the synchronous flush to steer initiators (ChatPage / ChatPane):
  // an optimistic steer card dispatched while a chunk is still in this
  // buffer would land ABOVE text that belongs before it (see
  // lib/pendingChunkDrain.ts). Identity-guarded unregister, so a StrictMode
  // double-mount cannot strip the live registration.
  const { flushChunks } = buffers
  useEffect(() => registerPendingChunkDrain(flushChunks), [flushChunks])

  return buffers
}
