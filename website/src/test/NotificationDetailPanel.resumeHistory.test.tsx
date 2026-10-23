import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: { resumeChatSlot: vi.fn(), ackNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note = (history_key?: string): Notification => ({
  kind: 'agent',
  ts: '2026-10-22T00:00:00Z',
  title: 'Review ready',
  body: 'Open the publishing chat',
  slot: 'chat-2-200',
  history_key,
  acked: true,
})

beforeEach(() => {
  vi.mocked(api.resumeChatSlot).mockReset().mockResolvedValue({
    ok: true, key: 'chat-2-200', mode: '', messages: [],
  })
})

describe('NotificationDetailPanel Resume chat', () => {
  it('resumes with the transcript identity when the note carries one', async () => {
    renderWithProviders(<NotificationDetailPanel n={note('dashboard:chat-2-200')} onClose={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /Resume chat/i }))

    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard:chat-2-200', 'Review ready'))
  })

  it('falls back to the slot name for notes without a transcript identity', async () => {
    renderWithProviders(<NotificationDetailPanel n={note()} onClose={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /Resume chat/i }))

    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('chat-2-200', 'Review ready'))
  })
})
