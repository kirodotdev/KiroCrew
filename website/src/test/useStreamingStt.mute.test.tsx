/**
 * Mic mute on a streaming dictation.
 *
 * Muting keeps the socket fed: the recogniser closes a stream that has had no
 * audio for 15 seconds, so muted frames still go out, as zeros. These tests
 * read the bytes on the wire, because "the mute button was pressed" proves
 * nothing about what the backend heard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

vi.mock('../hooks/mic', () => ({
  acquireMicStream: () => Promise.resolve(makeStream()),
  humanizeMicError: (e: unknown) => String(e),
  createLevelMeter: () => () => {},
  setPreferredMicId: () => {},
  activeDeviceId: (s: { id?: string } | null) => s?.id ?? 'dev-1',
}))

type Track = { stop: ReturnType<typeof vi.fn>; enabled: boolean; readyState: string; label: string; getSettings: () => { deviceId: string } }
const tracks: Track[] = []
let noAudioTracks = false
let streamSeq = 0

function makeStream() {
  const id = `dev-${++streamSeq}`
  const track: Track = { stop: vi.fn(), enabled: true, readyState: 'live', label: 'Mock Mic', getSettings: () => ({ deviceId: id }) }
  tracks.push(track)
  return { id, getAudioTracks: () => (noAudioTracks ? [] : [track]), getTracks: () => [track] }
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
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    sockets.push(this)
    setTimeout(() => this.onopen?.(), 0)
  }
  send(payload: unknown) { this.sent.push(payload) }
  close() {
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }
  frame(msg: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(msg) }) }
  /** Audio frames only, each as whether it carries any non-zero byte. */
  audible() {
    return this.sent.filter((p): p is ArrayBuffer => p instanceof ArrayBuffer).map(b => new Uint8Array(b).some(x => x !== 0))
  }
}

const nodes: MockWorkletNode[] = []
const lastNode = () => nodes[nodes.length - 1]

class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  constructor() { nodes.push(this) }
  connect() {}
  disconnect() {}
  /** One frame of non-silent audio, as the worklet would emit for speech. */
  speak(bytes = 640) { this.port.onmessage?.({ data: new Uint8Array(bytes).fill(7).buffer }) }
}

class MockAudioContext {
  audioWorklet = { addModule: () => Promise.resolve() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  tracks.length = 0
  sockets.length = 0
  nodes.length = 0
  noAudioTracks = false
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(makeStream()), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
  // The stub above built a stream of its own; only capture's streams count.
  tracks.length = 0
})

afterEach(() => { vi.unstubAllGlobals() })

async function startReady(opts: { onEndpoint?: () => void; onLevel?: (v: number) => void } = {}) {
  const { useStreamingStt } = await import('../hooks/useStreamingStt')
  const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn(), ...opts }))
  await act(async () => { await hook.result.current.start() })
  await waitFor(() => expect(lastNode()).toBeTruthy())
  await act(async () => { lastSocket().frame({ type: 'ready' }) })
  return hook
}

describe('streaming dictation mute', () => {
  it('keeps sending frames while muted, as zeros, and real audio again after unmute', async () => {
    const hook = await startReady()
    const ws = lastSocket()
    await act(async () => { lastNode().speak() })
    await act(async () => { expect(hook.result.current.setMuted(true)).toBe(true) })
    expect(hook.result.current.muted).toBe(true)
    expect(tracks[0].enabled).toBe(false)
    // The browser should deliver silence, but a frame already in flight (or a
    // browser that ignores `enabled`) still carries sound. None of it may leave.
    await act(async () => { lastNode().speak(); lastNode().speak() })
    await act(async () => { hook.result.current.setMuted(false) })
    expect(tracks[0].enabled).toBe(true)
    await act(async () => { lastNode().speak() })
    expect(ws.audible()).toEqual([true, false, false, true])
  })

  it('zeros frames buffered before the server is ready, too', async () => {
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn() }))
    await act(async () => { await hook.result.current.start() })
    await waitFor(() => expect(lastNode()).toBeTruthy())
    await act(async () => { lastNode().speak() })
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { lastNode().speak() })
    await act(async () => { lastSocket().frame({ type: 'ready' }) })
    expect(lastSocket().audible()).toEqual([true, false])
  })

  it('ignores an end-of-speech verdict while muted, so silence never sends the draft', async () => {
    const onEndpoint = vi.fn()
    const hook = await startReady({ onEndpoint })
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { lastSocket().frame({ type: 'endpoint', complete: true }) })
    expect(onEndpoint).not.toHaveBeenCalled()
    await act(async () => { hook.result.current.setMuted(false) })
    await act(async () => { lastSocket().frame({ type: 'endpoint', complete: true }) })
    expect(onEndpoint).toHaveBeenCalledTimes(1)
  })

  it('zeroes the level meter on mute', async () => {
    const onLevel = vi.fn()
    const hook = await startReady({ onLevel })
    onLevel.mockClear()
    await act(async () => { hook.result.current.setMuted(true) })
    expect(onLevel).toHaveBeenLastCalledWith(0)
  })

  it('stops the dictation when there is no track to mute', async () => {
    const hook = await startReady()
    noAudioTracks = true
    let ok = true
    await act(async () => { ok = hook.result.current.setMuted(true) })
    expect(ok).toBe(false)
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.muted).toBe(false)
  })

  it('carries the mute onto a device switched to mid-dictation', async () => {
    const hook = await startReady()
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { await hook.result.current.switchDevice('dev-other') })
    expect(tracks).toHaveLength(2)
    expect(tracks[1].enabled).toBe(false)
  })

  it('does nothing without a live capture, and clears on the next session', async () => {
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const idle = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn() }))
    expect(idle.result.current.setMuted(true)).toBe(false)
    expect(idle.result.current.muted).toBe(false)

    const hook = await startReady()
    await act(async () => { hook.result.current.setMuted(true) })
    await act(async () => { hook.result.current.cancel() })
    expect(hook.result.current.muted).toBe(false)
  })
})
