import { describe, it, expect, beforeEach, onTestFinished, vi } from 'vitest'
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
import { render, fireEvent, screen, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const dnd = vi.hoisted(() => ({ until: null as number | null, fail: false }))
vi.mock('../api/client', () => ({
  api: {
    notificationDnd: vi.fn(async () => ({ until: dnd.until })),
    setNotificationDnd: vi.fn(async (until: number | null) => {
      if (dnd.fail) throw new Error('500')
      dnd.until = until
      return { until }
    }),
  },
}))

import { api } from '../api/client'
import { DoNotDisturbRow } from '../pages/settings/NotificationsDndRow'
import { dndEndMs, NOTIFICATION_DND_KEY, type NotificationDnd } from '../hooks/useNotificationDnd'

function renderRow() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={qc}><DoNotDisturbRow /></QueryClientProvider>)
  onTestFinished(() => { view.unmount(); qc.clear() })
  return { ...view, qc }
}

const select = () => screen.getByRole('combobox', { name: 'Do not disturb' })
const lastSaved = () => vi.mocked(api.setNotificationDnd).mock.calls.at(-1)?.[0]
const choose = (name: RegExp) => { fireEvent.click(select()); fireEvent.click(screen.getByRole('option', { name })) }

describe('DoNotDisturbRow', () => {
  beforeEach(() => {
    dnd.until = null
    dnd.fail = false
    vi.mocked(api.setNotificationDnd).mockClear()
  })

  it('starts Do Not Disturb on the gateway in epoch seconds, shows when it ends, and ends it', async () => {
    const start = new Date(2026, 10, 6, 21, 0).getTime()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(start)
    renderRow()
    await waitFor(() => expect(select().textContent).toBe('Off'))

    choose(/^For 2 hours$/)
    await waitFor(() => expect(select().textContent).toMatch(/^On until /))
    expect(lastSaved()).toBeCloseTo(start / 1000 + 7200, 0)

    choose(/^Until /)
    await waitFor(() => expect(lastSaved()).toBe(new Date(2026, 10, 7, 8, 0).getTime() / 1000))

    choose(/^Off$/)
    await waitFor(() => expect(select().textContent).toBe('Off'))
    expect(api.setNotificationDnd).toHaveBeenLastCalledWith(null)
  })

  it('keeps a saved value when the initial read finishes later', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date('2026-11-06T21:00:00Z'))
    let finishRead!: (value: NotificationDnd) => void
    const initialRead = new Promise<NotificationDnd>(resolve => { finishRead = resolve })
    vi.mocked(api.notificationDnd).mockReturnValueOnce(initialRead)
    const { qc } = renderRow()
    expect(qc.getQueryState(NOTIFICATION_DND_KEY)?.fetchStatus).toBe('fetching')
    const fetching = qc.getQueryCache().find({ queryKey: NOTIFICATION_DND_KEY })!.promise

    choose(/^For 1 hour$/)
    await waitFor(() => expect(select().textContent).toMatch(/^On until /))
    const saved = { until: new Date('2026-11-06T22:00:00Z').getTime() / 1000 }
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toEqual(saved)

    await act(async () => {
      finishRead({ until: null })
      await initialRead
      await Promise.allSettled([fetching])
    })
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toEqual(saved)
    expect(select().textContent).toMatch(/^On until /)
  })

  it('reads Off again by itself once the end time passes', async () => {
    const start = new Date(2026, 10, 6, 21, 0).getTime()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(start)
    dnd.until = start / 1000 + 3600
    renderRow()
    await waitFor(() => expect(select().textContent).toMatch(/^On until /))
    act(() => { vi.advanceTimersByTime(3_600_000) })
    expect(select().textContent).toBe('Off')
  })

  it('says the state is unknown and offers no choice when the read fails', async () => {
    vi.mocked(api.notificationDnd).mockRejectedValueOnce(new Error('500'))
    renderRow()
    expect(await screen.findByText(/Couldn't read Do not disturb/)).toBeTruthy()
    expect(select().hasAttribute('data-disabled')).toBe(true)
    expect(select().textContent).toBe('Unknown')
  })

  it('shows an unknown state and disables the select when a refetch fails with cached Off', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date('2026-11-06T21:00:00Z'))
    const { qc } = renderRow()
    await waitFor(() => expect(qc.getQueryState(NOTIFICATION_DND_KEY)?.status).toBe('success'))
    expect(select().textContent).toBe('Off')
    expect(select().hasAttribute('data-disabled')).toBe(false)

    vi.mocked(api.notificationDnd).mockRejectedValueOnce(new Error('500'))
    await act(async () => { await qc.invalidateQueries({ queryKey: NOTIFICATION_DND_KEY }) })

    expect(await screen.findByText(/Couldn't read Do not disturb/)).toBeTruthy()
    expect(select().hasAttribute('data-disabled')).toBe(true)
    expect(select().textContent).toBe('Unknown')
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toEqual({ until: null })
  })

  it('shows an error and keeps the gateway state when the save fails', async () => {
    dnd.fail = true
    renderRow()
    await waitFor(() => expect(select().textContent).toBe('Off'))
    choose(/^For 1 hour$/)
    expect(await screen.findByText(/Couldn't change Do not disturb/)).toBeTruthy()
    expect(select().textContent).toBe('Off')
  })
})

describe('dndEndMs', () => {
  it('reads only a future, finite end time', () => {
    expect(dndEndMs({ until: 100 }, 99_000)).toBe(100_000)
    expect(dndEndMs({ until: 100 }, 100_000)).toBeNull()
    expect(dndEndMs({ until: null }, 0)).toBeNull()
    expect(dndEndMs({ until: Number.NaN }, 0)).toBeNull()
    expect(dndEndMs(undefined, 0)).toBeNull()
  })
})
