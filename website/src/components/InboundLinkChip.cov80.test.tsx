import { screen, fireEvent, waitFor } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../test/helpers'
import InboundLinkChip from './InboundLinkChip'
import { sseSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import type { ChatSlot, SessionLink } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, unlinkMirror: vi.fn() } }
})

const unlinkMirror = vi.mocked(api.unlinkMirror)

function link(over: Partial<SessionLink> = {}): SessionLink {
  return { channel: 'slack', label: 'zzq-chan', target: 'C1', direction: 'both', live: true, ...over }
}

function slot(links: SessionLink[]): ChatSlot {
  return { key: 'zzq-slot', messages: 0, running: false, links } as ChatSlot
}

function storeWith(links: SessionLink[]) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot(links)]))
  return store
}

describe('InboundLinkChip', () => {
  beforeEach(() => {
    unlinkMirror.mockReset()
    unlinkMirror.mockResolvedValue(undefined as never)
  })

  it('renders nothing without a slotKey', () => {
    const { container } = renderWithProviders(<InboundLinkChip />, { store: storeWith([link()]) })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for origin-only and one-way out links', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith([link({ direction: 'origin' }), link({ direction: 'out' })]),
    })
    expect(container.firstChild).toBeNull()
  })

  it('renders the chip for a two-way link', () => {
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store: storeWith([link()]) })
    expect(screen.getByText(/zzq-chan/)).toBeInTheDocument()
    expect(screen.getByRole('button')).toBeEnabled()
  })

  it('a declined confirm leaves the link alone', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store: storeWith([link()]) })
    fireEvent.click(screen.getByRole('button'))
    expect(confirm).toHaveBeenCalled()
    expect(unlinkMirror).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('a confirmed release calls the API, drops the link and notifies', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const store = storeWith([link(), link({ direction: 'origin', label: 'zzq-keep' })])
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store })

    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith('zzq-slot'))

    await waitFor(() => {
      const links = store.getState().dashboard.slots[0].links ?? []
      expect(links.map(l => l.direction)).toEqual(['origin'])
    })
    const notes = store.getState().notifications.items ?? []
    expect(notes.some(n => n.kind === 'success')).toBe(true)
  })

  it('a failed release notifies with the error reason and keeps the link', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    unlinkMirror.mockRejectedValue(new Error('zzq-unlink-broke'))
    const store = storeWith([link()])
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store })

    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => {
      const notes = store.getState().notifications.items ?? []
      expect(notes.some(n => n.kind === 'error' && n.title.includes('zzq-unlink-broke'))).toBe(true)
    })
    expect(store.getState().dashboard.slots[0].links).toHaveLength(1)
  })
})
