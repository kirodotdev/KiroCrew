/**
 * The Speech-to-Text microphone row (#17601): "System default" names the device
 * it resolves to, and "Test microphone" shows a live level from the picked
 * input and releases it again.
 *
 * The browser half is faked at the API boundary (enumerateDevices,
 * getUserMedia, AudioContext, rAF), so what is under test is the panel's own
 * decisions: which label the default gets, which constraint opens which device,
 * and that every way out of a test stops the tracks and closes the context.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { MicTestStrip } from '../components/MicTestStrip'
import { defaultMicName, stripDefaultPrefix } from '../hooks/mic'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    sttConfig: vi.fn(),
    saveSttConfig: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const dev = (deviceId: string, label: string, groupId = deviceId) =>
  ({ deviceId, label, groupId, kind: 'audioinput' }) as MediaDeviceInfo

describe('stripDefaultPrefix', () => {
  it('removes Chromium\'s "Default - " prefix and leaves other labels alone', () => {
    expect(stripDefaultPrefix('Default - Studio Mic')).toBe('Studio Mic')
    expect(stripDefaultPrefix('default: Studio Mic')).toBe('Studio Mic')
    expect(stripDefaultPrefix('Webcam Mic')).toBe('Webcam Mic')
    expect(stripDefaultPrefix('')).toBe('')
  })
})

describe('defaultMicName', () => {
  it('strips Chromium\'s "Default - " prefix', () => {
    expect(defaultMicName([dev('default', 'Default - MacBook Pro Microphone (Built-in)', 'g1'), dev('abc', 'MacBook Pro Microphone (Built-in)', 'g1')]))
      .toBe('MacBook Pro Microphone (Built-in)')
  })

  it('falls back to the groupId sibling when the label has no prefix', () => {
    expect(defaultMicName([dev('default', 'Default', 'g2'), dev('x', 'USB Headset', 'g2'), dev('y', 'Webcam', 'g3')]))
      .toBe('USB Headset')
  })

  it('uses an unprefixed label with no sibling as is', () => {
    expect(defaultMicName([dev('default', 'Fake Default Audio Input', 'g9'), dev('y', 'Fake Audio Input 1', 'g8')]))
      .toBe('Fake Default Audio Input')
  })

  it('knows nothing when there is no default entry (Safari, Firefox) or labels are hidden', () => {
    expect(defaultMicName([dev('a', 'Built-in Microphone')])).toBe('')
    expect(defaultMicName([dev('default', ''), dev('a', '')])).toBe('')
    expect(defaultMicName([])).toBe('')
  })
})

// ── fake media stack ──
type FakeTrack = {
  stop: ReturnType<typeof vi.fn>; readyState: string; label: string; getSettings: () => { deviceId: string }
  listeners: Record<string, (() => void)[]>
  addEventListener: (type: string, cb: () => void) => void
  removeEventListener: (type: string, cb: () => void) => void
}
let streams: { constraints: MediaStreamConstraints; tracks: FakeTrack[] }[]
let contexts: { closed: boolean }[]
let gum: ReturnType<typeof vi.fn>
let devices: MediaDeviceInfo[]
let deviceListeners: (() => void)[]

function fakeStream(constraints: MediaStreamConstraints, label: string) {
  const track: FakeTrack = {
    readyState: 'live',
    label,
    stop: vi.fn(() => { track.readyState = 'ended' }),
    getSettings: () => ({ deviceId: 'whatever' }),
    listeners: {},
    addEventListener: (type, cb) => { (track.listeners[type] ||= []).push(cb) },
    removeEventListener: (type, cb) => { track.listeners[type] = (track.listeners[type] || []).filter(l => l !== cb) },
  }
  streams.push({ constraints, tracks: [track] })
  return { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
}

const liveTracks = () => streams.flatMap(s => s.tracks).filter(t => t.readyState === 'live').length
const openContexts = () => contexts.filter(c => !c.closed).length

beforeEach(async () => {
  vi.clearAllMocks()
  await initI18n('en')
  streams = []
  contexts = []
  deviceListeners = []
  devices = [
    dev('default', 'Default - Studio Mic', 'g1'),
    dev('studio', 'Studio Mic', 'g1'),
    dev('webcam', 'Webcam Mic', 'g2'),
  ]
  gum = vi.fn(async (c: MediaStreamConstraints) => {
    const audio = c.audio as { deviceId?: { exact: string } } | true
    const id = audio === true ? 'default' : audio.deviceId?.exact
    return fakeStream(c, devices.find(d => d.deviceId === id)?.label ?? 'Unknown')
  })
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: gum,
      enumerateDevices: async () => devices,
      addEventListener: (_: string, cb: () => void) => { deviceListeners.push(cb) },
      removeEventListener: (_: string, cb: () => void) => { deviceListeners = deviceListeners.filter(l => l !== cb) },
    },
  })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.stubGlobal('AudioContext', class {
    rec = { closed: false }
    constructor() { contexts.push(this.rec) }
    createMediaStreamSource() { return { connect: vi.fn() } }
    createAnalyser() {
      return { fftSize: 0, frequencyBinCount: 256, connect: vi.fn(), getByteTimeDomainData: vi.fn(), getByteFrequencyData: vi.fn() }
    }
    close() { this.rec.closed = true }
  })
  vi.stubGlobal('fetch', vi.fn())
  localStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/**
 * Wait until the strip is LIVE. The meter alone is not proof: it renders in the
 * `starting` state, in the same commit as the click, while the stream and the
 * AudioContext arrive in a later render that this waits for.
 */
