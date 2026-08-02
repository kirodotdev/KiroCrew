import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import ChatFooter, { pickDistinct, resolveLoader, resolveLoaderIcons, SwapCarousel, STREAM_IDLE_MS } from '../pages/chat/ChatFooter'
import { GHOST_POSE_ICONS, GHOST_POSE_URLS } from '../components/GhostPoses'
import { registerThemeBranding } from '../themeBranding'

const base = { running: false, stopping: false, state: '', lastRole: '', avatar: '/logo.png', botName: 'KiroCrew' }

afterEach(() => document.documentElement.removeAttribute('data-theme'))

describe('ChatFooter', () => {
  it('returns null when not running', () => {
    const { container } = render(<ChatFooter {...base} />)
    expect(container.innerHTML).toBe('')
  })

  // The indicator must survive the WHOLE turn. It previously fell through the
  // guards in the gap after a tool completed, so it blinked out mid-turn.
  it('stays visible while a tool is running', () => {
    const { container } = render(<ChatFooter {...base} running={true} state="tool_running" lastRole="user" />)
    expect(container.querySelector('.csb4')).toBeInTheDocument()
  })

  it('stays visible in the post-tool gap (lastRole is tool)', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="tool" />)
    expect(container.querySelector('.csb4')).toBeInTheDocument()
  })

  // ...but yields while text streams, so it never doubles up with the real inline
  // .streaming-caret that MarkdownRenderer injects.
  it('is hidden while streaming', () => {
    const { container } = render(<ChatFooter {...base} running={true} state="streaming" lastRole="streaming" streamTick={1} />)
    expect(container.innerHTML).toBe('')
  })

  // The gap the user actually sees: the text block finished, the model is
  // generating a tool call, and NOTHING streams back. `lastRole` is still
  // 'streaming' (chat_segment is withheld until the tool ordering is settled), so
  // the loader used to stay hidden for the whole quiet window.
  it('takes over once the text stream goes quiet mid-turn', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<ChatFooter {...base} running={true} state="streaming" lastRole="streaming" streamTick={7} />)
      expect(container.innerHTML).toBe('')
      act(() => { vi.advanceTimersByTime(STREAM_IDLE_MS + 50) })
      expect(container.querySelector('.csb4')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('yields again as soon as chunks resume', () => {
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(<ChatFooter {...base} running={true} state="streaming" lastRole="streaming" streamTick={7} />)
      act(() => { vi.advanceTimersByTime(STREAM_IDLE_MS + 50) })
      expect(container.querySelector('.csb4')).toBeInTheDocument()
      // A new chunk advances the tick — back to the inline caret owning the state.
      rerender(<ChatFooter {...base} running={true} state="streaming" lastRole="streaming" streamTick={8} />)
      expect(container.innerHTML).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })

  // A tool group leaves the trailing 'streaming' message unfinalized for its whole
  // duration, so the role alone would hide the loader until post-tool text arrived.
  // The slot state moving off 'streaming' shows it immediately — no idle wait.
  it('shows during a tool group even though lastRole is still streaming', () => {
    const { container } = render(<ChatFooter {...base} running={true} state="tool_running" lastRole="streaming" streamTick={7} />)
    expect(container.querySelector('.csb4')).toBeInTheDocument()
  })

  it('shows stopping indicator', () => {
    render(<ChatFooter {...base} running={true} stopping={true} lastRole="user" />)
    expect(screen.getByText('Stopping…')).toBeInTheDocument()
  })

  it('shows compacting indicator', () => {
    render(<ChatFooter {...base} running={true} state="compacting" lastRole="user" />)
    expect(screen.getByText(/Compacting…/)).toBeInTheDocument()
  })

  it('renders 4 slots, each with both cross-fade layers', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    const slots = Array.from(container.querySelectorAll('.csb4 .slot'))
    expect(slots).toHaveLength(4)
    slots.forEach(s => {
      // The animation rides the .lyr wrapper, so the icon inside can be swapped
      // without restarting it.
      expect(s.querySelector('.lyr[data-layer="a"]')).toBeInTheDocument()
      expect(s.querySelector('.lyr[data-layer="b"]')).toBeInTheDocument()
    })
  })

  it('gives each layer 4 distinct icons, and the two layers differ', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    const layer = (l: string) => Array.from(container.querySelectorAll(`.lyr[data-layer="${l}"]`))
      .map(s => s.getAttribute('data-item'))
    const a = layer('a'), b = layer('b')
    expect(new Set(a).size).toBe(4)          // no duplicates within a layer
    expect(new Set(b).size).toBe(4)
    expect(a).not.toEqual(b)                 // the two visible groups differ
  })

  it('uses the bundled ghost poses on a Kiro theme', () => {
    document.documentElement.setAttribute('data-theme', 'kiro-dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    // Brand art is an ASSET rendered as <img> (use-lucide-icons brand-mark rule),
    // never inline SVG paths.
    const img = container.querySelector('.csb4 img.kp')
    expect(img).toBeInTheDocument()
    expect(GHOST_POSE_URLS).toContain(img!.getAttribute('src'))
    expect(container.querySelector('.lucide')).toBeNull()
  })

  it('renders no inline <svg> for the pose artwork', () => {
    document.documentElement.setAttribute('data-theme', 'kiro-dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelectorAll('.csb4 svg').length).toBe(0)
  })

  it('uses the default icons on a theme with no registered artwork', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelector('.csb4 svg')).toBeInTheDocument()   // lucide
    expect(container.querySelector('img.kp')).toBeNull()
  })
})

