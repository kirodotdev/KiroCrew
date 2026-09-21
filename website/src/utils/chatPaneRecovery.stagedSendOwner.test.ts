/** The duplicate-send warning marker is keyed by SLOT alone, so two tabs on one slot share it: either
 *  tab's dismiss retired the OTHER tab's warning, and either tab's stage overwrote it.
 *
 *  The pane PAYLOAD already carries `tabId` ownership; this marker never did. Modelled the way the
 *  browser makes two tabs -- one shared localStorage, a sessionStorage per browsing context -- so
 *  swapping the owner entry is what makes this two tabs rather than two reloads of one.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { loadStagedSend, setStagedSend, clearStagedSend, PANE_RECOVERY_KEY } from './chatPaneRecovery'

const OWNER_KEY = `${PANE_RECOVERY_KEY}:tab`
const SLOT = 'slot-two-tab-warn'

/** Put the runtime in a named browsing context. The owner entry is SEEDED rather than left to lazy
 *  minting, so the id under test is deterministic and the same on both sides of the fix -- the
 *  module short-circuits when the stored `origin` matches this load's. */
const asTab = (id: string, origin: number, navType: 'navigate' | 'reload' = 'navigate') => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
    t === 'navigation' ? [{ type: navType }] : []) as typeof performance.getEntriesByType)
  sessionStorage.setItem(OWNER_KEY, JSON.stringify({ id, origin }))
}

describe('a staged-send warning is RETIRED only by the tab that staged it', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  it('survives ANOTHER tab clearing its own staged send', () => {
    asTab('tab-a', 1_000_000)
    setStagedSend(SLOT, 's-tab-a')
    expect(loadStagedSend(SLOT), 'premise: tab A reads its own marker').toBe('s-tab-a')

    // Tab B: its own sessionStorage, the SAME localStorage.
    asTab('tab-b', 2_000_000)
    expect(loadStagedSend(SLOT),
      'premise: B SEES A\u2019s warning, because the draft it describes is shared').toBe('s-tab-a')
    clearStagedSend(SLOT)

    // Back in tab A -- its sessionStorage was never B's.
    asTab('tab-a', 1_000_000, 'reload')

    expect(loadStagedSend(SLOT),
      'another tab\u2019s dismiss must not retire this tab\u2019s duplicate-send warning').toBe('s-tab-a')
  })

  it('is still retired by the OWNING tab\u2019s own clear', () => {
    // Positive control: refusing every clear would satisfy the assertion above and leave a warning
    // the user dismissed permanently armed.
    asTab('tab-a', 1_000_000)
    setStagedSend(SLOT, 's-own')
    clearStagedSend(SLOT)

    expect(loadStagedSend(SLOT)).toBeUndefined()
  })

  it('is still readable by the owning tab after a reload', () => {
    // Second positive control: keying on anything that dies with the page load would satisfy the
    // cross-tab assertion while losing the marker on the reload it exists to survive.
    asTab('tab-a', 1_000_000)
    setStagedSend(SLOT, 's-reload')

    asTab('tab-a', 3_000_000, 'reload')

    expect(loadStagedSend(SLOT)).toBe('s-reload')
  })
})
