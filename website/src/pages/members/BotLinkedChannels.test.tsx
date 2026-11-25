// A Bot's own chat has no session menu, so its channel link needs a control
// of its own on the Bot thread header (#34978): the session menu's channel
// rows, under a chip that shows while the Bot's slot has a link or a channel
// it can connect to (how an Unlink is undone), and a link to the channel
// settings while there is neither.
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { createTestStore, renderWithProviders } from '../../test/helpers'
import BotLinkedChannels from './BotLinkedChannels'
import { sseSlots } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ChatSlot, SessionLink } from '../../types'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      channelTargets: vi.fn(),
      pauseSlack: vi.fn(),
      unlinkMirror: vi.fn(),
    },
  }
})

/**
 * happy-dom cannot drive a real Radix menu open (no PointerEvent), so the menu
 * shell collapses to plain elements: the content renders inline and each item
 * is a button. What is under test is the wiring, not Radix itself.
 */
// The menu shell's open-change and close-autofocus handlers, captured so a
// test can drive them the way Radix does.
const menuHooks = vi.hoisted(() => ({
  onOpenChange: undefined as ((open: boolean) => void) | undefined,
  open: undefined as boolean | undefined,
  onCloseAutoFocus: undefined as ((e: Event) => void) | undefined,
}))

vi.mock('../../components/ui/dropdown-menu', async importOriginal => {
  const mod = await importOriginal<Record<string, unknown>>()
  const Item = ({ children, onSelect }: { children?: React.ReactNode; onSelect?: (e: Event) => void }) => (
    <button type="button" onClick={() => onSelect?.(new Event('select', { cancelable: true }))}>{children}</button>
  )
  return {
    ...mod,
    DropdownMenu: ({ children, open, onOpenChange }: { children?: React.ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }) => {
      menuHooks.onOpenChange = onOpenChange
      menuHooks.open = open
      return <>{children}</>
    },
    DropdownMenuTrigger: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DropdownMenuContent: ({ children, onCloseAutoFocus }: { children?: React.ReactNode; onCloseAutoFocus?: (e: Event) => void }) => {
      menuHooks.onCloseAutoFocus = onCloseAutoFocus
      return <div role="menu">{children}</div>
    },
    DropdownMenuItem: Item,
  }
})

const channelTargets = vi.mocked(api.channelTargets)
const unlinkMirror = vi.mocked(api.unlinkMirror)

const BOT_SLOT = 'member-mate.memory-member-mate-zzq'

function slackLink(): SessionLink {
  return {
    channel: 'slack', label: 'Slack', target: 'D1', binding: 'b-1', direction: 'out', drives_session: true, live: true,
  }
}

function storeWith(links: SessionLink[]) {
  const store = createTestStore()
  store.dispatch(sseSlots([{ key: BOT_SLOT, messages: 0, running: false, links } as ChatSlot]))
  return store
}

const row = (k: string, vars?: Record<string, unknown>) => i18nT(`components.linkedSurfacesSection.${k}`, vars)

