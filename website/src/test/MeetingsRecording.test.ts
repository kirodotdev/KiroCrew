// The meeting recording socket, its level meter, and the capture wiring that feeds
// both.
//
// There is no audio device, no AudioWorklet and no real WebSocket here, so this
// splits three ways:
//   • the pure units (`classifyServerError`, `meterFill`) are called directly
//   • the socket protocol is exercised against a controllable fake WebSocket
//   • the AudioWorklet graph — which cannot be constructed in this environment at
//     all — is pinned by asserting on the shipping source, the same technique
//     MeetingsSessionLogic.test.ts uses for the dispatch and watchdog invariants

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import {
  classifyServerError,
  useMeetingRecording,
} from '../apps/meetings/hooks/useMeetingRecording'
import { meterFill } from '../apps/meetings/components/RecordingMeter'
import EN_CATALOG from '../i18n/locales/en.json'

const TranscriptionSource = readFileSync(
  'src/apps/meetings/hooks/useMeetingTranscription.ts', 'utf-8',
)
const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const WorkletSource = readFileSync('public/pcm-worklet.js', 'utf-8')

// ─── fake WebSocket ─────────────────────────────────────────────────────────

class FakeWS {
  static instances: FakeWS[] = []
  static readonly OPEN = 1
  static readonly CLOSED = 3

  url: string
  readyState = 0
  binaryType = ''
  sent: Array<string | ArrayBuffer> = []
  closed = false
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWS.instances.push(this)
  }

  send(data: string | ArrayBuffer) {
    this.sent.push(data)
  }

  close() {
    if (this.closed) return
    this.closed = true
    this.readyState = FakeWS.CLOSED
    this.onclose?.()
  }

  /** Test helper: complete the upgrade. */
  accept() {
    this.readyState = FakeWS.OPEN
    this.onopen?.()
  }

  /** Test helper: deliver a server event. */
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }

  get textFrames(): string[] {
    return this.sent.filter((f): f is string => typeof f === 'string')
  }

  get binaryFrames(): ArrayBuffer[] {
    return this.sent.filter((f): f is ArrayBuffer => typeof f !== 'string')
  }
}

const pcm = (byte: number): ArrayBuffer => new Uint8Array([byte, 0]).buffer

/** Drive the hook to a live, `ready` socket. Returns the hook result and the socket. */
async function startedRecording(onError = vi.fn()) {
  const hook = renderHook(() => useMeetingRecording({ meetingId: 'meet-1', title: 'Weekly', onError }))
  let pending: Promise<void> | undefined
  act(() => {
    pending = hook.result.current.start()
  })
  const ws = FakeWS.instances[FakeWS.instances.length - 1]
  await act(async () => {
    ws.accept()
    await pending
  })
  return { hook, ws, onError }
}

