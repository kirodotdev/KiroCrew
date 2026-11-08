/**
 * The /notifications stat cards (Total, Unread, Cron, Hooks, Heartbeat) print
 * their counts through the locale's number formatter. They used to render the
 * raw JS number, so Bengali read Latin digits ("12") beside translated labels.
 */

import { describe, it, expect, afterEach, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationsPage from '../pages/NotificationsPage'
import type { RootState } from '../store'
import type { Notification } from '../types'
import { i18next } from '../i18n/all'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

/** Twelve notes: 12 total, 12 unread, 12 cron, 0 hooks, 0 heartbeat. */
const items: Notification[] = Array.from({ length: 12 }, (_, i) => ({
  kind: 'cron', ts: `2026-05-29T10:${String(i).padStart(2, '0')}:00Z`, title: `Note ${i}`, body: 'b', acked: false,
}))

function cardValues(): string[] {
  return screen.getAllByTestId('stat-card').map(c => within(c).getByTestId('stat-card-value').textContent ?? '')
}

afterEach(async () => { await i18next.changeLanguage('en') })

describe('NotificationsPage stat counts', () => {
  it('formats the counts in the reader\'s numerals', async () => {
    await i18next.changeLanguage('bn')
    const store = createTestStore({ notifications: { items } as RootState['notifications'] } as Partial<RootState>)
    renderWithProviders(<NotificationsPage />, { store })
    expect(cardValues()).toEqual(['১২', '১২', '১২', '০', '০'])
  })

  it('keeps plain digits in English', () => {
    const store = createTestStore({ notifications: { items } as RootState['notifications'] } as Partial<RootState>)
    renderWithProviders(<NotificationsPage />, { store })
    expect(cardValues()).toEqual(['12', '12', '12', '0', '0'])
  })
})
