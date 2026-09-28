/**
 * Single source of truth for L2 theme-experience consent.
 *
 * An installed L2 pack can inject a persona into the agent's system prompt
 * (backend §6.5) and play audio, so activating one requires an explicit,
 * per-slug, first-activation opt-in. That grant is stored in localStorage under
 * `mc-theme-consent-<slug>`.
 *
 * This module is the single reader/writer for the theme-consent token, so its
 * format cannot drift across call sites: if the persona side wrote a
 * sha256/`consented-v2` token while the chat side (client.ts's sendChat)
 * checked for a legacy `'1'` token, the wire flag would be permanently false
 * and installed personas would never activate.
 *
 * Wire contract (two-tier consent): the chat wire does not send a trust
 * boolean. client.ts transmits the RAW stored grant (getStoredConsent) as
 * `theme_consent_sha`, and the backend verifies content-binding — it injects
 * the persona only if that token equals sha256 of the persona.md it reads. So a
 * re-installed/changed persona (new sha) does not match the stored grant and
 * the never-consented persona is not injected.
 *
 * Read model:
 *  - hasConsent(slug, expectedToken) — STRICT. Used by the layer, which holds
 *    the persona descriptor: the stored grant must equal the current content
 *    fingerprint (sha256) so a re-installed/changed persona re-prompts.
 *  - hasAnyConsent(slug) — RELAXED. A "some non-legacy grant is stored" helper
 *    for any descriptor-less caller. Legacy `'1'` is deliberately treated as NOT
 *    consented so a grant from an older build re-prompts exactly once (matches
 *    the shipped re-prompt semantics).
 */
import { safeGetItem, safeSetItem } from './safeStorage'

/** localStorage key prefix for per-slug experience consent. */
export const CONSENT_PREFIX = 'mc-theme-consent-'

/**
 * Consent token written by older builds. It is intentionally NOT a valid grant:
 * personas are content-bound (sha256) and audio uses a stable sentinel, neither
 * of which is `'1'`. Any `'1'` grant re-prompts once.
 */
const LEGACY_TOKEN = '1'

/** Persist a granted consent for `slug`, bound to `token` (persona sha256 or sentinel). */
export function grantConsent(slug: string, token: string): void {
  if (!slug) return
  // safeSetItem is quota-safe (reclaim+retry); consent grants are tiny but we
  // route through it for parity with every other localStorage write.
  safeSetItem(CONSENT_PREFIX + slug, token)
}

/** Remove any stored consent for `slug` (best-effort; never throws). */
export function revokeConsent(slug: string): void {
  if (!slug || typeof localStorage === 'undefined') return
  try {
    localStorage.removeItem(CONSENT_PREFIX + slug)
  } catch {
    /* storage disabled / SecurityError — nothing to clean up */
  }
}

/** Raw stored grant for `slug`, or null if none / storage unavailable. */
export function getStoredConsent(slug: string): string | null {
  if (!slug) return null
  return safeGetItem(CONSENT_PREFIX + slug)
}

/**
 * STRICT check: is there a stored grant for `slug` that exactly matches
 * `expectedToken` (the current persona sha256 or audio sentinel)? A changed
 * persona (new sha256) or a legacy `'1'` grant fails this and re-prompts.
 */
export function hasConsent(slug: string, expectedToken: string): boolean {
  const stored = getStoredConsent(slug)
  return stored !== null && stored === expectedToken
}

/**
 * RELAXED check for callers without the descriptor: is ANY non-legacy grant
 * stored for `slug`? Legacy `'1'` (and empty) count as NOT consented, so an
 * older build's grant re-prompts rather than silently activating.
 */
export function hasAnyConsent(slug: string): boolean {
  const stored = getStoredConsent(slug)
  return stored !== null && stored !== '' && stored !== LEGACY_TOKEN
}
