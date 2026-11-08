/** One frame of buffered stream traffic, landed as a single dispatch.
 *
 *  The socket buffers chat chunks, reasoning text, `chat_status` labels,
 *  sub-agent text and slot recency between animation frames
 *  (hooks/websocket/streamBuffers.ts). Every dispatch runs every reducer and
 *  notifies every subscriber, so the frame's burst is one `streamFlush` action
 *  instead of one action per slot per kind. The chat slice applies the text
 *  and the status line; the dashboard slice applies the recency bumps. */
import { createAction } from '@reduxjs/toolkit'
import type { BatchedChunkPart } from './chatSlice'
import type { SlotStatusDetail } from './chat/state'

/** The status line a slot's buffered events imply, in arrival order:
 *  a `chat_status` label replaces the line, then a content chunk after it
 *  moves the line to `streaming` and a reasoning chunk to `thinking` (unless
 *  it already shows `streaming` or `thinking`). Content and reasoning give
 *  the same result in either order, so two flags and the last label are the
 *  whole sequence.
 *
 *  `seen` is the slot's `slotStatusDetail` entry when the newest of these
 *  events arrived. A status written directly since then (a tool call, a turn
 *  end) is newer than every buffered event, so the reducer drops the pending
 *  status when the entry no longer matches `seen`. */
export type PendingSlotStatus = {
  seen: SlotStatusDetail | undefined
  label?: SlotStatusDetail
  content: boolean
  thinking: boolean
}

/** One slot's buffered text and status. `thinking` lands before `content`,
 *  matching a turn's thought-then-answer order. */
export type StreamFlushSlot = {
  slot: string
  thinking?: string
  content?: { text: string; seq: number | undefined; gen: string | undefined; parts: BatchedChunkPart[] }
  status?: PendingSlotStatus
}

export type StreamFlushPayload = {
  slots: StreamFlushSlot[]
  subagents: { id: string; slot: string; text: string }[]
  recency: { key: string; ts: string; settled: boolean }[]
}

export const streamFlush = createAction<StreamFlushPayload>('stream/flush')
