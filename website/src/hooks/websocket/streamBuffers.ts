/** The socket's bounded stream buffers and their one per-frame flush.
 *
 *  Streaming frames arrive per token; dispatching each one would recompute
 *  the O(N) displayItems / index maps (and re-rank the sidebar) per token.
 *  Three buffers absorb them instead:
 *   - the chat stream: per-slot content chunks plus reasoning text;
 *   - subagent streams: per-(slot, agent) text;
 *   - slot recency: the newest activity timestamp per slot.
 *  All three land together as ONE `streamFlush` action per animation frame
 *  (store/streamFlush.ts), whatever the number of streaming slots. Every
 *  dispatch runs every reducer and notifies every subscriber, so one action
 *  per slot per kind multiplied that cost by the slot count.
 *  A UI animation holds the flush (lib/streamHold.ts). In a hidden window,
 *  where requestAnimationFrame never runs, the chat and subagent buffers are
 *  bounded by their 50k-char overflow flushes; slot recency keeps one entry
 *  per slot. */
import { useEffect, useMemo, useRef } from 'react'
import { store, type AppDispatch } from '../../store'
import { sseSubagentBatchChunks, type SlotStatusDetail } from '../../store/chatSlice'
import { streamFlush, type PendingSlotStatus, type StreamFlushPayload, type StreamFlushSlot } from '../../store/streamFlush'
import { streamingFlushHoldMs } from '../../lib/streamHold'
import { registerPendingChunkDrain } from '../../lib/pendingChunkDrain'
import type { SocketConnection } from './connection'
import { autoBatched } from '../../lib/streamBatch'

/** One buffered chunk: its text (gap marker included) and the seq it carried,
 *  kept apart so the reducer can hold each part against the slot's replay
 *  floor and drop exactly the chunks a snapshot already covers. */
type ChunkPart = { seq: number | undefined; text: string }
/** `status` is the status line the buffered events imply (PendingSlotStatus). */
export type ChunkBufEntry = {
  parts: ChunkPart[]; lastSeq: number | undefined; gen: string | undefined; thinking: string; chars: number
  status: PendingSlotStatus | null
}
const newChunkBufEntry = (): ChunkBufEntry => {
  return { parts: [], lastSeq: undefined, gen: undefined, thinking: '', chars: 0, status: null }
}
const bufferedText = (entry: ChunkBufEntry): string => entry.parts.map((p) => p.text).join('')

/** The entry's pending status, restarted when the store's entry moved since
 *  the last buffered event: a status written directly in between (a tool
 *  call) is newer than everything buffered before it, so only events after it
 *  count. */
const pendingStatus = (entry: ChunkBufEntry, slot: string): PendingSlotStatus => {
  const now: SlotStatusDetail | undefined = store.getState().chat.slotStatusDetail?.[slot]
  if (!entry.status || entry.status.seen !== now) entry.status = { seen: now, content: false, thinking: false }
  return entry.status
}

/** Buffered chars (content + thinking) per slot above which the chunk buffer
 *  is flushed synchronously instead of waiting for the next animation frame.
 *  requestAnimationFrame is suspended while the window is hidden, so a
 *  backgrounded renderer streaming a long turn would otherwise buffer the
 *  entire turn. The subagent buffer (bufferSubagentChunk) flushes at this
 *  same 50 KB threshold for the same reason. */
const CHUNK_BUF_FLUSH_CHARS = 50_000

/** The pending frame flush: at most one animation frame or timer per burst. */
type PendingFlush = {
  scheduled: boolean
  raf: number | null
  timer: ReturnType<typeof setTimeout> | null
}

const newPendingFlush = (): PendingFlush => ({ scheduled: false, raf: null, timer: null })

/** Schedule `flush` for the next frame unless one is already pending. While a
 *  UI animation holds the stream (see lib/streamHold.ts), defer the flush
 *  to the hold's end instead of the next frame. The buffers keep absorbing
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

/** Cancel the pending frame or timer and forget it. A synchronous flush does
 *  this first: nulling the ids without cancelling would orphan a frame
 *  (uncancellable by unmount/reconnect cleanup, firing a stale flush). From
 *  the frame callback itself the id has already fired, so the cancel is a
 *  no-op. */
