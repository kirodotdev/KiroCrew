/** The trust boundary of the chat state: everything here takes a value that
 *  arrived from the gateway (a WebSocket frame or an HTTP payload) and makes it
 *  safe to store. Prototype-pollution key guards, the slot-detail read and its
 *  normalization, queue-entry attachment lists, agent-authored workflow text
 *  and the oversize tool-payload clamp. No reducer lives here. */
import { api } from '../../api/client'
import type { ChatMessage, ToolPayloadCut } from '../../types'
import { readMessageQuote, type MessageQuote } from '../../chat-core/composer/messageQuote'

const SKIP_ROLES = new Set(['chunk', 'done'])
export const filterMessages = (msgs: ChatMessage[]) => msgs.filter(m => !SKIP_ROLES.has(m.role))

/** The three keys that can pollute `Object.prototype` when used to index a
 *  plain-object map (`obj[key] = ...`). Slot ids, subagent ids, run ids, and
 *  session keys all flow in from WebSocket action payloads; a crafted payload
 *  carrying `__proto__` / `constructor` / `prototype` would otherwise mutate the
 *  shared prototype through the per-slot state maps in this slice. */
/** True if `key` would pollute the prototype chain if used to index a plain
 *  object. Every reducer that indexes a `Record<string, …>` state map by an
 *  externally-supplied key rejects such a key up front (early return) — an
 *  explicit guard the CodeQL prototype-pollution query recognizes as a barrier.
 *  It is the single fail-closed chokepoint; a dropped frame for a hostile key is
 *  the correct outcome (no legitimate slot/subagent/run id is `__proto__`).
 *  Written as explicit `===` comparisons (not a Set lookup) so static analysis
 *  can model it as a sanitizing guard. */
export const isUnsafeKey = (key: string): boolean =>
  key === '__proto__' || key === 'constructor' || key === 'prototype'

/** Defense-in-depth companion to the early-return guards: reroutes a poisoned
 *  key to an inert own-property so any write that slips past a guard still can't
 *  reach the prototype. Real keys pass through unchanged. */
export const safeKey = (key: string): string => (isUnsafeKey(key) ? `unsafe-key:${key}` : key)

/** Per-entry ceiling on a tool result, and on its input, held in the live
 *  tool log. The server caps either at 1 MB (`_redact_tool_field`), and the
 *  log keeps 100 entries per open pane until the next user message — which in
 *  an autonomous or monitor-loop session can be hours away. Uncapped, that is
 *  ~100 MB of multi-hundred-KB strings per pane, and V8 parks strings that
 *  size in large-object space, the region a long-lived renderer exhausts
 *  first. Above the ceiling the head and tail are kept around a marker: the
 *  head carries the command echo, the tail the exit status or error, and the
 *  middle is the bulk. The full result stays on the server and is served on
 *  reload via the tool message's `meta.output`, so this trims only the live
 *  copy. */
export const TOOL_OUTPUT_MAX_CHARS = 64_000
const TOOL_OUTPUT_HEAD_CHARS = 48_000
const TOOL_OUTPUT_TAIL_CHARS = 12_000
const TOOL_OUTPUT_SNAP_WINDOW = 2_000

/** Clamp a tool result to `TOOL_OUTPUT_MAX_CHARS`, keeping head + tail.
 *
 *  Each cut snaps to a line break within `TOOL_OUTPUT_SNAP_WINDOW` of its raw
 *  offset so neither side of the seam starts with a short mid-line fragment.
 *  A cut without a nearby usable line break keeps its raw offset, preserving
 *  the intended head and tail budgets.
 *
 *  Returns the clamped `text` plus a structural `cut` — the seam offset and the
 *  exact number of characters elided — or `cut: null` when nothing was
 *  removed. The marker the user reads is NOT part of `text`: it is a locale
 *  string, and a reducer that baked it in would freeze it in the language
 *  active when the result arrived. `ToolDetails` renders it at the seam at
 *  view time instead. */
export function clampToolOutput(output: string): { text: string; cut: ToolPayloadCut | null } {
  if (output.length <= TOOL_OUTPUT_MAX_CHARS) return { text: output, cut: null }
  const headCut = output.lastIndexOf('\n', TOOL_OUTPUT_HEAD_CHARS)
  const headEnd = headCut >= TOOL_OUTPUT_HEAD_CHARS - TOOL_OUTPUT_SNAP_WINDOW
    ? headCut
    : TOOL_OUTPUT_HEAD_CHARS
  const rawTailStart = output.length - TOOL_OUTPUT_TAIL_CHARS
  let tailStart = rawTailStart
  if (output[rawTailStart - 1] !== '\n') {
    const tailCut = output.indexOf('\n', rawTailStart)
    if (
      tailCut >= 0
      && tailCut < rawTailStart + TOOL_OUTPUT_SNAP_WINDOW
      && tailCut + 1 < output.length
    ) tailStart = tailCut + 1
  }
  const parts = [
    output.slice(0, headEnd),
    '\n',
    output.slice(tailStart),
  ]
  // V8's multi-part Array#join path copies the characters into a fresh
  // sequential string instead of retaining the sliced parents through a cons.
  return { text: parts.join(''), cut: { at: headEnd + 1, count: tailStart - headEnd } }
}

