/**
 * Overview built-in suppression seam, at the render site.
 *
 * `extensionSeams.test.tsx` pins the registry's own behaviour. This file pins the
 * thing that actually matters to a downstream distribution: that
 * `suppressOverviewBuiltin('tailnet-mobile')` removes the card from the rendered
 * page, and that the stock build still renders it.
 *
 * Two things make these tests non-vacuous, and both were needed:
 *
 *  - **A positive control.** The "it is gone" assertion is worthless on its own,
 *    because the card renders nothing for many unrelated reasons — a missing
 *    `api.tailnetMobile` mock alone is enough, and its ErrorBoundary swallows the
 *    difference. `OverviewPage.test.tsx` is in exactly that state, which is why
 *    this lives in its own file: `TailnetMobileCard` is replaced by a sentinel
 *    that always renders, so its absence can only be the seam.
 *  - **Module isolation per case.** Suppression is deliberately one-way (see
 *    `overviewBuiltins.ts`), so the two cases cannot share a module registry: the
 *    suppressed case would leak into the stock case and the ORDER of the two
 *    tests would decide the result. Each case re-imports the registry and the
 *    page after `vi.resetModules()`, so the page under test reads the same fresh
 *    registry instance the test just wrote to.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import type { RootState } from '../store'

// The sentinel. Unlike the real card this never depends on a query, a daemon
// probe, or an owner check, so it is present unless the page chose not to render
// it — which is the only signal these tests are allowed to rely on.
vi.mock('../components/TailnetMobileCard', () => ({
  TailnetMobileCard: () => <div data-testid="tailnet-mobile-card" />,
}))

// The rest of the page's surfaces are irrelevant here and are stubbed so the
// shell mounts without network.
vi.mock('../pages/overview', () => ({
  MemoryTab: () => <div data-testid="memory-tab">MemoryTab</div>,
  UsageTab: () => <div data-testid="usage-tab">UsageTab</div>,
}))

vi.mock('../hooks/useUptime', () => ({
  useUptime: () => '2h 30m',
}))

vi.mock('../api/client', () => ({
  api: {
    memorySettings: vi.fn().mockResolvedValue({
      history_idle_hours: 3,
      history_max_days: 90,
      migrated: false,
    }),
  },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'test',
    displayName: 'Test Provider',
    capabilities: { usageBilling: false },
    fetchUsage: vi.fn().mockResolvedValue({}),
  }),
}))

function statusStore() {
  return createTestStore({
    dashboard: {
      status: {
        uptime: '2h',
        sessions: 3,
        messages: 42,
        cron_jobs: 1,
        subagents: 0,
        lessons: 5,
        version: '0.1.0',
      },
      connected: true,
      slots: [],
      refreshTrigger: 0,
    } as RootState['dashboard'],
  })
}

/** Mount Overview against a FRESH suppression registry. */
async function mountFresh(suppress: boolean) {
  vi.resetModules()
  const { suppressOverviewBuiltin } = await import('../pages/overviewBuiltins')
  if (suppress) suppressOverviewBuiltin('tailnet-mobile')
  const { default: OverviewPage } = await import('../pages/OverviewPage')
  return renderWithProviders(<OverviewPage />, { store: statusStore() })
}

describe('OverviewPage — tailnet-mobile suppression seam', () => {
  it('renders the phone-access card in the stock build, inside its spacing wrapper', async () => {
    await mountFresh(false)
    const card = screen.getByTestId('tailnet-mobile-card')
    expect(card).toBeInTheDocument()
    // Anchors the wrapper relationship the suppressed case asserts the absence
    // of. Without this, "no empty .mb-6" below could pass because the wrapper
    // never had that class in the first place.
    expect(card.closest('div.mb-6')).not.toBeNull()
  })

  it('renders no phone-access card once a distribution suppresses it', async () => {
    await mountFresh(true)
    expect(screen.queryByTestId('tailnet-mobile-card')).toBeNull()
  })

  it('leaves no empty spacing wrapper behind when suppressed', async () => {
    // The gate sits OUTSIDE the `mb-6` wrapper on purpose. Gating only the card
    // itself would leave a 24px margin where it used to be — a gap that reads as
    // a rendering fault on the page the seam exists to tidy up.
    //
    // Asserted as "no EMPTY .mb-6", not "no .mb-6": the class is shared with the
    // stat grid above, which is legitimately present and legitimately non-empty.
    const { container } = await mountFresh(true)
    expect(container.querySelector('div.mb-6:empty')).toBeNull()
  })

  it('still renders the rest of the page when suppressed', async () => {
    // Suppression must remove one card, not blank the surface around it.
    await mountFresh(true)
    expect(screen.getByText('Memory')).toBeInTheDocument()
  })
})