function resetFlush(pending: PendingFlush): void {
  if (pending.raf != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(pending.raf)
  if (pending.timer != null) clearTimeout(pending.timer)
  pending.scheduled = false
  pending.raf = null
  pending.timer = null
}

/** Which buffers a flush lands. The scheduled frame lands all of them. A
 *  synchronous flush ahead of a row lands only the buffer that row must
 *  follow, so a burst of rows still coalesces the other buffers to the frame.
 *  Unmount lands all but chat content, which the next mount's refresh
 *  restores from the server. Reconnect lands reasoning only, which is
 *  client-only and cannot be refetched; it drops the rest. */
type FlushSet = { content: boolean; thinking: boolean; subagents: boolean; recency: boolean }
const FLUSH_FRAME: FlushSet = { content: true, thinking: true, subagents: true, recency: true }
const FLUSH_CHAT: FlushSet = { content: true, thinking: true, subagents: false, recency: false }
const FLUSH_SUBAGENTS: FlushSet = { content: false, thinking: false, subagents: true, recency: false }
const FLUSH_RECENCY: FlushSet = { content: false, thinking: false, subagents: false, recency: true }
const FLUSH_UNMOUNT: FlushSet = { content: false, thinking: true, subagents: true, recency: true }
const FLUSH_RECONNECT: FlushSet = { content: false, thinking: true, subagents: false, recency: false }

export interface StreamBuffersDeps {
  dispatch: AppDispatch
  socket: SocketConnection
  /** Called after a flush landed content in the active slot. */
  onActiveSlotFlushed: (activeSlot: string) => void
}

export interface StreamBuffers {
  /** Whether this slot has received buffered stream content in this connection. */
  hasPendingChunks(slot: string): boolean
  /** Land everything buffered now, as one `streamFlush`. Runs once per
   *  animation frame, and synchronously before a `chat_message` row, a steer
   *  echo, a segment or a turn end so buffered text lands above it. An
   *  approval's permission row does not flush first. */
  flushChunks(): void
  /** Buffer one `chat_chunk` for its slot; null for a repeated delivery. */
  bufferChunk(slot: string, seq: number | undefined, text: string, gen: unknown): ChunkBufEntry | null
  /** Drain now past the overflow threshold, else schedule the frame flush. */
  drainOrScheduleChunks(entry: ChunkBufEntry): void
  /** Buffer `chat_thinking` text in the slot's shared entry. */
  bufferThinking(slot: string, text: string): void
  /** Buffer a `chat_status` label; it lands with the slot's text on the frame. */
  bufferStatusLabel(slot: string, detail: SlotStatusDetail): void
  /** Whether the slot's status line will change at the next flush, so the
   *  store's entry is not the line the slot shows by then. */
  hasPendingStatus(slot: string): boolean
  /** Forget the status the slot's buffered events imply: a status written
   *  directly now is newer than all of them. Needed besides the reducer's
   *  identity check because `setSlotStatusDetail` skips a write equal to the
   *  stored entry, which then keeps the identity the buffer recorded. */
  supersedePendingStatus(slot: string): void
  /** Drop a slot's buffered stream text (content + thinking) and status. */
  dropSlotChunks(slot: string): void
  bufferSlotActivity(slot: string, ts: string, settled: boolean): void
  /** Same flush as flushChunks; named for callers that need recency landed. */
  flushSlotActivity(): void
  bufferSubagentChunk(slot: string, id: string, text: string): void
  /** Same flush as flushChunks; named for callers that need sub-agent text landed. */
  flushSubagentChunks(): void
  /** Land one agent's buffered text ahead of a frame that must follow it. */
  flushSubagentKey(slot: string, id: string): void
  /** Forget one agent's buffered text: a snapshot already includes it. */
  dropSubagentKey(slot: string, id: string): void
  /** Reconnect: drop pre-disconnect partial buffers; the catch-up refetch
   *  recovers them, except reasoning, which is salvaged first. */
  dropForReconnect(): void
  /** Unmount: cancel the pending frame, then land what the store must keep. */
  flushForUnmount(): void
}

export function useStreamBuffers({ dispatch, socket, onActiveSlotFlushed }: StreamBuffersDeps): StreamBuffers {
  // Streaming-chunk coalescing: accumulate per-slot chunk text and flush once
  // per animation frame. lastSeq is carried across flushes so cross-batch gap
  // detection mirrors the reducer's per-chunk "N chunk(s) missed" marker.
  // `thinking` buffers reasoning-stream text (chat_thinking) in the SAME entry
  // so both content types share one lifecycle (reconnect clear, chat_done
  // delete, unmount cancel); the reducer applies thinking before content,
  // matching a turn's thought-then-answer arrival order.
  const chunkBufRef = useRef<Map<string, ChunkBufEntry>>(new Map())
  // A fresh entry (first frame of a turn, or the first after a reconnect cleared
  // the buffer) starts with no seq. The buffer's lastSeq is about WS delivery
  // only: a repeated delivery of the same seq and a forward gap between two
  // deliveries. The snapshot replay floor (`lastChunkSeq`) is the reducer's;
  // each flush hands it the buffered parts with their seqs and the reducer drops
  // the ones a snapshot already holds. The hook has no view of that floor and
  // needs none.

  // Subagent-chunk coalescing: buffer per-agent text.
  const subagentChunkBufRef = useRef<Map<string, { slot: string; id: string; text: string }>>(new Map())

  // Slot-recency coalescing: last ts seen per slot, plus whether the burst
  // contained a SETTLING row (a prompt) — one settled event anywhere in the
  // burst settles the flush, since the reducer's settled bump is additive
  // rather than a toggle.
  // Last-seen wins — the reducer is last-write-wins, so this is the burst's end state.
  const slotActivityBufRef = useRef<Map<string, { ts: string; settled: boolean }>>(new Map())

  // One pending frame for all three buffers.
  const frameFlushRef = useRef<PendingFlush>(newPendingFlush())

  const buffers = useMemo<StreamBuffers>(() => {
    /** Whether any buffer still holds something for the frame to land. */
    const anyBuffered = () => {
      if (subagentChunkBufRef.current.size > 0 || slotActivityBufRef.current.size > 0) return true
      for (const entry of chunkBufRef.current.values()) if (entry.thinking || entry.parts.length > 0 || entry.status) return true
      return false
    }

    /** Land the `sel` buffers as one `streamFlush`. The pending frame is
     *  cancelled once nothing is left for it; a partial synchronous flush
     *  leaves it scheduled for the other buffers. */
    const flush = (sel: FlushSet, notifyNow = false) => {
      const activeSlot = store.getState().chat.activeSlot
      const slots: StreamFlushSlot[] = []
      let openSlotText = false
      let activeContent = false
      for (const [slot, entry] of chunkBufRef.current) {
        const out: StreamFlushSlot = { slot }
        if (sel.thinking && entry.thinking) {
          out.thinking = entry.thinking
          entry.chars -= entry.thinking.length
          entry.thinking = ''
        }
        if (sel.content) {
          // Everything buffered for this slot lands now, so the overflow
          // counter restarts here.
          entry.chars = 0
          // The reducer holds each part against the slot's replay floor and
          // drops what a snapshot already holds; the hook only batches.
          const text = bufferedText(entry)
          if (text) {
            out.content = { text, seq: entry.lastSeq, gen: entry.gen, parts: entry.parts }
            entry.parts = []
            if (slot === activeSlot) activeContent = true
          }
        }
        // The status lands with the reasoning, so a reconnect or unmount
        // salvage keeps the line its reasoning implies.
        if (sel.thinking && entry.status) {
          const st = entry.status
          if (st.label || st.content || st.thinking) out.status = st
          entry.status = null
        }
        if (!out.thinking && !out.content && !out.status) continue
        if (slot === activeSlot) openSlotText = true
        slots.push(out)
      }

      const subagents: StreamFlushPayload['subagents'] = []
      if (sel.subagents) {
        for (const entry of subagentChunkBufRef.current.values()) {
          if (!entry.text) continue
          subagents.push({ id: entry.id, slot: entry.slot, text: entry.text })
          if (entry.slot === activeSlot) openSlotText = true
        }
        subagentChunkBufRef.current.clear()
      }

      const recency: StreamFlushPayload['recency'] = []
      if (sel.recency) {
        // A flush during reconnect backoff drops recency, not dispatches it: the
        // on-open refetch is authoritative. Unmount sets closing and still lands
        // it deliberately, since the store outlives the hook and a dropped bump
        // would leave a stale sidebar tint.
        const ws = socket.wsRef.current
        if (socket.isClosing() || (ws && ws.readyState === WebSocket.OPEN)) {
          // Every buffered bump goes out: the "never move a timestamp backwards"
          // rule lives in the reducer, which holds both fields. It has to be
          // per-field — mid-turn `last_ts` runs ahead of `last_turn_ts`, so one
          // shared check would drop a settling bump whose ts is older than the
          // newest streamed row.
          for (const [key, { ts, settled }] of slotActivityBufRef.current) recency.push({ key, ts, settled })
        }
        slotActivityBufRef.current.clear()
      }

      if (!anyBuffered()) resetFlush(frameFlushRef.current)
      if (slots.length === 0 && subagents.length === 0 && recency.length === 0) return
      const action = streamFlush({ slots, subagents, recency })
      // The flush defers its store notification (lib/streamBatch.ts) unless it
      // carries text for the open slot, which renders on the frame it lands.
      // Recency only re-ranks the sidebar, so one frame later is invisible.
      // Reconnect and unmount salvage notify at once.
      dispatch(!notifyNow && !openSlotText ? autoBatched(action) : action)
      if (activeContent && activeSlot) onActiveSlotFlushed(activeSlot)
    }

    const flushFrame = () => flush(FLUSH_FRAME)
    const flushChat = () => flush(FLUSH_CHAT)
    const scheduleFrame = () => scheduleFlush(frameFlushRef.current, flushFrame)

    return {
      hasPendingChunks(slot) { return chunkBufRef.current.has(slot) },
      flushChunks: flushChat,
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
        pendingStatus(entry, slot).content = true
        if (seq !== undefined) entry.lastSeq = seq
        // The gateway generation that numbered the seqs (see floorForGen).
        if (typeof gen === 'string') entry.gen = gen
        return entry
      },
      drainOrScheduleChunks(entry) {
        // A hidden window never runs the scheduled frame; past the
        // threshold, drain now so the buffer cannot hold a whole turn.
        if (entry.chars > CHUNK_BUF_FLUSH_CHARS) flushChat()
        else scheduleFrame()
      },
      bufferThinking(slot, text) {
        const buf = chunkBufRef.current
        let entry = buf.get(slot)
        if (!entry) { entry = newChunkBufEntry(); buf.set(slot, entry) }
        entry.thinking += text
        entry.chars += text.length
        pendingStatus(entry, slot).thinking = true
        // Same hidden-window guard as chat_chunk; reasoning streams are
        // the long ones, so this branch is the one that usually trips it.
        if (entry.chars > CHUNK_BUF_FLUSH_CHARS) flushChat()
        else scheduleFrame()
      },
      bufferStatusLabel(slot, detail) {
        const buf = chunkBufRef.current
        let entry = buf.get(slot)
        if (!entry) { entry = newChunkBufEntry(); buf.set(slot, entry) }
        // The label replaces the line, so the chunks before it no longer
        // decide it; only chunks after it do.
        const st = pendingStatus(entry, slot)
        st.label = detail
        st.content = false
        st.thinking = false
        scheduleFrame()
      },
      hasPendingStatus(slot) {
        const st = chunkBufRef.current.get(slot)?.status
        return !!st && st.seen === store.getState().chat.slotStatusDetail?.[slot] && (!!st.label || st.content || st.thinking)
      },
      supersedePendingStatus(slot) {
        const entry = chunkBufRef.current.get(slot)
        if (entry) entry.status = null
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
        scheduleFrame()
      },
      flushSlotActivity() { flush(FLUSH_RECENCY) },
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
        scheduleFrame()
      },
      flushSubagentChunks() { flush(FLUSH_SUBAGENTS) },
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
        // Reasoning is client-only, so unlike content the refresh cannot
        // recover it: land it before the drop.
        flush(FLUSH_RECONNECT, true)
        chunkBufRef.current.clear()  // drop pre-disconnect partial chunks; refreshSlot recovers state
        // Same for subagent chunks: pre-disconnect text must not cross a reconnect.
        subagentChunkBufRef.current.clear()
        // Same for pending recency bumps: the slot refetch carries authoritative last_ts.
        slotActivityBufRef.current.clear()
        // Cancel the frame too, so a chunk arriving right after reconnect
        // cannot race a stale frame into a second concurrent flush.
        resetFlush(frameFlushRef.current)
      },
      flushForUnmount() {
        // Land rather than drop what only this hook holds: the store outlives
        // it. Buffered content stays unlanded; the next mount's refresh
        // restores it from the server.
        flush(FLUSH_UNMOUNT, true)
        resetFlush(frameFlushRef.current)
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
