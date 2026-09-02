/**
 * Suppression seam for BUILT-IN Overview surfaces.
 *
 * The other Overview seams are additive: `overviewStatCards` appends tiles and
 * `overviewPanel` claims the region below the summary grid. Neither can express
 * the opposite need — a downstream distribution whose environment makes a
 * built-in surface permanently inapplicable, and which therefore wants it gone
 * rather than rendered as a dead end.
 *
 * The motivating case is `tailnet-mobile`. An enterprise distribution may
 * disable tailnet access outright (its own remote-access path is something
 * else), and for those users the phone-access card can never reach a usable
 * state: it renders a `Blocked by policy` body whose only advice is "ask your
 * administrator", under a `warn` "needs attention" badge for a state in which
 * nothing is pending and nothing is theirs to change. That is a permanent,
 * unactionable panel on the landing page. Without a seam the only ways to remove
 * it are to patch `OverviewPage.tsx` on every sync, or to hide it with CSS
 * against a selector nothing guarantees — both of which rot.
 *
 * Three properties are deliberate.
 *
 * **The id set is a typed union, not a free string.** A misspelled free-form id
 * would suppress nothing and say nothing, and the symptom (the card is still
 * there) looks identical to the seam not working. A union makes that a compile
 * error at the call site instead.
 *
 * **Suppressing twice is NOT a collision.** `overviewPanel` treats a second
 * registration as a conflict because two owners cannot both render into one
 * slot. Absence has no such problem: two parties that both want a surface gone
 * agree, so this registry is a set and re-entrant registration (HMR, a module
 * imported twice) is silently idempotent rather than a `reportSeamCollision`.
 *
 * **It is one-way, and it is not a security control.** There is no `unsuppress`:
 * registration happens at module load during composition, before the page
 * renders, and this registry is not reactive. Suppression removes a piece of
 * GUIDANCE from one page — it does not relax anything. Whatever policy made the
 * surface inapplicable is still enforced server-side (for `tailnet-mobile` the
 * status endpoint still derives its step and the QR mint still refuses a pinned
 * install with `governance_pinned`), so hiding the card cannot grant access that
 * the backend would otherwise have denied.
 *
 * The core suppresses nothing, so the stock build is unchanged.
 */

/**
 * Built-in Overview surfaces a downstream distribution may suppress.
 *
 * Keep this union minimal and add a member only alongside a real consumer: an id
 * with no caller is an API surface that has never been exercised.
 *
 * - `tailnet-mobile` — the phone-access guidance card (`TailnetMobileCard`),
 *   rendered above the summary cards.
 */
export type SuppressibleOverviewBuiltin = 'tailnet-mobile'

const SUPPRESSED = new Set<SuppressibleOverviewBuiltin>()

/**
 * Suppress a built-in Overview surface, so it renders nothing at all.
 *
 * Named `suppress*` rather than `register*` on purpose: the other seams register
 * a contribution, and reading this one as an addition at a call site would
 * invert its meaning.
 *
 * Idempotent — see the module docstring for why a repeat is not a collision.
 */
export function suppressOverviewBuiltin(id: SuppressibleOverviewBuiltin): void {
  SUPPRESSED.add(id)
}

/** Whether a built-in Overview surface has been suppressed downstream. */
export function isOverviewBuiltinSuppressed(id: SuppressibleOverviewBuiltin): boolean {
  return SUPPRESSED.has(id)
}
