vi.mock('../../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    status: vi.fn(),
  },
}))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../../test/helpers'
import { api } from '../../api/client'
import { sseStatus } from '../../store/dashboardSlice'
import type { StatusData } from '../../types'
import { SecurityPanel } from './SecurityPanel'

const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>

/** A status reply shaped like the HTTP `/api/status` answer, which is the only
 *  status source that carries `yolo_duration`. */
const httpStatus = (yolo_duration: StatusData['yolo_duration']) => ({
  uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo: false, yolo_duration,
}) as StatusData
/** The 5-second WebSocket `dashboard` frame: no duration key at all. */
const wsFrame = () => ({ uptime: '2h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }) as StatusData

async function saveDuration(label: RegExp, token: string) {
  fireEvent.click(await screen.findByRole('radio', { name: label }))
  await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.yolo_duration', token))
}

/** The approval-mode picker reads the configured duration from
 *  `dashboard.status`. Saving a new duration here must move that value at once
 *  and keep it there, or the picker names the page-load duration until reload
 *  while the gateway already grants the saved one. */
describe('YoloDurationCard save updates dashboard.status.yolo_duration', () => {
  beforeEach(() => {
    mocked(api.deniedCommands).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    mocked(api.governancePolicy).mockRejectedValue(new Error('no policy'))
    mocked(api.securityPosture).mockRejectedValue(new Error('no posture'))
    mocked(api.tailnetStatus).mockRejectedValue(new Error('no tailnet'))
    mocked(api.status).mockRejectedValue(new Error('probe off'))
    mocked(api.kirocrewConfig).mockResolvedValue({ agent: { yolo_duration: '30m' } })
    mocked(api.patchConfig).mockResolvedValue({})
  })

  it('a successful save names the new duration at once, with no status round trip', async () => {
    const store = createTestStore()
    store.dispatch(sseStatus(httpStatus('30m')))
    renderWithProviders(<SecurityPanel />, { route: '/?section=approval', store })

    await saveDuration(/24 hours/, '24h')

    expect(store.getState().dashboard.status?.yolo_duration).toBe('24h')
  })

  it('a status reply that was in flight before the save cannot roll it back', async () => {
    const store = createTestStore()
    store.dispatch(sseStatus(httpStatus('30m')))
    renderWithProviders(<SecurityPanel />, { route: '/?section=approval', store })

    await saveDuration(/24 hours/, '24h')
    // The boot read (or an earlier request) resolving late with the old token.
    store.dispatch(sseStatus(httpStatus('30m')))
    store.dispatch(sseStatus(wsFrame()))

    expect(store.getState().dashboard.status?.yolo_duration).toBe('24h')
  })

  it('successive saves settle on the last one', async () => {
    const store = createTestStore()
    store.dispatch(sseStatus(httpStatus('30m')))
    renderWithProviders(<SecurityPanel />, { route: '/?section=approval', store })

    await saveDuration(/24 hours/, '24h')
    mocked(api.kirocrewConfig).mockResolvedValue({ agent: { yolo_duration: '24h' } })
    await saveDuration(/12 hours/, '12h')
    store.dispatch(sseStatus(httpStatus('24h')))

    expect(store.getState().dashboard.status?.yolo_duration).toBe('12h')
  })

  it('a save made before any status arrived is applied when the first status lands', async () => {
    const store = createTestStore()
    expect(store.getState().dashboard.status).toBeNull()
    renderWithProviders(<SecurityPanel />, { route: '/?section=approval', store })

    await saveDuration(/24 hours/, '24h')
    store.dispatch(sseStatus(wsFrame()))

    expect(store.getState().dashboard.status?.yolo_duration).toBe('24h')
  })

  it('a failed save leaves the stored duration alone', async () => {
    mocked(api.patchConfig).mockRejectedValue(new Error('nope'))
    const store = createTestStore()
    store.dispatch(sseStatus(httpStatus('30m')))
    renderWithProviders(<SecurityPanel />, { route: '/?section=approval', store })

    fireEvent.click(await screen.findByRole('radio', { name: /24 hours/ }))
    await screen.findByText('Could not save the duration.')
    store.dispatch(sseStatus(wsFrame()))

    expect(store.getState().dashboard.status?.yolo_duration).toBe('30m')
  })
})
