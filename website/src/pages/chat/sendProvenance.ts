/**
 * Who SENT a user-role transcript row, when another chat did.
 *
 * `session_send` / `session_broadcast` deliver a peer's message as a user row whose
 * text opens with `[sent by session <key> via <verb>]` -- an envelope for the
 * TARGET MODEL, which weighs a peer's instruction differently from its user's. The
 * transcript draws the same fact as a muted "From <chat>" label over the bubble and
 * hides the envelope line.
 *
 * Read from `meta.sendProvenance`, which only the gateway's delivery path writes
 * (the composer's client meta cannot carry it: `RESERVED_ROW_META_KEYS`). A row
 * written before that record existed has only the envelope, so the envelope is
 * parsed once here as a fallback -- held to rules typed text cannot meet:
 *
 *  - a row the gateway marks as typed by a person (`meta.human`) or sent by an
 *    app (`meta.turnActor`), or a bubble this client minted (`optimistic`,
 *    `sendId` / `sendIds`), is never labelled, structured record or not. (Not
 *    `clientTs`: the store stamps that on every frame it keys, server ones included);
 *  - the envelope must be the row's exact opening line, spelled as the gateway
 *    spells it (`[sent by session <key> via session_send|session_broadcast]`);
 *    `[quoted: sent by session …` is the gateway's NEUTRALIZED form of an
 *    envelope found inside a body and never opens a genuine delivery;
 *  - the text may hold no second envelope: a merged queue row stands for several
 *    messages, and one sender cannot label all of them;
 *  - the row must carry the gateway's own message id (`meta.mid`). A row this
 *    client rebuilt from a live frame (a `queue_pop`, an edit-resend) has none,
 *    and that is the one place a channel or composer author's text could arrive
 *    without the `human` mark a persisted row of theirs carries. A live frame
 *    from a real delivery carries the structured record instead
 *    (`queueEntrySendProvenance`), so the fallback is only ever needed for rows
 *    read back from the transcript;
 *  - a row the drain marks as rebuilt from a restored queue entry
 *    (`turnProvenanceRestored`) is never read either: a restart drops the entry's
 *    actor, so its author can no longer be vouched for;
 *  - and the row must have been WRITTEN before this record existed
 *    (`LEGACY_CUTOFF`, by the gateway's own row timestamp). A gateway running this
 *    code stamps the record on every delivery it writes, so a newer row with an
 *    envelope and no record is by construction not a delivery -- whatever path
 *    put it there. That bound is what keeps the fallback from becoming a way to
 *    pose as another chat, rather than a list of every path that loses a mark.
 */

/** Rows written at or after this instant come from a gateway that stamps
 *  `sendProvenance` on every delivery, so only older rows may be read from text. */
export const LEGACY_CUTOFF = Date.UTC(2026, 9, 20) // 2026-10-20T00:00:00Z (month is 0-based)

export const SEND_PROVENANCE_META_KEY = 'sendProvenance'

export type SendMode = 'started' | 'queued' | 'steered'

export interface SendProvenance {
  /** The sending session's slot key. */
  from: string
  /** The sender's title when the message was sent, when known. */
  title?: string
  /** `session_send` or `session_broadcast`. */
  via: string
  /** How the message reached this chat's turn; absent on a legacy row. */
  mode?: SendMode
  /** A broadcast's audience size; absent on a legacy row and on a plain send. */
  count?: number
  /** Read out of the text envelope rather than a structured record. */
  legacy?: boolean
}

export interface ReadSendProvenance {
  provenance: SendProvenance
  /** The row's text with the envelope line removed. */
  body: string
}

const VIAS = new Set(['session_send', 'session_broadcast'])
const MODES = new Set<SendMode>(['started', 'queued', 'steered'])
/** The gateway's envelope, exactly: a slot key, one of the two verbs, then the blank line. */
const LEGACY_ENVELOPE_RE = /^\[sent by session ([A-Za-z0-9._:@-]{1,128}) via (session_send|session_broadcast)\](?:\n\n?|$)/
const ENVELOPE_MARK = '[sent by session '

function envelopeFor(from: string, via: string): string {
  return `[sent by session ${from} via ${via}]`
}

/** Drop `envelope` and the blank line after it off the head of `content`. */
function stripEnvelope(content: string, envelope: string): string {
  if (!content.startsWith(envelope)) return content
  return content.slice(envelope.length).replace(/^\n\n?/, '')
}

/** The header the gateway's queue merge puts before the joined entries
 *  (`_dequeue_next_message` / `responsive_chat`): `[N queued messages merged]`. */
