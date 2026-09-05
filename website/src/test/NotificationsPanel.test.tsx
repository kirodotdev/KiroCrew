import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, act } from '@testing-library/react'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests, playPreset, presetForKind, loadSoundSettings } from '../hooks/useNotificationSound'

// Mock only playPreset: the panel's Test buttons and dropdown previews call it,
// and the assertions below need to observe the (preset, volume) pair without
// touching the Web Audio API. Everything else (loadSoundSettings, persistence)
// stays real so the tests exercise the actual settings round-trip.
vi.mock('../hooks/useNotificationSound', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return { ...actual, playPreset: vi.fn() }
})

const STORAGE_KEY = 'mc-notification-sound'

// Stub AudioContext to prevent real Web Audio calls during component render.
beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  vi.mocked(playPreset).mockClear()
  ;(window as unknown as { AudioContext: unknown }).AudioContext = vi.fn(() => ({
    state: 'running',
    currentTime: 0,
    destination: {},
    resume: vi.fn(() => Promise.resolve()),
    createOscillator: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), type: '', frequency: { value: 0 }, onended: null })),
    createGain: vi.fn(() => ({ gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() })),
  }))
})

describe('NotificationsPanel', () => {
  it('renders with defaults (toggle on, volume 35%, chime fallback)', () => {
    const { container } = render(<NotificationsPanel />)

    // Toggle uses role="switch" with aria-checked
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    expect(toggle.getAttribute('aria-checked')).toBe('true')

    const slider = container.querySelector('input[type="range"]') as HTMLInputElement | null
    expect(slider).not.toBeNull()
    expect(slider!.value).toBe('35')

    // Default fallback displayed in the "all" category selector
    expect(container.textContent).toContain('Chime')
  })

  it('clicking the master toggle persists enabled=false to localStorage', () => {
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    fireEvent.click(toggle)
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.enabled).toBe(false)
  })

  it('changing volume persists the new value (0.5) to localStorage', () => {
    const { container } = render(<NotificationsPanel />)
    const slider = container.querySelector('input[type="range"]') as HTMLInputElement
    fireEvent.change(slider, { target: { value: '50' } })
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.volume).toBe(0.5)
  })

  it('loads seeded cron override from localStorage on initial render', () => {
    // Seed with an existing cron override before render
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      enabled: true,
      volume: 0.35,
      perCategory: { all: 'chime', cron: 'ding' },
    }))
    render(<NotificationsPanel />)
    // The cron row should show "Ding" (the override), not "Use default"
    expect(screen.getAllByText(/Ding/).length).toBeGreaterThan(0)
  })

  it('renders the Agent messages row and loads its seeded override', () => {
    // Seed the exact configuration the row exists for: silent default,
    // audible agent messages.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      enabled: true,
      volume: 0.35,
      perCategory: { all: 'none', agent: 'ding' },
    }))
    render(<NotificationsPanel />)
    expect(screen.getAllByText('Proactive agent messages').length).toBeGreaterThan(0)
    // The agent row shows "Ding" (the override), not "Use default"
    expect(screen.getAllByText(/Ding/).length).toBeGreaterThan(0)
  })

  it('persists volume=0 when slider is at 0 (enforces quiet mode)', () => {
    const { container } = render(<NotificationsPanel />)
    const slider = container.querySelector('input[type="range"]') as HTMLInputElement
    fireEvent.change(slider, { target: { value: '0' } })
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.volume).toBe(0)
  })

  it('disabling via toggle + loading again shows disabled state', () => {
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.enabled).toBe(false)
  })

  it('Test buttons are disabled when master toggle is off', () => {
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    fireEvent.click(toggle) // disable

    // All Test buttons should be disabled now
    const testBtns = screen.getAllByRole('button', { name: 'Test' })
    expect(testBtns.length).toBeGreaterThan(0)
    testBtns.forEach(btn => expect((btn as HTMLButtonElement).disabled).toBe(true))
  })

  it('Test sound button plays the fallback preset at the current volume', () => {
    render(<NotificationsPanel />)
    const btn = screen.getByRole('button', { name: 'Test sound' })
    expect((btn as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(btn)
    expect(playPreset).toHaveBeenCalledTimes(1)
    expect(playPreset).toHaveBeenCalledWith('chime', 0.35)
  })

  it('Test sound button uses the user-selected fallback preset and volume', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      enabled: true,
      volume: 0.6,
      perCategory: { all: 'ding' },
    }))
    render(<NotificationsPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Test sound' }))
    expect(playPreset).toHaveBeenCalledWith('ding', 0.6)
  })

  it('Test sound button is disabled when sound is off, volume is 0, or fallback is none', () => {
    // Sound disabled
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: false, volume: 0.35, perCategory: { all: 'chime' } }))
    const first = render(<NotificationsPanel />)
    expect((screen.getByRole('button', { name: 'Test sound' }) as HTMLButtonElement).disabled).toBe(true)
    first.unmount()

    // Volume at 0
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: true, volume: 0, perCategory: { all: 'chime' } }))
    const second = render(<NotificationsPanel />)
    expect((screen.getByRole('button', { name: 'Test sound' }) as HTMLButtonElement).disabled).toBe(true)
    second.unmount()

    // Fallback preset set to none
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: true, volume: 0.35, perCategory: { all: 'none' } }))
    render(<NotificationsPanel />)
    const btn = screen.getByRole('button', { name: 'Test sound' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(playPreset).not.toHaveBeenCalled()
  })

  it('approval-row preview matches runtime (presetForKind): plays pulse with no override', () => {
    // Category rows render in CATEGORY_ROWS order:
    // all, turn, agent, cron, approval, hook, heartbeat, subagent, taskrunner, skills
    // The per-row Test button plays `effective`, which now goes through
    // presetForKind — so approval with no override must play its built-in
    // 'pulse', exactly what runtime plays, not the 'all' fallback.
    render(<NotificationsPanel />)
    const rowTestBtns = screen.getAllByRole('button', { name: 'Test' })
    const approvalIdx = 4
    fireEvent.click(rowTestBtns[approvalIdx])
    const settings = loadSoundSettings()
    expect(presetForKind('approval', settings)).toBe('pulse')
    expect(playPreset).toHaveBeenCalledWith('pulse', settings.volume)
  })

  it('does not adopt a change into local state when persistence fails (quota)', () => {
    render(<NotificationsPanel />)
    // Make the underlying write fail as a persistent quota error.
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    fireEvent.click(toggle) // attempt enabled -> false
    // Save failed: local state must not have adopted the change, so the switch
    // still reads checked. (The persisted value is also unchanged.)
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    setSpy.mockRestore()
  })

  it('reloads the rendered panel when another window writes the sound settings (storage event)', () => {
    // Panel mounts on defaults: sound enabled, chime fallback.
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    expect(toggle.getAttribute('aria-checked')).toBe('true')

    // A DIFFERENT window persists new settings (sound off) then the browser
    // delivers a cross-window `storage` event to this window. The panel must
    // reload from localStorage and re-render, not keep showing its stale state.
    const next = JSON.stringify({ enabled: false, volume: 0.35, perCategory: { all: 'chime' } })
    localStorage.setItem(STORAGE_KEY, next)
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY,
        newValue: next,
        storageArea: localStorage,
      }))
    })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })

  it('honors a cross-window clear() (storage event with key=null)', () => {
    // Mount on a non-default (sound off) so a reset to DEFAULTS is observable.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: false, volume: 0.35, perCategory: { all: 'chime' } }))
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    expect(toggle.getAttribute('aria-checked')).toBe('false')

    // A whole-store clear() in another window fires a storage event with
    // key === null. loadSoundSettings() then returns DEFAULTS (enabled=true).
    localStorage.clear()
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: null,
        newValue: null,
        storageArea: localStorage,
      }))
    })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  })

  it('ignores storage events for unrelated keys and non-localStorage areas', () => {
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })
    expect(toggle.getAttribute('aria-checked')).toBe('true')

    // Persist a change that WOULD flip the toggle if adopted, but announce it
    // under an unrelated key — the panel must ignore it.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: false, volume: 0.35, perCategory: { all: 'chime' } }))
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'some-other-key',
        newValue: 'x',
        storageArea: localStorage,
      }))
    })
    expect(toggle.getAttribute('aria-checked')).toBe('true')

    // Same for a write to a different storageArea (e.g. sessionStorage).
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY,
        newValue: JSON.stringify({ enabled: false, volume: 0.35, perCategory: { all: 'chime' } }),
        storageArea: sessionStorage,
      }))
    })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  })

  it('a local edit preserves a newer persisted field the panel render snapshot never saw', () => {
    // Panel mounts on defaults (volume 0.35). Its React render snapshot now
    // holds volume=0.35 and never re-renders for what follows.
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Play sound on new notifications/i })

    // Another window advances the VOLUME to 0.8 and persists it WITHOUT a
    // storage event reaching this panel (event dropped, or the write happened
    // between renders). The panel's local `settings` is now stale: it still
    // thinks volume is 0.35.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: true, volume: 0.8, perCategory: { all: 'chime' } }))

    // The user toggles sound OFF in this panel. A stale-state write would
    // spread the panel's old snapshot and clobber volume back to 0.35. Deriving
    // from a fresh persisted snapshot must keep volume=0.8 while only flipping
    // enabled.
    fireEvent.click(toggle)
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.enabled).toBe(false)
    expect(saved.volume).toBe(0.8)
  })

  it('a local edit preserves a newer persisted per-category override the snapshot never saw', () => {
    // Panel mounts on defaults; render snapshot has perCategory { all: 'chime' }.
    const { container } = render(<NotificationsPanel />)

    // Another window adds a cron override the panel's render snapshot never saw.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      enabled: true, volume: 0.35, perCategory: { all: 'chime', cron: 'ding' },
    }))

    // A local edit (volume slider) must fold onto the FRESH persisted snapshot,
    // preserving the newer cron override rather than dropping it back to the
    // stale { all: 'chime' } the panel last rendered.
    const slider = container.querySelector('input[type="range"]') as HTMLInputElement
    fireEvent.change(slider, { target: { value: '55' } })
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(saved.volume).toBe(0.55)
    expect(saved.perCategory.cron).toBe('ding')
    expect(saved.perCategory.all).toBe('chime')
  })
})
