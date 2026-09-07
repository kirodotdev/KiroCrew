import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const connectInstance = vi.fn()
vi.mock('../api/client', () => ({ api: { connectInstance: (...a: unknown[]) => connectInstance(...a) } }))

import { connectInstanceInto } from './connectInstance'
import { setWarm } from '../store/instancesSlice'

function captureInfo() {
  const lines: string[] = []
  vi.spyOn(console, 'info').mockImplementation((...args: unknown[]) => { lines.push(args.join(' ')) })
  return lines
}

describe('connectInstanceInto pane journal', () => {
  beforeEach(() => { connectInstance.mockReset() })
  afterEach(() => { vi.restoreAllMocks() })

  it('journals a live connect as `warm` with the caller tag and never the token', async () => {
    const lines = captureInfo()
    const dispatch = vi.fn()
    connectInstance.mockResolvedValue({ state: 'connected', local_port: 7781, token: 'supersecret' })
    await connectInstanceInto(dispatch as never, 'shizuka', 'auto-connect')
    expect(dispatch).toHaveBeenCalledWith(setWarm({ id: 'shizuka', conn: { port: 7781, token: 'supersecret' } }))
    expect(lines).toEqual(['[pane] warm id=shizuka port=7781 via=auto-connect'])
    expect(lines.join('\n')).not.toContain('supersecret')
  })

  it('journals a connected-but-unusable response as `warm-declined` and leaves warm untouched', async () => {
    const lines = captureInfo()
    const dispatch = vi.fn()
    connectInstance.mockResolvedValue({ state: 'connected', local_port: 0, token: '', error: 'x' })
    await connectInstanceInto(dispatch as never, 'shizuka')
    expect(dispatch).not.toHaveBeenCalled()
    expect(lines).toEqual([
      '[pane] warm-declined id=shizuka via=select state=connected hasPort=false hasToken=false error=x',
    ])
  })

  it('journals a rejection as `warm-failed` and re-throws it unchanged', async () => {
    const lines = captureInfo()
    const err = new Error('502 tunnel down')
    connectInstance.mockRejectedValue(err)
    await expect(connectInstanceInto(vi.fn() as never, 'shizuka', 'auto-connect')).rejects.toBe(err)
    expect(lines).toEqual(['[pane] warm-failed id=shizuka via=auto-connect error="502 tunnel down"'])
  })
})