async function waitLive() {
  await screen.findByText(/^listening/i)
  expect(screen.getByRole('meter')).toBeTruthy()
}

describe('MicTestStrip', () => {
  it('opens the picked device with exact constraints and shows a meter', async () => {
    render(<MicTestStrip deviceId="webcam" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    expect(gum).toHaveBeenCalledWith({ audio: { deviceId: { exact: 'webcam' } } })
    expect(await screen.findByText(/listening on webcam mic/i)).toBeTruthy()
    expect(liveTracks()).toBe(1)
    expect(openContexts()).toBe(1)
  })

  it('opens the system default with no device constraint', async () => {
    render(<MicTestStrip deviceId="" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    expect(gum).toHaveBeenCalledWith({ audio: true })
    // Safari and Firefox name no default device in the picker; the live track
    // does, so the test still says which microphone it is hearing. Chromium's
    // "Default - " prefix is stripped, so the strip and the picker agree.
    expect(await screen.findByText(/listening on studio mic\./i)).toBeTruthy()
    expect(screen.queryByText(/default - /i)).toBeNull()
  })

  it('stop releases the tracks and closes the audio context', async () => {
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    fireEvent.click(screen.getByRole('button', { name: /stop test/i }))
    expect(liveTracks()).toBe(0)
    expect(openContexts()).toBe(0)
    expect(screen.queryByRole('meter')).toBeNull()
    expect(screen.getByRole('button', { name: /test microphone/i })).toBeTruthy()
  })

  it('switching device mid-test reopens on the new one and releases the old', async () => {
    const { rerender } = render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    rerender(<MicTestStrip deviceId="webcam" />)
    await waitFor(() => expect(gum).toHaveBeenLastCalledWith({ audio: { deviceId: { exact: 'webcam' } } }))
    await screen.findByText(/listening on webcam mic/i)
    expect(streams[0].tracks[0].readyState).toBe('ended')
    expect(liveTracks()).toBe(1)
    expect(openContexts()).toBe(1)
  })

  it('an idle strip does not open the mic when the picker changes', async () => {
    const { rerender } = render(<MicTestStrip deviceId="studio" />)
    rerender(<MicTestStrip deviceId="webcam" />)
    await act(async () => {})
    expect(gum).not.toHaveBeenCalled()
  })

  it('unmounting (leaving Settings) releases a running test', async () => {
    const { unmount } = render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    unmount()
    expect(liveTracks()).toBe(0)
    expect(openContexts()).toBe(0)
  })

  it('a stream that arrives after Stop is stopped, not kept', async () => {
    let resolve!: (s: MediaStream) => void
    gum.mockImplementationOnce((c: MediaStreamConstraints) => new Promise(r => { resolve = () => r(fakeStream(c, 'Studio Mic')) }))
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    fireEvent.click(screen.getByRole('button', { name: /stop test/i }))
    await act(async () => { resolve({} as MediaStream) })
    expect(liveTracks()).toBe(0)
    expect(contexts).toHaveLength(0)
  })

  it('a permission denial shows the mic error and holds nothing open', async () => {
    gum.mockRejectedValueOnce(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await screen.findByText(/permission denied/i)
    expect(screen.queryByRole('meter')).toBeNull()
    expect(liveTracks()).toBe(0)
  })

  it('a device that ends mid-test shows an error and releases capture', async () => {
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    const track = streams[0].tracks[0]
    await act(async () => { track.readyState = 'ended'; track.listeners.ended?.forEach(l => l()) })
    // Its own sentence: the generic mic error sends the user to Settings, which
    // is the page they are already on.
    await screen.findByText(/the microphone disconnected\. reconnect it or pick another one above/i)
    expect(screen.queryByRole('meter')).toBeNull()
    expect(openContexts()).toBe(0)
  })

  it('a new pick clears the previous device\'s error', async () => {
    gum.mockRejectedValueOnce(Object.assign(new Error('gone'), { name: 'NotFoundError' }))
    const { rerender } = render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await screen.findByText(/no microphone found/i)
    rerender(<MicTestStrip deviceId="webcam" />)
    await waitFor(() => expect(screen.queryByText(/no microphone found/i)).toBeNull())
    expect(gum).toHaveBeenCalledTimes(1)
  })

  it('offers the agent hand-off on an error only when the host allows it', async () => {
    gum.mockRejectedValue(Object.assign(new Error('busy'), { name: 'NotReadableError' }))
    const { unmount } = render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await screen.findByText(/microphone is unavailable/i)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    unmount()
    render(<MicTestStrip deviceId="studio" askAgent={false} />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await screen.findByText(/microphone is unavailable/i)
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('keeps one status line through idle, live and stopped, so the page does not jump', async () => {
    render(<MicTestStrip deviceId="studio" />)
    const strip = screen.getByTestId('mic-test')
    const lines = () => strip.querySelectorAll(':scope > p').length
    expect(screen.getByText(/shows your microphone's level\. nothing is recorded or sent/i)).toBeTruthy()
    expect(lines()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await screen.findByText(/listening on studio mic/i)
    expect(lines()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: /stop test/i }))
    await screen.findByText(/shows your microphone's level\. nothing is recorded or sent/i)
    expect(lines()).toBe(1)
  })

  it('says the microphone is opening while the permission prompt may be up', async () => {
    let resolve!: () => void
    gum.mockImplementationOnce((c: MediaStreamConstraints) => new Promise(r => { resolve = () => r(fakeStream(c, 'Studio Mic')) }))
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    expect(await screen.findByText(/opening the microphone/i)).toBeTruthy()
    expect(screen.queryByText(/nothing is recorded or sent/i)).toBeNull()
    // No meter until the device is open: an empty track reads as a silent mic.
    expect(screen.queryByRole('meter')).toBeNull()
    await act(async () => { resolve() })
    await screen.findByText(/listening on studio mic/i)
  })

  it('offers no agent hand-off for a disconnect, whose fix is the picker above', async () => {
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    const track = streams[0].tracks[0]
    await act(async () => { track.readyState = 'ended'; track.listeners.ended?.forEach(l => l()) })
    await screen.findByText(/the microphone disconnected/i)
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('sends nothing over the network', async () => {
    render(<MicTestStrip deviceId="studio" />)
    fireEvent.click(screen.getByRole('button', { name: /test microphone/i }))
    await waitLive()
    fireEvent.click(screen.getByRole('button', { name: /stop test/i }))
    expect(fetch).not.toHaveBeenCalled()
  })
})

function mountSettings() {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true, provider: 'local', model: 'base', streaming: false,
    providers: ['local'], streaming_providers: ['local'], language_codes: ['en-US'], prereqs: [],
  })
  mockApi.sttStatus.mockResolvedValue({
    available: true, code: '', detail: '', models: [],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
}

async function micOptions() {
  const trigger = await screen.findByRole('combobox', { name: /microphone/i })
  fireEvent.click(trigger)
  const labels = (await screen.findAllByRole('option')).map(o => o.textContent)
  fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
  return labels
}

describe('SttSettings microphone picker', () => {
  it('names the device the system default resolves to, once', async () => {
    mountSettings()
    await waitFor(async () => expect(await micOptions()).toEqual(['System default (Studio Mic)', 'Studio Mic', 'Webcam Mic']))
  })

  it('follows a change of the OS default on devicechange', async () => {
    mountSettings()
    await waitFor(async () => expect((await micOptions())[0]).toBe('System default (Studio Mic)'))
    devices = [dev('default', 'Default - Webcam Mic', 'g2'), dev('studio', 'Studio Mic', 'g1'), dev('webcam', 'Webcam Mic', 'g2')]
    await act(async () => { deviceListeners.forEach(l => l()) })
    await waitFor(async () => expect((await micOptions())[0]).toBe('System default (Webcam Mic)'))
  })

  it('reads plain System default when the browser names no default', async () => {
    devices = [dev('a', 'Built-in Microphone'), dev('b', 'USB Mic')]
    mountSettings()
    await waitFor(async () => expect(await micOptions()).toEqual(['System default', 'Built-in Microphone', 'USB Mic']))
  })

  it('shows a saved Chromium "default" preference as the System default option', async () => {
    localStorage.setItem('mc-mic-device-id', 'default')
    mountSettings()
    const trigger = await screen.findByRole('combobox', { name: /microphone/i })
    await waitFor(() => expect(trigger.textContent).toContain('System default (Studio Mic)'))
  })
})
