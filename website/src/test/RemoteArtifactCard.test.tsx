/**
 * Regression tests for RemoteArtifactCard — the browse-surface row.
 *
 * Covers:
 *  1. Keyboard activation of the inner Fork/Clone buttons must NOT be
 *     hijacked by the row's Enter/Space handler (which navigates to the
 *     in-app remote-detail viewer).
 *  2. A millisecond-epoch updated_at string must render a sane relative age,
 *     not "now" forever (ms value misread as a far-future seconds epoch).
 *
 * The row's primary action NAVIGATES to the in-app read-only remote-detail
 * viewer (/artifacts/remote/:provider/:externalId) rather than opening the
 * provider's own view_url in a new tab — the "open original" affordance lives
 * on that detail page. useNavigate requires a Router, so renders are wrapped in
 * MemoryRouter and navigation is asserted via a mocked useNavigate.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import RemoteArtifactCard from '../components/RemoteArtifactCard'
import type { RemoteArtifact } from '../types'

const navigateMock = vi.fn()

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateMock }
})

vi.mock('../api/client', () => ({
  api: { forkRemoteArtifact: vi.fn(), cloneRemoteArtifact: vi.fn() },
}))

const mkRemote = (o: Partial<RemoteArtifact> = {}): RemoteArtifact => ({
  external_id: 'ext-1',
  title: 'Remote Widget',
  owner: 'someone',
  view_url: 'https://remote.example.com/a/ext-1',
  snippet: '',
  tags: [],
  local_slug: null,
  ...o,
})

const renderCard = (props: Parameters<typeof RemoteArtifactCard>[0]) =>
  render(
    <MemoryRouter>
      <RemoteArtifactCard {...props} />
    </MemoryRouter>
  )

describe('RemoteArtifactCard keyboard activation', () => {
  beforeEach(() => vi.clearAllMocks())

  it('Enter on the Fork button forks — does not navigate to the viewer', async () => {
    const { api } = await import('../api/client')
    ;(api.forkRemoteArtifact as ReturnType<typeof vi.fn>).mockResolvedValue({ slug: 'local-x' })
    const onForked = vi.fn()
    renderCard({ artifact: mkRemote(), provider: 'companion', onForked })

    const forkBtn = screen.getByTitle(/Fork into your local artifacts/i)
    // React attaches keydown at the root; a keydown on the button bubbles to
    // the row. The row must ignore it (target !== currentTarget).
    fireEvent.keyDown(forkBtn, { key: 'Enter' })
    // Native button click still fires on real Enter; simulate the activation.
    fireEvent.click(forkBtn)

    await waitFor(() => expect(api.forkRemoteArtifact).toHaveBeenCalledWith('companion', 'ext-1'))
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('Enter on the row itself navigates to the in-app remote-detail viewer', () => {
    renderCard({ artifact: mkRemote(), provider: 'companion' })
    const row = screen.getByTitle(/Open read-only viewer/i)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(navigateMock).toHaveBeenCalledWith('/artifacts/remote/companion/ext-1')
  })

  it('percent-encodes provider + external_id in the viewer route', () => {
    renderCard({ artifact: mkRemote({ external_id: 'a/b c' }), provider: 'a co' })
    const row = screen.getByTitle(/Open read-only viewer/i)
    fireEvent.click(row)
    expect(navigateMock).toHaveBeenCalledWith('/artifacts/remote/a%20co/a%2Fb%20c')
  })
})

describe('RemoteArtifactCard actionsDisabled', () => {
  beforeEach(() => vi.clearAllMocks())

  it('disables Fork/Clone and does not fork when actionsDisabled (stale rows)', async () => {
    const { api } = await import('../api/client')
    renderCard({ artifact: mkRemote({ editable: true }), provider: 'companion', actionsDisabled: true })
    const forkBtn = screen.getByTitle(/Fork into your local artifacts/i) as HTMLButtonElement
    const cloneBtn = screen.getByTitle(/Clone into your artifacts/i) as HTMLButtonElement
    expect(forkBtn).toBeDisabled()
    expect(cloneBtn).toBeDisabled()
    // A click on a disabled action must not fire the API (stale-row guard).
    fireEvent.click(forkBtn)
    expect(api.forkRemoteArtifact).not.toHaveBeenCalled()
  })
})

describe('RemoteArtifactCard updated_at rendering', () => {
  it('renders a millisecond-epoch updated_at as a real age, not "now"', () => {
    // 2020-01-01T00:00:00Z in MILLISECONDS — years in the past.
    renderCard({ artifact: mkRemote({ updated_at: '1577836800000' }), provider: 'companion' })
    // Should read as days/years ago, never "now" (which is the bug: a ms
    // value misread as seconds lands in the far future → negative age).
    expect(screen.queryByText('now')).not.toBeInTheDocument()
    expect(screen.getByText(/\d+(s|m|h|d|mo|y) ago/)).toBeInTheDocument()
  })

  it('still renders a seconds-epoch updated_at correctly', () => {
    // 2020-01-01T00:00:00Z in SECONDS.
    renderCard({ artifact: mkRemote({ updated_at: '1577836800' }), provider: 'companion' })
    expect(screen.queryByText('now')).not.toBeInTheDocument()
    expect(screen.getByText(/\d+(s|m|h|d|mo|y) ago/)).toBeInTheDocument()
  })
})
