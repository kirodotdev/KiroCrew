/** Patches to rows a view already holds (`chat_message_update`), applied live and
 *  replayed onto a read that was in flight across them (`ChatState.rowPatchLog`). */
import type { ChatMessage } from '../../types'
import { ROW_PATCH_LOG_CAP, type ChatState, type RowPatch } from './state'

/** Apply one patch to `msgs` in place. A tool row is found by `tool_call_id`
 *  (newest first), any other by `mid`, else by `ts`; no match changes nothing. */
export function applyRowPatch(msgs: ChatMessage[], patch: Omit<RowPatch, 'seq'>): void {
  const { tcid, mid, ts, content, meta, variantIdx } = patch
  let target: ChatMessage | undefined
  if (tcid) {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'tool' && (m.meta as Record<string, unknown> | undefined)?.tool_call_id === tcid) { target = m; break }
    }
  } else if (mid) {
    target = msgs.find(m => m.meta?.mid === mid)
  } else if (ts) {
    target = msgs.find(m => m.ts === ts)
  }
  if (!target) return
  if (meta) target.meta = { ...(target.meta || {}), ...meta }
  if (content !== undefined) target.content = content
  if (variantIdx !== undefined) target.variant_idx = variantIdx
}

/** Record a patch to any slot's rows, numbered, keeping the newest. */
export function logRowPatch(state: ChatState, patch: Omit<RowPatch, 'seq'>): void {
  const seq = (state.rowPatchSeq ?? 0) + 1
  state.rowPatchSeq = seq
  const log = (state.rowPatchLog ??= [])
  log.push({ ...patch, seq })
  if (log.length > ROW_PATCH_LOG_CAP) log.splice(0, log.length - ROW_PATCH_LOG_CAP)
}

/** The patches to `slot` numbered past `since`, or `null` when the log no longer
 *  holds all of them (more than the cap landed since). */
export function rowPatchesSince(state: ChatState, slot: string, since: number): RowPatch[] | null {
  const seq = state.rowPatchSeq ?? 0
  if (seq === since) return []
  const log = state.rowPatchLog ?? []
  if (!log.length || log[0].seq > since + 1) return null
  return log.filter(p => p.seq > since && p.slot === slot)
}

/** `read` with every patch to `slot` numbered past `since` replayed onto its rows,
 *  so a read in flight across a patch cannot put the patched row back as it was.
 *  Throws when the log no longer holds them all: installing the read would undo
 *  some, and the caller's rejection is the honest outcome. */
export function withReplayedPatches<T extends { messages: ChatMessage[] }>(
  state: ChatState, slot: string, since: number | undefined, read: T,
): T {
  // A payload that carries no position (built by hand, or by an older caller)
  // has nothing it can be shown to predate.
  if (since === undefined) return read
  const patches = rowPatchesSince(state, slot, since)
  if (patches === null) throw new Error('row patches outran the replay log')
  if (!patches.length) return read
  const rows = read.messages.map(m => ({ ...m, meta: m.meta ? { ...m.meta } : m.meta }))
  for (const p of patches) applyRowPatch(rows, p)
  return { ...read, messages: rows }
}
