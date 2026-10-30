import { contentHash } from '../lib/contentHash'
import { quoteBlock, readMessageQuote } from '../chat-core/composer/messageQuote'

/** Fingerprint of the injected head, recorded on meta.knowledge at send time. */
export interface KnowledgeEnvelopeMark {
  len: number
  hash: string
}

/** Ignore trailing whitespace, which the backend trims from stored rows. */
export function knowledgeEnvelopeMark(expanded: string): KnowledgeEnvelopeMark {
  const head = expanded.trimEnd()
  return { len: head.length, hash: contentHash(head) }
}

/** End of the recorded head, or zero for an absent, malformed or mismatched mark.
 *  This non-cryptographic fingerprint is a display hint, not a security boundary. */
export function knowledgeEnvelopeEnd(content: string, mark: unknown): number {
  if (!mark || typeof mark !== 'object' || Array.isArray(mark)) return 0
  const m = mark as Partial<KnowledgeEnvelopeMark>
  if (typeof m.hash !== 'string' || !Number.isInteger(m.len)) return 0
  const len = m.len as number
  if (len <= 0 || len > content.length) return 0
  if (len < content.length && content[len] !== '\n') return 0
  return contentHash(content.slice(0, len)) === m.hash ? len : 0
}

/** End of the recorded envelope in a row, after any exact carried quote prefix. */
export function knowledgeEnvelopeEndInRow(content: string, meta: Record<string, unknown> | undefined): number {
  const mark = (meta?.knowledge as { envelope?: unknown } | undefined)?.envelope
  const q = readMessageQuote(meta)
  let off = 0
  if (q) {
    const prefix = quoteBlock(q)
    if (content.startsWith(prefix)) {
      off = prefix.length
      while (content[off] === '\n') off++
    }
  }
  const end = knowledgeEnvelopeEnd(content.slice(off), mark)
  return end ? off + end : 0
}

/** Hide only the recorded head; older rows and optimistic bubbles stay intact. */
export function stripKnowledgeEnvelope(content: string, mark: unknown): string {
  const end = knowledgeEnvelopeEnd(content, mark)
  return end ? content.slice(end).replace(/^\n+/, '') : content
}
