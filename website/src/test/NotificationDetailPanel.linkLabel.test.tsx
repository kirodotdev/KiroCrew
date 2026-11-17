/**
 * The detail panel's link button names where it goes for a note whose copy the
 * gateway named: the paused-share note's button reads "Review shared servers",
 * in the dashboard's language, and no bare "Open" is drawn beside it. A note
 * without such a label keeps "Open".
 */

import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { i18nT } from '../i18n/t'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: { taskRunToChat: vi.fn(), cronToChat: vi.fn(), crons: vi.fn(), resolveApproval: vi.fn(), ackNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const OPEN = i18nT('components.notifications.notificationDetailPanel.open')
const pausedNote: Notification = {
  kind: 'agent', ts: '2026-11-13T07:00:00Z', title: 'A shared MCP server is paused',
  body: 'stored English', url: '/mcp?view=sharing&review=1',
  i18n: { id: 'mcp_share_paused', params: { names: 'alpha-mcp' } },
} as unknown as Notification

describe('NotificationDetailPanel -- link button label', () => {
  it('names the paused-share note\'s button after the review it opens', () => {
    renderWithProviders(<NotificationDetailPanel n={pausedNote} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Review shared servers' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: OPEN })).toBeNull()
  })

  it('keeps "Open" for a note that names no label', () => {
    const plain = { kind: 'agent', ts: '2026-11-13T07:01:00Z', title: 't', body: 'b', url: '/schedule' } as unknown as Notification
    renderWithProviders(<NotificationDetailPanel n={plain} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: OPEN })).toBeTruthy()
  })
})
