/**
 * The two horizontally-scrolling tables on the Hooks page get deliberately
 * different keyboard treatment, and this pins both halves.
 *
 * The provider-hooks table is READ-ONLY: every cell is plain text, and its
 * columns reserve 700px, so at phone width ~412px of the Command column is
 * only reachable by scrolling. Measured in Chromium 145: a scroller with no
 * focusable children is ALREADY the first tab stop without any tabindex
 * (keyboard-focusable scrollers, Chromium >=130), so the explicit stop is
 * belt-and-braces for engines that have not shipped that — while role and
 * aria-label are load-bearing everywhere, since the auto-focused scroller would
 * otherwise land focus on an anonymous <div>.
 *
 * The editable hooks table above it is the case Chromium excludes: its rows hold
 * tabbable controls, so focus arrives via the control and the scroller is
 * skipped (also measured). A stop there would only insert an extra Tab press
 * between every row, so it is asserted to stay absent — the tempting
 * "make them consistent" change is a regression on that table.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: true },
    labels: { hooksSection: 'Provider hooks', configFile: '~/.kiro/config.json' },
    fetchProviderHooks: () =>
      Promise.resolve({
        PreToolUse: [
          { source: 'config', matcher: 'Bash', command: 'echo before-tool-use' },
        ],
      }),
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  hooksPayload = {
    hooks: [
      {
        id: 'h1',
        name: 'fmt',
        event: 'PreToolUse',
        command: 'black .',
        matcher: 'Bash',
        enabled: true,
        run_count: 2,
        last_status: 'ok',
      },
    ],
  }
})

describe('Hooks page scroll containers — keyboard reach', () => {
  it('makes the read-only provider-hooks scrollport a named, focusable region', async () => {
    renderPage()
    const region = await screen.findByRole('region', { name: 'Provider hooks' })
    // An explicit tab stop, for the engines that do not focus a childless
    // scroller on their own. Chromium already would; nothing else is known to.
    expect(region.tabIndex).toBe(0)
    // The stop has to be ON the element that actually scrolls, not a wrapper.
    expect(region.className).toContain('overflow-x-auto')
    // And it really is the provider table's container.
    expect(region.querySelector('table')).not.toBeNull()
  })

  it('contains nothing else focusable, which is why the container needs the stop', async () => {
    renderPage()
    const region = await screen.findByRole('region', { name: 'Provider hooks' })
    const focusable = region.querySelectorAll(
      'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    // Only the container itself carries a tabindex; nothing within it does.
    expect([...focusable].filter(el => el !== region)).toHaveLength(0)
  })

  it('leaves the editable hooks table container without a redundant tab stop', async () => {
    renderPage()
    // The editable table is the one with a sortable "Name" column; the
    // provider table has no Name header.
    await waitFor(() => expect(screen.getByText('Name')).toBeInTheDocument())
    const editable = [...document.querySelectorAll('table')].find(t =>
      [...t.querySelectorAll('th')].some(th => th.textContent?.trim() === 'Name'),
    )
    expect(editable).toBeDefined()
    const scroller = editable!.parentElement!
    expect(scroller.className).toContain('overflow-x-auto')
    // Its rows are already tabbable, so the container must NOT be a tab stop.
    expect(scroller.getAttribute('tabindex')).toBeNull()
    expect(scroller.getAttribute('role')).toBeNull()
    expect(
      scroller.querySelectorAll('button, input, [role="switch"]').length,
    ).toBeGreaterThan(0)
  })
})
