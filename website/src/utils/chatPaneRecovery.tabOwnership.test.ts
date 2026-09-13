/** two tabs on one slot share the recovery store, and the slot-wide
 *  newest-by-gen reader had no ownership identity — so a sibling tab adopted the other's parked
 *  send and its own settlement then retired it, losing the prompt unrecoverably.
 *
 *  A new TAB is modelled the way the browser makes one: sessionStorage is per browsing context so it
 *  starts empty, while localStorage (the record store) is shared. A RELOAD is the same tab, so its
 *  sessionStorage survives — that case must still recover, which is what makes the in-memory
 *  `TAB_ID` unusable as the owner here.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { setPaneRecoveryFor, loadPaneRecovery, loadPaneRecoveryById } from './chatPaneRecovery'

const A_SEND = 's-tab-a'
const A_PROMPT = 'the prompt tab A parked'

describe('a parked send belongs to the tab that parked it', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })

  it('does not hand tab A\u2019s parked send to a sibling tab on the same slot', () => {
    setPaneRecoveryFor('slot-shared', { text: A_PROMPT, files: ['/a.png'], sendId: A_SEND, gen: 1 })
    expect(loadPaneRecovery('slot-shared')?.text).toBe(A_PROMPT)

    // Tab B: a fresh browsing context, so a fresh sessionStorage over the SAME record store.
    sessionStorage.clear()

    expect(loadPaneRecovery('slot-shared'), 'tab B adopting this record is what retires it').toBeUndefined()
    expect(loadPaneRecoveryById('slot-shared', A_SEND)).toBeUndefined()
  })

  it('still recovers the tab\u2019s OWN park across a reload', () => {
    // Positive control. Owning by the in-memory per-page id would satisfy the test above while
    // destroying the reload recovery this store exists for.
    setPaneRecoveryFor('slot-reload', { text: 'survives F5', files: [], sendId: 's-reload', gen: 1 })
    expect(loadPaneRecovery('slot-reload')?.text).toBe('survives F5')
    expect(loadPaneRecoveryById('slot-reload', 's-reload')?.text).toBe('survives F5')
  })

  it('adopts a record carrying NO owner, so a pre-upgrade park is not stranded', () => {
    // Second positive control: refusing an unowned record would lose a real prompt, the same harm
    // class as the overwrite being fixed.
    const key = 'mc-chat-pane-recovery:pane:slot-legacy|s-old'
    localStorage.setItem(key, JSON.stringify({ v: { text: 'parked before the upgrade', files: [] }, ts: Date.now() }))
    expect(loadPaneRecovery('slot-legacy')?.text).toBe('parked before the upgrade')
  })
})
