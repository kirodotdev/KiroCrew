import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: { notificationDnd: vi.fn() },
}))

import { api } from '../api/client'
import { NOTIFICATION_DND_KEY } from '../hooks/useNotificationDnd'
import { fmtTime } from '../i18n/format'
import { DoNotDisturbBadge } from '../shell/topbar/shellHeader'
import { isGatewayConnected, setGatewayConnected } from '../utils/errorReport'

function renderBadge() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(<QueryClientProvider client={qc}><DoNotDisturbBadge /></QueryClientProvider>)
  onTestFinished(() => { view.unmount(); qc.clear() })
  return { ...view, qc }
}

describe('DoNotDisturbBadge', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date('2026-11-06T21:00:00Z'))
    vi.mocked(api.notificationDnd).mockReset().mockResolvedValue({ until: null })
    const connected = isGatewayConnected()
    setGatewayConnected(true)
    onTestFinished(() => { setGatewayConnected(connected) })
  })

  it('shows a compact failure notice with agent hand-off when the read fails', async () => {
    vi.mocked(api.notificationDnd).mockRejectedValue(new Error('500'))
    renderBadge()

    const notice = await screen.findByRole('status')
    const message = within(notice).getByText("Do not disturb: couldn't read")
    expect(message.classList.contains('text-[11px]')).toBe(true)
    expect(notice.classList.contains('inline-flex')).toBe(true)
    expect(notice.parentElement?.classList.contains('hidden')).toBe(true)
    expect(notice.parentElement?.classList.contains('md:flex')).toBe(true)
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
    expect(screen.queryByTestId('dnd-badge')).toBeNull()
  })

  it('shows only the end time for an active setting, with its full accessible label', async () => {
    const endMs = new Date('2026-11-06T22:00:00Z').getTime()
    vi.mocked(api.notificationDnd).mockResolvedValue({ until: endMs / 1000 })
    renderBadge()

    const badge = await screen.findByRole('status', { name: `Do not disturb until ${fmtTime(endMs)}` })
    expect(badge.textContent).toBe(fmtTime(endMs))
    expect(badge.title).toBe(`Do not disturb until ${fmtTime(endMs)}`)
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).toBeNull()
  })

  it('replaces the cached active end time with a compact notice when a refetch fails', async () => {
    const endMs = new Date('2026-11-06T22:00:00Z').getTime()
    vi.mocked(api.notificationDnd).mockResolvedValueOnce({ until: endMs / 1000 })
    const { qc } = renderBadge()
    expect((await screen.findByTestId('dnd-badge')).textContent).toBe(fmtTime(endMs))

    vi.mocked(api.notificationDnd).mockRejectedValueOnce(new Error('500'))
    await act(async () => { await qc.invalidateQueries({ queryKey: NOTIFICATION_DND_KEY }) })

    const message = await screen.findByText("Do not disturb: couldn't read")
    const notice = screen.getByRole('status')
    expect(message.classList.contains('text-[11px]')).toBe(true)
    expect(notice.classList.contains('inline-flex')).toBe(true)
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
    expect(screen.queryByTestId('dnd-badge')).toBeNull()
    expect(screen.queryByText(fmtTime(endMs))).toBeNull()
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toEqual({ until: endMs / 1000 })
  })

  it('renders nothing after reading a confirmed Off setting', async () => {
    const { qc, container } = renderBadge()
    await waitFor(() => expect(qc.getQueryState(NOTIFICATION_DND_KEY)?.status).toBe('success'))
    expect(container.childElementCount).toBe(0)
  })
})
