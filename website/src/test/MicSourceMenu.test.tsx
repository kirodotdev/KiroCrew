import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import MicSourceMenu from '../components/MicSourceMenu'
import { isDeviceStale, activeDeviceId, setPreferredMicId, getPreferredMicId } from '../hooks/mic'

const DEVICES = [
  { deviceId: 'builtin', kind: 'audioinput', label: 'MacBook Pro Microphone', groupId: 'g1' },
  { deviceId: 'airpods', kind: 'audioinput', label: 'AirPods Pro', groupId: 'g2' },
  { deviceId: 'cam', kind: 'videoinput', label: 'FaceTime HD', groupId: 'g3' },
]

function mockDevices(list: unknown[] = DEVICES) {
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { enumerateDevices: vi.fn().mockResolvedValue(list) },
  })
}

/** Minimal MediaStream stub exposing one audio track with the given settings. */
function streamOn(deviceId: string | undefined): MediaStream {
  return {
    getAudioTracks: () => [{ getSettings: () => (deviceId === undefined ? {} : { deviceId }) }],
  } as unknown as MediaStream
}

describe('MicSourceMenu', () => {
  beforeEach(() => {
    localStorage.clear()
    mockDevices()
  })
  afterEach(() => vi.restoreAllMocks())

  it('enumerates devices when opened, not on mount', async () => {
    const enumerate = navigator.mediaDevices.enumerateDevices as ReturnType<typeof vi.fn>
    render(<MicSourceMenu deviceLabel="MacBook Pro Microphone" onSelect={() => {}} />)
    // Labels are only populated after permission is granted and hardware can be
    // plugged in at any moment, so the list must be built at open time.
    expect(enumerate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(enumerate).toHaveBeenCalled())
  })

  it('lists only audio inputs and drops cameras', async () => {
    render(<MicSourceMenu onSelect={() => {}} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByText('AirPods Pro')
    expect(screen.getByText('MacBook Pro Microphone')).toBeTruthy()
    expect(screen.queryByText('FaceTime HD')).toBeNull()
  })

  it('reports the chosen deviceId and closes', async () => {
    const onSelect = vi.fn()
    render(<MicSourceMenu onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button'))
    fireEvent.click(await screen.findByText('AirPods Pro'))
    expect(onSelect).toHaveBeenCalledWith('airpods')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('always reports the pick, so re-selecting can retry a silent fallback', async () => {
    // The menu is presentational: it only knows the SAVED preference, so it must
    // not decide whether a pick is redundant. When session-start acquisition
    // fell back, the live track is not on the preferred device and re-tapping the
    // checked entry IS the user's retry — swallowing it here would make the
    // fallback uncorrectable. The genuine no-op case is decided in `switchDevice`,
    // which knows the live device (see useStreamingStt).
    const onSelect = vi.fn()
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button'))
    fireEvent.click(await screen.findByText('AirPods Pro'))
    expect(onSelect).toHaveBeenCalledWith('airpods')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('reports the empty string for system default', async () => {
    const onSelect = vi.fn()
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button'))
    fireEvent.click(await screen.findByText('System default'))
    // '' is the sentinel meaning "whatever the OS says right now", NOT a device.
    expect(onSelect).toHaveBeenCalledWith('')
  })

  it('warns that a mid-recording switch costs audio only when the switch is live', async () => {
    const { unmount } = render(<MicSourceMenu onSelect={() => {}} recording liveSwitch />)
    fireEvent.click(screen.getByRole('button'))
    expect(await screen.findByText(/drops about 0.2s/)).toBeTruthy()
    unmount()

    // Batch capture cannot swap a MediaRecorder's source, so the honest promise
    // is "next recording" — showing the audio-loss warning there would be a lie.
    render(<MicSourceMenu onSelect={() => {}} recording liveSwitch={false} />)
    fireEvent.click(screen.getByRole('button'))
    expect(await screen.findByText(/next recording/)).toBeTruthy()
    expect(screen.queryByText(/drops about 0.2s/)).toBeNull()
  })

  it('says the saved device is unavailable instead of faking a checkmark', async () => {
    // Session-start acquisition falls back to the default when the saved id is
    // stale, so a stale saved id would otherwise render as a happy selection.
    setPreferredMicId('unplugged-interface')
    render(<MicSourceMenu onSelect={() => {}} />)
    fireEvent.click(screen.getByRole('button'))
    expect(await screen.findByText(/unavailable/)).toBeTruthy()
  })

  it('while recording, the checkmark follows the LIVE device, not the preference', async () => {
    // The reported bug: pick AirPods → dropdown moves, audio stays on the
    // built-in mic. The mark must report what is actually capturing, so a
    // switch that did not land is visible instead of papered over.
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={() => {}} recording liveSwitch activeDeviceId="builtin" />)
    fireEvent.click(screen.getByRole('button'))
    const builtin = await screen.findByRole('menuitemradio', { name: 'MacBook Pro Microphone' })
    const airpods = await screen.findByRole('menuitemradio', { name: 'AirPods Pro' })
    expect(builtin.querySelector('svg')).toBeTruthy()
    expect(airpods.querySelector('svg')).toBeNull()
    // The icon is aria-hidden and the rest is colour, so the programmatic state
    // is the only thing assistive tech can perceive — assert it, not just pixels.
    expect(builtin.getAttribute('aria-checked')).toBe('true')
    expect(airpods.getAttribute('aria-checked')).toBe('false')
  })

  it('falls back to label identity when the live id is permission-redacted', async () => {
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={() => {}} recording liveSwitch deviceLabel="MacBook Pro Microphone" />)
    fireEvent.click(screen.getByRole('button'))
    const builtin = await screen.findByRole('menuitemradio', { name: 'MacBook Pro Microphone' })
    const airpods = await screen.findByRole('menuitemradio', { name: 'AirPods Pro' })
    expect(builtin.querySelector('svg')).toBeTruthy()
    expect(airpods.querySelector('svg')).toBeNull()
  })

  it('marks NO row while recording when the live device is unknowable', async () => {
    // No id and no label: marking the preference would be a guess dressed as a
    // fact — the honest render is no checkmark at all (including the default row).
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={() => {}} recording liveSwitch />)
    fireEvent.click(screen.getByRole('button'))
    const menu = await screen.findByRole('menu')
    expect(menu.querySelectorAll('[role="menuitemradio"] svg').length).toBe(0)
  })

  it('idle, the checkmark shows the intent: what the next capture will request', async () => {
    setPreferredMicId('airpods')
    render(<MicSourceMenu onSelect={() => {}} />)
    fireEvent.click(screen.getByRole('button'))
    const airpods = await screen.findByRole('menuitemradio', { name: 'AirPods Pro' })
    expect(airpods.querySelector('svg')).toBeTruthy()
  })

  it('closes on Escape and on an outside pointerdown', async () => {
    render(<MicSourceMenu onSelect={() => {}} />)
    const trigger = screen.getByRole('button')
    fireEvent.click(trigger)
    await screen.findByRole('menu')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.click(trigger)
    await screen.findByRole('menu')
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('clamps its height and stays scrollable in a short viewport', async () => {
    // Regression for the blocking finding: with ~7+ inputs (routine on a dev Mac
    // running BlackHole / Loopback / Krisp / aggregates) an unclamped menu grew
    // past the viewport edge with no scroll container, so its first entries were
    // unreachable — no page scroll could recover them.
    const many = Array.from({ length: 8 }, (_, i) => ({
      deviceId: `d${i}`, kind: 'audioinput', label: `Interface ${i}`, groupId: `g${i}`,
    }))
    mockDevices(many)
    const originalHeight = window.innerHeight
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 300 })
    try {
      render(<MicSourceMenu onSelect={() => {}} recording liveSwitch />)
      fireEvent.click(screen.getByRole('button'))
      const menu = await screen.findByRole('menu')
      // Bounded AND scrollable — either alone still hides entries.
      expect(menu.style.maxHeight).toBeTruthy()
      expect(parseFloat(menu.style.maxHeight)).toBeLessThanOrEqual(300)
      expect(menu.className).toContain('overflow-y-auto')
      // Anchored to exactly one edge, never both (that would stretch it).
      expect(!!menu.style.top !== !!menu.style.bottom).toBe(true)
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight })
    }
  })

  it('shares the portal layer used by every other dropdown', async () => {
    // z-[60] painted UNDER the 27 sibling body-level portals at z-[9999].
    render(<MicSourceMenu onSelect={() => {}} />)
    fireEvent.click(screen.getByRole('button'))
    const menu = await screen.findByRole('menu')
    expect(menu.className).toContain('z-[9999]')
  })
})

describe('mic device identity helpers', () => {
  beforeEach(() => localStorage.clear())

  it('reads the deviceId a stream is actually capturing from', () => {
    expect(activeDeviceId(streamOn('airpods'))).toBe('airpods')
    expect(activeDeviceId(null)).toBe('')
  })

  it('flags a stream that predates the user changing their input', () => {
    // The bug this closes: a pre-warmed stream is bound to the device it was
    // acquired with, so reusing it after a Settings change records from the OLD
    // mic and the setting appears to do nothing.
    setPreferredMicId('airpods')
    expect(isDeviceStale(streamOn('builtin'))).toBe(true)
    expect(isDeviceStale(streamOn('airpods'))).toBe(false)
  })

  it('treats "system default" as never stale', () => {
    setPreferredMicId('')
    expect(getPreferredMicId()).toBe('')
    // No saved preference means "follow the OS", so any live device is correct.
    expect(isDeviceStale(streamOn('builtin'))).toBe(false)
  })

  it('does not call an unknown deviceId stale', () => {
    // Browsers redact deviceId until permission is granted; treating '' as a
    // mismatch would re-acquire the mic on every single check.
    setPreferredMicId('airpods')
    expect(isDeviceStale(streamOn(undefined))).toBe(false)
  })
})
