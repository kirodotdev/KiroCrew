import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests, playPreset } from '../hooks/useNotificationSound'

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
})
