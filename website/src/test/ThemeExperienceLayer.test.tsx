import type React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { render as rtlRender, screen, act, type RenderOptions } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Controllable useTheme mock — each test sets `mockTheme` before rendering.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let mockTheme: any
const setColorTheme = vi.fn()
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => mockTheme,
}))

import ThemeExperienceLayer from '../components/ThemeExperienceLayer'
import { OVERLAY_Z_MAX, THEME_DECOR_SLOT_ID, registerThemeDecorSlot, __resetThemeDecorSlot } from '../lib/themeDecorLayer'

// The layer reads `instances.activeId` (it unmounts when a remote Crew is active),
// so every mount needs a Redux Provider. Shim `render` through RTL's `wrapper`
// option — not a hand-wrapped element — so `rerender(<ThemeExperienceLayer />)`
// in the cases below keeps the Provider instead of dropping it.
let testStore = createTestStore()
const StoreWrapper = ({ children }: { children: React.ReactNode }) => (
  <Provider store={testStore}>{children}</Provider>
)
const render = (ui: React.ReactElement, options: Omit<RenderOptions, 'wrapper'> = {}) =>
  rtlRender(ui, { wrapper: StoreWrapper, ...options })

function mockMatchMedia(reduced: boolean) {
  const mql = {
    matches: reduced,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia
}

// Query-aware matchMedia: `match(query)` decides per-query, so a test can make
// the mobile breakpoint match while reduced-motion does not (or vice versa).
function mockMatchMediaBy(match: (q: string) => boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: match(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

function l2Assets(overrides: Record<string, unknown> = {}) {
  return {
    overlays: ['bubbles'],
    topbar: { dark: true, light: true },
    hasAudio: true,
    hasPersona: true,
    branding: { botName: 'Bubbles' },
    ...overrides,
  }
}

function setTheme(opts: {
  colorTheme?: string
  theme?: 'dark' | 'light'
  level?: number
  assets?: Record<string, unknown> | null
} = {}) {
  const slug = 'bikini-bottom'
  const map = new Map<string, unknown>()
  if (opts.colorTheme !== 'emerald') {
    map.set(slug, {
      slug,
      name: 'Bikini Bottom',
      emoji: '🫧',
      level: opts.level ?? 2,
      assets: opts.assets === null ? undefined : opts.assets ?? l2Assets(),
    })
  }
  mockTheme = {
    theme: opts.theme ?? 'dark',
    colorTheme: opts.colorTheme ?? `custom-${slug}`,
    customThemeDataMap: map,
    setColorTheme,
  }
}

const frames = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLIFrameElement>('iframe[data-theme-frame="1"]'))

describe('ThemeExperienceLayer', () => {
  beforeEach(() => {
    localStorage.clear()
    setColorTheme.mockReset()
    mockMatchMedia(false)
    testStore = createTestStore()
    __resetThemeDecorSlot()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders nothing for a built-in (non-L2) theme', () => {
    setTheme({ colorTheme: 'emerald' })
    const { container } = render(<ThemeExperienceLayer />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the consent gate (no iframes) for an unconsented persona/audio pack', () => {
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Enable .*experience/i })).toBeInTheDocument()
    expect(frames(container)).toHaveLength(0) // gated — nothing mounted yet
  })

  it('Enable persists consent and mounts overlay + topbar iframes', async () => {
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    await userEvent.click(screen.getByRole('button', { name: /enable experience/i }))
    expect(localStorage.getItem('mc-theme-consent-bikini-bottom')).toBe('consented-v2')
    const fs = frames(container)
    // overlay (bubbles) + topbar (dark)
    expect(fs).toHaveLength(2)
    for (const f of fs) expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(fs.some((f) => f.getAttribute('src') === '/api/theme/bikini-bottom/overlay/bubbles')).toBe(true)
    expect(fs.some((f) => f.getAttribute('src') === '/api/theme/bikini-bottom/topbar/dark')).toBe(true)
  })

  it('"Keep colors only" reverts the selection', async () => {
    setTheme()
    render(<ThemeExperienceLayer />)
    await userEvent.click(screen.getByRole('button', { name: /keep colors only/i }))
    expect(setColorTheme).toHaveBeenCalledWith('emerald')
  })

  it('suppresses motion overlays under prefers-reduced-motion (topbar still shown)', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    mockMatchMedia(true)
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const srcs = frames(container).map((f) => f.getAttribute('src'))
    expect(srcs).toContain('/api/theme/bikini-bottom/topbar/dark')
    expect(srcs).not.toContain('/api/theme/bikini-bottom/overlay/bubbles')
  })

  it('renders overlays immediately for an overlays-only L2 pack (no consent needed)', () => {
    setTheme({ assets: { overlays: ['bubbles'], hasAudio: false, hasPersona: false } })
    const { container } = render(<ThemeExperienceLayer />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(frames(container).some((f) => f.getAttribute('src')?.includes('/overlay/bubbles'))).toBe(true)
  })

  it('shows a mute toggle when a consented pack ships audio', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    render(<ThemeExperienceLayer />)
    expect(screen.getByRole('button', { name: /mute theme sounds/i })).toBeInTheDocument()
  })

  it('message router honours theme:resize from a theme frame and ignores other types', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const topbar = frames(container).find((f) => f.getAttribute('src')?.includes('/topbar/'))
    // jsdom may not expose a live contentWindow; only assert when it does.
    if (topbar?.contentWindow) {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'theme:resize', height: 123 },
          source: topbar.contentWindow as unknown as Window,
        }),
      )
      expect(topbar.style.height).toBe('123px')
      // A non-allowlisted type must be a no-op (height unchanged).
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'theme:evil', height: 999 },
          source: topbar.contentWindow as unknown as Window,
        }),
      )
      expect(topbar.style.height).toBe('123px')
    }
  })

  it('theme:resize clamps a pointer-interactive topbar to the security ceiling (no viewport takeover)', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const topbar = frames(container).find((f) => f.getAttribute('src')?.includes('/topbar/'))
    if (topbar?.contentWindow) {
      // A malicious runtime resize far beyond the 200px topbar ceiling must be
      // clamped — the topbar is click-through (pointer-events:none) but a
      // full-width z=45 strip must still not grow to cover the viewport.
      // Height is clamped by its declared ceiling regardless.
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'theme:resize', height: 9999 },
          source: topbar.contentWindow as unknown as Window,
        }),
      )
      expect(topbar.style.height).toBe('200px')
      // A within-ceiling resize is still honoured verbatim.
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'theme:resize', height: 48 },
          source: topbar.contentWindow as unknown as Window,
        }),
      )
      expect(topbar.style.height).toBe('48px')
    }
  })

  it('decorative topbar iframe is click-through (pointer-events:none) so it cannot eat clicks meant for the dashboard controls beneath it', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const topbar = frames(container).find((f) => f.getAttribute('src')?.includes('/topbar/'))
    expect(topbar).toBeTruthy()
    // The topbar is a fixed, full-width, z=45 branding strip over the real
    // dashboard header — it must not intercept pointer events.
    expect(topbar!.style.pointerEvents).toBe('none')
  })

  it('message router drops a message from a non-theme source (source spoof)', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const topbar = frames(container).find((f) => f.getAttribute('src')?.includes('/topbar/'))
    if (!topbar) return
    const before = topbar.style.height
    // `window` is not one of our theme-frame contentWindows -> must be ignored.
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'theme:resize', height: 500 },
        source: window as unknown as Window,
      }),
    )
    expect(topbar.style.height).toBe(before)
  })

  it('theme:sound plays a safe audio file and rejects unsafe names', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    const AudioMock = vi.fn(() => ({
      loop: false,
      play: () => Promise.resolve(),
      pause() {},
      addEventListener() {},
    }))
    ;(globalThis as unknown as { Audio: unknown }).Audio = AudioMock
    setTheme() // audioEnabled: consented + hasAudio + not muted + not reduced
    const { container } = render(<ThemeExperienceLayer />)
    const frame = frames(container)[0]
    if (!frame?.contentWindow) return
    const post = (name: string) =>
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'theme:sound', name },
          source: frame.contentWindow as unknown as Window,
        }),
      )
    // Unsafe names (traversal, wrong/blocked extension) never reach `new Audio`.
    post('../evil.mp3')
    post('x.js')
    post('beep.exe')
    expect(AudioMock).not.toHaveBeenCalled()
    // A safe bare filename with an allowed extension plays.
    post('chime.wav')
    expect(AudioMock).toHaveBeenCalledTimes(1)
    expect(AudioMock).toHaveBeenCalledWith(expect.stringContaining('/audio/chime.wav'))
  })

  it('routes message-received + notification app events to the theme manifest chime', () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    const AudioMock = vi.fn(() => ({
      loop: false,
      play: () => Promise.resolve(),
      pause() {},
      addEventListener() {},
    }))
    ;(globalThis as unknown as { Audio: unknown }).Audio = AudioMock
    // Manifest with the two event-driven triggers (no `activate`, no ambient) —
    // so nothing plays on mount; only a real app event should chime.
    setTheme({
      assets: l2Assets({
        audio: {
          triggers: {
            'message-received': { src: 'audio/chime.wav', volume: 0.4 },
            notification: { src: 'audio/chime.wav', volume: 0.5 },
          },
          ambient: null,
        },
      }),
    })
    render(<ThemeExperienceLayer />)
    // No `activate` trigger + null ambient → activation is silent.
    expect(AudioMock).not.toHaveBeenCalled()
    // An agent reply arriving fires the `message-received` chime.
    window.dispatchEvent(
      new CustomEvent('mc-theme-sound', { detail: { trigger: 'message-received' } }),
    )
    expect(AudioMock).toHaveBeenCalledWith(expect.stringContaining('/audio/chime.wav'))
    // The shared notification event maps to the `notification` trigger.
    AudioMock.mockClear()
    window.dispatchEvent(new CustomEvent('mc-notification', { detail: { kind: 'info' } }))
    expect(AudioMock).toHaveBeenCalledWith(expect.stringContaining('/audio/chime.wav'))
    // An unknown trigger with no manifest entry is a silent no-op.
    AudioMock.mockClear()
    window.dispatchEvent(new CustomEvent('mc-theme-sound', { detail: { trigger: 'nope' } }))
    expect(AudioMock).not.toHaveBeenCalled()
  })

  // ── §3.1 manifest-driven overlay placement ──
  it('renders a declared corner overlay with corner placement and a legacy string overlay fullscreen', () => {
    setTheme({
      assets: {
        overlays: [
          { id: 'corner', position: 'bottom-right', zIndex: 30, pointerEvents: true, trigger: 'continuous' },
          'legacy',
        ],
        hasAudio: false,
        hasPersona: false,
      },
    })
    const { container } = render(<ThemeExperienceLayer />)
    const fs = frames(container)
    const corner = fs.find((f) => f.getAttribute('src')?.endsWith('/overlay/corner'))
    const legacy = fs.find((f) => f.getAttribute('src')?.endsWith('/overlay/legacy'))
    expect(corner).toBeTruthy()
    expect(legacy).toBeTruthy()
    // Declared corner box: fixed, anchored bottom-right, 40% box, zIndex 30, interactive.
    expect(corner!.style.position).toBe('fixed')
    expect(corner!.style.bottom).toBe('0px')
    expect(corner!.style.right).toBe('0px')
    expect(corner!.style.width).toBe('40%')
    expect(corner!.style.height).toBe('40%')
    expect(corner!.style.zIndex).toBe('30')
    expect(corner!.style.pointerEvents).toBe('auto')
    // Legacy string overlay → fullscreen default, non-interactive, default zIndex 40.
    expect(legacy!.style.width).toBe('100%')
    expect(legacy!.style.height).toBe('100%')
    expect(legacy!.style.zIndex).toBe('40')
    expect(legacy!.style.pointerEvents).toBe('none')
  })

  // ── #7377 overlays render inside the shell's decor slot, below the header ──
  it('portals overlays into the registered decor slot and clamps zIndex below the top bar', () => {
    const slot = document.createElement('div')
    slot.id = THEME_DECOR_SLOT_ID
    document.body.appendChild(slot)
    try {
      registerThemeDecorSlot(slot)
      setTheme({
        assets: {
          overlays: [{ id: 'scan', position: 'fullscreen', zIndex: 9999, pointerEvents: false, trigger: 'continuous' }],
          topbar: { dark: true, light: true },
          hasAudio: false,
          hasPersona: false,
        },
      })
      const { container } = render(<ThemeExperienceLayer />)
      const inSlot = frames(slot)
      const inline = frames(container)
      // The overlay lives in the slot (the shell's stacking context)...
      expect(inSlot).toHaveLength(1)
      expect(inSlot[0].getAttribute('src')).toMatch(/\/overlay\/scan$/)
      // ...clamped to the derived ceiling, however much the manifest asked for.
      expect(inSlot[0].style.zIndex).toBe(String(OVERLAY_Z_MAX))
      // The topbar branding strip is MEANT to paint over the header, so it stays inline at the root.
      expect(inline).toHaveLength(1)
      expect(inline[0].getAttribute('src')).toMatch(/\/topbar\/dark$/)
      expect(inline[0].style.zIndex).toBe('45')
    } finally {
      slot.remove()
    }
  })

  it('renders overlays inline when no shell has registered a slot (onboarding, bootstrap)', () => {
    setTheme({
      assets: { overlays: ['stars'], hasAudio: false, hasPersona: false },
    })
    const { container } = render(<ThemeExperienceLayer />)
    const fs = frames(container)
    expect(fs).toHaveLength(1)
    expect(fs[0].getAttribute('src')).toMatch(/\/overlay\/stars$/)
  })

  // ── §3.1 topbar height + hideOnMobile ──
  it('applies a declared topbar height and hides the topbar on mobile', () => {
    const assets = {
      overlays: [],
      topbar: { dark: true, light: true, height: '40px', hideOnMobile: true },
      hasAudio: false,
      hasPersona: false,
    }
    // Wide viewport (beforeEach mocks matchMedia → not narrow, not reduced).
    setTheme({ assets })
    const { container, unmount } = render(<ThemeExperienceLayer />)
    const bar = frames(container).find((f) => f.getAttribute('src')?.includes('/topbar/'))
    expect(bar).toBeTruthy()
    expect(bar!.style.height).toBe('40px')
    unmount()
    // Narrow viewport: (max-width: 767px) matches → hideOnMobile removes the topbar.
    mockMatchMediaBy((q) => /max-width:\s*767px/.test(q))
    setTheme({ assets })
    const { container: c2 } = render(<ThemeExperienceLayer />)
    expect(frames(c2).some((f) => f.getAttribute('src')?.includes('/topbar/'))).toBe(false)
  })

  // ── §3.1 idle-<N>s overlay trigger ──
  it('mounts an idle-5s overlay only after 5s idle and unmounts it on activity', () => {
    vi.useFakeTimers()
    try {
      setTheme({
        assets: {
          overlays: [{ id: 'idler', position: 'center', zIndex: 20, pointerEvents: false, trigger: 'idle-5s' }],
          hasAudio: false,
          hasPersona: false,
        },
      })
      const { container } = render(<ThemeExperienceLayer />)
      const idlerSrc = '/api/theme/bikini-bottom/overlay/idler'
      const mounted = () => frames(container).some((f) => f.getAttribute('src') === idlerSrc)
      // Not mounted initially (no idle elapsed yet).
      expect(mounted()).toBe(false)
      // After 5s with no activity → mounts.
      act(() => {
        vi.advanceTimersByTime(5000)
      })
      expect(mounted()).toBe(true)
      // Any activity hides it and restarts the countdown.
      act(() => {
        window.dispatchEvent(new Event('pointerdown'))
      })
      expect(mounted()).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  // ── §4.3 theme:state downlink ──
  it('posts theme:state to each theme iframe and re-posts when the mute toggles', async () => {
    localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
    setTheme()
    const { container } = render(<ThemeExperienceLayer />)
    const fs = frames(container)
    expect(fs.length).toBeGreaterThan(0)
    // Give each frame a stub contentWindow so the router/downlink can post to it.
    const spies = fs.map((f) => {
      const spy = vi.fn()
      Object.defineProperty(f, 'contentWindow', { configurable: true, value: { postMessage: spy } })
      return spy
    })
    // Toggling mute changes postThemeState identity → the downlink effect re-runs
    // and posts the fresh {type, mode, muted, reducedMotion} to every theme frame.
    await userEvent.click(screen.getByRole('button', { name: /mute theme sounds/i }))
    for (const spy of spies) {
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'theme:state', mode: 'dark', muted: true, reducedMotion: false }),
        '*',
      )
    }
  })

  // ── §3.3/§4.3 audio trigger engine ──
  it('plays a manifest audio trigger at its volume, stops it after maxDuration, ignores unknown triggers, and still plays legacy names', () => {
    vi.useFakeTimers()
    const origAudio = (globalThis as unknown as { Audio: unknown }).Audio
    class FakeAudio {
      src: string
      loop = false
      volume = 1
      ended = false
      play = vi.fn(() => Promise.resolve())
      pause = vi.fn()
      addEventListener = vi.fn()
      constructor(src: string) {
        this.src = src
        instances.push(this)
      }
    }
    const instances: FakeAudio[] = []
    ;(globalThis as unknown as { Audio: unknown }).Audio = FakeAudio
    try {
      localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
      setTheme({
        assets: l2Assets({
          overlays: [],
          audio: {
            triggers: { notification: { src: 'notify.mp3', volume: 0.5, maxDuration: 3 } },
            ambient: null,
          },
        }),
      })
      const { container } = render(<ThemeExperienceLayer />)
      const frame = frames(container)[0]
      const win = {} as unknown as Window
      Object.defineProperty(frame, 'contentWindow', { configurable: true, value: win })
      const send = (data: unknown) =>
        act(() => {
          window.dispatchEvent(new MessageEvent('message', { data, source: win }))
        })
      // No `activate`/ambient in the manifest → nothing plays on mount.
      expect(instances).toHaveLength(0)
      // Unknown trigger → silent (no Audio created).
      send({ type: 'theme:sound', trigger: 'does-not-exist' })
      expect(instances).toHaveLength(0)
      // Declared trigger → plays the manifest src at its declared volume.
      send({ type: 'theme:sound', trigger: 'notification' })
      expect(instances).toHaveLength(1)
      expect(instances[0].src).toContain('/api/theme/bikini-bottom/assets/notify.mp3')
      expect(instances[0].volume).toBe(0.5)
      expect(instances[0].play).toHaveBeenCalled()
      // Stops (pause + src cleared) once maxDuration (3s) elapses.
      act(() => {
        vi.advanceTimersByTime(3000)
      })
      expect(instances[0].pause).toHaveBeenCalled()
      expect(instances[0].src).toBe('')
      // Legacy bare `name` filename still plays (backward-compat).
      send({ type: 'theme:sound', name: 'chime.wav' })
      expect(instances).toHaveLength(2)
      expect(instances[1].src).toContain('/audio/chime.wav')
    } finally {
      ;(globalThis as unknown as { Audio: unknown }).Audio = origAudio
      vi.useRealTimers()
    }
  })

  // ── M2: activation effects must NOT re-fire on a theme-list rebuild ──
  // Installing/creating/deleting any theme rebuilds customThemeDataMap, handing
  // back a fresh `active`/`assets` object with identical content. That must not
  // replay the activate cue or stack a duplicate ambient loop.
  it('does not replay activate / duplicate the ambient loop when the theme list rebuilds with identical content', () => {
    const origAudio = (globalThis as unknown as { Audio: unknown }).Audio
    class FakeAudio {
      src: string
      loop = false
      volume = 1
      ended = false
      play = vi.fn(() => Promise.resolve())
      pause = vi.fn()
      addEventListener = vi.fn()
      constructor(src: string) {
        this.src = src
        instances.push(this)
      }
    }
    const instances: FakeAudio[] = []
    ;(globalThis as unknown as { Audio: unknown }).Audio = FakeAudio
    try {
      localStorage.setItem('mc-theme-consent-bikini-bottom', 'consented-v2')
      const audioAssets = l2Assets({
        overlays: [],
        audio: {
          triggers: { activate: { src: 'act.mp3', volume: 1, maxDuration: 2 } },
          ambient: { src: 'amb.mp3', volume: 0.3, loop: true },
        },
      })
      setTheme({ assets: audioAssets })
      const { rerender } = render(<ThemeExperienceLayer />)
      // On activation: the activate cue + the ambient bed are constructed once.
      expect(instances).toHaveLength(2)
      const activateCount = () => instances.filter((a) => a.src.includes('act.mp3')).length
      const ambientCount = () => instances.filter((a) => a.src.includes('amb.mp3')).length
      expect(activateCount()).toBe(1)
      expect(ambientCount()).toBe(1)
      // Simulate a theme-list rebuild: a BRAND-NEW map + theme object whose
      // `assets` is a fresh object graph with byte-identical content.
      setTheme({ assets: JSON.parse(JSON.stringify(audioAssets)) })
      act(() => {
        rerender(<ThemeExperienceLayer />)
      })
      // No second Audio was constructed; activate not replayed, no extra loop.
      expect(instances).toHaveLength(2)
      expect(activateCount()).toBe(1)
      expect(ambientCount()).toBe(1)
    } finally {
      ;(globalThis as unknown as { Audio: unknown }).Audio = origAudio
    }
  })

  it('still tears down old audio and plays the new theme activate on a real slug change', () => {
    const origAudio = (globalThis as unknown as { Audio: unknown }).Audio
    class FakeAudio {
      src: string
      loop = false
      volume = 1
      ended = false
      play = vi.fn(() => Promise.resolve())
      pause = vi.fn()
      addEventListener = vi.fn()
      constructor(src: string) {
        this.src = src
        instances.push(this)
      }
    }
    const instances: FakeAudio[] = []
    ;(globalThis as unknown as { Audio: unknown }).Audio = FakeAudio
    try {
      localStorage.setItem('mc-theme-consent-alpha-town', 'consented-v2')
      localStorage.setItem('mc-theme-consent-beta-town', 'consented-v2')
      const entry = (slug: string, actSrc: string) => ({
        slug,
        name: slug,
        emoji: '🎨',
        level: 2,
        assets: l2Assets({
          overlays: [],
          audio: { triggers: { activate: { src: actSrc, volume: 1, maxDuration: 2 } }, ambient: null },
        }),
      })
      const mapA = new Map<string, unknown>([['alpha-town', entry('alpha-town', 'actA.mp3')]])
      mockTheme = {
        theme: 'dark',
        colorTheme: 'custom-alpha-town',
        customThemeDataMap: mapA,
        setColorTheme,
      }
      const { rerender } = render(<ThemeExperienceLayer />)
      // Theme A activated → its activate cue plays.
      const aInstance = instances.find((a) => a.src.includes('actA.mp3'))
      expect(aInstance).toBeTruthy()
      // Real slug change → new map + theme B.
      const mapB = new Map<string, unknown>([['beta-town', entry('beta-town', 'actB.mp3')]])
      mockTheme = {
        theme: 'dark',
        colorTheme: 'custom-beta-town',
        customThemeDataMap: mapB,
        setColorTheme,
      }
      act(() => {
        rerender(<ThemeExperienceLayer />)
      })
      // A's audio was stopped …
      expect(aInstance!.pause).toHaveBeenCalled()
      // … and B's activate cue now plays.
      expect(instances.some((a) => a.src.includes('actB.mp3'))).toBe(true)
    } finally {
      ;(globalThis as unknown as { Audio: unknown }).Audio = origAudio
    }
  })

  // ── §8.2 persona-content-bound consent ──
  // Consent is bound to the persona's sha256 so a changed persona.md re-prompts,
  // legacy '1' grants are rejected, and the modal shows exactly what gets injected.
  describe('persona-bound consent', () => {
    const persona = (sha256: string, text = 'You are Karen, a sardonic computer.') => ({
      sha256,
      chars: text.length,
      text,
    })

    it('treats a legacy "1" grant as NOT consented and re-prompts', () => {
      localStorage.setItem('mc-theme-consent-bikini-bottom', '1')
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-abc') }) })
      const { container } = render(<ThemeExperienceLayer />)
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(frames(container)).toHaveLength(0)
    })

    it('honours a stored grant whose hash matches the current persona', () => {
      localStorage.setItem('mc-theme-consent-bikini-bottom', 'sha-abc')
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-abc') }) })
      const { container } = render(<ThemeExperienceLayer />)
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(frames(container).length).toBeGreaterThan(0)
    })

    it('re-prompts when the stored hash no longer matches (persona changed)', () => {
      // Grant recorded for an OLD persona hash; pack now ships a new one.
      localStorage.setItem('mc-theme-consent-bikini-bottom', 'sha-OLD')
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-NEW') }) })
      const { container } = render(<ThemeExperienceLayer />)
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(frames(container)).toHaveLength(0)
    })

    it('REVOKES the stale grant from localStorage when the persona sha256 changes (before the user answers)', () => {
      // A live grant for the OLD persona is present; the pack now ships a new sha.
      localStorage.setItem('mc-theme-consent-bikini-bottom', 'sha-OLD')
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-NEW') }) })
      const { container } = render(<ThemeExperienceLayer />)
      // Revoke-on-mismatch: the stale grant is deleted on mount, so it can't be
      // transmitted on the wire in the window before the re-prompt is answered.
      expect(localStorage.getItem('mc-theme-consent-bikini-bottom')).toBeNull()
      // …and the re-prompt is shown, nothing L2 mounted.
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(frames(container)).toHaveLength(0)
    })

    it('REVOKES a legacy "1" grant from localStorage on mount (still re-prompts)', () => {
      localStorage.setItem('mc-theme-consent-bikini-bottom', '1')
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-abc') }) })
      const { container } = render(<ThemeExperienceLayer />)
      expect(localStorage.getItem('mc-theme-consent-bikini-bottom')).toBeNull()
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(frames(container)).toHaveLength(0)
    })

    it('re-prompts after a re-install swaps the persona under a live grant', () => {
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-v1') }) })
      const { container, rerender } = render(<ThemeExperienceLayer />)
      // User consents to v1 → experience mounts, grant stored as the v1 hash.
      act(() => {
        screen.getByRole('button', { name: /enable experience/i }).click()
      })
      expect(localStorage.getItem('mc-theme-consent-bikini-bottom')).toBe('sha-v1')
      expect(frames(container).length).toBeGreaterThan(0)
      // Re-install swaps persona.md → new hash → the old grant no longer applies.
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-v2') }) })
      act(() => {
        rerender(<ThemeExperienceLayer />)
      })
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(frames(container)).toHaveLength(0)
    })

    it('renders the persona text verbatim in the consent modal', () => {
      const text = 'You are Karen. You disdain krabby patties.'
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-abc', text) }) })
      render(<ThemeExperienceLayer />)
      const pre = screen.getByTestId('consent-persona-text')
      expect(pre).toBeInTheDocument()
      expect(pre.textContent).toContain(text)
    })

    it('persists the persona sha256 (not "1") when consent is granted', async () => {
      setTheme({ assets: l2Assets({ personaInfo: persona('sha-xyz') }) })
      render(<ThemeExperienceLayer />)
      await userEvent.click(screen.getByRole('button', { name: /enable experience/i }))
      expect(localStorage.getItem('mc-theme-consent-bikini-bottom')).toBe('sha-xyz')
    })
  })
})
