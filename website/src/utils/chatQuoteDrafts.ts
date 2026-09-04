/**
 * Per-slot persistence for structured quotes staged in the composer.
 *
 * Quotes are session-scoped like staged session references: they must survive a
 * slot switch inside the current tab, but carrying a snapshot of another
 * message across a tab close would resurrect context the user can no longer
 * see. The sanitizer is deliberately lossless for valid quotes; send recovery
 * may temporarily hold more quotes than a fresh staging UI would normally
 * produce, and persistence must not discard work the server rejected.
 */
import { createSlotDraftStore } from './slotDraftStore'
import type { QuoteRef } from './quoteRefs'

export const QUOTE_DRAFTS_KEY = 'mc-chat-quote-drafts'

export type QuoteDrafts = Record<string, QuoteRef[]>

export function sanitizeQuoteRefs(value: unknown): QuoteRef[] | null {
  if (!Array.isArray(value)) return null
  const out: QuoteRef[] = []
  const seen = new Set<string>()
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const quote = item as Record<string, unknown>
    if (typeof quote.key !== 'string' || !quote.key || seen.has(quote.key)) continue
    if (typeof quote.text !== 'string' || !quote.text.trim()) continue
    seen.add(quote.key)
    out.push({
      key: quote.key,
      role: typeof quote.role === 'string' ? quote.role : '',
      time: typeof quote.time === 'string' ? quote.time : '',
      text: quote.text,
      mid: typeof quote.mid === 'string' && quote.mid ? quote.mid : undefined,
      ts: typeof quote.ts === 'string' && quote.ts ? quote.ts : undefined,
      code: quote.code === true || undefined,
    })
  }
  return out.length ? out : null
}

/** Merge quotes restored after a failed send behind anything staged since. */
export function mergeQuoteRefs(keep: QuoteRef[], incoming: QuoteRef[]): QuoteRef[] {
  if (!incoming.length) return keep
  const held = new Set(keep.map(quote => quote.key))
  return [...keep, ...incoming.filter(quote => !held.has(quote.key))]
}

const store = createSlotDraftStore<QuoteRef[]>({
  key: QUOTE_DRAFTS_KEY,
  storage: 'session',
  sanitize: sanitizeQuoteRefs,
})

export const loadQuoteDrafts = store.load
export const saveQuoteDrafts = store.save
export const setQuoteDraft = store.set
/** @internal test-only */
export const __resetQuoteDraftsForTests = store.__resetForTests
