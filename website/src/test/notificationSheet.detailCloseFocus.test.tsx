/**
 * Bell sheet: closing a note's detail must not drop focus to <body>.
 *
 * The detail unmounts while it holds focus (its Close button, Escape, an
 * Approve or Reject that removes the note), and the next Tab would start at
 * the top of the page behind the still-open sheet. Focus returns to the row
 * that opened the detail, or the first row when that note is gone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { Notification } from '../types'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { NotificationSheet, useNotificationSheet } from '../shell/notifications/notificationSheet'
import { deleteNotification } from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    deleteNotification: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    taskRunToChat: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

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

const first: Notification = {
  kind: 'cron', ts: '2026-05-29T10:00:00Z', title: 'Cron Result', body: 'cron body text', acked: true,
}
const second: Notification = {
  kind: 'hook', ts: '2026-05-29T09:00:00Z', title: 'Hook Fired', body: 'hook body text', acked: true,
}

function mount() {
  const store = createTestStore({ notifications: { items: [first, second] } as RootState['notifications'] } as Partial<RootState>)
  let sheet!: ReturnType<typeof useNotificationSheet>
  function Harness() {
    sheet = useNotificationSheet()
    return <NotificationSheet sheet={sheet} />
  }
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/chat']}>
          <LocalOverlayRoot hidden={false}><Harness /></LocalOverlayRoot>
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, open: (ts: string) => act(() => { sheet.openPanelOn(ts) }) }
}

const rowControl = (title: string) => screen.getByRole('button', { name: `Open notification: ${title}` })

beforeEach(() => {
  localStorage.clear()
})

describe('bell sheet detail close focus', () => {
  it('returns focus to the opened row when Close is pressed', () => {
    const { open } = mount()
    open(second.ts)
    const close = screen.getByRole('button', { name: /Close/ })
    close.focus()
    fireEvent.click(close)

    expect(document.activeElement).toBe(rowControl('Hook Fired'))
  })

  it('moves focus to the first row when the opened note is gone', () => {
    const { store, open } = mount()
    open(second.ts)
    screen.getByRole('button', { name: /Close/ }).focus()
    act(() => { store.dispatch(deleteNotification.fulfilled(second.ts, '', second.ts)) })

    expect(document.activeElement).toBe(rowControl('Cron Result'))
  })

  it('moves focus on to the first row when the delete lands after the detail closed', () => {
    // Approve and Reject close the detail, then their delete lands: focus
    // goes back to the row first, and that row then leaves the feed.
    const { store, open } = mount()
    open(second.ts)
    const close = screen.getByRole('button', { name: /Close/ })
    close.focus()
    fireEvent.click(close)
    expect(document.activeElement).toBe(rowControl('Hook Fired'))

    act(() => { store.dispatch(deleteNotification.fulfilled(second.ts, '', second.ts)) })

    expect(document.activeElement).toBe(rowControl('Cron Result'))
  })

  it('leaves focus alone when the user already moved it to another row', () => {
    const { open } = mount()
    open(second.ts)
    const other = rowControl('Cron Result')
    other.focus()
    fireEvent.click(other)

    expect(document.activeElement).toBe(other)
  })
})
