/**
 * The provenance line `session_send` puts on a message it delivers:
 * `[sent by session <key> via session_send]`, then a blank line, then the sender's
 * text (`session_messaging._SEND_PROVENANCE`).
 *
 * Only that exact envelope counts, and only on a row no person typed (a person's
 * own row carries `meta.human`). `[quoted: sent by session ...]` is the server's
 * DEFANGED copy of a forged envelope, kept visible on purpose so the reader sees
 * the sender wrote one; it is left as written and earns no label.
 *
 * The line is for the receiving agent. A person reading the worker's transcript
 * needs who sent it, not a slot key, so the view drops the line and names the
 * sender instead ("From conductor").
 */

const SEED_RE = /^\[sent by session ([\w.:-]+) via session_(?:send|create|broadcast)\]\n\n/

export interface SessionSeed {
  /** The sender's slot key, as the line names it. */
  sender: string
  /** The sender's text, without the line. */
  body: string
}

/** The seed line and body of *content*, or null when it does not open with one or
 *  the row is the person's own (`meta.human`). */
export function parseSessionSeed(content: string, meta?: Record<string, unknown> | null): SessionSeed | null {
  if (meta && meta.human) return null
  const match = SEED_RE.exec(content)
  if (!match) return null
  return { sender: match[1], body: content.slice(match[0].length) }
}

/** *content* without a leading seed line; unchanged when it has none. */
export function withoutSessionSeed(content: string, meta?: Record<string, unknown> | null): string {
  return parseSessionSeed(content, meta)?.body ?? content
}

/** {@link SEED_RE} at the start of every line, not only the first. */
const SEED_LINES_RE = new RegExp(SEED_RE.source, 'gm')

/** *content* without ANY line-start seed line, for a row the gateway already
 *  proved a delivery (`meta.relay`). A drain that merged several queued sends
 *  from one sender writes "[N queued messages merged]" and then each send under
 *  its own line; a sender cannot type a lookalike, because `session_send`
 *  defangs the envelope in the text it delivers. A person's own row is never
 *  stripped. */
export function withoutSessionSeeds(content: string, meta?: Record<string, unknown> | null): string {
  if (meta && meta.human) return content
  return content.replace(SEED_LINES_RE, '')
}

/** The slot-row fields the sender label reads. */
export interface SeedRow { key: string; crew_root?: string }

export type SeedSender = 'conductor' | 'worker' | 'session'

/** Who a delivered message is from, by role: the root of the worker *slotKey*,
 *  another worker of that root, or any other session. */
export function seedSender(rows: readonly SeedRow[], sender: string, slotKey: string | undefined): SeedSender {
  const root = rows.find(row => row.key === slotKey)?.crew_root || ''
  if (root && sender === root) return 'conductor'
  if (root && rows.find(row => row.key === sender)?.crew_root === root) return 'worker'
  return 'session'
}


/** Up to two initials of a sender's session title, for the avatar beside its
 *  name: the first letter of the first two words, or the first two letters of
 *  one word; a leading `@` / `#` is skipped. `?` when the title has none. */
export function senderInitials(title: string): string {
  // By code point, so a title opening with an emoji or another astral character
  // yields that whole character, never half of a surrogate pair.
  const words = title.trim().split(/\s+/).map(w => Array.from(w.replace(/^[@#]+/, ''))).filter(w => w.length)
  if (!words.length) return '?'
  const letters = words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2).join('')
  return letters.toUpperCase()
}
