import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
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
  return {
    notifications: { items: notifs } as RootState['notifications'],
  }
}

const STORAGE_KEY = 'mc:notif:activeKinds'

describe('NotificationsPage multi-select filter', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('shows all notifications by default with All button active', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
      mkN('hook', '2026-05-29T10:02:00Z', 'Webhook Fired'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    expect(screen.getByText('Subagent Done')).toBeInTheDocument()
    expect(screen.getByText('Webhook Fired')).toBeInTheDocument()

    const allBtn = screen.getByRole('button', { name: /^All$/ })
    expect(allBtn.getAttribute('aria-pressed')).toBe('true')
  })

  it('clicking a single category in the all-selected state deselects only that category', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
      mkN('hook', '2026-05-29T10:02:00Z', 'Webhook Fired'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    const subagentBtn = screen.getByRole('button', { name: /^Subagent$/ })
    fireEvent.click(subagentBtn)

    expect(subagentBtn.getAttribute('aria-pressed')).toBe('false')
    expect(screen.queryByText('Subagent Done')).not.toBeInTheDocument()
    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    expect(screen.getByText('Webhook Fired')).toBeInTheDocument()

    // All button should no longer be active because not every kind is selected
    const allBtn = screen.getByRole('button', { name: /^All$/ })
    expect(allBtn.getAttribute('aria-pressed')).toBe('false')
  })

  it('clicking All when all are selected clears every category (empty state)', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    const allBtn = screen.getByRole('button', { name: /^All$/ })
    fireEvent.click(allBtn)

    expect(allBtn.getAttribute('aria-pressed')).toBe('false')
    expect(screen.queryByText('Cron Result')).not.toBeInTheDocument()
    expect(screen.queryByText('Subagent Done')).not.toBeInTheDocument()
    expect(screen.getByText(/No categories selected/i)).toBeInTheDocument()
  })

  it('clicking All when none are selected re-selects every category', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    const allBtn = screen.getByRole('button', { name: /^All$/ })
    fireEvent.click(allBtn) // clear
    fireEvent.click(allBtn) // re-select

    expect(allBtn.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    expect(screen.getByText('Subagent Done')).toBeInTheDocument()
  })

  it('persists active kinds to localStorage and reloads them', () => {
    const store1 = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
    ]))
    const { unmount } = renderWithProviders(<NotificationsPage />, { store: store1 })

    fireEvent.click(screen.getByRole('button', { name: /^Subagent$/ }))

    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    expect(saved).not.toContain('subagent')
    expect(saved).toContain('cron')

    unmount()

    const store2 = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('subagent', '2026-05-29T10:01:00Z', 'Subagent Done'),
    ]))
    renderWithProviders(<NotificationsPage />, { store: store2 })

    expect(screen.queryByText('Subagent Done')).not.toBeInTheDocument()
    expect(screen.getByText('Cron Result')).toBeInTheDocument()
  })

  it('renders with role=group and labelled chips', () => {
    const store = createTestStore(stateWith([]))
    const { container } = renderWithProviders(<NotificationsPage />, { store })
    const group = container.querySelector('[role="group"][aria-label="Filter notifications by kind"]')
    expect(group).not.toBeNull()
    const chips = within(group as HTMLElement).getAllByRole('button')
    // 8 chips: All + 7 kinds
    expect(chips.length).toBe(8)
  })

  it('shows unknown-kind notifications when all kinds are active (back-compat)', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('mystery_kind', '2026-05-29T10:01:00Z', 'Unknown Kind'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    // Unknown kinds stay visible while all kinds are active (back-compat)
    expect(screen.getByText('Unknown Kind')).toBeInTheDocument()
  })

  it('hides unknown-kind notifications once any chip is deselected', () => {
    const store = createTestStore(stateWith([
      mkN('cron', '2026-05-29T10:00:00Z', 'Cron Result'),
      mkN('mystery_kind', '2026-05-29T10:01:00Z', 'Unknown Kind'),
    ]))
    renderWithProviders(<NotificationsPage />, { store })

    fireEvent.click(screen.getByRole('button', { name: /^Subagent$/ }))

    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    // Strict filter active — unknown kinds drop out
    expect(screen.queryByText('Unknown Kind')).not.toBeInTheDocument()
  })
})
