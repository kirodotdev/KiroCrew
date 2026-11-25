import type { KiroAccountUsage } from '../../components/KiroAccountModal'
import { isAccountOnly } from '../../api/kiroUsage'

/**
 * Whether Settings > Kiro account draws the Credits card.
 *
 * - The Credits entry is off (`showCredits` false): hidden.
 * - A build with no Kiro sign-in: always shown, it is the tab's only card.
 * - Shown when Kiro Crew is signed in (and not lapsed), or when there is
 *   something to report: a reading (an object, account-only included),
 *   `no-limit`, a failed usage read (`failed`) or a failed config read
 *   (`config-unreadable`), each with its own notice.
 * - Shown in every usage state when kiro-cli is active (sessions run on
 *   kiro-cli's login, or the backend is kiro-cli): that login has a balance
 *   behind it, `none` there means "no reading yet, refresh", and a missing or
 *   refused credential gets the card's kiro-cli remedy.
 * - Hidden only in the plain case: Crew signed out or lapsed, kiro-cli not
 *   active, and nothing read (`null`, `none`, `api-key`, or a sign-in state
 *   that only Crew's own sign-in header can fix).
 */
export function creditsVisible({ showCredits, signInOffered, crewSignedIn, kiroCliActive, usage }: {
  showCredits: boolean
  signInOffered: boolean
  crewSignedIn: boolean
  kiroCliActive: boolean
  usage: KiroAccountUsage
}): boolean {
  if (!showCredits) return false
  if (!signInOffered || crewSignedIn || kiroCliActive) return true
  if (typeof usage === 'object' && usage !== null) return true
  return usage === 'no-limit' || usage === 'failed' || usage === 'config-unreadable'
}

/**
 * Whether the Credits card names the reading's account itself. Only a reading
 * names one, and the card stays quiet when the header above already names it:
 * Crew signed in with a name from the shared reading, or Crew signed out while
 * sessions run on kiro-cli's login and the reading or the sources read has the
 * account's e-mail or name.
 */
export function creditsShowIdentity({ usage, signInOffered, crewSignedIn, headerName, kiroCliHeader, sourcesEmail }: {
  usage: KiroAccountUsage
  signInOffered: boolean
  crewSignedIn: boolean
  headerName: string
  kiroCliHeader: boolean
  sourcesEmail: string | undefined
}): boolean {
  if (typeof usage !== 'object' || usage === null) return false
  const readingNamesAccount = !isAccountOnly(usage) && Boolean(usage.email || usage.account)
  if (kiroCliHeader && (readingNamesAccount || Boolean(sourcesEmail))) return false
  return !(signInOffered && crewSignedIn && headerName !== '')
}
