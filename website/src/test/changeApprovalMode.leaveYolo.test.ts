import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, chatMode: vi.fn().mockResolvedValue({}) } }
})

import { api } from '../api/client'
import { changeApprovalMode, sseYolo } from '../store/dashboardSlice'
import { createTestStore } from './helpers'

/** A fake gateway: `yolo` turns the app-wide setting on, `normal` turns it off,
 *  and a slot-scoped `trust` / `trust_reads` leaves it as it was. */
function fakeGateway() {
  let yolo = false
  vi.mocked(api.chatMode).mockImplementation(async (mode: string) => {
    if (mode === 'yolo') yolo = true
    else if (mode === 'normal') yolo = false
    return {} as never
  })
  return { yolo: () => yolo }
}

describe('changeApprovalMode leaving YOLO', () => {
  beforeEach(() => {
    vi.mocked(api.chatMode).mockReset()
  })

  it.each(['trust', 'trust_reads'])(
    'switching from YOLO to %s turns the app-wide setting off and stays off',
    async (mode) => {
      const gw = fakeGateway()
      const store = createTestStore()
      await store.dispatch(changeApprovalMode({ mode: 'yolo', slot: 'dashboard:1' }))
      expect(store.getState().dashboard.approvalMode).toBe('yolo')

      await store.dispatch(changeApprovalMode({ mode, slot: 'dashboard:1' }))
      expect(api.chatMode).toHaveBeenLastCalledWith(mode, 'dashboard:1')
      expect(gw.yolo()).toBe(false)

      // The next status frame reports the gateway's real setting.
      store.dispatch(sseYolo(gw.yolo()))
      expect(store.getState().dashboard.approvalMode).toBe(mode)
    },
  )

  it('a Trust pick that does not start from YOLO sends one request', async () => {
    fakeGateway()
    const store = createTestStore()
    await store.dispatch(changeApprovalMode({ mode: 'trust', slot: 'dashboard:1' }))
    expect(vi.mocked(api.chatMode).mock.calls).toEqual([['trust', 'dashboard:1']])
  })
})