// A theme owns its loading indicator. It can replace the WHOLE loader, or just
// the artwork the default carousel cycles through.
describe('loader — theme seam', () => {
  // Stand-in icon. Deliberately NOT an inline <svg>: the use-lucide-icons rule
  // blocks added inline SVG across src/**/*.tsx, tests included.
  const Mark = () => <i className="seam-mark" />
  const CustomLoader = () => <div className="seam-custom-loader">custom</div>

  it('falls back to the default icons for an unregistered theme', () => {
    const got = resolveLoader('no-such-theme')
    expect(got.kind).toBe('icons')
    expect(got.kind === 'icons' && got.icons.length).toBeGreaterThanOrEqual(4)
  })

  it('serves the bundled artwork for a core theme', () => {
    const got = resolveLoader('kiro')
    expect(got.kind === 'icons' && got.icons).toBe(GHOST_POSE_ICONS)
  })

  it('lets a newly registered theme supply its own icons', () => {
    const icons = [Mark, Mark, Mark, Mark, Mark]
    registerThemeBranding({ 'seam-loader-theme': { loaderIcons: icons } })
    expect(resolveLoaderIcons('seam-loader-theme')).toBe(icons)
  })

  it('renders a registered theme’s icons in the footer', () => {
    registerThemeBranding({ 'seam-render-theme': { loaderIcons: [Mark, Mark, Mark, Mark] } })
    document.documentElement.setAttribute('data-theme', 'seam-render-theme-dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelectorAll('.seam-mark').length).toBe(8)  // 4 slots x 2 layers
    expect(container.querySelector('.lucide')).toBeNull()
  })

  // The broader seam: a theme is not limited to swapping icons.
  it('lets a theme replace the ENTIRE loader with its own component', () => {
    registerThemeBranding({ 'seam-whole': { loader: CustomLoader } })
    const got = resolveLoader('seam-whole')
    expect(got.kind).toBe('custom')
    expect(got.kind === 'custom' && got.Component).toBe(CustomLoader)
  })

  it('renders a theme’s custom loader instead of the carousel', () => {
    registerThemeBranding({ 'seam-whole-render': { loader: CustomLoader } })
    document.documentElement.setAttribute('data-theme', 'seam-whole-render-dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelector('.seam-custom-loader')).toBeInTheDocument()
    expect(container.querySelector('.csb4')).toBeNull()   // stock carousel stood down
  })

  it('gives `loader` precedence over `loaderIcons`', () => {
    registerThemeBranding({ 'seam-both': { loader: CustomLoader, loaderIcons: [Mark, Mark, Mark, Mark] } })
    const got = resolveLoader('seam-both')
    expect(got.kind).toBe('custom')
  })

  it('still honours the turn-visibility rules for a custom loader', () => {
    registerThemeBranding({ 'seam-whole-hidden': { loader: CustomLoader } })
    document.documentElement.setAttribute('data-theme', 'seam-whole-hidden-dark')
    // streaming -> hidden (the real caret owns that state)
    const streaming = render(<ChatFooter {...base} running={true} state="streaming" lastRole="streaming" streamTick={1} />)
    expect(streaming.container.innerHTML).toBe('')
    // idle -> hidden
    const idle = render(<ChatFooter {...base} />)
    expect(idle.container.innerHTML).toBe('')
  })

  it('a custom loader does not take over the stopping / compacting states', () => {
    registerThemeBranding({ 'seam-whole-states': { loader: CustomLoader } })
    document.documentElement.setAttribute('data-theme', 'seam-whole-states-dark')
    render(<ChatFooter {...base} running={true} state="compacting" lastRole="user" />)
    expect(screen.getByText(/Compacting…/)).toBeInTheDocument()
  })

  it('a registered theme can override bundled artwork', () => {
    const icons = [Mark, Mark, Mark, Mark]
    registerThemeBranding({ 'seam-override': { loaderIcons: icons } })
    expect(resolveLoaderIcons('seam-override')).toBe(icons)
  })

  // The loader is decorative and theme-supplied. Unguarded, a component that
  // throws escapes to the ROUTE-level ErrorBoundary and replaces the whole chat
  // UI with the error card — a third-party theme bug taking out the app. Both
  // branches render inside ErrorBoundary fallback={null}, so it collapses to
  // nothing instead.
  it('contains a throwing custom loader instead of losing the chat', () => {
    const Boom = () => { throw new Error('theme loader exploded') }
    registerThemeBranding({ 'seam-throwing-loader': { loader: Boom } })
    document.documentElement.setAttribute('data-theme', 'seam-throwing-loader-dark')
    expect(() =>
      render(<ChatFooter {...base} running={true} lastRole="user" />)
    ).not.toThrow()
  })

  it('contains a throwing loader ICON the same way', () => {
    const Boom = () => { throw new Error('theme icon exploded') }
    registerThemeBranding({ 'seam-throwing-icon': { loaderIcons: [Boom, Boom, Boom, Boom] } })
    document.documentElement.setAttribute('data-theme', 'seam-throwing-icon-dark')
    expect(() =>
      render(<ChatFooter {...base} running={true} lastRole="user" />)
    ).not.toThrow()
  })

  it('ignores an empty registered pool and keeps the defaults', () => {
    registerThemeBranding({ 'seam-empty': { loaderIcons: [] } })
    expect(resolveLoaderIcons('seam-empty').length).toBeGreaterThanOrEqual(4)
  })

  it('a theme registering only a logo still gets the default icons', () => {
    registerThemeBranding({ 'seam-logo-only': { logo: '/x.png' } })
    expect(resolveLoaderIcons('seam-logo-only').length).toBeGreaterThanOrEqual(4)
  })

  // Regression: switching to a theme with a SMALLER pool changes `icons` during
  // render while `sets` still holds the old pool's indices (the re-seed effect
  // runs afterwards). Rendering `icons[7]` of a 4-icon pool yields undefined, and
  // `<undefined />` throws "Element type is invalid", taking the footer down.
  // Driven through SwapCarousel directly: a data-theme change updates via
  // MutationObserver, which does not flush synchronously, so a theme-level test
  // cannot actually fail here. Repeated because which indices were sampled is
  // random — over this many shrinks an out-of-range index is a near-certainty.
  it('survives the pool shrinking under it (stale indices)', () => {
    const big = Array.from({ length: 8 }, () => Mark)
    const small = Array.from({ length: 2 }, () => Mark)
    for (let i = 0; i < 25; i++) {
      const { rerender, container, unmount } = render(<SwapCarousel icons={big} />)
      expect(() => rerender(<SwapCarousel icons={small} />)).not.toThrow()
      // Every layer still renders a real icon — no blank slot.
      container.querySelectorAll('.lyr').forEach(l => expect(l.firstChild).not.toBeNull())
      unmount()
    }
  })

  it('renders nothing rather than crashing on an empty pool', () => {
    const { container } = render(<SwapCarousel icons={[]} />)
    expect(container.innerHTML).toBe('')
  })

  // The mirror of the shrink case, and the one that actually bit: a pool that
  // GROWS. Sets seeded from a 3-icon pool hold only 3 indices, but 4 slots now
  // render, so sets.a[3] is undefined -> `undefined % 8` is NaN -> icons[NaN] is
  // undefined -> `<undefined />` throws. Slot count must follow the sets too.
  it('survives the pool growing under it (sets shorter than the slot count)', () => {
    const small = Array.from({ length: 3 }, () => Mark)
    const big = Array.from({ length: 8 }, () => Mark)
    for (let i = 0; i < 10; i++) {
      const { rerender, container, unmount } = render(<SwapCarousel icons={small} />)
      expect(() => rerender(<SwapCarousel icons={big} />)).not.toThrow()
      container.querySelectorAll('.lyr').forEach(l => expect(l.firstChild).not.toBeNull())
      unmount()
    }
  })

  it('reports item indices inside the current pool after a pool change', () => {
    const big = Array.from({ length: 8 }, () => Mark)
    const small = Array.from({ length: 2 }, () => Mark)
    const { rerender, container } = render(<SwapCarousel icons={big} />)
    rerender(<SwapCarousel icons={small} />)
    Array.from(container.querySelectorAll('.lyr')).forEach(l => {
      const idx = Number(l.getAttribute('data-item'))
      expect(idx).toBeGreaterThanOrEqual(0)
      expect(idx).toBeLessThan(2)
    })
  })
})

// The slug is recovered from <html data-theme>, which applyTheme composes as
// `<slug>-<mode>` (bare `<mode>` for the base theme).
describe('theme slug recovery', () => {
  // Stand-in icon. Deliberately NOT an inline <svg>: the use-lucide-icons rule
  // blocks added inline SVG across src/**/*.tsx, tests included.
  const Mark = () => <i className="seam-mark" />

  it('resolves a suffixed slug to the theme, not the mode', () => {
    registerThemeBranding({ 'seam-slug': { loaderIcons: [Mark, Mark, Mark, Mark] } })
    document.documentElement.setAttribute('data-theme', 'seam-slug-light')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelectorAll('.seam-mark').length).toBe(8)
  })

  // Regression: a loose /-?(dark|light)$/ also ate the tail of a slug that merely
  // ENDS in those letters — 'highlight' became 'high' and lost its registration.
  it('does not mangle a slug ending in the letters of a mode', () => {
    registerThemeBranding({ highlight: { loaderIcons: [Mark, Mark, Mark, Mark] } })
    document.documentElement.setAttribute('data-theme', 'highlight-dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelectorAll('.seam-mark').length).toBe(8)
  })

  it('treats a bare mode as the base theme', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    // Base theme registers no artwork -> default icons, not a crash or blank.
    expect(container.querySelector('.csb4 svg')).toBeInTheDocument()
  })
})

describe('pickDistinct', () => {
  const total = GHOST_POSE_ICONS.length

  it('exposes 8 ghost poses from the design system', () => {
    expect(total).toBe(8)
  })

  it('always returns 4 distinct indices', () => {
    for (let i = 0; i < 200; i++) {
      const got = pickDistinct(total)
      expect(got).toHaveLength(4)
      expect(new Set(got).size).toBe(4)
      got.forEach(p => expect(p).toBeGreaterThanOrEqual(0))
      got.forEach(p => expect(p).toBeLessThan(total))
    }
  })

  it('never repeats a set it is told to avoid, so every swap visibly changes', () => {
    let prev = pickDistinct(total)
    for (let i = 0; i < 200; i++) {
      const next = pickDistinct(total, prev)
      expect(next).not.toEqual(prev)
      prev = next
    }
  })

  it('can avoid multiple sets at once (its own previous AND the other layer)', () => {
    for (let i = 0; i < 200; i++) {
      const a = pickDistinct(total)
      const b = pickDistinct(total, a)
      const next = pickDistinct(total, a, b)
      expect(next).not.toEqual(a)
      expect(next).not.toEqual(b)
    }
  })

  it('actually varies its selection across loops', () => {
    const seen = new Set<string>()
    let prev: number[] | undefined
    for (let i = 0; i < 60; i++) {
      prev = pickDistinct(total, prev)
      seen.add(prev.join(','))
    }
    expect(seen.size).toBeGreaterThan(5)
  })

  it('degrades safely when a theme ships fewer icons than slots', () => {
    const got = pickDistinct(3)
    expect(got).toHaveLength(3)
    expect(new Set(got).size).toBe(3)
  })
})
