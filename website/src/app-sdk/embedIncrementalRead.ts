/**
 * ChatEmbed's incremental poll (chat-core P5-f).
 *
 * The slot-detail endpoint hands a paged read a `cursor` ({from, since,
 * digest}) when asked (`cursor=1`). The next poll echoes it back, and the
 * server answers with only the rows from `since` on, marked `delta`, when the
 * rows the embed holds before `since` still render exactly as they did. Any
 * other change (a row edited in place, a rewind, a different page) gets the
 * full page back with no `delta`, which replaces what is held. The server owns
 * the comparison: this side only echoes the cursor and splices.
 *
 * A gateway that predates the cursor never sends one, so every poll stays the
 * plain bounded read.
 */

export interface EmbedCursor {
  from: number
  since: number
  digest: string
}

const DIGEST_RE = /^[0-9a-f]{32}$/

const isIndex = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0

/** Whether a response's `cursor` is one this side can echo. */
export function isEmbedCursor(v: unknown): v is EmbedCursor {
  if (typeof v !== 'object' || v == null) return false
  const c = v as Record<string, unknown>
  return isIndex(c.from) && isIndex(c.since) && c.since >= c.from && typeof c.digest === 'string' && DIGEST_RE.test(c.digest)
}

/** The poll's request path: the cursor echoed when one is held, else a full
 *  read that asks for one. */
export function embedReadPath(slotKey: string, limit: number, cursor: EmbedCursor | null): string {
  const base = '/api/chat/slots/' + encodeURIComponent(slotKey) + '?limit=' + limit
  if (!cursor) return base + '&cursor=1'
  return `${base}&from=${cursor.from}&since=${cursor.since}&digest=${cursor.digest}`
}

interface EmbedRead<M> {
  messages?: M[]
  next_before?: number
  delta?: unknown
}

/** Fold one read into the page held.
 *
 *  A response without `delta` is a whole page and replaces what is held. A
 *  `delta` response carries the rows from `since` on; the held rows from the
 *  response's window start (`next_before`) up to `since` are kept in front of
 *  them. Returns `null` when a `delta` does not answer the cursor that was
 *  sent or does not fit the held rows -- the caller then reads the page whole
 *  rather than splice onto a guess. */
export function mergeEmbedRead<M, T extends EmbedRead<M>>(held: T | undefined, sent: EmbedCursor | null, res: T): T | null {
  const delta = res.delta
  if (delta == null) return res
  if (!held || !sent || typeof delta !== 'object') return null
  const d = delta as Record<string, unknown>
  if (d.from !== sent.from || d.since !== sent.since) return null
  const rows = held.messages ?? []
  const start = res.next_before
  if (!isIndex(start) || start < sent.from || rows.length < sent.since - sent.from) return null
  const keep = start < sent.since ? rows.slice(start - sent.from, sent.since - sent.from) : []
  return { ...res, messages: [...keep, ...(res.messages ?? [])] }
}
