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
import { normalizeInstalledApp } from '../components/appstore/types'
import type { InstalledApp } from '../components/appstore/types'

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

  it('a store row with a string `highlights` and broken `mcpServers` entries still renders', async () => {
    // app.json is third-party: the backend copies `highlights` and checks only
    // that `mcpServers` is an object, so each value can be any JSON.
    getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    listRegistry.mockResolvedValue(envelope([{
      name: 'odd-row',
      displayName: 'Odd Row',
      description: 'An app with odd fields.',
      installed: false,
      origin: 'registry',
      highlights: 'Fast and simple',
      manifest: { mcpServers: { broken: null, text: 'nope', ok: { command: 'odd-mcp', autoApprove: '*' } } },
    }]))
    renderDetail('odd-row')

    await waitFor(() => expect(heroTitle()).toBe('Odd Row'))
    expect(screen.getByText('odd-mcp')).toBeInTheDocument()
    expect(screen.queryByText('broken')).not.toBeInTheDocument()
    expect(screen.queryByText('Fast and simple')).not.toBeInTheDocument()
  })

  it("a store row's raw permissions show only what the parser would grant", async () => {
    // The parser grants a capability only on a literal `true` and a list only
    // from a real list; the raw store row said "false" and gave `api` a string.
    getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    listRegistry.mockResolvedValue(envelope([{
      name: 'perm-row',
      displayName: 'Perm Row',
      description: 'An app with odd permissions.',
      installed: false,
      origin: 'registry',
      manifest: { permissions: { storage: 'false', network: true, api: '/api/chat' } },
    }]))
    renderDetail('perm-row')

    await waitFor(() => expect(heroTitle()).toBe('Perm Row'))
    expect(screen.getByText('Network: yes')).toBeInTheDocument()
    expect(screen.queryByText('Storage: yes')).not.toBeInTheDocument()
    expect(screen.queryByText('/api/chat')).not.toBeInTheDocument()
  })

  it('an installed app with broken `mcpServers` entries still renders its valid one', async () => {
    listRegistry.mockResolvedValue(envelope([]))
    // `api.getApp` normalizes in the client, so the mock does what it does.
    getApp.mockResolvedValue(normalizeInstalledApp({
      name: 'odd-installed', displayName: 'Odd Installed', version: '1.0.0', enabled: true,
      origin: 'local', installed: true,
      manifest: {
        displayName: 'Odd Installed', description: 'Installed with odd servers.', version: '1.0.0',
        mcpServers: { broken: null, ok: { url: 'http://127.0.0.1:9/mcp', autoApprove: ['read', 7] } },
      },
    } as unknown as InstalledApp))
    renderDetail('odd-installed')

    await waitFor(() => expect(heroTitle()).toBe('Odd Installed'))
    expect(screen.getByText('http://127.0.0.1:9/mcp')).toBeInTheDocument()
    expect(screen.getByText('read')).toBeInTheDocument()
    expect(screen.queryByText('broken')).not.toBeInTheDocument()
  })
})
