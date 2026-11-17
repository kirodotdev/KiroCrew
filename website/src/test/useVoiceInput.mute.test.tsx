/**
 * Mic mute on a batch recording.
 *
 * The recorder pauses while muted, so the muted stretch is left out of the file
 * rather than handed to the transcriber as silence. A paused recorder is still
 * a live microphone, so every path that ends a recording has to treat it as one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, muted: false, setMuted: vi.fn(), start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))

const sttTranscribe = vi.fn().mockResolvedValue({ text: 'hello world' })
vi.mock('../api/client', () => ({ api: { sttTranscribe: (...a: unknown[]) => sttTranscribe(...a) } }))

interface FakeTrack { stop: ReturnType<typeof vi.fn>; enabled: boolean; readyState: string; label: string }
let trackless = false
function makeStream() {
  const track: FakeTrack = { stop: vi.fn(), enabled: true, readyState: 'live', label: 'Mock Mic' }
  return { _track: track, getAudioTracks: () => (trackless ? [] : [track]), getTracks: () => [track] }
}

const recorders: MockMediaRecorder[] = []
const lastRecorder = () => recorders[recorders.length - 1]
class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' | 'paused' = 'inactive'
  stream: ReturnType<typeof makeStream>
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(stream: ReturnType<typeof makeStream>) { this.stream = stream; recorders.push(this) }
  start() { this.state = 'recording' }
  pause() { this.state = 'paused' }
  resume() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
  feed(bytes = 200) { this.ondataavailable?.({ data: new Blob(['x'.repeat(bytes)]) }) }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return { fftSize: 0, frequencyBinCount: 16, getByteTimeDomainData() {}, getByteFrequencyData() {}, connect() {} }
  }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  recorders.length = 0
  trackless = false
  sttTranscribe.mockClear()
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockImplementation(() => Promise.resolve(makeStream())), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
  vi.stubGlobal('MediaRecorder', MockMediaRecorder as unknown as typeof MediaRecorder)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function recording() {
  const { useVoiceInput } = await import('../hooks/useVoiceInput')
  const hook = renderHook(() => useVoiceInput(vi.fn()))
  await act(async () => { await hook.result.current.start() })
  await waitFor(() => expect(hook.result.current.recording).toBe(true))
  return hook
}

describe('batch dictation mute', () => {
  it('pauses the recorder and disables the track, then resumes both', async () => {
    const hook = await recording()
    const mr = lastRecorder()
    await act(async () => { expect(hook.result.current.setMuted(true)).toBe(true) })
    expect(mr.state).toBe('paused')
    expect(mr.stream._track.enabled).toBe(false)
    expect(hook.result.current.muted).toBe(true)
    expect(hook.result.current.recording).toBe(true)
    await act(async () => { hook.result.current.setMuted(false) })
    expect(mr.state).toBe('recording')
    expect(mr.stream._track.enabled).toBe(true)
    expect(hook.result.current.muted).toBe(false)
  })

  it('stopping while muted ends the recording and transcribes what came before', async () => {
    const hook = await recording()
    const mr = lastRecorder()
    mr.feed()
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { hook.result.current.stop() })
    expect(mr.state).toBe('inactive')
    expect(mr.stream._track.stop).toHaveBeenCalled()
    await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(1))
    expect(hook.result.current.muted).toBe(false)
  })

  it('cancelling while muted discards without transcribing', async () => {
    const hook = await recording()
    const mr = lastRecorder()
    mr.feed()
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { hook.result.current.cancel() })
    expect(mr.state).toBe('inactive')
    expect(sttTranscribe).not.toHaveBeenCalled()
  })

  it('stops the recording when there is no track to mute', async () => {
    const hook = await recording()
    const mr = lastRecorder()
    trackless = true
    let ok = true
    await act(async () => { ok = hook.result.current.setMuted(true) })
    expect(ok).toBe(false)
    expect(mr.state).toBe('inactive')
    expect(hook.result.current.recording).toBe(false)
  })

  it('does nothing with no recording', async () => {
    const { useVoiceInput } = await import('../hooks/useVoiceInput')
    const hook = renderHook(() => useVoiceInput(vi.fn()))
    expect(hook.result.current.setMuted(true)).toBe(false)
    expect(hook.result.current.muted).toBe(false)
  })
})
