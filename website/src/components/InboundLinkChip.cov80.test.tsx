// InboundLinkChip — an INFORMATION-ONLY header chip for a session driven from
// another channel. The Release button was deliberately removed (connecting and
// disconnecting live in the session menu's one row per channel), so these tests
// assert the chip carries no interactive elements at all.
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../test/helpers'
import InboundLinkChip from './InboundLinkChip'
import { sseSlots } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import type { ChatSlot, SessionLink } from '../types'

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
  it('renders nothing without a slotKey', () => {
    const { container } = renderWithProviders(<InboundLinkChip />, { store: storeWith([link()]) })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for an unknown slot key', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-missing" />, {
      store: storeWith([link()]),
    })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for origin-only and one-way out links', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith([link({ direction: 'origin' }), link({ direction: 'out' })]),
    })
    expect(container.firstChild).toBeNull()
  })

  it('renders the chip for a two-way link, with no action attached', () => {
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store: storeWith([link()]) })
    expect(
      screen.getByText(i18nT('components.inboundLinkChip.driven_from', { label: 'zzq-chan' })),
    ).toBeInTheDocument()
    // Information only: the old Release button is deliberately gone, and the
    // chip must not grow a second, contradictory control.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('stays visible when the channel is disconnected (paused stops outbound only)', () => {
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith([link({ paused: true })]),
    })
    expect(
      screen.getByText(i18nT('components.inboundLinkChip.driven_from', { label: 'zzq-chan' })),
    ).toBeInTheDocument()
  })
})
