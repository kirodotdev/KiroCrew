/**
 * The heading rail on the artifact page (#26608).
 *
 * A markdown FILE gets a rail of heading ticks on its right edge; a markdown
 * ARTIFACT on its full page got none. The page now passes `headingRail` to
 * the native body, which mounts the same rail over the body's own scroller.
 * The rail shows for two or more headings, reads only the document's headings
 * (not the page's "Activity" heading), hides while editing, and stays off for
 * callers that do not ask for it (the chat side panel).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createRef } from 'react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { ArtifactBodyNative } from '../components/ArtifactBody'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const SPEC = '# Spec\n\nIntro.\n\n## Goals\n\nSome goals.\n\n## Design\n\nThe design.'

const mkArtifact = (o: Partial<Artifact> = {}): Artifact => ({
  slug: 'spec', name: 'Spec', kind: 'markdown', source: 'chat', description: '',
  tags: [], version: 1, created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00', content: SPEC, ...o,
})

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/spec' },
  )
}

const rail = () => screen.queryByRole('navigation', { name: 'Table of contents' })

describe('ArtifactDetailPage heading rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'spec', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'spec', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('shows one tick per document heading on a markdown artifact', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    renderPage()
    const nav = await waitFor(() => {
      const el = rail()
      expect(el).not.toBeNull()
      return el!
    })
    const ticks = within(nav).getAllByRole('button')
    expect(ticks.map(t => t.getAttribute('aria-label'))).toEqual(['Spec', 'Goals', 'Design'])
    // The page's own "Activity" heading is not part of the document.
    expect(screen.getByRole('heading', { name: 'Activity' })).toBeInTheDocument()
    expect(within(nav).queryByRole('button', { name: 'Activity' })).toBeNull()
  })

  it('ticks are keyboard buttons that jump to their heading', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    renderPage()
    const tick = await screen.findByRole('button', { name: 'Design' })
    expect(tick.tagName).toBe('BUTTON')
    expect(tick.tabIndex).toBe(0)
    // Focus opens the labelled flyout, as hover does.
    act(() => { tick.focus() })
    expect(document.activeElement).toBe(tick)
    expect((rail()!.querySelector(':scope > div[aria-hidden]') as HTMLElement).className).toContain('opacity-100')
    fireEvent.click(tick)
    // The clicked tick becomes the lit (current) one.
    expect((tick.firstElementChild as HTMLElement).style.background).toBe('var(--accent)')
  })

  it('shows no rail for a document with fewer than two headings', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ content: '# Only one\n\nBody text.' }))
    renderPage()
    await screen.findByRole('heading', { name: 'Only one' })
    expect(rail()).toBeNull()
  })
})

describe('ArtifactBodyNative heading rail', () => {
  const base = { kind: 'markdown' as const, content: SPEC, onChange: vi.fn() }

  it('is off unless the caller asks for it (the chat side panel)', async () => {
    render(<ArtifactBodyNative {...base} editing={false} previewRef={createRef<HTMLDivElement>()} flush />)
    await screen.findByRole('heading', { name: 'Design' })
    expect(rail()).toBeNull()
  })

  it('hides while editing', () => {
    render(<ArtifactBodyNative {...base} editing previewRef={createRef<HTMLDivElement>()} headingRail />)
    expect(rail()).toBeNull()
  })

  it('stays off for a non-markdown body', () => {
    render(<ArtifactBodyNative {...base} kind="text" content={'# a\n# b'} editing={false} previewRef={createRef<HTMLDivElement>()} headingRail />)
    expect(rail()).toBeNull()
  })
})
