/**
 * The feed shows EVERY notification, unconditionally.
 *
 * Replaces `NotificationsPage.filter.test.tsx`, whose eight tests all drove the
 * per-kind filter chips that no longer exist. Two of its assertions were about a
 * real invariant rather than the chips, and those are what survive here: a
 * notification of any kind renders, including one whose `kind` the frontend does
 * not know.
 *
 * That used to be conditional. The feed only included unknown kinds while EVERY
 * known kind happened to be selected (`allActive`), so deselecting one chip
 * silently hid every unknown-kind notification — and adding a new kind turned a
 * stored full selection into a partial one, which hid the new kind for existing
 * installs. With the filter gone the invariant is structural, and these tests
 * pin it so a future reintroduction of filtering has to confront it.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationsPage from '../pages/NotificationsPage'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    taskRunToChat: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

// jsdom shims
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const mkN = (kind: string, ts: string, title: string): Notification => ({
  kind, ts, title, body: `body for ${title}`, acked: true,
})

function stateWith(notifs: Notification[]): Partial<RootState> {
  return { notifications: { items: notifs } as RootState['notifications'] }
}

beforeEach(() => {
  localStorage.clear()
})

describe('NotificationsPage without kind filters', () => {
  it('renders notifications of every kind at once', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
      mkN('approval', '2026-05-29T10:02:00Z', 'Approval Needed'),
      mkN('skills', '2026-05-29T10:03:00Z', 'Skill Awaiting Review'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    expect(screen.getByText('Subagent Done')).toBeInTheDocument()
    expect(screen.getByText('Approval Needed')).toBeInTheDocument()
    expect(screen.getByText('Skill Awaiting Review')).toBeInTheDocument()
  })

  it('renders a notification whose kind the frontend does not know', () => {
    // The load-bearing case: an unknown kind used to be visible only while every
    // known kind was selected, so it was one chip click away from disappearing.
    const store = createTestStore(stateWith([
      mkN('some-future-kind', '2026-05-29T10:00:00Z', 'From The Future'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(screen.getByText('From The Future')).toBeInTheDocument()
  })

  it('offers no kind-filter controls', () => {
    // Guards against the chips coming back by accident: the old row was a
    // role=group of toggle buttons, and the empty state had a "no categories
    // selected" subtitle. Neither should exist.
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(screen.queryByRole('group', { name: /filter/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^All$/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/no categories selected/i)).not.toBeInTheDocument()
  })

  it('does not persist any filter selection', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    // Neither the versioned key nor its predecessor should be written.
    expect(localStorage.getItem('mc:notif:activeKinds:v2')).toBeNull()
    expect(localStorage.getItem('mc:notif:activeKinds')).toBeNull()
  })
})
