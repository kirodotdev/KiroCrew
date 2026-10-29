/**
 * The one parser for the `/api/sessions/usage` envelope.
 *
 * Both readers of that envelope share it: the top-bar credit pill's query in
 * `shell/topbar/kiroUsageReadout.tsx` (the polled GET) and the account modal's Refresh button (the POST to
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
  | KiroAccountOnly
  | 'none'
  | 'signin-required'
  | null

/**
 * A sign-in with no balance (`reason: account_unproven`): the whoami names
 * the provider and no user, so only the account type is known. The gateway
 * publishes this state only when email and start URL are absent, so the type
 * is the one field it carries.
 */
export interface KiroAccountOnly {
  accountOnly: true
  accountType?: string
}

/** True for the signed-in-account-without-a-balance state. */
export const isAccountOnly = (state: unknown): state is KiroAccountOnly =>
  typeof state === 'object' && state !== null && (state as { accountOnly?: unknown }).accountOnly === true

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
  // harness shell/topbar/kiroUsageReadout.tsx hides the segment (see
  // isKiroBackend). No readable Kiro credential -> terminal, with its remedy
  // named: sign in again. Empty cache (Kiro warming) -> spinner. API-key auth
  // is not a terminal state: the /usage scrape returns the real balance for it
  // just as it does for an SSO sign-in, so it resolves to a reading.
  if (u.available === false) {
    if (u.reason === 'signin_required') return 'signin-required' as const
    if (u.reason === 'account_unproven') {
      const type = u.account_type
      return { accountOnly: true, accountType: typeof type === 'string' && type ? type : undefined }
    }
    return 'none' as const
  }
  return null
}

/** Fast poll while the reading has not settled; the steady cadence once it has. */
export const KIRO_USAGE_WARMING_POLL_MS = 2_500
export const KIRO_USAGE_STEADY_POLL_MS = 30_000

/**
 * How often the credit pill should re-poll `/api/sessions/usage`, given the last
 * settled state and whether the last read errored.
 *
 * The gateway serves the reading from a cache that the GET itself warms in the
 * background: a reopen hits an empty cache (`null`) and, before kiro-cli
 * readiness is verified, a 503 the query surfaces as an error with no data. At
 * the steady cadence each of those unsettled reads costs a full interval before
 * the next poll sees the warmed value, so the pill sits "checking" for roughly
 * two intervals after a reopen. Polling fast while the state is unsettled
 * collapses that wait to a couple of seconds; a settled state — a real reading,
 * or a terminal `none` / `api-key` / `signin-required` the next poll would not
 * change — drops back to the steady cadence so a resolved pill is not polled
 * every few seconds forever.
 *
 * `null` is the warming cache. `isError` is the pre-readiness 503 (and any other
 * failed read): the query holds no data, so `state` is `null` there too, but it
 * is called out explicitly because an errored read is exactly the boot condition
 * this acceleration targets.
 *
 * The fast cadence is bounded by `settledReads`, the number of reads the query
 * has finished so far (data plus error updates). A reopen settles within a few
 * reads; a state that never settles — a signed-out kiro-cli's standing 503, a
 * gateway that is down, a cache that stays empty — or a pill that had a reading
 * and then starts failing is past the cap, so it falls back to the steady
 * cadence instead of polling 12x faster for as long as the tab is open.
 */
export const KIRO_USAGE_WARMING_MAX_READS = 12

export function kiroUsageRefetchInterval(
  state: KiroUsageState,
  isError: boolean,
  settledReads: number,
): number {
  if (settledReads >= KIRO_USAGE_WARMING_MAX_READS) return KIRO_USAGE_STEADY_POLL_MS
  if (isError || state === null) return KIRO_USAGE_WARMING_POLL_MS
  return KIRO_USAGE_STEADY_POLL_MS
}
