/**
 * The one parser for the `/api/sessions/usage` envelope.
 *
 * Both readers of that envelope share it: the top-bar credit pill's query in
 * `App.tsx` (the polled GET) and the account modal's Refresh button (the POST to
 * `/api/sessions/usage/refresh`, which answers with the same `{usage}` shape).
 * One parser means the reading the modal shows after a click can never be
 * normalized differently from the reading the pill polls thirty seconds later.
 */
import type { KiroCreditUsage, KiroUsagePayload } from './client'

const MAX_KIRO_BONUS_GRANT_NAME_CHARS = 100
const MAX_KIRO_BONUS_CREDITS = 1_000_000
const MAX_KIRO_BONUS_DAYS_LEFT = 3_650

/**
 * What the envelope resolves to: a reading, or one of the terminal sentinels the
 * pill and modal branch on, or `null` while the gateway's cache is still warming.
 * `null` is the ONLY value that means "still loading".
 */
export type KiroUsageState =
  | KiroCreditUsage
  | 'none'
  | 'api-key'
  | 'signin-required'
  | null

export function parseKiroUsagePayload(d: { usage?: KiroUsagePayload } | undefined): KiroUsageState {
  const u: KiroUsagePayload = d?.usage || {}
  // Kiro credit plan (internal) — the only usage this pill surfaces.
  // Number.isFinite guards against a stray NaN ever rendering as "NaN / NaN".
  if (typeof u.credits_plan === 'number' && Number.isFinite(u.credits_plan)) {
    const limit = Math.round(u.credits_plan)
    // credits_used is the real total (backend sets it to covered + overage);
    // fall back to 0 (not the limit) when the source omits it, so a partial
    // payload never implies a maxed plan.
    const used = typeof u.credits_used === 'number' && Number.isFinite(u.credits_used)
      ? Math.round(u.credits_used)
      : 0
    const overage = typeof u.credits_overage === 'number' && Number.isFinite(u.credits_overage)
      ? u.credits_overage
      : Math.max(0, used - limit)
    // Bonus grants come from untrusted CLI output. Validate every field so
    // one malformed grant cannot poison the readout or account panel.
    const bonusCredits = Array.isArray(u.bonus_credits)
      ? u.bonus_credits.flatMap(grant => {
          if (
            !grant
            || typeof grant.name !== 'string'
            || !grant.name
            || grant.name.length > MAX_KIRO_BONUS_GRANT_NAME_CHARS
            || typeof grant.used !== 'number'
            || !Number.isFinite(grant.used)
            || grant.used < 0
            || grant.used > MAX_KIRO_BONUS_CREDITS
            || typeof grant.total !== 'number'
            || !Number.isFinite(grant.total)
            || grant.total <= 0
            || grant.total > MAX_KIRO_BONUS_CREDITS
            || (grant.days_left !== undefined
              && (typeof grant.days_left !== 'number'
                || !Number.isFinite(grant.days_left)
                || grant.days_left < 0
                || grant.days_left > MAX_KIRO_BONUS_DAYS_LEFT))
          ) return []
          return [{
            name: grant.name,
            used: grant.used,
            total: grant.total,
            daysLeft: grant.days_left,
          }]
        })
      : []
    const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
    const parsedOverageRate = typeof u.overage_rate === 'number'
      ? u.overage_rate
      : Number.parseFloat(u.overage_rate ?? '')
    const normalized: KiroCreditUsage = {
      used,
      limit,
      overage,
      resets: u.resets,
      plan: u.plan,
      costUsd: u.cost_usd,
      overageRate: Number.isFinite(parsedOverageRate) ? parsedOverageRate : undefined,
      bonusCredits,
      stale: u.stale === true,
      account: str(u.account),
      email: str(u.email),
      accountType: str(u.account_type),
      startUrl: str(u.start_url),
    }
    return normalized
  }
  // No reading (kiro-cli absent, or neither the API nor the /usage scrape
  // produced a plan) -> 'none': on the Kiro backend a dash whose modal carries
  // the Refresh that can fill it, so the segment stays on screen; on any other
  // harness App.tsx hides the segment (see isKiroBackend). API-key auth -> terminal "not
  // available for this auth type" (a dash with that reason; for this account
  // type the state is permanent, not a warming cache). No readable Kiro
  // credential -> also terminal, with its remedy named: sign in again. Empty
  // cache (Kiro warming) -> spinner.
  if (u.available === false) {
    if (u.reason === 'api_key_auth') return 'api-key' as const
    if (u.reason === 'signin_required') return 'signin-required' as const
    return 'none' as const
  }
  return null
}
