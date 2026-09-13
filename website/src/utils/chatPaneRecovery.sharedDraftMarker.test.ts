/** The duplicate-send marker was keyed by the TAB that staged it, while the composer draft it warns
 *  about is keyed by SLOT and shared between tabs — and that draft is deliberately left populated for
 *  a revisit-and-resend. A second tab on the same slot therefore restored the draft with no warning,
 *  and resending duplicated a turn the server had already accepted.
 *
 *  A new TAB is modelled the way the browser makes one: sessionStorage starts empty, while
 *  localStorage — the record store AND the claim heartbeats — is shared.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { setStagedSend, loadStagedSend, clearStagedSend } from './chatPaneRecovery'

const CLAIM = 'mc-chat-pane-claim:'
const SLOT = 'slot-two-tabs'
const SEND = 's-staged-by-a'

/** Claim heartbeats currently held, so a test can age one out and model a context that is gone. */
const claimKeys = (): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(CLAIM)) out.push(k)
  }
  return out
}

describe('the staged-send marker is visible wherever the draft it describes is', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })

  it('shows tab B the warning tab A staged, because the draft is shared', () => {
    setStagedSend(SLOT, SEND)
    expect(loadStagedSend(SLOT), 'premise: the staging tab sees its own marker').toBe(SEND)

    sessionStorage.clear()

    expect(loadStagedSend(SLOT),
      'tab B restores the same slot-scoped draft, so it must see the warning').toBe(SEND)
  })

  it('does not report a marker staged for a DIFFERENT slot', () => {
    // Mutation control: a reader that returned any marker in the store would satisfy the test
    // above while warning on every slot, so this must fail for that implementation.
    setStagedSend('slot-other', 's-elsewhere')
    sessionStorage.clear()

    expect(loadStagedSend(SLOT)).toBeUndefined()
    expect(loadStagedSend('slot-other'), 'and the right slot still reports').toBe('s-elsewhere')
  })

  it('leaves a LIVE sibling tab\u2019s marker alone when this tab dismisses', () => {
    setStagedSend(SLOT, SEND)
    expect(claimKeys().length, 'premise: the staging tab holds a claim').toBeGreaterThan(0)

    sessionStorage.clear()
    clearStagedSend(SLOT)

    expect(loadStagedSend(SLOT),
      'A still confirms its claim, so B dismissing must not retire A\u2019s warning').toBe(SEND)
  })

  it('retires a marker whose owning context is GONE', () => {
    setStagedSend(SLOT, SEND)
    sessionStorage.clear()
    // The staging context's heartbeat ages past CLAIM_LIVE_MS: nothing is left to settle its
    // marker, so a tab that can now SEE the warning must be able to dismiss it.
    for (const k of claimKeys()) localStorage.setItem(k, String(Date.now() - 600_000))

    clearStagedSend(SLOT)

    expect(loadStagedSend(SLOT)).toBeUndefined()
  })
})
