import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import RemoteArtifactCard from './RemoteArtifactCard'
import { api } from '../api/client'
import type { RemoteArtifact } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, forkRemoteArtifact: vi.fn(), cloneRemoteArtifact: vi.fn() },
  }
})

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => {
  const mod = await importOriginal<typeof import('react-router-dom')>()
  return { ...mod, useNavigate: () => navigate }
})

const forkRemoteArtifact = vi.mocked(api.forkRemoteArtifact)
const cloneRemoteArtifact = vi.mocked(api.cloneRemoteArtifact)

function artifact(over: Partial<RemoteArtifact> = {}): RemoteArtifact {
  return { external_id: 'zzq/ext-1', title: 'zzq-title', ...over } as RemoteArtifact
}

function mount(over: Partial<React.ComponentProps<typeof RemoteArtifactCard>> = {}) {
  const onForked = vi.fn()
  const onCloned = vi.fn()
  const view = renderWithProviders(
    <RemoteArtifactCard
      artifact={artifact()}
      provider="zzq-prov"
      onForked={onForked}
      onCloned={onCloned}
      {...over}
    />,
  )
  return { onForked, onCloned, ...view }
}

const row = () => screen.getAllByRole('button')[0]

describe('RemoteArtifactCard', () => {
  beforeEach(() => {
    navigate.mockReset()
    forkRemoteArtifact.mockReset()
    cloneRemoteArtifact.mockReset()
    forkRemoteArtifact.mockResolvedValue({ slug: 'zzq-forked' } as never)
    cloneRemoteArtifact.mockResolvedValue({ slug: 'zzq-cloned' } as never)
  })

  it('percent-encodes both provider and id when opening the viewer', () => {
    mount()
    fireEvent.click(row())
    expect(navigate).toHaveBeenCalledWith('/artifacts/remote/zzq-prov/zzq%2Fext-1')
  })

  it('Enter and Space on the row itself open the viewer', () => {
    mount()
    fireEvent.keyDown(row(), { key: 'Enter' })
    fireEvent.keyDown(row(), { key: ' ' })
    expect(navigate).toHaveBeenCalledTimes(2)
  })

  it('other keys, and keys bubbling from inner buttons, do not open the viewer', () => {
    mount()
    fireEvent.keyDown(row(), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByTitle(/^Fork into/), { key: 'Enter' })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('names the provider label in the row tooltip, falling back to the id', () => {
    const { unmount } = mount({ providerLabel: 'Zzq Registry' })
    expect(row().getAttribute('title')).toContain('Zzq Registry')
    unmount()
    mount()
    expect(row().getAttribute('title')).toContain('zzq-prov')
  })

  it('renders visibility, tags, owner, version and a snippet when present', () => {
    mount({
      artifact: artifact({
        visibility: 'public',
        tags: ['zzq-tag'],
        owner: 'zzq-owner',
        current_version: 4,
        snippet: 'zzq-snippet',
        updated_at: '2024-01-02T03:04:05Z',
      }),
    })
    expect(screen.getByText('public')).toBeInTheDocument()
    expect(screen.getByText('zzq-tag')).toBeInTheDocument()
    expect(screen.getByText('zzq-owner')).toBeInTheDocument()
    expect(screen.getByText('zzq-snippet')).toBeInTheDocument()
  })

  it('reads a millisecond epoch as ms, not as a far-future seconds epoch', () => {
    const msNow = String(Date.now())
    mount({ artifact: artifact({ updated_at: msNow }) })
    // A ms value read as seconds would land in the year ~55000 and never say "ago".
    expect(screen.getByText(/ago|now/i)).toBeInTheDocument()
  })

  it('drops an unparseable timestamp instead of rendering a bogus age', () => {
    mount({ artifact: artifact({ updated_at: 'zzq-not-a-date' }) })
    expect(screen.queryByText(/ago/i)).not.toBeInTheDocument()
  })

  it('Fork reports the new slug upward and never opens the viewer', async () => {
    const { onForked } = mount()
    fireEvent.click(screen.getByTitle(/^Fork into/))
    await waitFor(() =>
      expect(forkRemoteArtifact).toHaveBeenCalledWith('zzq-prov', 'zzq/ext-1'))
    await waitFor(() => expect(onForked).toHaveBeenCalledWith('zzq-forked'))
    expect(navigate).not.toHaveBeenCalled()
  })

  it('a fork error from the body is shown and nothing is reported upward', async () => {
    forkRemoteArtifact.mockResolvedValue({ error: 'zzq-fork-refused' } as never)
    const { onForked } = mount()
    fireEvent.click(screen.getByTitle(/^Fork into/))
    expect(await screen.findByText('zzq-fork-refused')).toBeInTheDocument()
    expect(onForked).not.toHaveBeenCalled()
  })

  it('a thrown fork failure shows its message, and a non-Error the fallback', async () => {
    forkRemoteArtifact.mockRejectedValue(new Error('zzq-fork-threw'))
    const { unmount } = mount()
    fireEvent.click(screen.getByTitle(/^Fork into/))
    expect(await screen.findByText('zzq-fork-threw')).toBeInTheDocument()
    unmount()

    forkRemoteArtifact.mockRejectedValue('zzq-not-an-error')
    mount()
    fireEvent.click(screen.getByTitle(/^Fork into/))
    expect(await screen.findByText('Fork failed')).toBeInTheDocument()
  })

  it('Clone is offered only for an editable artifact', () => {
    const { unmount } = mount()
    expect(screen.queryByTitle(/^Clone into/)).not.toBeInTheDocument()
    unmount()
    mount({ artifact: artifact({ editable: true }) })
    expect(screen.getByTitle(/^Clone into/)).toBeInTheDocument()
  })

  it('Clone reports the new slug upward', async () => {
    const { onCloned } = mount({ artifact: artifact({ editable: true }) })
    fireEvent.click(screen.getByTitle(/^Clone into/))
    await waitFor(() =>
      expect(cloneRemoteArtifact).toHaveBeenCalledWith('zzq-prov', 'zzq/ext-1'))
    await waitFor(() => expect(onCloned).toHaveBeenCalledWith('zzq-cloned'))
    expect(navigate).not.toHaveBeenCalled()
  })

  it('a clone error from the body is shown, and a throw falls back', async () => {
    cloneRemoteArtifact.mockResolvedValue({ error: 'zzq-clone-refused' } as never)
    const { onCloned, unmount } = mount({ artifact: artifact({ editable: true }) })
    fireEvent.click(screen.getByTitle(/^Clone into/))
    expect(await screen.findByText('zzq-clone-refused')).toBeInTheDocument()
    expect(onCloned).not.toHaveBeenCalled()
    unmount()

    cloneRemoteArtifact.mockRejectedValue('zzq-not-an-error')
    mount({ artifact: artifact({ editable: true }) })
    fireEvent.click(screen.getByTitle(/^Clone into/))
    expect(await screen.findByText('Clone failed')).toBeInTheDocument()
  })

  it('both actions are disabled while the row may be stale', () => {
    mount({ artifact: artifact({ editable: true }), actionsDisabled: true })
    expect(screen.getByTitle(/^Clone into/)).toBeDisabled()
    expect(screen.getByTitle(/^Fork into/)).toBeDisabled()
  })
})