const MERGE_HEADER_RE = /^\[\d+ queued messages merged\]\n\n/

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * The display body of a row whose sender record says `envelope` opened it. A
 * merged row is the header followed by each entry's own text joined by a blank
 * line, so every entry starts with the same envelope (a record survives the
 * merge only when one sender wrote every entry): each one that opens an entry is
 * dropped, and the header stays, as it does on a person's own merged row. Display
 * only -- the stored text and what the model read keep every envelope.
 */
function stripEnvelopes(content: string, envelope: string): string {
  const header = MERGE_HEADER_RE.exec(content)
  if (!header) return stripEnvelope(content, envelope)
  const entries = content.slice(header[0].length)
  const atEntryStart = new RegExp(`(?<=^|\\n\\n)${escapeRegExp(envelope)}(?:\\n\\n?|$)`, 'g')
  return header[0] + entries.replace(atEntryStart, '')
}

function structured(raw: unknown): SendProvenance | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const from = typeof r.from === 'string' ? r.from.trim() : ''
  const via = typeof r.via === 'string' ? r.via : ''
  if (!from || !VIAS.has(via)) return null
  const out: SendProvenance = { from, via }
  if (typeof r.title === 'string' && r.title.trim()) out.title = r.title.trim()
  if (typeof r.mode === 'string' && MODES.has(r.mode as SendMode)) out.mode = r.mode as SendMode
  if (typeof r.count === 'number' && Number.isInteger(r.count) && r.count > 0) out.count = r.count
  return out
}

/** The validated `sendProvenance` a live frame's meta carries, ready to spread
 *  onto the row a reducer rebuilds from it; empty when the frame has none. */
export function queueEntrySendProvenance(meta: unknown): { sendProvenance?: SendProvenance } {
  const record = meta && typeof meta === 'object' ? structured((meta as Record<string, unknown>)[SEND_PROVENANCE_META_KEY]) : null
  return record ? { sendProvenance: record } : {}
}

/** The sender of `content`/`meta`, or `null` for a row nobody else sent. */
export function readSendProvenance(content: string, meta: Record<string, unknown> | undefined, ts?: string): ReadSendProvenance | null {
  const m = meta ?? {}
  // A person's or app's own send, or a bubble this client minted (only the
  // composer and the steer box stamp a send-correlation id; a peer delivery
  // never carries one).
  if (m.human === true || m.turnActor != null || m.optimistic === true) return null
  if (typeof m.sendId === 'string' || Array.isArray(m.sendIds)) return null
  const text = typeof content === 'string' ? content : ''
  const record = structured(m[SEND_PROVENANCE_META_KEY])
  if (record) return { provenance: record, body: stripEnvelopes(text, envelopeFor(record.from, record.via)) }
  if (typeof m.mid !== 'string' || !m.mid) return null
  if (m.turnProvenanceRestored === true) return null
  const writtenAt = typeof ts === 'string' ? Date.parse(ts) : NaN
  if (!Number.isFinite(writtenAt) || writtenAt >= LEGACY_CUTOFF) return null
  const match = LEGACY_ENVELOPE_RE.exec(text)
  if (!match) return null
  const body = text.slice(match[0].length)
  if (body.includes(ENVELOPE_MARK)) return null
  return { provenance: { from: match[1], via: match[2], legacy: true }, body }
}

/**
 * The text Quote hands the MODEL for a row another chat sent: the sender's
 * envelope put back in front of the shown body. The bubble hides that line from
 * the person, but a quote without it would hand the agent another chat's words
 * as an earlier message of the user's with nothing saying otherwise.
 */
export function quotedPeerText(provenance: SendProvenance, body: string): string {
  return [envelopeFor(provenance.from, provenance.via), body].join('\n\n')
}

/** `text` without a leading gateway envelope line: for an excerpt a PERSON
 *  reads (the quote card), never for what the model reads. */
export function withoutLeadingEnvelope(text: string): string {
  const match = LEGACY_ENVELOPE_RE.exec(text)
  return match ? text.slice(match[0].length) : text
}

/**
 * A short, readable form of a slot key for a sender that is not open: the
 * trailing creation stamp dropped (`chat-316-1792176321` -> `chat-316`), a long
 * key cut with an ellipsis.
 */
export function shortSessionKey(key: string): string {
  const stamped = /^(.+?)-\d{9,}$/.exec(key)
  const base = stamped ? stamped[1] : key
  return base.length > 24 ? `${base.slice(0, 23)}…` : base
}