beforeEach(() => {
  FakeWS.instances = []
  vi.stubGlobal('WebSocket', FakeWS)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ─── pure units ─────────────────────────────────────────────────────────────

describe('classifyServerError', () => {
  it('separates the duration cap from a genuine failure', () => {
    // The cap is not a fault: the audio up to it IS saved, so calling it "failed"
    // would tell the user the opposite of what happened.
    expect(classifyServerError('max recording duration exceeded')).toBe('duration')
    expect(classifyServerError('session already started')).toBe('server')
    expect(classifyServerError('recording storage unavailable')).toBe('server')
    expect(classifyServerError(undefined)).toBe('server')
  })
})

describe('meterFill', () => {
  it('is zero for silence and for junk', () => {
    expect(meterFill(0)).toBe(0)
    expect(meterFill(-1)).toBe(0)
    expect(meterFill(NaN)).toBe(0)
  })

  it('lifts speech-level RMS into a visible range', () => {
    // Speech RMS sits around 0.02-0.2. Linear, that reads as a dead bar at normal
    // talking volume, which defeats the meter's only purpose.
    expect(meterFill(0.02)).toBeGreaterThan(0.2)
    expect(meterFill(0.2)).toBeGreaterThan(0.7)
  })

  it('is monotonic and clamped to 1', () => {
    expect(meterFill(0.05)).toBeLessThan(meterFill(0.1))
    expect(meterFill(1)).toBeLessThanOrEqual(1)
    expect(meterFill(50)).toBe(1)
  })
})

// ─── socket protocol ────────────────────────────────────────────────────────

describe('useMeetingRecording — the /api/ws/recording protocol', () => {
  it('connects to the recording socket, not the STT one', async () => {
    const { ws } = await startedRecording()
    expect(ws.url).toMatch(/\/api\/ws\/recording$/)
    expect(ws.binaryType).toBe('arraybuffer')
  })

  it('names the meeting in the start frame, so the WAV lands in its directory', async () => {
    const { ws } = await startedRecording()
    expect(JSON.parse(ws.textFrames[0])).toEqual({
      type: 'start',
      meeting_id: 'meet-1',
      title: 'Weekly',
    })
  })

  it('is not active until the server reports ready', async () => {
    const { hook, ws } = await startedRecording()
    // Nothing is persisted until the server has a writer open; a meter that lit up
    // on connect would claim otherwise.
    expect(hook.result.current.active).toBe(false)
    await act(async () => { ws.emit({ type: 'ready', meeting_id: 'meet-1' }) })
    expect(hook.result.current.active).toBe(true)
  })

  it('buffers pre-ready audio and flushes it in order', async () => {
    const { hook, ws } = await startedRecording()

    act(() => {
      hook.result.current.pushPcm(pcm(1))
      hook.result.current.pushPcm(pcm(2))
    })
    // The server discards binary frames that arrive before `start` completes, so
    // sending them immediately would drop whatever was said while the WAV writer
    // was opening.
    expect(ws.binaryFrames).toHaveLength(0)

    await act(async () => { ws.emit({ type: 'ready' }) })

    expect(ws.binaryFrames).toHaveLength(2)
    expect(new Uint8Array(ws.binaryFrames[0])[0]).toBe(1)
    expect(new Uint8Array(ws.binaryFrames[1])[0]).toBe(2)
  })

  it('sends audio straight through once ready', async () => {
    const { hook, ws } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })
    act(() => { hook.result.current.pushPcm(pcm(7)) })
    expect(ws.binaryFrames).toHaveLength(1)
    expect(new Uint8Array(ws.binaryFrames[0])[0]).toBe(7)
  })

  it('drops the OLDEST buffered audio past the cap', async () => {
    const { hook, ws } = await startedRecording()
    // 8s cap at 32 KB/s. Push well past it in 32 KB chunks and confirm the most
    // recent speech is what survives.
    const chunk = (marker: number) => {
      const buf = new Uint8Array(32 * 1024)
      buf[0] = marker
      return buf.buffer
    }
    act(() => {
      for (let i = 1; i <= 12; i++) hook.result.current.pushPcm(chunk(i))
    })
    await act(async () => { ws.emit({ type: 'ready' }) })

    const markers = ws.binaryFrames.map(b => new Uint8Array(b)[0])
    expect(markers).toContain(12)
    expect(markers).not.toContain(1)
  })

  it('delivers level events to a subscriber, and stops on unsubscribe', async () => {
    const { hook, ws } = await startedRecording()
    const seen: number[] = []
    let unsubscribe = () => {}
    act(() => { unsubscribe = hook.result.current.subscribeLevel(v => seen.push(v)) })

    await act(async () => { ws.emit({ type: 'level', rms: 0.42 }) })
    expect(seen).toContain(0.42)

    act(() => { unsubscribe() })
    await act(async () => { ws.emit({ type: 'level', rms: 0.99 }) })
    expect(seen).not.toContain(0.99)
  })

  it('pauses and resumes over the wire rather than tearing the socket down', async () => {
    // A stop finalizes the WAV, so pausing has to be a control frame: stopping and
    // restarting would reopen audio.wav in the same directory and lose the first half.
    const { hook, ws } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })

    act(() => { hook.result.current.pause() })
    expect(hook.result.current.paused).toBe(true)
    expect(JSON.parse(ws.textFrames[ws.textFrames.length - 1]).type).toBe('pause')

    act(() => { hook.result.current.resume() })
    expect(hook.result.current.paused).toBe(false)
    expect(JSON.parse(ws.textFrames[ws.textFrames.length - 1]).type).toBe('resume')
    expect(ws.closed).toBe(false)
  })

  it('asks the server to stop and lets IT close, so the WAV is finalized', async () => {
    const { hook, ws } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })

    act(() => { hook.result.current.stop() })

    expect(JSON.parse(ws.textFrames[ws.textFrames.length - 1]).type).toBe('stop')
    // Closing from here first would leave the WAV header's length field unwritten.
    expect(ws.closed).toBe(false)
  })

  it('reports an unexpected close but does NOT reconnect', async () => {
    // Reconnecting would open a SECOND session writing a second file, and the
    // server's cap is one session — so a retry would race the teardown.
    const { hook, ws, onError } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })
    const socketCount = FakeWS.instances.length

    await act(async () => { ws.close() })

    expect(onError).toHaveBeenCalledWith('disconnected')
    expect(hook.result.current.active).toBe(false)
    expect(FakeWS.instances).toHaveLength(socketCount)
  })

  it('does not report a disconnect when the duration cap closed the socket', async () => {
    const { ws, onError } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })

    await act(async () => {
      ws.emit({ type: 'error', message: 'max recording duration exceeded' })
      ws.close()
    })

    expect(onError).toHaveBeenCalledWith('duration')
    // One cause, one message.
    expect(onError).not.toHaveBeenCalledWith('disconnected')
  })

  it('tears down after a pre-ready error so recording can be retried', async () => {
    // The server keeps the socket open after rejecting a start, but `start()` returns
    // early while a socket exists — so without this the Record button would silently
    // do nothing for the rest of the meeting.
    const { hook, ws, onError } = await startedRecording()

    await act(async () => { ws.emit({ type: 'error', message: 'recording storage unavailable' }) })
    expect(onError).toHaveBeenCalledWith('server')

    const before = FakeWS.instances.length
    let pending: Promise<void> | undefined
    act(() => { pending = hook.result.current.start() })
    expect(FakeWS.instances.length).toBe(before + 1)
    await act(async () => {
      FakeWS.instances[FakeWS.instances.length - 1].accept()
      await pending
    })
  })

  it('reports "unavailable" when the upgrade is refused', async () => {
    // The guards in api_ws_recording (origin, loopback, the one-session cap) all
    // refuse before the upgrade, and the WebSocket API does not expose the status.
    const onError = vi.fn()
    const hook = renderHook(() => useMeetingRecording({ meetingId: 'meet-1', onError }))
    let pending: Promise<void> | undefined
    act(() => { pending = hook.result.current.start() })
    await act(async () => {
      FakeWS.instances[0].onerror?.()
      await pending
    })
    expect(onError).toHaveBeenCalledWith('unavailable')
    expect(hook.result.current.active).toBe(false)
  })

  it('closes the socket on unmount', async () => {
    const { hook, ws } = await startedRecording()
    await act(async () => { ws.emit({ type: 'ready' }) })
    hook.unmount()
    expect(ws.closed).toBe(true)
  })
})

