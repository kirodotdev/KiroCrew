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

  it('enables but does not dial a crew whose transport this build does not map', async () => {
    // The gateway would dial an unrecognised method over SSH, which is exactly
    // the mislabel the unmapped-transport guard exists to stop.
    vi.mocked(api.updateInstance).mockResolvedValue({ id: 'a', connection_method: 'outbound' } as never)
    const dispatch = vi.fn()
    await setCrewEnabled(dispatch, 'a', true)
    expect(api.updateInstance).toHaveBeenCalledWith('a', { disabled: false })
    expect(connectInstanceInto).not.toHaveBeenCalled()
  })

  it.each(['ssh', 'ssm', 'fargate', ''])('still reconnects a mapped %s crew on enable', async method => {
    vi.mocked(api.updateInstance).mockResolvedValue({ id: 'a', connection_method: method } as never)
    const dispatch = vi.fn()
    await setCrewEnabled(dispatch, 'a', true)
    expect(connectInstanceInto).toHaveBeenCalledWith(dispatch, 'a')
  })
})
