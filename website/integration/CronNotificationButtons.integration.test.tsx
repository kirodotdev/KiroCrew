import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'
import NotificationsPage from '../src/pages/NotificationsPage'
import type { Notification } from '../src/types'
import type { ChatSlot } from '../src/types'

function makeNotification(overrides: Partial<Notification> = {}): Notification {
  return { kind: 'cron', title: 'Cron result', body: 'Job completed', ts: '2026-05-15T10:00:00Z', ...overrides }
}

function makeSlot(overrides: Partial<ChatSlot> = {}): ChatSlot {
  return { key: 'slot-abc', title: 'Cron session', messages: 1, running: false, ...overrides }
}

const mockedNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockedNavigate }
})

describe('Cron Notification Buttons', () => {
  function renderWithNotification(n: Notification, slots: ChatSlot[] = []) {
    const store = createTestStore({
      notifications: { items: [n] } as any,
      dashboard: { slots, status: 'ok', version: '', slotsLoaded: true, unreadSlots: [] } as any,
    })
    renderWithProviders(<NotificationsPage />, { store })
    return store
  }

  it('renders "Continue session" when slot is present', async () => {
    const user = userEvent.setup()
    const n = makeNotification({ job_id: 'cron-1', slot: 'slot-abc' })
    renderWithNotification(n, [makeSlot()])

    // Click the notification to open detail panel
    await user.click(screen.getByText('Cron result'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /continue session/i })).toBeInTheDocument()
    })
  })

  it('renders "View last result" when job_id present but no slot', async () => {
    const user = userEvent.setup()
    const n = makeNotification({ job_id: 'cron-1' })
    renderWithNotification(n)

    await user.click(screen.getByText('Cron result'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /view last result/i })).toBeInTheDocument()
    })
  })

  it('shows only "Continue session" when both job_id and slot present (mutual exclusivity)', async () => {
    const user = userEvent.setup()
    const n = makeNotification({ job_id: 'cron-1', slot: 'slot-abc' })
    renderWithNotification(n, [makeSlot()])

    await user.click(screen.getByText('Cron result'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /continue session/i })).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: /view last result/i })).not.toBeInTheDocument()
  })

  it('shows no chat action button when neither job_id nor slot', async () => {
    const user = userEvent.setup()
    const n = makeNotification({})
    renderWithNotification(n)

    await user.click(screen.getByText('Cron result'))

    await waitFor(() => {
      expect(screen.getByText(/source/i)).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: /continue session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /view last result/i })).not.toBeInTheDocument()
  })

  it('"Continue session" dispatches switchSlot and navigates to /chat', async () => {
    const user = userEvent.setup()
    const n = makeNotification({ job_id: 'cron-1', slot: 'slot-abc' })
    const store = renderWithNotification(n, [makeSlot()])

    await user.click(screen.getByText('Cron result'))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /continue session/i })).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: /continue session/i }))

    expect(mockedNavigate).toHaveBeenCalledWith('/chat')
    expect(store.getState().chat.activeSlot).toBe('slot-abc')
  })

  it('"View last result" calls cronToChat API and navigates on success', async () => {
    const user = userEvent.setup()
    server.use(
      http.post('/api/crons/cron-1/to-chat', () => {
        return HttpResponse.json({ slot: 'new-slot-xyz' })
      })
    )
    const n = makeNotification({ job_id: 'cron-1' })
    renderWithNotification(n)

    await user.click(screen.getByText('Cron result'))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /view last result/i })).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: /view last result/i }))

    await waitFor(() => {
      expect(mockedNavigate).toHaveBeenCalledWith('/chat')
    })
  })

  it('hides "Go to Chat" when cron-specific "Continue session" is shown (dedup)', async () => {
    const user = userEvent.setup()
    // Notification has both job_id AND slot — triggers the dedup condition
    const n = makeNotification({ job_id: 'cron-1', slot: 'slot-abc' })
    // Slot exists in store — would normally show "Go to Chat" via directSlot
    renderWithNotification(n, [makeSlot()])

    await user.click(screen.getByText('Cron result'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /continue session/i })).toBeInTheDocument()
    })
    // "Go to Chat" should be suppressed because cron-specific button takes priority
    expect(screen.queryByRole('button', { name: /go to chat/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /resume chat/i })).not.toBeInTheDocument()
  })
})
