/**
 * Persisted-history render under a relayed pane.
 *
 * A critique's history entry stores the logical `/api/file-raw` URL so it stays
 * portable across a direct and a relayed load. This mounts the real page from a
 * seeded history and checks the pair the persistence contract turns on: the
 * rendered thumbnail addresses the pane's capability prefix, while the saved
 * entry is left on its logical URL. (Call-site rationale: see
 * paneMediaSinks.relay.test.tsx.)
 *
 * The page's only outward seam is `./api`, mocked as in DesignCritiquePage's
 * coverage fixture; `fileUrl` stays real (a pure string builder). Fake timers
 * guard the page's poll loops, though a history-only mount starts none.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { initDashboardRuntime } from '../lib/dashboardRuntime'
import { HKEY } from '../apps/design-critique/constants'
import type { HistoryEntry } from '../apps/design-critique/types'

initDashboardRuntime({ pathname: '/instance-pane/K_cap01/' })
const LOGICAL = '/api/file-raw?path=%2Ftmp%2Fold.png'
const RELOCATED = '/instance-pane/K_cap01' + LOGICAL

vi.mock('../apps/design-critique/api', () => ({
  designCritiqueApi: {
    openSlot: vi.fn(), getSlot: vi.fn().mockResolvedValue({ running: false, messages: [] }),
    send: vi.fn(), deleteSlot: vi.fn().mockResolvedValue(undefined), uploadFiles: vi.fn(),
    discover: vi.fn(), render: vi.fn(), pollDiscover: vi.fn(), pollRender: vi.fn(), method: vi.fn(),
  },
  fileUrl: (p: string) => '/api/file-raw?path=' + encodeURIComponent(p),
}))

const DesignCritiquePage = (await import('../apps/design-critique/DesignCritiquePage')).default

function seededEntry(): HistoryEntry {
  return {
    id: 1,
    ts: Date.now(),
    slotKey: 'slot-old',
    screens: [{ step: 1, label: 'Dashboard', url: LOGICAL }],
    thumbUrl: LOGICAL,
    read: 'A tidy dashboard.',
    report: { overallRead: 'A tidy dashboard.', tally: { minor: 1 }, findings: [] },
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('DesignCritique history under a relayed pane', () => {
  it('renders the persisted thumbnail from the pane origin without rewriting the saved URL', () => {
    localStorage.setItem(HKEY, JSON.stringify([seededEntry()]))
    const { container } = render(<DesignCritiquePage />)

    const srcs = [...container.querySelectorAll('img')].map(i => i.getAttribute('src'))
    // The browser sink loads from the pane's capability prefix…
    expect(srcs).toContain(RELOCATED)
    // …and never the bare loopback path.
    expect(srcs).not.toContain(LOGICAL)

    // The stored entry is untouched: portable, logical, no capability baked in.
    const saved = localStorage.getItem(HKEY) || ''
    expect(saved).toContain(LOGICAL)
    expect(saved).not.toContain('/instance-pane/')
  })
})
