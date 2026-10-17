/**
 * A session's own turn time limit, picked from the session menu or from the
 * card a timed-out turn leaves.
 *
 * Locked here:
 *   (1) the picker reads the session's limit when it opens and marks the row in
 *       effect as the checked radio, with "Same as all sessions" naming the global;
 *   (2) a pick writes that number of seconds, and "Same as all sessions" writes null;
 *   (3) the menu stays open and the check moves once the write lands, and a
 *       refused write leaves the check where it was with the reason on screen
 *       and the agent hand-off as its own menu item;
 *   (4) the timeout card keeps Resume plus ONE limit control: a menu holding
 *       this session's limit and the Settings link, or the Settings link alone
 *       when the session is unknown.
 */
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({
  chatSlotTurnTimeout: vi.fn(),
  setChatSlotTurnTimeout: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import {
  SessionTurnLimitButton,
  SessionTurnLimitSubmenu,
  TURN_LIMIT_PRESETS_SECS,
  fmtTurnLimit,
  turnLimitOptions,
  turnLimitQueryKey,
} from '../components/SessionTurnLimitMenu'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '../components/ui/context-menu'
import { ErrorCard } from '../pages/chat/ErrorCard'
import { i18nT } from '../i18n/t'

const RANGE = { min: 300, max: 86400 }

function renderButton(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={client}>
      <SessionTurnLimitButton slotKey="slot-1" />
    </QueryClientProvider>,
  )
}

async function openPicker(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId('error-card-session-turn-limit'))
  return screen.findByRole('menu')
}

describe('turn limit formatting', () => {
  it('renders a limit as hours and minutes', () => {
    expect(fmtTurnLimit(1800)).toBe('30m')
    expect(fmtTurnLimit(14400)).toBe('4h')
    expect(fmtTurnLimit(5400)).toBe('1h 30m')
  })

  it('offers the in-range presets plus a current limit that is not one of them', () => {
    expect(turnLimitOptions({ secs: null, ...RANGE })).toEqual([...TURN_LIMIT_PRESETS_SECS])
    expect(turnLimitOptions({ secs: 14400, ...RANGE })).toEqual([...TURN_LIMIT_PRESETS_SECS])
    expect(turnLimitOptions({ secs: 5400, ...RANGE })).toEqual([1800, 3600, 5400, 7200, 14400, 28800, 43200, 86400])
    expect(turnLimitOptions({ secs: null, min: 3600, max: 14400 })).toEqual([3600, 7200, 14400])
  })
})

