/**
 * The "really cancels" half of the drain-exit rule.
 *
 * `ChatInput.drainCancelAffordance.test.tsx` asserts that a discard control is on
 * screen for exactly the window that can be discarded. That is only worth
 * anything if the discard it calls genuinely ends the session, which is what this
 * file holds: the socket closes, the microphone claim goes, a transcript arriving
 * afterwards is dropped rather than committed, and the progress line the strip was
 * showing goes with it.
 *
 * Harness shared in shape with useStreamingStt.stopBeforeReady.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

vi.mock('../hooks/mic', () => ({
  acquireMicStream: () => Promise.resolve(makeStream()),
  humanizeMicError: (e: unknown) => String(e),
  createLevelMeter: () => () => {},
  setPreferredMicId: () => {},
  activeDeviceId: () => 'dev-1',
}))

const trackStops: ReturnType<typeof vi.fn>[] = []
function makeStream() {
  const track = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic', getSettings: () => ({ deviceId: 'dev-1' }) }
  trackStops.push(track.stop)
  return { getAudioTracks: () => [track], getTracks: () => [track] }
}

const sockets: MockSocket[] = []
const lastSocket = () => sockets[sockets.length - 1]

class MockSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  readyState = 1
  binaryType = ''
  sent: unknown[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() { sockets.push(this); setTimeout(() => this.onopen?.(), 0) }
  send(payload: unknown) { this.sent.push(payload) }
  close() {
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    this.closed = true
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }
  becomeReady(fields: Record<string, unknown> = {}) { this.onmessage?.({ data: JSON.stringify({ type: 'ready', ...fields }) }) }
  /** A final transcript the backend delivers after the stop frame. */
  deliverFinal(text: string) { this.onmessage?.({ data: JSON.stringify({ type: 'final', text }) }) }
}

const nodes: MockWorkletNode[] = []
const lastNode = () => nodes[nodes.length - 1]
class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  constructor() { nodes.push(this) }
  connect() {}
  disconnect() {}
  speak(bytes = 640) { this.port.onmessage?.({ data: new ArrayBuffer(bytes) }) }
}

class MockAudioContext {
  audioWorklet = { addModule: () => Promise.resolve() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  trackStops.length = 0
  sockets.length = 0
  nodes.length = 0
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(makeStream()), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

async function startRecording() {
  const { useStreamingStt } = await import('../hooks/useStreamingStt')
  const onFinal = vi.fn()
  const onDownload = vi.fn()
  const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal, onDownload }))
  await act(async () => { hook.result.current.start() })
  await waitFor(() => expect(lastNode()).toBeTruthy())
  return { hook, onFinal, onDownload }
}

/** Release before ready: capture ends, the socket is held, `draining` is up. */
async function enterDrain() {
  const started = await startRecording()
  const ws = lastSocket()
  await act(async () => { lastNode().speak() })
  await act(async () => { started.hook.result.current.stop() })
  expect(started.hook.result.current.recording, 'release clears recording').toBe(false)
  expect(started.hook.result.current.draining, 'release raises draining').toBe(true)
  return { ...started, ws }
}

describe('a drain in flight is really cancellable', () => {
  it('cancel() during the drain closes the socket and clears draining', async () => {
    const { hook, ws } = await enterDrain()
    await act(async () => { hook.result.current.cancel() })
    expect(ws.closed, 'the socket must actually close').toBe(true)
    expect(hook.result.current.draining).toBe(false)
    expect(hook.result.current.recording).toBe(false)
  })

  it('cancel() during the drain releases the microphone claim', async () => {
    const { hook } = await enterDrain()
    await act(async () => { hook.result.current.cancel() })
    expect(trackStops.some(s => s.mock.calls.length > 0), 'mic tracks must be stopped').toBe(true)
  })

  it('a final arriving after cancel() is discarded, not delivered', async () => {
    const { hook, ws, onFinal } = await enterDrain()
    await act(async () => { hook.result.current.cancel() })
    await act(async () => { ws.deliverFinal('the abandoned utterance') })
    expect(onFinal, 'a cancelled drain must commit nothing').not.toHaveBeenCalled()
  })

  it('cancel() clears the progress line it was showing', async () => {
    const { hook, ws, onDownload } = await enterDrain()
    await act(async () => { ws.onmessage?.({ data: JSON.stringify({ type: 'status', stage: 'downloading', done: 4, total: 10 }) }) })
    onDownload.mockClear()
    await act(async () => { hook.result.current.cancel() })
    expect(onDownload).toHaveBeenCalledWith(null)
  })
})
