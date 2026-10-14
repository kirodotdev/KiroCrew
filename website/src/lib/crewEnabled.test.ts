import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({ api: { updateInstance: vi.fn() } }))
vi.mock('./connectInstance', () => ({ connectInstanceInto: vi.fn() }))
import { api } from '../api/client'
import { connectInstanceInto } from './connectInstance'
import { removeWarm } from '../store/instancesSlice'
import { setCrewEnabled } from './crewEnabled'

beforeEach(() => vi.clearAllMocks())

describe('setCrewEnabled', () => {
  it('drops the warm pane once the gateway accepts the disable', async () => {
    vi.mocked(api.updateInstance).mockResolvedValue({} as never)
    const dispatch = vi.fn()
    await setCrewEnabled(dispatch, 'a', false)
    expect(api.updateInstance).toHaveBeenCalledWith('a', { disabled: true })
    expect(dispatch).toHaveBeenCalledWith(removeWarm('a'))
    expect(connectInstanceInto).not.toHaveBeenCalled()
  })

  it('keeps the open pane when the disable is refused', async () => {
    vi.mocked(api.updateInstance).mockRejectedValue(new Error('tunnel_teardown_failed'))
    const dispatch = vi.fn()
    await expect(setCrewEnabled(dispatch, 'a', false)).rejects.toThrow('tunnel_teardown_failed')
    expect(dispatch).not.toHaveBeenCalled()
  })

  it('reconnects after enabling', async () => {
    vi.mocked(api.updateInstance).mockResolvedValue({} as never)
    const dispatch = vi.fn()
    await setCrewEnabled(dispatch, 'a', true)
    expect(api.updateInstance).toHaveBeenCalledWith('a', { disabled: false })
    expect(connectInstanceInto).toHaveBeenCalledWith(dispatch, 'a')
  })
})
