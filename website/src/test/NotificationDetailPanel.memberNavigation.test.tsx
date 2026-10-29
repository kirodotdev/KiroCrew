import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useLocation } from 'react-router-dom'

import { createTestStore, renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'
import type { ChatSlot, Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    resumeChatSlot: vi.fn(),
    members: vi.fn().mockResolvedValue({ members: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../store/chatSlice', async importOriginal => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: vi.fn(actual.switchSlot) }
})
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note = (overrides: Partial<Notification> = {}): Notification => ({
  kind: 'agent',
  ts: '2026-10-22T00:00:00Z',
  title: 'Review ready',
  body: 'Open the publishing chat',
  slot: 'chat-2-200',
  acked: true,
  ...overrides,
})

const slot = (overrides: Partial<ChatSlot> = {}): ChatSlot => ({
  key: 'chat-2-200',
  messages: 1,
  running: false,
  ...overrides,
})

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname + location.search}</output>
}

function renderPanel(n: Notification, slots: ChatSlot[] = []) {
  const store = createTestStore()
  if (slots.length) store.dispatch(sseSlots(slots))
  return renderWithProviders(<><NotificationDetailPanel n={n} onClose={() => {}} /><LocationProbe /></>, {
    store,
    route: '/notifications',
  })
}

beforeEach(() => {
  vi.mocked(api.resumeChatSlot).mockReset().mockResolvedValue({
    ok: true, key: 'chat-2-200', mode: '', messages: [],
  })
  vi.mocked(api.members).mockClear()
  vi.mocked(switchSlot).mockClear()
})

describe('NotificationDetailPanel session-page navigation', () => {
  it('opens an active member thread on the Members page', async () => {
    renderPanel(note({ slot: 'member-alice' }), [slot({ key: 'member-alice', mode: 'member', agent: 'Alice' })])

    await userEvent.click(screen.getByRole('button', { name: /Go to chat/i }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Alice'))
    expect(switchSlot).not.toHaveBeenCalled()
    expect(screen.getByTestId('location')).not.toHaveTextContent('/chat')
  })

  it('opens the Members page for an active member thread that names no mate', async () => {
    renderPanel(note({ slot: 'member-alice' }), [slot({ key: 'member-alice', mode: 'member' })])

    await userEvent.click(screen.getByRole('button', { name: /Go to chat/i }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/members$/))
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('opens a closed member thread on the Members page without resuming history', async () => {
    renderPanel(note({
      slot: 'member-alice-smith',
      member: { slug: 'alice-smith', name: 'Alice Smith' },
    }))

    await userEvent.click(screen.getByRole('button', { name: /Resume chat/i }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Alice%20Smith'))
    expect(api.resumeChatSlot).not.toHaveBeenCalled()
  })

  it('resumes a closed member thread from history when the note names no mate', async () => {
    renderPanel(note({ slot: 'member-alice-smith', history_key: 'dashboard:member-alice-smith' }))

    await userEvent.click(screen.getByRole('button', { name: /Resume chat/i }))

    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard:member-alice-smith', 'Review ready'))
    expect(screen.getByTestId('location')).not.toHaveTextContent('/members')
  })

  it('keeps an active ordinary chat on the chat page', async () => {
    renderPanel(note(), [slot()])

    await userEvent.click(screen.getByRole('button', { name: /Go to chat/i }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/chat'))
    expect(switchSlot).toHaveBeenCalledWith({ key: 'chat-2-200', announceOnMissing: true, gesture: true })
  })

  // #23564: the bell's sheet closes on a pathname change only, so a jump to
  // /chat made from /chat must close it through onLeave, or the sheet stays
  // over the chat the gesture just opened and that open reads nothing.
  it.each([
    ['an ordinary chat link', note(), [slot()]],
    ['a cron run\'s Go to chat', note({ kind: 'cron', slot: 'chat-2-200', job_id: 'job-a' }), [slot()]],
  ] as const)('tells its host it is leaving on %s, also when the page is already /chat', async (_label, n, slots) => {
    const onLeave = vi.fn()
    const store = createTestStore()
    store.dispatch(sseSlots([...slots]))
    renderWithProviders(<><NotificationDetailPanel n={n} onClose={() => {}} onLeave={onLeave} /><LocationProbe /></>, { store, route: '/chat' })

    await userEvent.click(screen.getByRole('button', { name: /Go to chat/i }))

    await waitFor(() => expect(onLeave).toHaveBeenCalledTimes(1))
    expect(switchSlot).toHaveBeenCalledWith({ key: 'chat-2-200', announceOnMissing: true, gesture: true })
  })
})
