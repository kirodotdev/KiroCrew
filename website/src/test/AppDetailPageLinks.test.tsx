/**
 * The app's own `links` in the detail page's Details card: each valid entry is
 * an outbound button showing its label and its host, an unsafe entry from a
 * row that skipped the gateway's check never becomes an href, and an app with
 * no `links` renders no link buttons at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
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
    appContributors: () => Promise.resolve({ contributors: [] }),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'
import { usableAppLinks } from '../pages/AppDetailLinks'

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/detail/todo-ledger']}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const registryPayload = (links: unknown) => ({
  apps: [{
    name: 'todo-ledger',
    displayName: 'Todo Ledger',
    description: 'A third-party app.',
    version: '1.2.0',
    author: 'octocat',
    installed: false,
    origin: 'registry',
    repo: 'https://github.com/octocat/todo-ledger',
    ...(links === undefined ? {} : { links }),
  }],
  serverPlatform: { os: 'linux', arch: 'x86_64' },
})

describe('AppDetailPage — app links', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
    getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
  })

  it('renders each link as a new-tab button with its host under the label', async () => {
    listRegistry.mockResolvedValue(registryPayload([
      { label: 'Wiki', url: 'https://wiki.example.com/todo' },
      { label: 'Guide', url: 'https://example.com/guide' },
    ]))
    renderDetail()
    const wiki = (await screen.findByText('Wiki')).closest('a')!
    expect(wiki).toHaveAttribute('href', 'https://wiki.example.com/todo')
    expect(wiki).toHaveAttribute('target', '_blank')
    expect(wiki).toHaveAttribute('rel', 'noopener noreferrer')
    expect(wiki).toHaveTextContent('wiki.example.com')
    const guide = screen.getByText('Guide').closest('a')!
    expect(guide).toHaveTextContent('example.com')
    // Sits beside the existing Issues link in the same Details card.
    expect(screen.getByText('Report an issue').closest('a')).toHaveAttribute(
      'href', 'https://github.com/octocat/todo-ledger/issues',
    )
  })

  it('never turns an unsafe entry into a link', async () => {
    listRegistry.mockResolvedValue(registryPayload([
      { label: 'Script', url: 'javascript:alert(1)' },
      { label: 'Plain', url: 'http://example.com/' },
      { label: 'Creds', url: 'https://user@example.com/' },
      { label: 'Port', url: 'https://example.com:8443/' },
    ]))
    renderDetail()
    await screen.findAllByText('Todo Ledger')
    expect(screen.queryByTestId('app-detail-links')).toBeNull()
    for (const label of ['Script', 'Plain', 'Creds', 'Port']) expect(screen.queryByText(label)).toBeNull()
  })

  it('renders no link buttons when the app declares no links', async () => {
    listRegistry.mockResolvedValue(registryPayload(undefined))
    renderDetail()
    await screen.findAllByText('Todo Ledger')
    expect(screen.queryByTestId('app-detail-links')).toBeNull()
  })

  it('caps the list at six entries', () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ label: `L${i}`, url: `https://example.com/${i}` }))
    expect(usableAppLinks(many).map((l) => l.label)).toEqual(['L0', 'L1', 'L2', 'L3', 'L4', 'L5'])
  })
})
