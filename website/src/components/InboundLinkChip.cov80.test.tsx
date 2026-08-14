// InboundLinkChip — the information-only header chip for a session driven from
// another channel. Only a `direction: 'both'` link earns the chip; it carries
// no action (connect/disconnect lives in the session menu), and it stays
// visible even when the channel is disconnected, because inbound delivery
// keeps working either way.
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../test/helpers'
import InboundLinkChip from './InboundLinkChip'
import { sseSlots } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import type { ChatSlot, SessionLink } from '../types'

function link(over: Partial<SessionLink> = {}): SessionLink {
  return { channel: 'slack', label: 'zzq-chan', target: 'C1', direction: 'both', live: true, ...over }
}

function slot(links?: SessionLink[]): ChatSlot {
  return { key: 'zzq-slot', messages: 0, running: false, links } as ChatSlot
}

function storeWith(links?: SessionLink[]) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot(links)]))
  return store
}

const drivenFrom = (label: string) => i18nT('components.inboundLinkChip.driven_from', { label })

describe('InboundLinkChip', () => {
  it('renders nothing without a slotKey', () => {
    const { container } = renderWithProviders(<InboundLinkChip />, { store: storeWith([link()]) })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing for an unknown slot', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-missing" />, {
      store: storeWith([link()]),
    })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when the slot has no links at all', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith(undefined),
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
    expect(screen.getByText(drivenFrom('zzq-chan'))).toBeInTheDocument()
    // Information only: disconnecting happens in the session menu, so the chip
    // must not offer a second, contradictory control.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('stays visible when the channel is disconnected', () => {
    // A disconnect stops outbound delivery only — messages from the channel
    // still land here, which is exactly what the chip claims.
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith([link({ live: false })]),
    })
    expect(screen.getByText(drivenFrom('zzq-chan'))).toBeInTheDocument()
  })

  it('surfaces the first two-way link when the slot carries several', () => {
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, {
      store: storeWith([
        link({ direction: 'out', label: 'zzq-skip' }),
        link({ label: 'zzq-first' }),
        link({ label: 'zzq-second' }),
      ]),
    })
    expect(screen.getByText(drivenFrom('zzq-first'))).toBeInTheDocument()
    expect(screen.queryByText(drivenFrom('zzq-second'))).not.toBeInTheDocument()
  })
})
