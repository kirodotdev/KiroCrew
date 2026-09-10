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

/**
 * Reorder the picker list to honour a user-authored order (config
 * `agent.model_order`), applied on top of `withAutoFirst`'s output.
 *
 * ## Contract
 *
 * - Models named in `order` come first, in exactly that order.
 * - A name in `order` that no live model matches is silently SKIPPED, never
 *   rendered as a phantom row. kiro renames and re-prices models, so a saved
 *   order routinely outlives some of the ids it lists; the write side keeps a
 *   well-formed-but-unknown id rather than rejecting it, and this is the render
 *   side that makes that safe.
 * - Every model NOT named in `order` is appended after the ordered ones, in the
 *   backend order it arrived in. "My picks first, kiro's order below" is the
 *   whole feature: it is predictable, and nothing is ever hidden (the settings
 *   drag UI shows every model). This is deliberately NOT the neighbour-relative
 *   reinsertion of `appstore/categories.ts::mergeCategoryOrder` — that solves a
 *   different problem (a partial order over a fixed category map) and its
 *   semantics would surprise here, so it is not reused or generalised.
 * - `auto` is pinned first REGARDLESS of the saved order, preserving
 *   `withAutoFirst`'s merge/dedup contract and the 1.0x-baseline reading of the
 *   credit-multiplier badges. An `auto` entry inside `order` is ignored rather
 *   than honoured, so a saved order can never demote the baseline row.
 * - An empty or absent `order` returns the input unchanged (same reference), so
 *   the default install pays nothing and the `useMemo` in `useAvailableModels`
 *   stays referentially stable.
 *
 * Pure and total: it reads `order` and `models`, allocates a new array, and
 * never touches the network or the query cache — which is what lets
 * `useAvailableModels` apply it in a `useMemo` so a settings change reorders
 * every picker on the next render instead of on the next model refetch.
 */
export function applyModelOrder(models: ModelInfo[], order: string[]): ModelInfo[] {
  // Empty/absent order is the default install: identity, same reference.
  if (!order || order.length === 0) return models

  // Auto is pinned separately and is never subject to the saved order, so it is
  // lifted out before ordering and prepended back at the end. Matched by exact
  // id, mirroring withAutoFirst.
  const auto = models.find(m => m.name === 'auto')
  const orderable = auto ? models.filter(m => m.name !== 'auto') : models

  const byName = new Map(orderable.map(m => [m.name, m]))
  const placed = new Set<string>()
  const ordered: ModelInfo[] = []
  for (const name of order) {
    // Skip `auto` (pinned, not ordered), unknown-but-well-formed stale ids
    // (no live match), and any duplicate the order lists twice.
    if (name === 'auto' || placed.has(name)) continue
    const m = byName.get(name)
    if (m) { ordered.push(m); placed.add(name) }
  }

  // Everything the order did not name, in backend (input) order.
  const tail = orderable.filter(m => !placed.has(m.name))
  const reordered = [...ordered, ...tail]
  return auto ? [auto, ...reordered] : reordered
}

/**
 * Merge a REORDERED sequence of live model names back into the saved order,
 * for the write side of a drag: the result is what gets PATCHed to
 * `agent.model_order`.
 *
 * Why this exists: the drag surface renders `applyModelOrder`'s output, which
 * deliberately SKIPS saved ids the live list doesn't currently advertise (a
 * stale id must never add a phantom row). Persisting that rendered array
 * verbatim would therefore silently and permanently DELETE every stale id on
 * the first drag — while the write side's documented contract (see
 * `applyModelOrder` above, and the backend's grammar-only validation) is that
 * a well-formed-but-unknown id is KEPT, precisely because kiro renames models
 * and a saved order must outlive a rename.
 *
 * Semantics: each saved slot keeps its kind. A slot holding a LIVE id is
 * re-filled with the next name from the reordered live sequence; a slot
 * holding a STALE id keeps that id exactly where it was, so when the model
 * comes back it reappears in the position the user gave it, not at the
 * bottom. Live names beyond the saved ones (the unlisted backend tail, which
 * the drag surface also renders) append after, giving every live model an
 * explicit slot in the written order.
 */
export function mergeReorderedNames(savedOrder: string[], liveReordered: string[]): string[] {
  const live = new Set(liveReordered)
  const out: string[] = []
  const seen = new Set<string>()
  let li = 0
  for (const name of savedOrder) {
    if (seen.has(name)) continue
    if (live.has(name)) {
      // A live-occupied slot: fill with the next name of the new live
      // sequence. Skipping names already emitted keeps the output
      // duplicate-free even if a caller hands over an unnormalized order.
      while (li < liveReordered.length && seen.has(liveReordered[li])) li++
      if (li < liveReordered.length) { out.push(liveReordered[li]); seen.add(liveReordered[li]); li++ }
    } else {
      out.push(name)
      seen.add(name)
    }
  }
  while (li < liveReordered.length) {
    if (!seen.has(liveReordered[li])) { out.push(liveReordered[li]); seen.add(liveReordered[li]) }
    li++
  }
  return out
}
