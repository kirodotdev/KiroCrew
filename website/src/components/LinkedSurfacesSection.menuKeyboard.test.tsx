import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { addSlotOptimistic } from '../store/dashboardSlice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import type { ChatSlot, SessionLink } from '../types'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import {
  DropdownMenu,
  DropdownMenuContent,
} from './ui/dropdown-menu'

const mocks = vi.hoisted(() => ({
  channelTargets: vi.fn(),
  pauseMirror: vi.fn(),
  pauseSlack: vi.fn(),
  slackLink: vi.fn(),
  linkMirror: vi.fn(),
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, ...mocks } }
})

const SLOT = 'menu-keyboard-slot'

function link(): SessionLink {
  return {
    channel: 'discord',
    label: 'Discord',
    target: 'channel-1',
    direction: 'out',
    live: true,
  }
}

function mount(links: SessionLink[] = []) {
  const store = createTestStore()
  store.dispatch(addSlotOptimistic({
    key: SLOT,
    messages: 0,
    running: false,
    links,
  } as ChatSlot))
  return renderWithProviders(
    <DropdownMenu defaultOpen>
      <DropdownMenuContent>
        <LinkedSurfacesSection slotKey={SLOT} variant="dropdown" />
      </DropdownMenuContent>
    </DropdownMenu>,
    { store },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.channelTargets.mockResolvedValue([])
  mocks.pauseMirror.mockResolvedValue({ ok: true })
  mocks.pauseSlack.mockResolvedValue({ ok: true })
  mocks.slackLink.mockResolvedValue({ ok: true })
  mocks.linkMirror.mockResolvedValue({ ok: true })
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('LinkedSurfacesSection menu error hand-offs', () => {
  it('turns a target-list failure into a described, muted menu item', async () => {
    const user = userEvent.setup()
    mocks.channelTargets.mockRejectedValue(new Error('targets unavailable'))
    mount()

    const alert = await screen.findByTestId('linked-surfaces-targets-error')
    const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
    expect(handoff).toHaveAttribute('aria-describedby', alert.id)
    expect(handoff.getAttribute('title')).toBeTruthy()
    expect(handoff.querySelector('svg')).toHaveClass('text-muted')

    handoff.focus()
    await user.keyboard('{Enter}')
    expect(consumeChatHandoff()).toContain('Couldn’t load the channels this session can connect to.')
  })

  it.each([
    ['Enter', '{Enter}'],
    ['Space', ' '],
  ])('keeps the channel row action and stages its failure with %s', async (_label, key) => {
    const user = userEvent.setup()
    mocks.pauseMirror.mockRejectedValue(new Error('channel refused'))
    mount([link()])

    const row = await screen.findByRole('menuitem', { name: /disconnect from discord/i })
    row.focus()
    await user.keyboard('{Enter}')
    const alert = await screen.findByTestId('linked-surfaces-error-discord')
    expect(mocks.pauseMirror).toHaveBeenCalledTimes(1)

    row.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(mocks.pauseMirror).toHaveBeenCalledTimes(2))
    await screen.findByTestId('linked-surfaces-error-discord')

    row.focus()
    await user.keyboard('{ArrowDown}')
    const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
    expect(handoff).toHaveFocus()
    expect(handoff).toHaveAttribute('aria-describedby', alert.id)
    await user.keyboard(key)

    expect(consumeChatHandoff()).toContain('channel refused')
    expect(mocks.pauseMirror).toHaveBeenCalledTimes(2)
  })
})
