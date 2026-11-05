/**
 * A pending-confirmation row's age under clock skew: `created_at_epoch` comes
 * from the gateway's clock, the "now" it is subtracted from comes from the
 * browser's. A remote gateway whose clock runs ahead stamps a request in the
 * browser's future, and the row must read "0m ago" until the browser catches
 * up rather than a negative age counting toward zero.
 *
 * The browser's clock is pinned, so each skew below is an exact offset from a
 * fixed instant. The fake clock advances with real time (`shouldAdvanceTime`)
 * because the panel's query carries a `refetchInterval` and the render is
 * awaited: a frozen timer would stall `waitFor` instead of pinning `Date.now`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'

const NOW_MS = 1_700_000_000_000

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

/** Serve one pending entry stamped `skewSeconds` from the pinned browser clock.
 *  A positive value puts the stamp in the browser's future (gateway ahead). */
function servePending(skewSeconds: number) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.endsWith('/pending')) {
      return new Response(JSON.stringify({
        pending: [{
          id: 'p1',
          site_id: 'my-app',
          artifact_slug: 'my-app',
          local_dir: '',
          profile: 'default',
          ttl_hours: 72,
          scan_summary: 'clean',
          created_at_epoch: NOW_MS / 1000 + skewSeconds,
          override_scan_required: false,
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
}

/** The row's metadata line, the one carrying the age. */
async function metaLine(): Promise<string> {
  const el = await screen.findByText(/· Scan:/)
  return el.textContent ?? ''
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW_MS)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ArtifactDeployPage pending age under clock skew', () => {
  it('reads 0m ago when the gateway stamp is 31s in the browser future', async () => {
    // 31s is the smallest skew the unclamped Math.round reached -1 at, which is
    // the reproduction in the issue: anything past half a minute read "-1m ago".
    servePending(31)
    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(async () => {
      expect(await metaLine()).toMatch(/· 0m ago$/)
    })
  })

  it('still reads 0m ago under a ten-minute skew, never a negative age', async () => {
    servePending(600)
    render(<ArtifactDeployPage />, { wrapper })

    const line = await waitFor(async () => {
      const text = await metaLine()
      expect(text).toMatch(/m ago$/)
      return text
    })
    expect(line).toMatch(/· 0m ago$/)
    expect(line).not.toMatch(/-\d+m ago/)
  })

  it('control: a stamp seven minutes in the past still reads 7m ago', async () => {
    servePending(-420)
    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(async () => {
      expect(await metaLine()).toMatch(/· 7m ago$/)
    })
  })
})
