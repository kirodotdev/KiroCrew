import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'widget',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: ['ops', 'cr'],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '<div>CR Queue widget body</div>',
  ...overrides,
})

function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

describe('ArtifactDetailPage — sticky header', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Well-formed blob: URI, not a bare 'blob:test' literal — see the note in
    // WidgetFrame.test.tsx's beforeEach for why a malformed mock value here
    // risks a deferred ECONNREFUSED crashing an unrelated shard.
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:http://localhost:6776/test')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('toolbar container has sticky positioning', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const backBtn = screen.getByRole('button', { name: /Back/ })
    // Walk up from the Back button to find the toolbar container with sticky class
    let el: HTMLElement | null = backBtn.parentElement
    let foundSticky = false
    while (el) {
      if (el.className && el.className.includes('sticky')) {
        foundSticky = true
        break
      }
      el = el.parentElement
    }
    expect(foundSticky).toBe(true)
  })

  it('Back button is NOT inside the scrollable area', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const backBtn = screen.getByRole('button', { name: /Back/ })
    // The Back button must not be nested inside an overflow-y-auto container
    const scrollParent = backBtn.closest('.overflow-y-auto')
    expect(scrollParent).toBeNull()
  })

  it('toolbar and scrollable content area are siblings', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const backBtn = screen.getByRole('button', { name: /Back/ })
    // Find the sticky toolbar container (ancestor of Back button with 'sticky')
    let toolbar: HTMLElement | null = backBtn.parentElement
    while (toolbar && !toolbar.className?.includes('sticky')) {
      toolbar = toolbar.parentElement
    }
    expect(toolbar).not.toBeNull()

    // The scrollable content area should be a sibling of the toolbar
    const parent = toolbar!.parentElement!
    const children = Array.from(parent.children)
    const scrollableChild = children.find(
      (child) => child.className?.includes('overflow-y-auto'),
    )
    expect(scrollableChild).toBeDefined()
    expect(children).toContain(toolbar)
    expect(children).toContain(scrollableChild)
    // They must be distinct (toolbar ≠ scrollable)
    expect(toolbar).not.toBe(scrollableChild)
  })
})
