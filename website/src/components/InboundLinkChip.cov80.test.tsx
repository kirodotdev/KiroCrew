// InboundLinkChip — the header chip for a session driven from another channel.
// The LIVE chip is information only (the Release button was deliberately
// removed; connecting and disconnecting live in the session menu's one row per
// channel). The PAUSED chip is a control: a menu trigger that opens those same
// rows right under the chip, because a chip that names a problem and answers a
// click with nothing is a dead click at the moment of need.
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../test/helpers'
import InboundLinkChip from './InboundLinkChip'
import { sseSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { ChatSlot, SessionLink } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      channelTargets: vi.fn(),
      pauseMirror: vi.fn(),
      pauseSlack: vi.fn(),
      unlinkMirror: vi.fn(),
      unlinkSlack: vi.fn(),
    },
  }
})

/**
 * happy-dom cannot drive a real Radix menu open (no PointerEvent), so the menu
 * shell collapses to plain elements: the trigger renders its child button, the
 * content renders inline, and each item is a button. What is under test is the
 * wiring — that the paused chip is the trigger and the rows are its content —
 * not Radix itself.
 */
vi.mock('./ui/dropdown-menu', async importOriginal => {
  const mod = await importOriginal<Record<string, unknown>>()
  const Item = ({ children, onSelect, ...rest }: {
    children?: React.ReactNode
    onSelect?: (e: Event) => void
    'aria-disabled'?: boolean
    'aria-busy'?: boolean
    title?: string
    className?: string
  }) => (
    <button
      type="button"
      aria-disabled={rest['aria-disabled']}
      aria-busy={rest['aria-busy']}
      title={rest.title}
      className={rest.className}
      onClick={() => onSelect?.(new Event('select', { cancelable: true }))}
    >
      {children}
    </button>
  )
  return {
    ...mod,
    DropdownMenu: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DropdownMenuTrigger: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DropdownMenuContent: ({ children }: { children?: React.ReactNode }) => <div role="menu">{children}</div>,
    DropdownMenuItem: Item,
  }
})

const channelTargets = vi.mocked(api.channelTargets)
const pauseMirror = vi.mocked(api.pauseMirror)

function link(over: Partial<SessionLink> = {}): SessionLink {
  // A resume binding: `both` on the wire, and the wire states that it drives
  // this session.
  return {
    channel: 'slack', label: 'zzq-chan', target: 'C1', binding: 'b-1', direction: 'both', drives_session: true, live: true, ...over,
  }
}

function slot(links: SessionLink[]): ChatSlot {
  return { key: 'zzq-slot', messages: 0, running: false, links } as ChatSlot
}

function storeWith(links: SessionLink[]) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot(links)]))
  return store
}

const chip = (k: string, vars?: Record<string, unknown>) => i18nT(`components.inboundLinkChip.${k}`, vars)
const row = (k: string, vars?: Record<string, unknown>) => i18nT(`components.linkedSurfacesSection.${k}`, vars)

describe('InboundLinkChip', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    channelTargets.mockResolvedValue([] as never)
    pauseMirror.mockResolvedValue({ ok: true } as never)
  })

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

  it('renders the live chip for a two-way link, with no action attached', () => {
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store: storeWith([link()]) })
    expect(screen.getByText(chip('driven_from', { label: 'zzq-chan' }))).toBeInTheDocument()
    // Information only: the old Release button is deliberately gone, and the
    // live chip must not grow a second, contradictory control. Nothing to
    // repair either, so no hint and no menu.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(container.firstChild).not.toHaveAttribute('title')
  })

  it('the paused chip names the pause and IS a control: a menu trigger with the hint as its title', () => {
    // Disconnect stops OUTBOUND delivery only, so messages sent there still land
    // here and the chip stays — but a chip that reads exactly like the connected
    // one makes the disconnect look like it did not happen. The paused state is
    // named, and the binding's survival with it. It is also the state that
    // invites repair: the reader clicks it hoping to resume, so it is a button
    // (Tab-reachable, blind-readable) rather than a span with a hover-only hint.
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store: storeWith([link({ paused: true })]) })
    const trigger = screen.getByRole('button', { name: chip('driven_from_paused', { label: 'zzq-chan' }) })
    expect(trigger).toHaveAttribute('title', chip('driven_from_paused_hint', { label: 'zzq-chan' }))
    expect(screen.queryByText(chip('driven_from', { label: 'zzq-chan' }))).not.toBeInTheDocument()
  })

  it("the paused chip's menu is the session menu's own Linked surfaces rows: Resume replies and Unlink", async () => {
    // Not a second control — the same `LinkedSurfacesSection` the session menu
    // renders, so the verbs cannot disagree. The Resume row is what resumes.
    const store = storeWith([link({ channel: 'discord', paused: true })])
    renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store })
    const menu = await screen.findByRole('menu')
    expect(menu).toHaveTextContent(row('resume_replies_to', { label: 'Discord' }))
    expect(menu).toHaveTextContent(row('still_linked'))
    expect(menu).toHaveTextContent(row('unlink_from', { label: 'Discord' }))
    fireEvent.click(screen.getByText(row('resume_replies_to', { label: 'Discord' })))
    await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith('zzq-slot', false, false))
    // The resume lands: the row patches `paused: false`, the chip reads live
    // again (its own link label, where the rows use the brand label), and the
    // trigger gives way to the plain, action-free chip.
    await waitFor(() => expect(screen.getByText(chip('driven_from', { label: 'zzq-chan' }))).toBeInTheDocument())
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('disappears once the binding is unlinked', () => {
    // The third state: an unlinked channel leaves no two-way link, so the store
    // carries none and the chip renders nothing rather than a stale claim.
    const store = storeWith([link()])
    store.dispatch(sseSlots([slot([])]))
    const { container } = renderWithProviders(<InboundLinkChip slotKey="zzq-slot" />, { store })
    expect(container.firstChild).toBeNull()
  })
})
