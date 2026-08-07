import type { ModelInfo } from './types'

/**
 * True when `value` is a multiplier we can actually show as a price.
 *
 * Rejects `undefined` (nothing was reported) and 0 / negative / non-finite
 * (a malformed row). The zero case is the one worth spelling out: `0` is falsy
 * but NOT absent, so a bare `!== undefined` check lets it through and the badge
 * renders "0x" — which reads as "this model is free". No model kiro serves is
 * free; the cheapest is 0.01x. Treating a malformed value as unknown shows
 * nothing, which is the honest outcome.
 *
 * Used at BOTH boundaries — the adapter that ingests /api/models and the
 * component that renders the badge — so a row reaching the picker from some
 * other path (a cached list, a test, a future caller) cannot skip the check.
 */
export function isPricedMultiplier(value: number | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/**
 * The model-picker list: a short-labelled Auto row first, then every other
 * model in the backend's own order.
 *
 * ## Why this exists
 *
 * Four surfaces render the model picker (ChatPage, ChatPane, ChatSidebar's bulk
 * switcher, AgentsPage) and all four independently open-coded
 * `[{ name: 'auto', description: 'Default' }, ...models.filter(m => m.name !== 'auto')]`.
 * That synthetic first row exists for a good reason — kiro's own Auto
 * description ("Models chosen by task for optimal usage and consistent quality")
 * is far longer than the neighbouring rows and unbalances the list — but
 * replacing the row wholesale also DISCARDED everything else the live row
 * carried.
 *
 * That became load-bearing with the credit multiplier: Auto is the 1.0x baseline
 * every other badge is relative to, so it is the single most useful number in
 * the list, and it lives on exactly the row the synthetic entry threw away.
 * Rather than hardcode 1.0 (a price we would be asserting, not reading — and
 * kiro does re-price models), this merges: the short label is kept, the live
 * row's data is carried over.
 *
 * Auto is matched by exact id. Every `auto` row is folded into the single first
 * entry — if the backend ever sent two, the second does NOT survive as a
 * separate option, because the tail filter drops all of them. That is
 * deliberate (one Auto row is the contract; a duplicate is a backend bug we
 * should not render twice) and is asserted by an output-length test.
 */
export function withAutoFirst(models: ModelInfo[]): ModelInfo[] {
  const live = models.find(m => m.name === 'auto')
  const rest = models.filter(m => m.name && m.name !== 'auto')
  // `description: ''` rather than the English word: the picker renders Auto's
  // short label from a catalog key at render time
  // (`components.modelDropdownList.auto_default`). Carrying the literal here
  // would put untranslated user-visible copy in a data module — which the
  // i18n gate measures per-file against the base, so relocating it from the
  // five old call sites into this one still counts as new untranslated copy.
  // Translating it HERE would be worse than the literal: the result is cached
  // in React Query, so it would freeze whichever language was active when the
  // list was fetched.
  return [{ ...live, name: 'auto', description: '' }, ...rest]
}
