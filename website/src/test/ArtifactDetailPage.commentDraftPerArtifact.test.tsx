/**
 * A comment the sidebar holds unsent belongs to the artifact it was typed on.
 *
 * The route element is reused across artifacts, so with the next artifact
 * already cached the page skips its loading branch and the sidebar stays
 * mounted: before this was keyed per artifact, the first artifact's open add box
 * showed on the second, and Comment posted it there. Exits wired to the shell
 * ask first instead of dropping the text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation, useNavigationStakeNow } from '../components/NavigationLeaveGuard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactComment } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// The desktop layout, where a commented artifact opens its sidebar by itself.
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const ADD_BOX = 'Add a comment on the whole artifact…'

const mkArtifact = (slug: string): Artifact => ({
  slug, name: slug, kind: 'markdown', source: 'chat', description: '', tags: [], version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00', updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: `# ${slug}`,
})

const mkComment = (slug: string): ArtifactComment => ({
  id: `c-${slug}`, origin: 'local', scope: 'private', author: 'me', is_agent: false,
  body: `existing note on ${slug}`, thread_id: `t-${slug}`, status: 'open', sync_state: 'synced',
  created_at: '2026-05-21T22:00:00.000000+00:00', updated_at: '2026-05-21T22:00:00.000000+00:00',
})

let ask: () => boolean = () => true
// What the browser Back guard reads, before any gesture.
let stakeNow: () => boolean = () => false
function ShellProbe() {
  ask = useMayLeaveForNavigation()
  stakeNow = useNavigationStakeNow()
  return null
}

/** A plain in-app navigation that does not ask the shell first. */
function GoTo({ slug }: { slug: string }) {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(`/artifacts/${slug}`)}>go to {slug}</button>
}

describe('ArtifactDetailPage sidebar comment drafts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifact = vi.fn((slug: string) => Promise.resolve(mkArtifact(slug)))
    vi.mocked(api).artifactVersions = vi.fn().mockImplementation((slug: string) => Promise.resolve({ slug, versions: [1] }))
    vi.mocked(api).artifactEvents = vi.fn().mockImplementation((slug: string) => Promise.resolve({ slug, events: [] }))
    vi.mocked(api).artifactComments = vi.fn().mockImplementation((slug: string) => Promise.resolve({ comments: [mkComment(slug)] }))
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('never shows one artifact’s unsent comment on the next, and asks before a shell exit drops it', async () => {
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <ShellProbe />
        <GoTo slug="doc-a" />
        <GoTo slug="doc-b" />
        <Routes>
          <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        </Routes>
      </NavigationLeaveGuardProvider>,
      { route: '/artifacts/doc-b' },
    )
    // Visit B first, so it is cached when the user comes back to it.
    expect(await screen.findByText('existing note on doc-b')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'go to doc-a' }))
    expect(await screen.findByText('existing note on doc-a')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add comment/ }))
    fireEvent.change(screen.getByPlaceholderText(ADD_BOX), { target: { value: 'unsent on doc-a' } })

    fireEvent.click(screen.getByRole('button', { name: 'go to doc-b' }))
    expect(await screen.findByText('existing note on doc-b')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('unsent on doc-a')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Add comment/ }))
    expect(screen.getByPlaceholderText(ADD_BOX)).toHaveValue('')
    expect(stakeNow()).toBe(false)

    fireEvent.change(screen.getByPlaceholderText(ADD_BOX), { target: { value: 'unsent on doc-b' } })
    expect(stakeNow()).toBe(true)
    let answer = true
    act(() => { answer = ask() })
    expect(answer).toBe(false)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard your unsaved comment?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(screen.queryByPlaceholderText(ADD_BOX)).toBeNull())
    expect(ask()).toBe(true)
    // The stake drops on the panel's own report, one commit after the box closes.
    await waitFor(() => expect(stakeNow()).toBe(false))
  })
})
