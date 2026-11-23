import { readPersistedString } from '../hooks/usePersistedString'
import { safeSetItem } from './safeStorage'

/**
 * The last company (IAM Identity Center) sign-in the user submitted: the access
 * portal URL and its optional region.
 *
 * Two forms ask for the same pair: the Kiro sign-in chooser (Settings -> Agent
 * Harness, and the full-screen gate) and the Remote Crew launch form. Both kept
 * it in component or in-memory state only, so every reload or gateway restart
 * handed the user an empty field to fill again. Both forms now seed from these
 * keys and write them when the user submits, so one entry fills the other too.
 *
 * A portal URL and a region name are not secrets (the sign-in already records the
 * URL and `kirocrew kas whoami` prints it). Only what the user actually submitted
 * is kept, never a half-typed value.
 */
export const COMPANY_SIGN_IN_URL_KEY = 'mc-company-sign-in-url'
export const COMPANY_SIGN_IN_REGION_KEY = 'mc-company-sign-in-region'

// Remote Crew accepts scheme-less URLs; the sign-in chooser uses type="url".
function normalizeCompanySignInUrl(startUrl: string): string {
  const url = startUrl.trim()
  return !url || /^[a-z][a-z\d+.-]*:\/\//i.test(url) ? url : `https://${url}`
}

export function readCompanySignIn(): { startUrl: string; region: string } {
  return {
    startUrl: normalizeCompanySignInUrl(readPersistedString(COMPANY_SIGN_IN_URL_KEY, '')),
    region: readPersistedString(COMPANY_SIGN_IN_REGION_KEY, ''),
  }
}

/**
 * What the Kiro sign-in form's company fields start with: the pair this browser
 * last submitted, else the gateway's default (`GET /api/kas-login`
 * `company_sign_in`: the saved setting, else the edition's), else empty. The
 * pair is taken whole from one source, so a remembered URL never gets the
 * gateway's region. `fromDefault` is true only when the pair is the gateway's,
 * so the form can say where the URL came from.
 */
export function initialCompanySignIn(fallback?: {
  start_url?: string
  region?: string
}): { startUrl: string; region: string; fromDefault: boolean } {
  const remembered = readCompanySignIn()
  if (remembered.startUrl) return { ...remembered, fromDefault: false }
  const startUrl = (fallback?.start_url ?? '').trim()
  return startUrl
    ? { startUrl, region: (fallback?.region ?? '').trim(), fromDefault: true }
    : { startUrl: '', region: '', fromDefault: false }
}

/** Remember a submitted company sign-in. An empty region clears the old one. */
export function rememberCompanySignIn(startUrl: string, region: string): void {
  const url = normalizeCompanySignInUrl(startUrl)
  if (!url) return
  safeSetItem(COMPANY_SIGN_IN_URL_KEY, url)
  safeSetItem(COMPANY_SIGN_IN_REGION_KEY, region.trim())
}

/**
 * Do two portal URLs name the same portal? Compares the way a user would write
 * one: case-insensitive, `https://` optional, trailing `/` ignored, and a missing
 * or root path treated as `/start`.
 */
export function sameCompanyPortal(a: string, b: string): boolean {
  const norm = (v: string) => {
    const value = v.trim().split('#')[0].toLowerCase().replace(/^https:\/\//, '').replace(/\/+$/, '')
    return value && !value.includes('/') ? `${value}/start` : value
  }
  return norm(a) !== '' && norm(a) === norm(b)
}