// ─── the capture graph, pinned at the source ────────────────────────────────

describe('the two-input capture graph', () => {
  it('builds the worklet node with two inputs', () => {
    // Not constructible here (no AudioWorklet), and the whole feature depends on it:
    // with the default single input, system audio would be connected to an input
    // that does not exist and the remote side of every meeting would be missing.
    expect(TranscriptionSource).toMatch(/new AudioWorkletNode\([\s\S]*?numberOfInputs: 2/)
  })

  it('downmixes each input to mono rather than dropping a channel', () => {
    expect(TranscriptionSource).toContain("channelCountMode: 'explicit'")
    expect(TranscriptionSource).toContain('channelCount: 1')
  })

  it('puts the microphone on input 0 and system audio on input 1', () => {
    expect(TranscriptionSource).toContain('source.connect(node, 0, 0)')
    expect(TranscriptionSource).toContain('sysSource.connect(node, 0, 1)')
  })

  it('tees PCM to the recording BEFORE the STT ready-gate', () => {
    // Order matters: gating the tee on STT readiness would silently omit the first
    // few seconds of every recording.
    const handler = TranscriptionSource.match(/node\.port\.onmessage = e => \{[\s\S]*?\n {4}\}/)
    expect(handler).toBeTruthy()
    const body = handler![0]
    expect(body.indexOf('onPcmRef.current?.(chunk)')).toBeGreaterThan(-1)
    expect(body.indexOf('onPcmRef.current?.(chunk)')).toBeLessThan(body.indexOf('if (ready)'))
  })

  it('keeps the system-audio stream across a watchdog reconnect', () => {
    // `cleanup` runs on every reconnect. Stopping the display capture there would
    // pop the browser's share picker again every time the socket stalled.
    const cleanup = TranscriptionSource.match(/const cleanup = useCallback\([\s\S]*?\n {2}\}, \[/)
    expect(cleanup).toBeTruthy()
    expect(cleanup![0]).toContain('if (!sysWantedRef.current)')
    expect(TranscriptionSource).toContain('if (sysWantedRef.current) wireSystemAudio()')
  })

  it('releases the display capture on unmount', () => {
    expect(TranscriptionSource).toMatch(/sysWantedRef\.current = false\n\s*cleanup\(\)/)
  })
})

describe('the worklet itself', () => {
  it('sums the two inputs rather than averaging them', () => {
    // Averaging would halve the level of whichever side is talking — in a meeting
    // that is almost always exactly one side — and cost accuracy on every utterance.
    expect(WorkletSource).toContain('const sum = (channel[idx] || 0) + (mix2 ? (mix2[idx] || 0) : 0)')
    expect(WorkletSource).toContain('Math.max(-1, Math.min(1, sum))')
  })

  it('checks for the second input per block, not once at construction', () => {
    // A screen share can start or stop mid-meeting.
    expect(WorkletSource).toMatch(/const second = inputs\[1\]/)
  })
})

// ─── session wiring ─────────────────────────────────────────────────────────

describe('session wiring', () => {
  it('tees capture into the recording hook', () => {
    expect(SessionSource).toContain('onPcm: recording.pushPcm')
  })

  it('pauses the recording on a meeting pause instead of stopping it', () => {
    const effect = SessionSource.match(/const rec = recordingRef\.current[\s\S]*?\n {2}\}, \[status, recordingActive\]\)/)
    expect(effect).toBeTruthy()
    expect(effect![0]).toContain('rec.pause()')
    expect(effect![0]).toContain('rec.resume()')
  })

  it('releases the display capture when the meeting ends, including from paused', () => {
    // The pause branch deliberately KEEPS the share so resuming does not re-prompt,
    // so ending from paused is the path where it would otherwise stay alive and leave
    // the browser's "sharing" indicator on after the meeting finished.
    const effect = SessionSource.match(/const rec = recordingRef\.current[\s\S]*?\n {2}\}, \[status, recordingActive\]\)/)
    expect(effect).toBeTruthy()
    expect(effect![0]).toContain('transcriptionRef.current.detachSystemAudio()')
  })

  it('asks for system audio before opening the socket', () => {
    // A cancelled picker must not leave a registered session behind: the server's cap
    // is one, so a stale one would refuse the next attempt.
    const body = SessionSource.match(/const startRecording = useCallback\([\s\S]*?\n {2}\}, \[notify\]\)/)
    expect(body).toBeTruthy()
    expect(body![0].indexOf('attachSystemAudio')).toBeLessThan(body![0].indexOf('.start()'))
  })

  it('has a catalog entry for every recording and system-audio failure code', () => {
    // The two file-scope maps in useMeetingSession.ts are what `check-i18n-keys.mjs`
    // can resolve; this is the runtime half of the same guarantee.
    const session = EN_CATALOG.apps.meetings.session as Record<string, string>
    for (const key of [
      'recUnsupported',
      'recUnavailable',
      'recDisconnected',
      'recDurationCap',
      'recFailed',
      'sysAudioUnsupported',
      'sysAudioCancelled',
      'sysAudioNoAudio',
      'sysAudioEnded',
    ]) {
      expect(session[key], key).toBeTruthy()
    }
  })
})
