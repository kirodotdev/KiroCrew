import { describe, expect, it } from 'vitest'
import type { KiroAccountUsage } from '../components/KiroAccountModal'
import { creditsVisible } from '../pages/developer/kiroCreditsVisibility'

const reading: KiroAccountUsage = { used: 10, limit: 100, overage: 0, bonusCredits: [], stale: false, email: 'a@example.com' }
const accountOnly: KiroAccountUsage = { accountOnly: true }

// Each state: [label, usage, signed out (KAS), signed in, signed out with kiro-cli active].
// kiro-cli active shows the card in every state: its login has a balance behind it.
const table: [string, KiroAccountUsage, boolean, boolean, boolean][] = [
  ['a reading', reading, true, true, true],
  ['an account-only answer', accountOnly, true, true, true],
  ['nothing read (null)', null, false, true, true],
  ['none', 'none', false, true, true],
  // The GPT r7 case: Crew signed out, backend unknown, usage read 503.
  ['failed', 'failed', true, true, true],
  ['signin-required', 'signin-required', false, true, true],
  ['signin-rejected', 'signin-rejected', false, true, true],
  ['api-key', 'api-key', false, true, true],
  ['no-limit', 'no-limit', true, true, true],
  ['config-unreadable', 'config-unreadable', true, true, true],
]

describe('creditsVisible', () => {
  it.each(table)('%s', (_label, usage, signedOut, signedIn, kiroCli) => {
    const base = { showCredits: true, signInOffered: true, usage }
    expect(creditsVisible({ ...base, crewSignedIn: false, kiroCliActive: false })).toBe(signedOut)
    expect(creditsVisible({ ...base, crewSignedIn: true, kiroCliActive: false })).toBe(signedIn)
    expect(creditsVisible({ ...base, crewSignedIn: false, kiroCliActive: true })).toBe(kiroCli)
    // The Credits entry off hides it in every case; a build without the sign-in shows it in every case.
    expect(creditsVisible({ ...base, showCredits: false, crewSignedIn: true, kiroCliActive: true })).toBe(false)
    expect(creditsVisible({ ...base, signInOffered: false, crewSignedIn: false, kiroCliActive: false })).toBe(true)
  })
})
