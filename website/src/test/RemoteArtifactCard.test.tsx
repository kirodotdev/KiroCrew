/**
 * Regression tests for RemoteArtifactCard — the browse-surface row.
 *
 * Covers two fork fixes:
 *  1. Keyboard activation of the inner Fork/Clone buttons must NOT be
 *     hijacked by the row's Enter/Space handler (which opens the remote).
 *  2. A millisecond-epoch updated_at string must render a sane relative age,
 *     not "just now" forever (ms value misread as a far-future seconds epoch).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RemoteArtifactCard from '../components/RemoteArtifactCard'
import type { RemoteArtifact } from '../types'

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

describe('RemoteArtifactCard keyboard activation', () => {
  beforeEach(() => vi.clearAllMocks())

  it('Enter on the Fork button forks — does not open the remote view URL', async () => {
    const { api } = await import('../api/client')
    ;(api.forkRemoteArtifact as ReturnType<typeof vi.fn>).mockResolvedValue({ slug: 'local-x' })
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    const onForked = vi.fn()
    render(<RemoteArtifactCard artifact={mkRemote()} provider="companion" onForked={onForked} />)

    const forkBtn = screen.getByTitle(/Fork into your local artifacts/i)
    // React attaches keydown at the root; a keydown on the button bubbles to
    // the row. The row must ignore it (target !== currentTarget).
    fireEvent.keyDown(forkBtn, { key: 'Enter' })
    // Native button click still fires on real Enter; simulate the activation.
    fireEvent.click(forkBtn)

    await waitFor(() => expect(api.forkRemoteArtifact).toHaveBeenCalledWith('companion', 'ext-1'))
    expect(openSpy).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })

  it('Enter on the row itself opens the remote view URL', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    render(<RemoteArtifactCard artifact={mkRemote()} provider="companion" />)
    const row = screen.getByTitle(/Open on/i)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(openSpy).toHaveBeenCalledWith('https://remote.example.com/a/ext-1', '_blank', 'noopener,noreferrer')
    openSpy.mockRestore()
  })

  it('does NOT open a non-http(s) view URL (malicious provider scheme)', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    render(
      <RemoteArtifactCard
        artifact={mkRemote({ view_url: 'javascript:alert(1)' })}
        provider="companion"
      />
    )
    const row = screen.getByTitle(/Open on/i)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(openSpy).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })
})

describe('RemoteArtifactCard actionsDisabled', () => {
  beforeEach(() => vi.clearAllMocks())

  it('disables Fork/Clone and does not fork when actionsDisabled (stale rows)', async () => {
    const { api } = await import('../api/client')
    render(
      <RemoteArtifactCard
        artifact={mkRemote({ editable: true })}
        provider="companion"
        actionsDisabled
      />
    )
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
  it('renders a millisecond-epoch updated_at as a real age, not "just now"', () => {
    // 2020-01-01T00:00:00Z in MILLISECONDS — years in the past.
    render(<RemoteArtifactCard artifact={mkRemote({ updated_at: '1577836800000' })} provider="companion" />)
    // Should read as days/years ago, never "just now" (which is the bug: a ms
    // value misread as seconds lands in the far future → negative age).
    expect(screen.queryByText('just now')).not.toBeInTheDocument()
    expect(screen.getByText(/\d+d ago/)).toBeInTheDocument()
  })

  it('still renders a seconds-epoch updated_at correctly', () => {
    // 2020-01-01T00:00:00Z in SECONDS.
    render(<RemoteArtifactCard artifact={mkRemote({ updated_at: '1577836800' })} provider="companion" />)
    expect(screen.queryByText('just now')).not.toBeInTheDocument()
    expect(screen.getByText(/\d+d ago/)).toBeInTheDocument()
  })
})
