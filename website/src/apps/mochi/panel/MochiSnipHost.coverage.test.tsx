import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { captureScreen, isScreenSnipSupported } from '../../../hooks/useScreenSnip'
import { deliverCapture, onCaptureRequested } from '../src/mochiApi'
import { reportStat } from './panelBridge'
import { MochiSnipHost } from './MochiSnipHost'

vi.mock('../../../components/SnipOverlay', () => ({
  default: ({ onComplete, onCancel }: { onComplete: (f: File) => void; onCancel: () => void }) => (
    <div>
      <button onClick={() => onComplete(new File(['x'], 'shot.png', { type: 'image/png' }))}>done</button>
      <button onClick={onCancel}>cancel</button>
    </div>
  ),
}))
vi.mock('../../../hooks/useScreenSnip', () => ({ captureScreen: vi.fn(), isScreenSnipSupported: vi.fn() }))
vi.mock('../src/mochiApi', () => ({ deliverCapture: vi.fn(), onCaptureRequested: vi.fn() }))
vi.mock('./panelBridge', () => ({ reportStat: vi.fn() }))

type Shell = { onStartSnip?: (cb: () => void) => () => void; onSnipDelivered?: (cb: (b: string) => void) => () => void }
const setShell = (s: Shell | undefined) => {
  ;(window as unknown as { mochi?: Shell }).mochi = s
}
const canvas = () => document.createElement('canvas')
let panelTrigger: () => void = () => {}
let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isScreenSnipSupported).mockReturnValue(true)
  vi.mocked(captureScreen).mockResolvedValue(canvas())
  vi.mocked(onCaptureRequested).mockImplementation((cb: () => void) => {
    panelTrigger = cb
    return vi.fn()
  })
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => {
  setShell(undefined)
  warn.mockRestore()
})

describe('MochiSnipHost shell delivery', () => {
  it('forwards a crop the shell delivers, and ignores an empty one', () => {
    let deliver: (b: string) => void = () => {}
    const off = vi.fn()
    setShell({ onSnipDelivered: (cb) => { deliver = cb; return off } })
    const { unmount } = render(<MochiSnipHost />)

    act(() => deliver(''))
    expect(deliverCapture).not.toHaveBeenCalled()
    act(() => deliver('QUJD'))
    expect(deliverCapture).toHaveBeenCalledWith('QUJD')
    expect(reportStat).toHaveBeenCalledWith('screenshot')
    unmount()
    expect(off).toHaveBeenCalled()
  })
})

describe('MochiSnipHost in-panel capture', () => {
  it('captures from the shell shortcut, crops, and delivers the base64 of the file', async () => {
    let start: () => void = () => {}
    setShell({ onStartSnip: (cb) => { start = cb; return () => {} } })
    render(<MochiSnipHost />)

    await act(async () => start())
    fireEvent.click(await screen.findByRole('button', { name: 'done' }))

    await waitFor(() => expect(deliverCapture).toHaveBeenCalledWith('eA=='))
    expect(reportStat).toHaveBeenCalledWith('screenshot')
    expect(screen.queryByRole('button', { name: 'done' })).toBeNull()
  })

  it('dismisses the overlay on cancel without delivering', async () => {
    render(<MochiSnipHost />)
    await act(async () => panelTrigger())
    fireEvent.click(await screen.findByRole('button', { name: 'cancel' }))
    expect(screen.queryByRole('button', { name: 'cancel' })).toBeNull()
    expect(deliverCapture).not.toHaveBeenCalled()
  })

  it('does not start a second capture while one frame is up', async () => {
    render(<MochiSnipHost />)
    await act(async () => panelTrigger())
    await screen.findByRole('button', { name: 'done' })
    await act(async () => panelTrigger())
    expect(captureScreen).toHaveBeenCalledTimes(1)
  })

  it('warns and skips when screen capture is not supported', async () => {
    vi.mocked(isScreenSnipSupported).mockReturnValue(false)
    render(<MochiSnipHost />)
    await act(async () => panelTrigger())
    expect(captureScreen).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('unavailable'))
  })

  it('warns when the capture yields no frame, and when it throws', async () => {
    render(<MochiSnipHost />)
    vi.mocked(captureScreen).mockResolvedValueOnce(null)
    await act(async () => panelTrigger())
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('no frame'))

    const reason = new Error('permission denied')
    vi.mocked(captureScreen).mockRejectedValueOnce(reason)
    await act(async () => panelTrigger())
    expect(warn).toHaveBeenCalledWith('[mochi] screen capture failed', reason)
    expect(screen.queryByRole('button', { name: 'done' })).toBeNull()
  })
})
