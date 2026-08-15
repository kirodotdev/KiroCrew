/**
 * SessionArchive empty state — the compaction hint chip must render as a REAL
 * navigable Link (ui mode) because `session.autocompact_pct` is registered in
 * the real generated SETTINGS_REGISTRY. Deliberately does NOT mock the
 * registry: this is the PR's live in-product example of issue #1870's
 * "setting available in the UI -> hyperlink to the settings screen" clause,
 * and a registry rename must fail THIS test, not silently downgrade the chip
 * to a popover.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SessionArchive from './SessionArchive'

// Archive list fetch: empty -> the empty state (and its hint) renders. The same
// stub answers SettingRef's /api/config/schema fetch; the chip resolves ui-mode
// from the registry before the schema query settles, so the shape is irrelevant.
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ archives: [] }),
  })) as unknown as typeof fetch)
})

describe('SessionArchive empty state', () => {
  it('renders the auto-compaction hint chip as a Link to the Chat settings tab with a highlight param', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SessionArchive />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => {
      const link = container.querySelector('a')
      expect(link, 'expected the settingRef chip to render as an anchor (ui mode)').not.toBeNull()
      const href = link!.getAttribute('href') ?? ''
      expect(href).toContain('/settings')
      expect(href).toContain('tab=chat')
      expect(href).toMatch(/highlight=key(%3A|:)session\.autocompact_pct/)
      expect(link!.textContent).toContain('session.autocompact_pct')
    })
  })
})

describe('SessionArchive filter row', () => {
  it('lets the filter input shrink so a narrow pane never clips the Reload button', async () => {
    // The row is one flex line: filter input · Reload button, inside a `w-1/3`
    // `overflow-hidden` pane. An <input> defaults to `min-width: auto` (~20
    // characters), so without an explicit min-w-0 the input refuses to yield and
    // the ROW overflows instead. The pane is overflow-hidden, so that overflow is
    // not scrollable — it clips whatever trails the input, and the Reload button
    // ends up entirely outside the pane's clip box below roughly an 800px
    // viewport. happy-dom has no layout engine, so the contract is pinned on the
    // class here; the geometric proof is in the capture harness.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SessionArchive />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const input = await screen.findByRole('textbox')
    expect(input.className).toContain('flex-1')
    expect(input.className).toContain('min-w-0')
    // The trailing control this protects must still be in the row.
    expect(input.parentElement?.querySelector('button')).not.toBeNull()
  })
})
