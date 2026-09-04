/**
 * Staged quote references — the data behind the composer's annotation pill.
 *
 * A quote is a SNAPSHOT of the text the user selected, not a live pointer into
 * the transcript. Regenerating or editing the source message must not silently
 * rewrite what the user is about to send, so the text is copied at selection
 * time and never re-derived.
 *
 * Identity is `mid` (the server-minted row id), not `ts`: a coarse clock can
 * stamp two rows in the same tick, and the steer path overwrites `ts` mid-stream.
 * `ts` rides along only as the fallback the jump-to-source path already accepts
 * for rows minted before `mid` existed.
 */
export interface QuoteRef {
  /** Stable per-quote key for React and for removal. */
  key: string
  /** Localized role label for display and for the wire tag ("Assistant", "You"). */
  role: string
  /** Localized clock label shown next to the role ("10:22"). */
  time: string
  /** The selected text, verbatim. Never truncated here — truncation is display-only. */
  text: string
  /** Server row id of the source message; the durable jump-to-source anchor. */
  mid?: string
  /** Source message timestamp; fallback anchor when `mid` is absent (streaming rows). */
  ts?: string
  /** True when the selection came from a preformatted/code context. */
  code?: boolean
}

/** Max characters of a quote shown in the pill's resting label. */
export const QUOTE_EXCERPT_MAX = 50
const LINE_BREAK = '\n'
const PARAGRAPH_BREAK = '\n\n'
const BLOCKQUOTE_PREFIX = '> '
const CODE_FENCE = '```'

/**
 * One-line excerpt for the pill's resting label.
 *
 * Collapses newlines so a multi-line selection cannot grow the pill's height,
 * and hard-caps the length so a long quote does not push the composer around.
 * The full text is always what gets sent — this is presentation only.
 */
export function quoteExcerpt(text: string, max: number = QUOTE_EXCERPT_MAX): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  return `${flat.slice(0, max).trimEnd()}…`
}

/** Distinct role labels, in first-seen order, for the multi-quote pill label. */
export function distinctRoles(quotes: QuoteRef[]): string[] {
  const seen: string[] = []
  for (const q of quotes) if (!seen.includes(q.role)) seen.push(q.role)
  return seen
}

/**
 * Serialize staged quotes into the text the agent receives.
 *
 * The shape is an ENRICHED BLOCKQUOTE — `> [Role · time] text` — deliberately
 * the same `>` form the agent already parses today, just carrying a provenance
 * tag. That is what makes the pill a presentation upgrade with no agent-side
 * change: an agent that knows nothing about pills still reads a labeled
 * blockquote.
 *
 * A code selection is wrapped in a fence inside the blockquote so indentation
 * and backticks survive; flattening code to prose corrupts its meaning.
 *
 * Quotes are emitted in the order given (the caller keeps conversation order)
 * and carry their FULL text — the pill's truncation never reaches the wire.
 */
export function serializeQuoteRefs(quotes: QuoteRef[]): string {
  if (!quotes.length) return ''
  return quotes
    .map(q => {
      const tag = `[${q.role} · ${q.time}]`
      if (q.code) {
        // The fence lines are themselves blockquoted so the whole block stays
        // one quotation rather than breaking out of it mid-way.
        const body = q.text
          .split(LINE_BREAK)
          .map(line => `${BLOCKQUOTE_PREFIX}${line}`)
          .join(LINE_BREAK)
        return [
          `${BLOCKQUOTE_PREFIX}${tag}`,
          `${BLOCKQUOTE_PREFIX}${CODE_FENCE}`,
          body,
          `${BLOCKQUOTE_PREFIX}${CODE_FENCE}`,
        ].join(LINE_BREAK)
      }
      const lines = q.text.split(LINE_BREAK)
      const [first, ...rest] = lines
      const head = `${BLOCKQUOTE_PREFIX}${tag} ${first}`
      if (!rest.length) return head
      return [head, ...rest.map(line => `${BLOCKQUOTE_PREFIX}${line}`)].join(LINE_BREAK)
    })
    .join(LINE_BREAK)
}

/**
 * Prepend the serialized quotes to the user's typed message.
 *
 * Quotes lead and the typed text follows, separated by a blank line: the
 * reference is context for the instruction, so the agent reads what is being
 * referred to before what to do about it. Mirrors `appendSessionRefLinks`'
 * non-splicing contract — the typed text is never rewritten, so paste-token
 * ranges computed against it stay valid.
 */
export function prependQuoteRefs(text: string, quotes: QuoteRef[]): string {
  const block = serializeQuoteRefs(quotes)
  if (!block) return text
  const typed = text.trim()
  return typed ? `${block}${PARAGRAPH_BREAK}${typed}` : block
}
