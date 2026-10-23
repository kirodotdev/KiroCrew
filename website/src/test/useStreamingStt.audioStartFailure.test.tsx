/**
 * The two ways the audio graph can fail to start say two different things.
 *
 * Starting a dictation loads the capture module with `audioWorklet.addModule`
 * and then resumes the `AudioContext`. They fail for unrelated reasons: the
 * module is a download from the dashboard server, so its failure is about the
 * connection, while a refused resume is the browser declining to start audio
 * at all, after the module loaded fine. One shared message tells the user
 * nothing about which of the two happened, and so nothing about what to do.
 *
 * Each case is asserted against the exact catalog string, so a message that
 * drifts back to a shared one, or to a key that does not resolve, is caught.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { i18nT } from '../i18n/t'

/**
 * How long a test waits for the hook's start-up chain (microphone, socket,
 * module load, resume) to reach the reported error. Every step is a mock that
 * settles on a microtask or a zero-delay timer, so this only bounds a slow
 * worker under a full parallel run.
 */
const START_FAILURE_WAIT = { timeout: 5000 }

const MODULE_FAILED = 'hooks.useStreamingStt.audio_module_load_failed'
const START_FAILED = 'hooks.useStreamingStt.audio_start_failed'
const CONNECTION_LOST = 'hooks.useStreamingStt.stt_connection_lost'

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

/** Every socket the hook opened, newest last. */
const sockets: MockSocket[] = []

class MockSocket {
  constructor() { sockets.push(this) }
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  readyState = 1
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  send() {}
  close() {
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }
}

class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  connect() {}
  disconnect() {}
}

/** What the next AudioContext does at each start-up step. */
const audio = {
  addModule: (): Promise<void> => Promise.resolve(),
  state: 'running' as AudioContextState,
  resume: (): Promise<void> => Promise.resolve(),
}

class MockAudioContext {
  audioWorklet = { addModule: () => audio.addModule() }
  get state() { return audio.state }
  resume() { return audio.resume() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  trackStops.length = 0
  sockets.length = 0
  audio.addModule = () => Promise.resolve()
  audio.state = 'running'
  audio.resume = () => Promise.resolve()
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(makeStream()), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
})

afterEach(() => { vi.unstubAllGlobals() })

async function startAndCollectError() {
  const { useStreamingStt } = await import('../hooks/useStreamingStt')
  const onError = vi.fn()
  const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn(), onError }))
  let started: boolean | undefined
  await act(async () => { started = await hook.result.current.start() })
  await waitFor(() => expect(onError).toHaveBeenCalledTimes(1), START_FAILURE_WAIT)
  return { hook, onError, started }
}

describe('audio start-up failures', () => {
  it('blames the download, not the browser, when the capture module fails to load', async () => {
    audio.addModule = () => Promise.reject(new Error('fetch failed'))

    const { hook, onError, started } = await startAndCollectError()

    expect(onError).toHaveBeenCalledWith(i18nT(MODULE_FAILED))
    expect(started).toBe(false)
    expect(hook.result.current.recording).toBe(false)
    // The microphone the start acquired is released, not left live behind an error.
    expect(trackStops.at(-1)).toHaveBeenCalled()
  })

  it('blames the browser, not the connection, when the context refuses to resume', async () => {
    audio.state = 'suspended'
    audio.resume = () => Promise.reject(new DOMException('not allowed', 'NotAllowedError'))

    const { hook, onError, started } = await startAndCollectError()

    expect(onError).toHaveBeenCalledWith(i18nT(START_FAILED))
    expect(started).toBe(false)
    expect(hook.result.current.recording).toBe(false)
    expect(trackStops.at(-1)).toHaveBeenCalled()
  })

  it('keeps the socket\'s own error when the server ends the session during the module load', async () => {
    // The server closes /api/ws/stt while addModule is pending (a provider whose
    // session failed to start). onclose reports that and cleanup() closes the
    // context, which is what rejects the load. That rejection is a consequence,
    // not a download failure, so it must not replace the real message.
    audio.addModule = () => {
      sockets.at(-1)?.close()
      return Promise.reject(new DOMException('context closed', 'AbortError'))
    }

    const { onError, started } = await startAndCollectError()
    // Let the rejected load settle; a second report would land here.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

    expect(started).toBe(false)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(i18nT(CONNECTION_LOST))
    expect(onError).not.toHaveBeenCalledWith(i18nT(MODULE_FAILED))
  })

  it('keeps the two messages distinct and resolved in the English catalog', () => {
    const moduleText = i18nT(MODULE_FAILED)
    const startText = i18nT(START_FAILED)
    // An unresolved key comes back as the key itself, which would render as
    // a dotted identifier in the voice status bar.
    expect(moduleText).not.toBe(MODULE_FAILED)
    expect(startText).not.toBe(START_FAILED)
    expect(moduleText).not.toBe(startText)
  })
})
