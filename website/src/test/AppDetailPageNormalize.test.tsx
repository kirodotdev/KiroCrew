/**
 * AppDetailPage normalizes the `listRegistry()` payload once, where it fetches
 * it, with the same `normalizeRegistryApp` the browse list uses (#7440).
 *
 * Normalizing changes the input of EVERY branch, not just the registry-only
 * one, and `normalizeRegistryApp` fills a missing `displayName` with the slug.
 * `mergeBuiltinRow` is row-first, so a filled slug handed to it would beat the
 * built-in's manifest name and render the slug as the page title. These tests
 * pin that precedence for the display fields, and that the registry-only
 * branch renders a malformed row through the normalized fallbacks.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

// A slug with no i18n catalog entry, so the rendered title is the merged value
// rather than a translated first-party name.
const SLUG = 'zz-pin-builtin'

function renderDetail(name = SLUG) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/apps/detail/${name}`]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const heroTitle = () => document.querySelector('span.text-xl')?.textContent

const envelope = (apps: unknown[]) => ({ apps, serverPlatform: { os: 'linux', arch: 'x86_64' } })

describe('AppDetailPage — registry rows normalized at the fetch site', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
  })

  it('a built-in row without display fields still renders the manifest values, not the slug', async () => {
    // The catalog row carries no displayName/description/author: the manifest
    // must fill them. Normalized, the row would claim displayName === slug.
    listRegistry.mockResolvedValue(envelope([{ name: SLUG, origin: 'builtin', installed: true }]))
    getApp.mockResolvedValue({
      name: SLUG,
      version: '2.0.0',
      enabled: true,
      origin: 'builtin',
      installed: true,
      manifest: {
        displayName: 'Manifest Title',
        description: 'Manifest description text.',
        author: 'Manifest Author',
        version: '2.0.0',
      },
    })
    renderDetail()

    // The hero title is the page's one `text-xl` span.
    await waitFor(() => expect(heroTitle()).toBe('Manifest Title'))
    expect(screen.getAllByText('Manifest description text.').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Manifest Author/).length).toBeGreaterThan(0)
  })

  it('a registry-only row with wrong-typed display fields renders the normalized fallbacks', async () => {
    getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    listRegistry.mockResolvedValue(envelope([{
      name: 'minimal-row',
      displayName: 42,
      description: { not: 'a string' },
      version: null,
      author: ['x'],
      installed: false,
      origin: 'registry',
      repo: 'https://example.invalid/o/minimal-row',
    }]))
    renderDetail('minimal-row')

    // displayName falls back to the slug, and nothing non-string reaches the DOM.
    await waitFor(() => expect(heroTitle()).toBe('minimal-row'))
    expect(screen.queryByText('42')).not.toBeInTheDocument()
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
  })
})