describe('BotLinkedChannels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    channelTargets.mockResolvedValue([] as never)
    unlinkMirror.mockResolvedValue({ ok: true, was_linked: true } as never)
  })

  it('with no link and nothing to connect to, links to the channel settings once the read answers', async () => {
    const { container } = renderWithProviders(
      <Routes>
        <Route path="/" element={<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />} />
        <Route path="/settings/channels" element={<p>zzq-channel-settings</p>} />
      </Routes>,
      { store: storeWith([]) },
    )
    // Before the read answers nothing is shown, so a crew with a channel sees no flash.
    expect(container.firstChild).toBeNull()
    const setUp = await screen.findByRole('button', { name: i18nT('pages.membersPage.open_messaging_channels') })
    // The label is on the button as text, not only in a tooltip.
    expect(setUp).toHaveTextContent(i18nT('pages.membersPage.open_messaging_channels'))
    expect(screen.queryByTestId('member-linked-channels')).toBeNull()
    expect(screen.queryByText('zzq-channel-settings')).toBeNull()
    fireEvent.click(setUp)
    expect(await screen.findByText('zzq-channel-settings')).toBeInTheDocument()
  })

  it('the settings button asks the leave guard, and stays put when it refuses', async () => {
    const guard = vi.fn(() => false)
    function Guarded() {
      useRegisterNavigationLeaveGuard(guard)
      return null
    }
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Guarded />
        <Routes>
          <Route path="/" element={<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />} />
          <Route path="/settings/channels" element={<p>zzq-channel-settings</p>} />
        </Routes>
      </NavigationLeaveGuardProvider>,
      { store: storeWith([]) },
    )
    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.open_messaging_channels') }))
    expect(guard).toHaveBeenCalled()
    expect(screen.queryByText('zzq-channel-settings')).toBeNull()
  })

  it('the phone top bar shows no settings button: it has no room beside the identity pill', async () => {
    // The header copy beside it shares the read, so once it shows the read has answered.
    renderWithProviders(
      <>
        <div data-testid="zzq-bar"><BotLinkedChannels slotKey={BOT_SLOT} inBar /></div>
        <BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />
      </>,
      { store: storeWith([]) },
    )
    expect(await screen.findAllByTestId('member-set-up-channel')).toHaveLength(1)
    expect(screen.getByTestId('zzq-bar')).toBeEmptyDOMElement()
  })

  it('an unlinked Bot chat with a channel to connect to keeps the chip, and its menu offers the connect', async () => {
    // How an Unlink is undone: the chip stays, and its rows offer the connect.
    channelTargets.mockResolvedValue([{
      channel_type: 'slack', target_id: 'zzq-dm', label: 'Slack DM', available: true, unavailable_reason: '',
    }] as never)
    renderWithProviders(<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />, { store: storeWith([]) })
    expect(await screen.findByRole('button', { name: i18nT('pages.membersPage.connect_channel') })).toBeInTheDocument()
    expect(await screen.findByText(row('connect_to', { label: 'Slack DM' }))).toBeInTheDocument()
  })

  it('a failed read of what can be connected keeps the chip, so its menu can say so', async () => {
    channelTargets.mockRejectedValue(new Error('zzq-down'))
    renderWithProviders(<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />, { store: storeWith([]) })
    expect(await screen.findByRole('button', { name: i18nT('pages.membersPage.connect_channel') })).toBeInTheDocument()
    expect(await screen.findByText(row('targets_load_failed'))).toBeInTheDocument()
    // The chip and its rows share one query: showing the rows must not hide
    // the chip again and refetch in a loop.
    expect(channelTargets.mock.calls.length).toBeLessThan(5)
  })

  it('a paused link reads as paused with the menu closed', () => {
    renderWithProviders(<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />, {
      store: storeWith([{ ...slackLink(), paused: true }]),
    })
    expect(screen.getByRole('button', { name: i18nT('pages.membersPage.linked_to_paused', { label: 'Slack' }) })).toBeInTheDocument()
    expect(screen.getByTestId('member-linked-channels-paused')).toBeInTheDocument()
  })

  it('a Slack-linked Bot chat gets the channel rows, and Unlink unlinks it', async () => {
    renderWithProviders(<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />, {
      store: storeWith([slackLink()]),
    })
    expect(screen.getByRole('button', { name: i18nT('pages.membersPage.linked_to', { label: 'Slack' }) })).toBeInTheDocument()
    const menu = await screen.findByRole('menu')
    expect(menu).toHaveTextContent(row('disconnect_from', { label: 'Slack' }))
    // A Bot chat has no session menu, so the Unlink row must not send the
    // reader to one to reconnect (#35044): it says what stops instead.
    expect(menu).toHaveTextContent(row('unlink_outcome_here', { label: 'Slack' }))
    expect(menu).not.toHaveTextContent(row('unlink_outcome', { label: 'Slack' }))
    fireEvent.click(screen.getByText(row('unlink_from', { label: 'Slack' })))
    await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(BOT_SLOT, { channel_type: 'slack', binding: 'b-1' }))
  })

  it('a one-way link gets the one-way Unlink line, also without the session-menu sentence', async () => {
    renderWithProviders(<BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />, {
      store: storeWith([{ ...slackLink(), channel: 'discord', label: 'zzq-guild', drives_session: false }]),
    })
    const menu = await screen.findByRole('menu')
    expect(menu).toHaveTextContent(row('unlink_outcome_out_here', { label: 'zzq-guild' }))
    expect(menu).not.toHaveTextContent(row('unlink_outcome_out', { label: 'zzq-guild' }))
  })

  it('an Unlink that removes the chip hands focus to the identity pill, not the page body', () => {
    const store = storeWith([slackLink()])
    renderWithProviders(
      <>
        <button type="button" data-testid="member-identity-pill">pill</button>
        <BotLinkedChannels slotKey={BOT_SLOT} inBar={false} />
      </>,
      { store },
    )
    // Opening the menu is a state change the component owns (it drops the tip).
    act(() => { menuHooks.onOpenChange?.(true) })
    expect(screen.getByRole('menu')).toBeInTheDocument()
    // Closed with the chip still there: Radix restores focus itself.
    const kept = new Event('close', { cancelable: true })
    act(() => { menuHooks.onCloseAutoFocus?.(kept) })
    expect(kept.defaultPrevented).toBe(false)
    // The last link goes: the chip leaves, and the close hands focus on.
    const close = menuHooks.onCloseAutoFocus
    act(() => { store.dispatch(sseSlots([{ key: BOT_SLOT, messages: 0, running: false, links: [] } as ChatSlot])) })
    expect(screen.queryByTestId('member-linked-channels')).toBeNull()
    const gone = new Event('close', { cancelable: true })
    act(() => { close?.(gone) })
    expect(gone.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(screen.getByTestId('member-identity-pill'))
    // A link that comes back brings the chip back closed, not its menu open.
    act(() => { store.dispatch(sseSlots([{ key: BOT_SLOT, messages: 0, running: false, links: [slackLink()] } as ChatSlot])) })
    expect(screen.getByTestId('member-linked-channels')).toBeInTheDocument()
    expect(menuHooks.open).toBe(false)
  })
})