describe('SessionTurnLimitButton', () => {
  beforeEach(() => {
    cleanup()
    mocks.chatSlotTurnTimeout.mockReset()
    mocks.setChatSlotTurnTimeout.mockReset()
  })

  it('marks "same as Settings" while the session follows the global limit', async () => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: null, global_secs: 14400, ...RANGE })
    const user = userEvent.setup()
    renderButton()
    const menu = await openPicker(user)
    const follow = await within(menu).findByRole('menuitemradio', {
      name: i18nT('components.sessionTurnLimit.follow_global', { limit: '4h' }),
    })
    expect(follow).toHaveAttribute('aria-checked', 'true')
    expect(within(menu).getByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'false')
    expect(mocks.chatSlotTurnTimeout).toHaveBeenCalledWith('slot-1')
  })

  it('writes the picked limit and moves the check without closing the menu', async () => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: null, global_secs: 14400, ...RANGE })
    mocks.setChatSlotTurnTimeout.mockResolvedValue({ ok: true, secs: 28800, global_secs: 14400, ...RANGE })
    const user = userEvent.setup()
    renderButton()
    const menu = await openPicker(user)
    await user.click(await within(menu).findByRole('menuitemradio', { name: '8h' }))
    expect(mocks.setChatSlotTurnTimeout).toHaveBeenCalledWith('slot-1', 28800)
    await waitFor(() => {
      expect(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'true')
    })
  })

  it('returns the session to the global limit with null', async () => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: 28800, global_secs: 14400, ...RANGE })
    mocks.setChatSlotTurnTimeout.mockResolvedValue({ ok: true, secs: null, global_secs: 14400, ...RANGE })
    const user = userEvent.setup()
    renderButton()
    const menu = await openPicker(user)
    expect(await within(menu).findByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'true')
    await user.click(within(menu).getByRole('menuitemradio', {
      name: i18nT('components.sessionTurnLimit.follow_global', { limit: '4h' }),
    }))
    expect(mocks.setChatSlotTurnTimeout).toHaveBeenCalledWith('slot-1', null)
  })

  it('does not rewrite the limit already in effect', async () => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: 28800, global_secs: 14400, ...RANGE })
    const user = userEvent.setup()
    renderButton()
    const menu = await openPicker(user)
    await user.click(await within(menu).findByRole('menuitemradio', { name: '8h' }))
    expect(mocks.setChatSlotTurnTimeout).not.toHaveBeenCalled()
  })

  it('keeps the old check and says why when the write is refused', async () => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: null, global_secs: 14400, ...RANGE })
    mocks.setChatSlotTurnTimeout.mockRejectedValue(new Error('session was deleted or rebound'))
    const user = userEvent.setup()
    renderButton()
    const menu = await openPicker(user)
    await user.click(await within(menu).findByRole('menuitemradio', { name: '8h' }))
    expect(await screen.findByTestId('session-turn-limit-save-error')).toHaveTextContent('session was deleted or rebound')
    expect(screen.getByRole('menuitem', { name: i18nT('components.askAgent.ask_the_agent') })).toBeVisible()
    expect(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'false')
  })

  it('says so when the limit cannot be read', async () => {
    mocks.chatSlotTurnTimeout.mockRejectedValue(new Error('gateway offline'))
    const user = userEvent.setup()
    renderButton()
    await openPicker(user)
    expect(await screen.findByTestId('session-turn-limit-load-error')).toHaveTextContent('gateway offline')
    // Inside a menu the agent hand-off is its own item, described by the notice.
    const handoff = screen.getByRole('menuitem', { name: i18nT('components.askAgent.ask_the_agent') })
    expect(handoff.getAttribute('aria-describedby')).toBe(
      screen.getByTestId('session-turn-limit-load-error').closest('[id]')?.id,
    )
  })

  it('says so when a refetch fails while an earlier read is still cached', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // An earlier read, old enough to be stale, so opening the menu refetches it.
    client.setQueryData(turnLimitQueryKey('slot-1'), { secs: 28800, global_secs: 14400, ...RANGE }, { updatedAt: 1 })
    mocks.chatSlotTurnTimeout.mockRejectedValue(new Error('gateway offline'))
    const user = userEvent.setup()
    renderButton(client)
    const menu = await openPicker(user)
    expect(await screen.findByTestId('session-turn-limit-load-error')).toHaveTextContent('gateway offline')
    // The cached choices stay visible beside the notice, with its hand-off item.
    expect(within(menu).getByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('menuitem', { name: i18nT('components.askAgent.ask_the_agent') })).toBeVisible()
  })
})

