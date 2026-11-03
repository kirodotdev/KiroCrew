/**
 * The crew window's effort-capability read: a dropped read is ridden out before
 * the window says it could not verify the options, and a 404 (a peer slot not
 * registered yet) is not a failure.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../../api/client', () => ({
  api: { crewPeerGet: vi.fn() },
}))

import { api } from '../../../api/client'
import { ApiError } from '../../../api/apiError'
import { useCrewWindowModelPicker } from './crewWindowModelPicker'

const peerGet = vi.mocked(api.crewPeerGet)

function renderPicker() {
  // No backoff, so the hook's own retry runs at once; nothing else retries.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const seen: boolean[] = []
  const view = renderHook(() => {
    const picker = useCrewWindowModelPicker({
      instanceId: 'peer-1',
      slotKey: 'crew-window:peer-1:s1',
      slotPath: 'api/chat/slots/s1',
      slot: { model: 'claude-opus-5-5' },
      caps: undefined,
      enabled: true,
      onWritten: vi.fn(),
    })
    seen.push(picker.effortCapsFailed)
    return picker
  }, { wrapper })
  return { ...view, seen, client }
}

const capsKey = ['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', 'claude-opus-5-5']

describe('useCrewWindowModelPicker effort-capability read', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('rides out a dropped read without flagging the effort options', async () => {
    peerGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    peerGet.mockResolvedValue({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const { result, seen } = renderPicker()

    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2), { timeout: 1000 })
    await waitFor(() => expect(result.current.chipProps).toBeTruthy())
    expect(seen).not.toContain(true)
  })

  it('does not treat a 404 (peer slot not registered yet) as a failed read', async () => {
    peerGet.mockRejectedValue(new ApiError(404, 'slot not found'))
    const { result, client } = renderPicker()

    await waitFor(() => expect(client.getQueryState(capsKey)?.status).toBe('error'))
    expect(result.current.effortCapsFailed).toBe(false)
  })

  it('still flags a real fault', async () => {
    peerGet.mockRejectedValue(new ApiError(500, 'boom'))
    const { result } = renderPicker()

    await waitFor(() => expect(result.current.effortCapsFailed).toBe(true))
  })
})
