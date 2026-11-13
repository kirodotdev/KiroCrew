import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'
import { api } from '../api/client'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import type { Artifact } from '../types'

vi.mock('../api/client')

/**
 * A static artifact published to the public web keeps its link in the AWS
 * account, not on the artifact. Its library entry reads the deploy's site
 * listing, so the link shows while the site exists and goes with it.
 */

const LIVE_URL = 'https://d111111abcdef8.cloudfront.net/'
const LINK_NAME = 'd111111abcdef8.cloudfront.net/'

const mk = (slug: string, name: string): Artifact => ({
  slug,
  name,
  kind: 'markdown',
  source: 'chat',
  pinned: false,
  description: '',
  content: '',
  tags: [],
  version: 1,
  created_at: '2026-11-01T10:00:00.000000+00:00',
  updated_at: '2026-11-01T10:00:00.000000+00:00',
})

function seed(artifacts: Artifact[], sites: { site_id: string; url: string }[]) {
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({ artifacts })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  m.artifact = vi.fn().mockImplementation((slug: string) =>
    Promise.resolve({ ...artifacts.find(a => a.slug === slug), content: '# Notes' }))
  m.themeBoot = vi.fn().mockResolvedValue({})
  m.publishProviders = vi.fn().mockResolvedValue({
    providers: [{ id: 'deploy-web-aws', endpoint: '/api/deploy/deploy' }],
  })
  m.deploySites = vi.fn().mockResolvedValue({ configured: true, sites })
  return m
}

/** Where the dashboard's router is, so a click that opened the artifact shows. */
function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

/** jsdom cannot follow a link; keep it from trying, without hiding the click. */
const noNavigation = (e: MouseEvent) => e.preventDefault()

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
  // Nothing on the page under test reaches the network itself; a stray bare
  // fetch answers not-ok rather than leaving the test environment.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  document.addEventListener('click', noNavigation, true)
})

afterEach(() => {
  document.removeEventListener('click', noNavigation, true)
  vi.unstubAllGlobals()
  localStorage.clear()
})

// The first wait covers a chain: the artifact list loads, then each entry and the
// notice mount and read the site listing. Bounded by name so a starved runner
// says which chain ran out.
const LISTING_CHAIN_MS = 5000

describe('ArtifactsPage — live public-web link', () => {
  it.each(['grid', 'table'])('links a deployed artifact from its %s entry until the site is gone', async (view) => {
    localStorage.setItem('mc-artifacts-view', view)
    const m = seed([mk('launch-notes', 'Launch Notes')], [{ site_id: 'launch-notes', url: LIVE_URL }])
    const { queryClient } = renderWithProviders(<><ArtifactsPage /><Where /></>, { route: '/artifacts' })
    const link = await screen.findByRole('link', { name: LINK_NAME }, namedCeiling('ARTIFACTS_THEN_SITE_LISTING', LISTING_CHAIN_MS))
    expect(link).toHaveAttribute('href', LIVE_URL)
    expect(link).toHaveAttribute('target', '_blank')

    // Following the link leaves the dashboard where it is.
    fireEvent.click(link)
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/artifacts$/)

    // Destroying the site on the Artifact Deploy page refreshes this same listing.
    m.deploySites.mockResolvedValue({ configured: true, sites: [] })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    })
    await waitFor(() => expect(screen.queryByRole('link', { name: LINK_NAME })).toBeNull())
    expect(screen.getByText('Launch Notes')).toBeInTheDocument()
  })

  it('links no listed URL that is not plain http(s)', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed([mk('launch-notes', 'Launch Notes'), mk('tampered-page', 'Tampered Page')], [
      { site_id: 'launch-notes', url: LIVE_URL },
      { site_id: 'tampered-page', url: 'javascript:alert(1)' },
    ])
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    // The safe site's link proves the listing has rendered.
    await screen.findByRole('link', { name: LINK_NAME }, namedCeiling('ARTIFACTS_THEN_SITE_LISTING', LISTING_CHAIN_MS))
    expect(screen.queryByRole('link', { name: /alert/ })).toBeNull()
  })

  it('names a listing that could not read an account once, and Retry reads it again', async () => {
    const m = seed([mk('launch-notes', 'Launch Notes'), mk('draft', 'Draft')], [{ site_id: 'launch-notes', url: LIVE_URL }])
    // A profile whose credentials expired: HTTP 200 with the reason beside the sites it could read.
    m.deploySites = vi.fn().mockResolvedValue({ configured: true, sites: [{ site_id: 'launch-notes', url: LIVE_URL }], profile_errors: ['prod: ExpiredToken'] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    const title = await screen.findByText('Could not check which artifacts are on the public web', {}, namedCeiling('ARTIFACTS_THEN_SITE_LISTING_NOTICE', LISTING_CHAIN_MS))
    const notice = title.closest('.mb-4') as HTMLElement
    expect(within(notice).getByText(/prod: ExpiredToken/)).toBeInTheDocument()
    // The entries stay usable, and the link the listing did read still shows.
    expect(screen.getByRole('link', { name: LINK_NAME })).toBeInTheDocument()
    expect(screen.getByText('Draft')).toBeInTheDocument()

    m.deploySites.mockResolvedValue({ configured: true, sites: [] })
    fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText('Could not check which artifacts are on the public web')).toBeNull())
    expect(m.deploySites).toHaveBeenCalledTimes(2)
  })

  it('names a listing request that failed outright', async () => {
    const m = seed([mk('launch-notes', 'Launch Notes')], [])
    m.deploySites = vi.fn().mockRejectedValue(new Error('gateway unreachable'))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    expect(await screen.findByText('Could not check which artifacts are on the public web', {}, namedCeiling('ARTIFACTS_THEN_SITE_LISTING_NOTICE', LISTING_CHAIN_MS))).toBeInTheDocument()
    expect(screen.getByText(/gateway unreachable/)).toBeInTheDocument()
    expect(screen.getByText('Launch Notes')).toBeInTheDocument()
  })

  it('shows no notice on a host where deploy cannot run', async () => {
    const m = seed([mk('launch-notes', 'Launch Notes')], [])
    // Native Windows: HTTP 200, nothing configured, the reason in `error`.
    m.deploySites = vi.fn().mockResolvedValue({ configured: false, sites: [], error: 'deploy features are not supported on Windows' })
    const { queryClient } = renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    // Settled, not just asked: the answer has reached the notice before it is checked.
    await waitFor(
      () => expect(queryClient.getQueryState(['deploy-web', 'sites'])?.status).toBe('success'),
      namedCeiling('ARTIFACTS_THEN_SITE_LISTING', LISTING_CHAIN_MS),
    )
    expect(screen.getByText('Launch Notes')).toBeInTheDocument()
    expect(screen.queryByText('Could not check which artifacts are on the public web')).toBeNull()
  })

  it('reads no site listing while the Artifact Deploy preview is off', async () => {
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const m = seed([mk('launch-notes', 'Launch Notes')], [{ site_id: 'launch-notes', url: LIVE_URL }])
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(m.artifact).toHaveBeenCalledWith('launch-notes'), namedCeiling('ARTIFACTS_THEN_DETAIL', LISTING_CHAIN_MS))
    expect(m.deploySites).not.toHaveBeenCalled()
    expect(screen.queryByRole('link', { name: LINK_NAME })).toBeNull()
  })
})
