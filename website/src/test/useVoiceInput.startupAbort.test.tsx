import { renderHook, act } from '@testing-library/react'

/* A slot switch that lands while a streaming startup is still awaiting the
 * microphone.
 *
 * Ownership of a voice session is assigned AFTER `start()` resolves, so in this
 * window no composer owns the session. Ending the capture with a stop would open
 * a drain in that state: the transcribing flag refuses dictation in every slot,
 * the drain is reported to none of them because each gates on ownership, and the
 * only gesture that ends it lives in the composer that owns the capture. So the
 * abort has to DISCARD -- which is also what the user asked for by leaving. */

const streamStop = vi.fn()
const streamCancel = vi.fn()
let resolveStart: (v: boolean) => void = () => {}

vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: true,
  useStreamingStt: () => ({
    recording: false,
    draining: false,
    partial: '',
    download: null,
    start: vi.fn(() => new Promise<boolean>(r => { resolveStart = r })),
    stop: streamStop,
    cancel: streamCancel,
    switchDevice: vi.fn(),
    deviceSwitchIsLive: false,
  }),
}))

vi.mock('../api/client', () => ({ api: { sttTranscribe: vi.fn().mockResolvedValue({ text: '' }) } }))

beforeEach(() => {
  streamStop.mockClear()
  streamCancel.mockClear()
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue({ getAudioTracks: () => [], getTracks: () => [] }) },
    configurable: true,
    writable: true,
  })
  vi.resetModules()
})

afterEach(() => { vi.restoreAllMocks() })

describe('useVoiceInput — a slot switch during a streaming startup', () => {
  it('discards the session instead of leaving a drain no composer owns', async () => {
    const { useVoiceInput } = await import('../hooks/useVoiceInput')
    const slot = { current: 'slot-a' }
    const { result, rerender } = renderHook(
      () => useVoiceInput(vi.fn(), { sessionId: slot.current, streaming: true }),
    )

    // The press begins and parks on the microphone.
    act(() => { void result.current.start() })
    // The user leaves for another chat while it is still parked.
    slot.current = 'slot-b'
    rerender()
    // Only now does the microphone answer.
    await act(async () => { resolveStart(true); await Promise.resolve() })

    expect(streamCancel).toHaveBeenCalledTimes(1)
    expect(streamStop).not.toHaveBeenCalled()
    // Nothing claimed the session, which is the whole reason a drain here would
    // have had no exit.
    expect(result.current.sessionOwner).toBeNull()
  })
})