describe('SessionTurnLimitSubmenu', () => {
  beforeEach(() => {
    cleanup()
    mocks.chatSlotTurnTimeout.mockReset()
  })

  it.each(['dropdown', 'context'] as const)('sits in the %s session menu as a submenu named for the setting', (variant) => {
    const client = new QueryClient()
    const Menu = variant === 'context' ? ContextMenu : DropdownMenu
    const Trigger = variant === 'context' ? ContextMenuTrigger : DropdownMenuTrigger
    const Content = variant === 'context' ? ContextMenuContent : DropdownMenuContent
    render(
      <QueryClientProvider client={client}>
        <Menu {...(variant === 'dropdown' ? { defaultOpen: true } : {})}>
          <Trigger>open</Trigger>
          <Content forceMount>
            <SessionTurnLimitSubmenu slotKey="slot-1" variant={variant} />
          </Content>
        </Menu>
      </QueryClientProvider>,
    )
    if (variant === 'context') fireEvent.contextMenu(screen.getByText('open'))
    const trigger = screen.getByTestId('session-turn-limit-submenu')
    expect(trigger).toHaveTextContent(i18nT('components.sessionTurnLimit.label'))
    expect(trigger).toHaveAttribute('title', i18nT('components.sessionTurnLimit.hint'))
    // The limit is read when the submenu opens, not when the parent menu does.
    expect(mocks.chatSlotTurnTimeout).not.toHaveBeenCalled()
  })

  it.each(['dropdown', 'context'] as const)('heads the open %s submenu with "This session", like the card picker', async (variant) => {
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: null, global_secs: 14400, min: 300, max: 86400 })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const Menu = variant === 'context' ? ContextMenu : DropdownMenu
    const Trigger = variant === 'context' ? ContextMenuTrigger : DropdownMenuTrigger
    const Content = variant === 'context' ? ContextMenuContent : DropdownMenuContent
    render(
      <QueryClientProvider client={client}>
        <Menu {...(variant === 'dropdown' ? { defaultOpen: true } : {})}>
          <Trigger>open</Trigger>
          <Content forceMount>
            <SessionTurnLimitSubmenu slotKey="slot-1" variant={variant} />
          </Content>
        </Menu>
      </QueryClientProvider>,
    )
    if (variant === 'context') fireEvent.contextMenu(screen.getByText('open'))
    const trigger = screen.getByTestId('session-turn-limit-submenu')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowRight' })
    expect(await screen.findByText(i18nT('components.sessionTurnLimit.this_session'))).toBeInTheDocument()
  })
})

describe('ErrorCard — a session limit on the timeout card', () => {
  beforeEach(() => {
    cleanup()
    mocks.chatSlotTurnTimeout.mockReset()
    mocks.chatSlotTurnTimeout.mockResolvedValue({ secs: 28800, global_secs: 14400, ...RANGE })
  })

  function renderCard(props: Partial<React.ComponentProps<typeof ErrorCard>>) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <ErrorCard content="⏱️ This turn hit the 8-hour limit." onContinue={() => {}} {...props} />
      </QueryClientProvider>,
    )
  }

  it('keeps Resume and one limit control, with both scopes inside it', async () => {
    const onOpenTurnLimit = vi.fn()
    const user = userEvent.setup()
    renderCard({ onOpenTurnLimit, turnLimitSlot: 'slot-1' })
    const card = screen.getByTestId('error-card')
    expect(within(card).getAllByRole('button')).toHaveLength(2)
    expect(screen.getByTestId('error-card-session-turn-limit')).toHaveTextContent(i18nT('pages.chat.errorCard.turn_limit'))
    const menu = await openPicker(user)
    expect(within(menu).getByText(i18nT('components.sessionTurnLimit.this_session'))).toBeVisible()
    expect(await within(menu).findByRole('menuitemradio', { name: '8h' })).toHaveAttribute('aria-checked', 'true')
    await user.click(within(menu).getByRole('menuitem', { name: i18nT('components.sessionTurnLimit.all_sessions') }))
    expect(onOpenTurnLimit).toHaveBeenCalledTimes(1)
  })

  it('leaves the Settings link out on a surface with no settings route', async () => {
    const user = userEvent.setup()
    renderCard({ turnLimitSlot: 'slot-1' })
    expect(screen.getByTestId('error-card')).toHaveAttribute('data-turn-timeout', 'true')
    const menu = await openPicker(user)
    await within(menu).findByRole('menuitemradio', { name: '8h' })
    expect(within(menu).queryByRole('menuitem', { name: i18nT('components.sessionTurnLimit.all_sessions') })).toBeNull()
  })

  it('keeps the plain Settings button when the session is unknown', () => {
    render(<ErrorCard content="⏱️ limit" onOpenTurnLimit={() => {}} />)
    expect(screen.getByTestId('error-card-turn-limit')).toBeVisible()
    expect(screen.queryByTestId('error-card-session-turn-limit')).toBeNull()
  })
})