/** The attachment lists a queue entry carries, as the server echoes them on
 *  the slot-detail `queue[]` item and the `queue_push` frame (the same `meta`
 *  the `queue_pop` frame already uses). `files` is the
 *  ORDERED non-image list an `[attached_file N]` marker indexes (`files[N-1]`),
 *  `dirs` the folder list `[attached_dir N]` indexes. */
export type QueueEntryAttachments = { files?: string[]; dirs?: string[] }

/** Reduce a wire `meta` to its attachment lists. Only a non-empty list of
 *  strings is kept: the lists are indexed by marker number, so a malformed
 *  entry would shift every later marker onto the wrong path. Returns `{}` for
 *  anything else, which is what an entry without attachments carries. */
/** The whole-message quote a queue entry carries (`meta.quote`, bounded by
 *  the gateway's `quote_meta`), re-validated here like the attachment lists:
 *  a cancel on THIS tab restores it as a staged card even when the send
 *  happened on another tab or before a reload. */
export function queueEntryQuote(meta: unknown): { quote?: MessageQuote } {
  const q = readMessageQuote(meta && typeof meta === 'object' ? (meta as Record<string, unknown>) : undefined)
  return q ? { quote: q } : {}
}

export function queueEntryAttachments(meta: unknown): QueueEntryAttachments {
  const out: QueueEntryAttachments = {}
  if (!meta || typeof meta !== 'object') return out
  for (const key of ['files', 'dirs'] as const) {
    const raw = (meta as Record<string, unknown>)[key]
    if (Array.isArray(raw) && raw.length && raw.every((p) => typeof p === 'string' && p)) out[key] = [...raw] as string[]
  }
  return out
}

/** One queued-message entry as normalized by `fetchSlotDetail` from the backend
 *  slot-detail `queue` field. */
export type SlotQueueItem = { content: string; queueId: string; ts: string; kind?: string; appLabel?: string; quote?: MessageQuote } & QueueEntryAttachments

/** Coerce one workflow wire field to the string `WorkflowRunProgress` declares.
 *
 *  Every text field on a run is AGENT-AUTHORED: a workflow script calls
 *  `ctx.phase(123)` or logs a dict, and that value rides the event stream and the
 *  runs API unchanged. The rendering path slices these (`(run.phase || '').slice`)
 *  so a number reaching the store throws inside render — the chat goes blank, not
 *  just this row. The type annotations claimed `string` without anything enforcing
 *  it, so this is the enforcement, applied at BOTH writers into the slice (the
 *  live `sseWorkflowEvent` and the `reconcileWorkflowRuns` read) rather than at one
 *  of them: the two paths carry the same values and a guard on only the newer one
 *  leaves the same crash reachable through the older.
 *
 *  A non-string is dropped rather than stringified: `String({})` renders
 *  "[object Object]" in the chat, which is worse than the field being absent. */
export const workflowText = (value: unknown): string => (typeof value === 'string' ? value : '')

export async function fetchSlotDetail(key: string, limit?: number) {
  // A limit takes the handler's most-recent-N slice. `undefined` is the
  // handler's read-everything shape; `refreshSlot` and `switchSlot` no longer
  // take it -- a window that misses the rows they hold is extended by
  // `walkWindowBackTo`, one bounded page at a time. `warmSlotCache` and the
  // pane hydrate still may. Omit the arg when unbounded to keep the one-arg
  // shape.
  const d = await (limit === undefined ? api.chatSlotDetail(key) : api.chatSlotDetail(key, limit))
  return normalizeSlotDetail(key, d, limit !== undefined)
}

/** One slot-detail response in the store's shape. Shared by `fetchSlotDetail`
 *  and the coverage walk, so a walked page reports its run state, queue and
 *  context meter exactly as a first page does. */
export function normalizeSlotDetail(key: string, d: Awaited<ReturnType<typeof api.chatSlotDetail>>, boundedRead: boolean) {
  type QueueItem = string | { content: string; id: string; meta?: unknown }
  return { key, boundedRead, nextBefore: d.next_before || 0, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as QueueItem[]).map((q: QueueItem) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString(), ...(typeof (q.meta as Record<string, unknown> | undefined)?.kind === 'string' ? { kind: (q.meta as Record<string, unknown>).kind as string } : {}), ...(typeof (q.meta as Record<string, unknown> | undefined)?.appLabel === 'string' ? { appLabel: (q.meta as Record<string, unknown>).appLabel as string } : {}), ...queueEntryAttachments(q.meta), ...queueEntryQuote(q.meta) }), context: d.context_pct != null ? { pct: d.context_pct, used: d.context_used_tokens ?? undefined, window: d.context_window_tokens ?? undefined } : undefined }
}
