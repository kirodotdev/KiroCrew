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
 * The model-picker list: Auto first, then every other model in the backend's
 * own order.
 *
 * ## Why this exists
 *
 * Four surfaces render the model picker (ChatPage, ChatPane, ChatSidebar's bulk
 * switcher, AgentsPage). This helper keeps their ordering consistent while
 * preserving every field reported on the live Auto row, including the description
 * now shown from the row's information button and the credit multiplier used as
 * the pricing baseline.
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
  return [{ ...live, name: 'auto', description: live?.description ?? '' }, ...rest]
}
