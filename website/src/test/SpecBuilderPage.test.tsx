import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Mock the heavy ChatEmbed (pulls in the full chat renderer) — the empty-state
// path under test never mounts it, but the lazy import graph should stay light.
vi.mock('../app-sdk/ChatEmbed', () => ({ default: () => <div data-testid="chat-embed" /> }))

import SpecBuilderPage from '../apps/spec-builder/SpecBuilderPage'

let queryClient: QueryClient

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SpecBuilderPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SpecBuilderPage', () => {
  it('renders the first-run empty state when there are no specs', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ specs: [] })),
    }))

    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Plan your next feature with a spec')).toBeInTheDocument()
    })
    expect(screen.getByText('Start your first spec')).toBeInTheDocument()
  })

  it('shows an error banner when the specs list request fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ error: 'boom' }),
    }))

    renderPage()

    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument()
    })
  })

  it('announces the error banner to assistive tech and labels its dismiss control', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ error: 'boom' }),
    }))

    renderPage()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveAttribute('aria-live', 'assertive')
    // Icon-only dismiss must carry an accessible name, not just a tooltip.
    expect(screen.getByRole('button', { name: 'Dismiss error' })).toBeInTheDocument()
  })
})

describe('SpecBuilder loading pattern (Issue Radar parity)', () => {
  it('shows a skeleton with an announced status while the first fetch is pending', async () => {
    // A never-resolving fetch keeps the page in its first-load state.
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(() => {})))
    renderPage()

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Loading specs…')
    // The empty state must NOT flash before the list resolves — that flash is
    // what the skeleton exists to prevent.
    expect(screen.queryByText('Plan your next feature with a spec')).toBeNull()
  })

  it('replaces the skeleton with the empty state once the list resolves empty', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ specs: [] })),
    }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Plan your next feature with a spec')).toBeInTheDocument()
    })
    expect(screen.queryByRole('status')).toBeNull()
  })
})

describe('SpecBuilder accessibility contract', () => {  const SPECS = [{ name: 'my-spec', phase: 'requirements', status: 'idle', running: false }]

  function stubSpecs() {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
      const body = String(url).includes('/specs/')
        ? { name: 'my-spec', phase: 'requirements', status: 'idle', running: false, working_dir: '/tmp/p', files: {} }
        : { specs: SPECS }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) })
    }))
  }

  it('exposes every icon-only control with an accessible name', async () => {
    stubSpecs()
    renderPage()

    await waitFor(() => expect(screen.getByText('my-spec')).toBeInTheDocument())

    // No button may reach the DOM without a discernible name — this is the
    // regression that the icon-button audit found across the whole app.
    for (const btn of screen.getAllByRole('button')) {
      const name = btn.getAttribute('aria-label') || btn.textContent?.trim() || ''
      expect(name.length, 'button without accessible name: ' + btn.outerHTML.slice(0, 120)).toBeGreaterThan(0)
    }
  })

  it('makes spec rows keyboard-operable rather than click-only', async () => {
    stubSpecs()
    renderPage()

    const row = await screen.findByRole('button', { name: /my-spec/ })
    // Clickable gives role=button + tabIndex — a bare clickable div would have
    // neither, which is what the audit flagged.
    expect(row).toHaveAttribute('tabindex', '0')
  })

  it('gives the resize splitter value semantics and keyboard operation', async () => {
    stubSpecs()
    renderPage()

    const row = await screen.findByRole('button', { name: /my-spec/ })
    row.click()

    const splitter = await screen.findByRole('separator', { name: 'Resize document panel' })
    expect(splitter).toHaveAttribute('aria-orientation', 'vertical')
    expect(splitter).toHaveAttribute('tabindex', '0')
    // Value semantics let a screen reader announce the current split.
    expect(Number(splitter.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(25)
    expect(Number(splitter.getAttribute('aria-valuemax'))).toBe(75)
  })
})
