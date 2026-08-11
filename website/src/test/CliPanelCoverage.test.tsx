/**
 * Behavioural coverage for CliPanel — the terminal pane host.
 *
 * A real xterm `Terminal` cannot boot under the test DOM (it measures a canvas
 * character cell), so this file follows the same approach as
 * `TerminalCompletion.test.tsx`: substitute a minimal xterm stand-in and drive
 * the contract CliPanel actually depends on (open/focus/selection events,
 * mutable `options`, `element.offsetParent`). What is under test here is
 * CliPanel's own logic — DOM attach, visibility gating, the floating selection
 * toolbar (clamping, copy, send-to-chat redaction handoff), the module-level
 * theme/font observers, and the teardown + delete-session exports.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor, cleanup } from '@testing-library/react'
import { renderWithProviders, renderHookWithProviders } from './helpers'
import { i18nT } from '../i18n/t'

/* ── xterm stand-in ───────────────────────────────────────────────────────── */

const xt = vi.hoisted(() => {
  interface FakeOptions {
    cursorBlink?: boolean
    fontSize?: number
    fontFamily?: string
    theme?: Record<string, string>
  }
  class FakeTerminal {
    static instances: FakeTerminal[] = []
    options: FakeOptions
    element: HTMLElement | undefined = undefined
    /** Ordered log of every fontFamily write — proves the re-measure toggle. */
    fontFamilyWrites: string[] = []
    openCalls = 0
    focusCalls = 0
    disposed = false
    clearCalls = 0
    addons: unknown[] = []
    selection = ''
    selectionListeners: (() => void)[] = []
    scrollListeners: (() => void)[] = []
    /** Controls `remeasureAndFit`'s laid-out-pane guard. */
    offsetParent: unknown = {}

    constructor(options: FakeOptions) {
      // Proxy fontFamily so writes are observable without hiding the value.
      this.options = new Proxy(options, {
        set: (target: FakeOptions, key: string, value: unknown) => {
          if (key === 'fontFamily') this.fontFamilyWrites.push(String(value))
          Reflect.set(target, key, value)
          return true
        },
      }) as FakeOptions
      FakeTerminal.instances.push(this)
    }
    loadAddon(addon: unknown) { this.addons.push(addon) }
    open(container: HTMLElement) {
      this.openCalls += 1
      const el = document.createElement('div')
      el.className = 'xterm'
      Object.defineProperty(el, 'offsetParent', { get: () => this.offsetParent, configurable: true })
      container.appendChild(el)
      this.element = el
    }
    focus() { this.focusCalls += 1 }
    dispose() { this.disposed = true }
    hasSelection() { return this.selection.length > 0 }
    getSelection() { return this.selection }
    clearSelection() { this.clearCalls += 1; this.selection = '' }
    onSelectionChange(cb: () => void) {
      this.selectionListeners.push(cb)
      return { dispose: () => { this.selectionListeners = this.selectionListeners.filter(c => c !== cb) } }
    }
    onScroll(cb: () => void) {
      this.scrollListeners.push(cb)
      return { dispose: () => { this.scrollListeners = this.scrollListeners.filter(c => c !== cb) } }
    }
  }
  class FakeFitAddon {
    static instances: FakeFitAddon[] = []
    fit = vi.fn()
    constructor() { FakeFitAddon.instances.push(this) }
  }
  return { FakeTerminal, FakeFitAddon }
})

vi.mock('@xterm/xterm', () => ({ Terminal: xt.FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: xt.FakeFitAddon }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class {} }))

const registry = vi.hoisted(() => ({
  ensureTerminalConnection: vi.fn(),
  disposeTerminalConnection: vi.fn(),
  getTerminalCwd: vi.fn<(id: string) => string | undefined>(() => undefined),
}))
vi.mock('../utils/terminalRegistry', () => registry)

// Both children own their own xterm hooks and are covered by their own suites;
// stub them so this file exercises CliPanel alone.
vi.mock('../components/TerminalCompletion', () => ({ default: () => <div data-testid="completion" /> }))
vi.mock('../components/TerminalKeyBar', () => ({ default: () => <div data-testid="key-bar" /> }))

const touch = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => touch.value }))

import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from '../components/CliPanel'
import { setTerminalFontSize, __resetTerminalFontStore } from '../hooks/useTerminalFont'

/* ── harness ──────────────────────────────────────────────────────────────── */

const SEND_LABEL = i18nT('components.cliPanel.send_to_chat')
const COPY_LABEL = i18nT('components.cliPanel.copy')
const COPIED_LABEL = i18nT('components.cliPanel.copied')
const COPY_FAILED_LABEL = i18nT('components.cliPanel.copy_failed')
const SEND_FAILED_LABEL = i18nT('components.cliPanel.failed_retry')

/** Layout numbers for the clamp maths — pane 400x300, toolbar 120x32. */
const boxProps = ['clientWidth', 'clientHeight', 'offsetWidth', 'offsetHeight'] as const
const savedBox = new Map<string, PropertyDescriptor | undefined>()
const sizes: Record<string, number> = { clientWidth: 400, clientHeight: 300, offsetWidth: 120, offsetHeight: 32 }
function stubLayout() {
  sizes.clientWidth = 400
  sizes.clientHeight = 300
  sizes.offsetWidth = 120
  sizes.offsetHeight = 32
  for (const p of boxProps) {
    savedBox.set(p, Object.getOwnPropertyDescriptor(HTMLElement.prototype, p))
    Object.defineProperty(HTMLElement.prototype, p, { get: () => sizes[p], configurable: true })
  }
}
function restoreLayout() {
  for (const p of boxProps) {
    const d = savedBox.get(p)
    if (d) Object.defineProperty(HTMLElement.prototype, p, d)
    else Reflect.deleteProperty(HTMLElement.prototype, p)
  }
  savedBox.clear()
}

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true, writable: true })
}

let seq = 0
/** Unique id per test — `termCache` is module-level and outlives a render. */
function nextId() { seq += 1; return `pty-${seq}` }

const live = new Set<string>()
function mount(props: Partial<{ sessionId: string; cwd: string; visible: boolean; onSendToChat: (t: string) => void }> = {}) {
  const sessionId = props.sessionId ?? nextId()
  live.add(sessionId)
  const view = renderWithProviders(
    <CliPanel
      sessionId={sessionId}
      cwd={props.cwd}
      visible={props.visible ?? true}
      onSendToChat={props.onSendToChat}
    />,
  )
  const term = xt.FakeTerminal.instances[xt.FakeTerminal.instances.length - 1]
  const fit = xt.FakeFitAddon.instances[xt.FakeFitAddon.instances.length - 1]
  return { ...view, sessionId, term, fit }
}

/** Finish the drag: the toolbar appears on the wrapper's mouseup. */
function endDrag(container: HTMLElement, at: { x: number; y: number }) {
  const wrap = container.firstElementChild!.firstElementChild as HTMLElement
  act(() => { fireEvent.mouseUp(wrap, { clientX: at.x, clientY: at.y }) })
  return wrap
}

beforeEach(() => {
  // Synchronous animation frames: CliPanel defers the selection read and the
  // theme/font refreshes by one frame, and a test must not wait on real ones.
  // Returning 0 matters — the coalescing guards store this handle and clear it
  // to 0 inside the callback, so a non-zero return would land AFTER the clear
  // and wedge every later refresh behind a permanently "pending" frame.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vi.stubGlobal('fetch', vi.fn())
  registry.ensureTerminalConnection.mockClear()
  registry.disposeTerminalConnection.mockClear()
  registry.getTerminalCwd.mockReset()
  registry.getTerminalCwd.mockReturnValue(undefined)
  xt.FakeTerminal.instances = []
  xt.FakeFitAddon.instances = []
  touch.value = false
  stubLayout()
  setClipboard({ writeText: vi.fn(() => Promise.resolve()) })
})

afterEach(() => {
  cleanup()
  for (const id of live) disposeTerminalSession(id)
  live.clear()
  restoreLayout()
  __resetTerminalFontStore()
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('data-theme')
  for (const s of Array.from(document.head.querySelectorAll('style'))) {
    if (s.id.startsWith('mc-custom-theme-') || s.id === 'unrelated-style') s.remove()
  }
})

/* ── mount / attach ───────────────────────────────────────────────────────── */

describe('CliPanel mount', () => {
  it('opens one xterm into the pane, fits it, and wires the persistent connection', () => {
    const { term, fit, sessionId, container } = mount({ cwd: '/srv/app' })
    expect(term.openCalls).toBe(1)
    expect(container.querySelector('.xterm')).not.toBeNull()
    expect(fit.fit).toHaveBeenCalled()
    expect(registry.ensureTerminalConnection).toHaveBeenCalledWith(sessionId, term, fit, '/srv/app')
    // A visible pane takes focus and is re-measured (monospace toggle + restore).
    expect(term.focusCalls).toBe(1)
    expect(term.fontFamilyWrites.slice(-2)).toEqual(['monospace', term.options.fontFamily])
  })

  it('loads the fit and web-links addons and seeds the theme from CSS custom properties', () => {
    const { term } = mount()
    expect(term.addons).toHaveLength(2)
    // No custom properties are set in the test document, so every slot falls
    // back to its hard-coded default rather than an empty string.
    expect(term.options.theme).toEqual({
      background: '#1e1e2e',
      foreground: '#cdd6f4',
      cursor: '#89b4fa',
      selectionBackground: '#313244',
    })
  })

  it('reads the theme from --bg/--text/--accent when the document defines them', () => {
    document.documentElement.style.setProperty('--bg', '#101010')
    document.documentElement.style.setProperty('--text', '#f0f0f0')
    try {
      const { term } = mount()
      expect(term.options.theme?.background).toBe('#101010')
      expect(term.options.theme?.foreground).toBe('#f0f0f0')
    } finally {
      document.documentElement.style.removeProperty('--bg')
      document.documentElement.style.removeProperty('--text')
    }
  })

  it('hides the pane and skips focus when not visible', () => {
    const { term, container } = mount({ visible: false })
    const wrap = container.firstElementChild!.firstElementChild as HTMLElement
    expect(wrap.style.display).toBe('none')
    expect(term.focusCalls).toBe(0)
  })

  it('re-attaches the cached xterm on remount instead of opening a second one', () => {
    const sessionId = nextId()
    const first = mount({ sessionId })
    cleanup()
    const second = mount({ sessionId })
    expect(second.term).toBe(first.term)
    expect(xt.FakeTerminal.instances).toHaveLength(1)
    expect(first.term.openCalls).toBe(1) // second mount took the appendChild path
    expect(second.container.querySelector('.xterm')).not.toBeNull()
  })

  it('renders the soft key bar only on a touch device', () => {
    mount()
    expect(screen.queryByTestId('key-bar')).toBeNull()
    cleanup()
    touch.value = true
    mount()
    expect(screen.getByTestId('key-bar')).toBeInTheDocument()
  })

  it('refits when the pane is resized, and skips a pane that has collapsed to zero height', () => {
    const observers: ResizeObserverCallback[] = []
    class CapturingResizeObserver {
      constructor(cb: ResizeObserverCallback) { observers.push(cb) }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', CapturingResizeObserver)
    const { fit } = mount()
    fit.fit.mockClear()
    const notify = () => act(() => {
      for (const cb of observers) cb([], {} as ResizeObserver)
    })
    notify()
    expect(fit.fit).toHaveBeenCalledTimes(1)
    sizes.offsetHeight = 0 // collapsed / hidden pane
    notify()
    expect(fit.fit).toHaveBeenCalledTimes(1) // no second fit
  })
})

/* ── selection toolbar ────────────────────────────────────────────────────── */

describe('CliPanel selection toolbar', () => {
  it('stays hidden when the drag selected nothing but whitespace', () => {
    const { term, container } = mount({ onSendToChat: vi.fn() })
    term.selection = '   \n '
    endDrag(container, { x: 200, y: 100 })
    expect(screen.queryByRole('button', { name: COPY_LABEL })).toBeNull()
  })

  it('stays hidden when xterm reports no selection at all on mouseup', () => {
    const { term, container } = mount({ onSendToChat: vi.fn() })
    term.selection = '' // hasSelection() false → nothing is captured
    endDrag(container, { x: 200, y: 100 })
    expect(screen.queryByRole('button', { name: COPY_LABEL })).toBeNull()
  })

  it('keeps the toolbar when the selection merely changes rather than clearing', () => {
    const { term, container } = mount()
    term.selection = 'first'
    endDrag(container, { x: 200, y: 100 })
    term.selection = 'grown wider'
    act(() => { for (const cb of term.selectionListeners) cb() })
    expect(screen.getByRole('button', { name: COPY_LABEL })).toBeInTheDocument()
  })

  it('appears above the pointer with both actions once text is selected', () => {
    const { term, container } = mount({ onSendToChat: vi.fn() })
    term.selection = 'total 12'
    endDrag(container, { x: 200, y: 100 })
    const copy = screen.getByRole('button', { name: COPY_LABEL })
    expect(copy).toBeInTheDocument()
    expect(screen.getByRole('button', { name: SEND_LABEL })).toBeInTheDocument()
    // left = clamp(200 - 120/2) = 140; top = 100 - 32 - 8 = 60 (room above)
    const bar = copy.parentElement as HTMLElement
    expect(bar.style.left).toBe('140px')
    expect(bar.style.top).toBe('60px')
    expect(bar.style.opacity).toBe('1')
  })

  it('clamps to the left edge and flips below the line when there is no room above', () => {
    const { term, container } = mount({ onSendToChat: vi.fn() })
    term.selection = 'x'
    endDrag(container, { x: 4, y: 10 })
    const bar = screen.getByRole('button', { name: COPY_LABEL }).parentElement as HTMLElement
    expect(bar.style.left).toBe('8px')  // 4 - 60 is off-pane → margin
    expect(bar.style.top).toBe('26px')  // 10 - 40 < 8 → drop below (10 + 16)
  })

  it('omits the send action when the host provides no chat sink', () => {
    const { term, container } = mount()
    term.selection = 'ls -la'
    endDrag(container, { x: 200, y: 100 })
    expect(screen.getByRole('button', { name: COPY_LABEL })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: SEND_LABEL })).toBeNull()
  })

  it('dismisses itself when xterm reports the selection was cleared', () => {
    const { term, container } = mount()
    term.selection = 'kept'
    endDrag(container, { x: 200, y: 100 })
    expect(screen.getByRole('button', { name: COPY_LABEL })).toBeInTheDocument()
    term.selection = ''
    act(() => { for (const cb of term.selectionListeners) cb() })
    expect(screen.queryByRole('button', { name: COPY_LABEL })).toBeNull()
  })

  it('dismisses itself when the viewport scrolls under it', () => {
    const { term, container } = mount()
    term.selection = 'kept'
    endDrag(container, { x: 200, y: 100 })
    act(() => { for (const cb of term.scrollListeners) cb() })
    expect(screen.queryByRole('button', { name: COPY_LABEL })).toBeNull()
  })

  it('prevents the default on its own mousedown so pressing a button cannot restart a drag', () => {
    const { term, container } = mount()
    term.selection = 'kept'
    endDrag(container, { x: 200, y: 100 })
    const bar = screen.getByRole('button', { name: COPY_LABEL }).parentElement as HTMLElement
    expect(fireEvent.mouseDown(bar, { clientX: 10, clientY: 10 })).toBe(false)
    expect(screen.getByRole('button', { name: COPY_LABEL })).toBeInTheDocument()
  })

  it('survives a mouseup on itself while the selection is still live', () => {
    const { term, container } = mount()
    term.selection = 'kept'
    endDrag(container, { x: 200, y: 100 })
    const bar = screen.getByRole('button', { name: COPY_LABEL }).parentElement as HTMLElement
    act(() => { fireEvent.mouseUp(bar, { clientX: 200, clientY: 100 }) })
    expect(screen.getByRole('button', { name: COPY_LABEL })).toBeInTheDocument()
  })

  it('re-measures the toolbar position for a second selection', () => {
    const { term, container } = mount()
    term.selection = 'first'
    endDrag(container, { x: 200, y: 100 })
    expect((screen.getByRole('button', { name: COPY_LABEL }).parentElement as HTMLElement).style.top).toBe('60px')
    term.selection = 'second'
    endDrag(container, { x: 60, y: 200 })
    const bar = screen.getByRole('button', { name: COPY_LABEL }).parentElement as HTMLElement
    expect(bar.style.left).toBe('8px')
    expect(bar.style.top).toBe('160px')
  })
})

/* ── copy ─────────────────────────────────────────────────────────────────── */

describe('CliPanel copy action', () => {
  it('copies the captured text, confirms, then dismisses the toolbar', async () => {
    // Only the timer is faked: vitest's default fake set includes
    // requestAnimationFrame, which would displace the synchronous stub the
    // deferred selection read depends on.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const writeText = vi.fn(() => Promise.resolve())
      setClipboard({ writeText })
      const { term, container } = mount()
      term.selection = 'drwxr-xr-x  4 user'
      endDrag(container, { x: 200, y: 100 })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: COPY_LABEL })) })
      expect(writeText).toHaveBeenCalledWith('drwxr-xr-x  4 user')
      expect(screen.getByRole('button', { name: COPIED_LABEL })).toBeInTheDocument()
      // The confirmation lingers deliberately, then the toolbar goes away.
      await act(async () => { await vi.advanceTimersByTimeAsync(900) })
      expect(screen.queryByRole('button', { name: COPIED_LABEL })).toBeNull()
      expect(term.clearCalls).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports failure and keeps the selection when the clipboard is unavailable', () => {
    setClipboard(undefined)
    const { term, container } = mount()
    term.selection = 'secret-free output'
    endDrag(container, { x: 200, y: 100 })
    act(() => { fireEvent.click(screen.getByRole('button', { name: COPY_LABEL })) })
    expect(screen.getByRole('button', { name: COPY_FAILED_LABEL })).toBeInTheDocument()
    expect(term.clearCalls).toBe(0)
  })

  it('reports failure when the clipboard write is rejected', async () => {
    setClipboard({ writeText: vi.fn(() => Promise.reject(new Error('denied'))) })
    const { term, container } = mount()
    term.selection = 'output'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: COPY_LABEL })) })
    expect(screen.getByRole('button', { name: COPY_FAILED_LABEL })).toBeInTheDocument()
    expect(term.clearCalls).toBe(0)
  })
})

/* ── send to chat ─────────────────────────────────────────────────────────── */

/** Answer POST /api/terminal/redact with `text`, or a failure status. */
function mockRedact(text: string, ok = true, status = 200) {
  const f = vi.fn(() => Promise.resolve({
    ok, status, json: () => Promise.resolve({ text }),
  } as unknown as Response))
  vi.stubGlobal('fetch', f)
  return f
}

describe('CliPanel send-to-chat', () => {
  it('redacts the whole selection server-side, then hands off a fenced block with the live cwd', async () => {
    const f = mockRedact('token REDACTED here')
    registry.getTerminalCwd.mockReturnValue('/srv/app/sub')
    const onSendToChat = vi.fn()
    const { term, container, sessionId } = mount({ cwd: '/srv/app', onSendToChat })
    term.selection = 'token abc123 here'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })

    expect(f).toHaveBeenCalledWith('/api/terminal/redact', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ text: 'token abc123 here' }),
    }))
    expect(registry.getTerminalCwd).toHaveBeenCalledWith(sessionId)
    // Live cwd wins over the spawn dir, and the fence is the default three ticks.
    expect(onSendToChat).toHaveBeenCalledWith(
      'Terminal output (`/srv/app/sub`):\n```\ntoken REDACTED here\n```',
    )
    expect(term.clearCalls).toBe(1)
    expect(screen.queryByRole('button', { name: SEND_LABEL })).toBeNull()
  })

  it('falls back to the spawn cwd when the live cwd is unknown', async () => {
    mockRedact('plain')
    const onSendToChat = vi.fn()
    const { term, container } = mount({ cwd: '/spawn/dir', onSendToChat })
    term.selection = 'plain'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })
    expect(onSendToChat).toHaveBeenCalledWith('Terminal output (`/spawn/dir`):\n```\nplain\n```')
  })

  it('omits the path entirely when no cwd is known', async () => {
    mockRedact('plain')
    const onSendToChat = vi.fn()
    const { term, container } = mount({ onSendToChat })
    term.selection = 'plain'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })
    expect(onSendToChat).toHaveBeenCalledWith('Terminal output:\n```\nplain\n```')
  })

  it('widens the fence past the longest backtick run in the output', async () => {
    mockRedact('see ```code``` and ````wide```` here')
    const onSendToChat = vi.fn()
    const { term, container } = mount({ onSendToChat })
    term.selection = 'anything'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })
    const handoff = onSendToChat.mock.calls[0][0] as string
    expect(handoff).toContain('\n`````\nsee ```code``` and ````wide```` here\n`````')
  })

  it('fails closed on a redaction error: nothing is inserted and the selection survives', async () => {
    mockRedact('', false, 500)
    const onSendToChat = vi.fn()
    const { term, container } = mount({ onSendToChat })
    term.selection = 'token abc123'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })
    expect(onSendToChat).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: SEND_FAILED_LABEL })).toBeInTheDocument()
    expect(term.clearCalls).toBe(0) // toolbar kept so the user can retry
  })

  it('fails closed when the redaction request itself throws', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline'))))
    const onSendToChat = vi.fn()
    const { term, container } = mount({ onSendToChat })
    term.selection = 'output'
    endDrag(container, { x: 200, y: 100 })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: SEND_LABEL })) })
    expect(onSendToChat).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: SEND_FAILED_LABEL })).toBeInTheDocument()
  })
})

/* ── theme + font observers ───────────────────────────────────────────────── */

describe('CliPanel theme and font sync', () => {
  it('repaints cached terminals when the document theme attribute flips', async () => {
    const { term } = mount()
    document.documentElement.style.setProperty('--bg', '#222233')
    try {
      act(() => { document.documentElement.setAttribute('data-theme', 'legacy-default') })
      await waitFor(() => expect(term.options.theme?.background).toBe('#222233'))
    } finally {
      document.documentElement.style.removeProperty('--bg')
    }
  })

  it('repaints when a custom-theme style element resolves into <head>', async () => {
    const { term } = mount()
    // Drain records queued by earlier tests so the only mutation the observer
    // sees here is the <head> insertion (a custom theme's vars arrive that way,
    // with no data-theme change to notice).
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    const style = document.createElement('style')
    style.id = 'mc-custom-theme-probe'
    style.textContent = ':root { --accent: #ff8800; }'
    act(() => { document.head.appendChild(style) })
    await waitFor(() => expect(term.options.theme?.cursor).toBe('#ff8800'))
  })

  it('ignores an unrelated style element added to <head>', async () => {
    const { term } = mount()
    // Drain any observer records queued by earlier tests before measuring.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    const before = term.options.theme
    const style = document.createElement('style')
    style.id = 'unrelated-style'
    act(() => { document.head.appendChild(style) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(term.options.theme).toBe(before) // same object → no refresh ran
  })

  it('pushes a font-size preference change onto every live terminal and refits', () => {
    const { term, fit } = mount()
    fit.fit.mockClear()
    act(() => { setTerminalFontSize(19) })
    expect(term.options.fontSize).toBe(19)
    expect(term.fontFamilyWrites.slice(-2)).toEqual(['monospace', term.options.fontFamily])
    expect(fit.fit).toHaveBeenCalled()
  })

  it('skips the refit for a hidden pane whose cell would measure zero', () => {
    const { term, fit } = mount()
    term.offsetParent = null // display:none / detached
    fit.fit.mockClear()
    const writesBefore = term.fontFamilyWrites.length
    act(() => { setTerminalFontSize(21) })
    expect(term.options.fontSize).toBe(21) // option still applied
    // Only the plain family assignment — no monospace/restore re-measure toggle.
    expect(term.fontFamilyWrites).toHaveLength(writesBefore + 1)
    expect(fit.fit).not.toHaveBeenCalled()
  })
})

/* ── web font readiness ───────────────────────────────────────────────────── */

describe('CliPanel web-font refit', () => {
  const original = Object.getOwnPropertyDescriptor(Document.prototype, 'fonts')
  function setFonts(value: unknown) {
    Object.defineProperty(document, 'fonts', { value, configurable: true })
  }
  afterEach(() => {
    Reflect.deleteProperty(document, 'fonts')
    if (original) Object.defineProperty(Document.prototype, 'fonts', original)
  })

  it('refits once the pending web fonts settle and after explicitly loading the terminal font', async () => {
    const load = vi.fn(() => Promise.resolve([]))
    setFonts({ ready: Promise.resolve(), load })
    const { term, fit } = mount()
    await act(async () => { await Promise.resolve() })
    expect(load).toHaveBeenCalledWith(expect.stringContaining('"JetBrains Mono"'))
    expect(fit.fit).toHaveBeenCalled()
    expect(term.fontFamilyWrites).toContain('monospace')
  })

  it('survives an engine that rejects the font-load spec', async () => {
    setFonts({ ready: Promise.resolve(), load: () => { throw new Error('bad spec') } })
    const { fit } = mount()
    await act(async () => { await Promise.resolve() })
    expect(fit.fit).toHaveBeenCalled() // the `ready` handler still refits
  })

  it('swallows a rejected font load without escaping the effect', async () => {
    setFonts({ ready: Promise.resolve(), load: () => Promise.reject(new Error('nope')) })
    mount()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByTestId('completion')).toBeInTheDocument()
  })

  it('no-ops when the document exposes no font set', () => {
    setFonts(undefined)
    const { term } = mount()
    expect(term.openCalls).toBe(1)
  })
})

/* ── teardown + PTY delete ────────────────────────────────────────────────── */

describe('disposeTerminalSession', () => {
  it('closes the socket, disposes the xterm, and evicts it from the cache', () => {
    const sessionId = nextId()
    const first = mount({ sessionId })
    cleanup()
    disposeTerminalSession(sessionId)
    expect(registry.disposeTerminalConnection).toHaveBeenCalledWith(sessionId)
    expect(first.term.disposed).toBe(true)
    // Evicted: the next mount of the same id constructs a fresh terminal.
    const second = mount({ sessionId })
    expect(second.term).not.toBe(first.term)
    expect(xt.FakeTerminal.instances).toHaveLength(2)
  })

  it('is a safe no-op for a session that was never cached', () => {
    expect(() => disposeTerminalSession('never-opened')).not.toThrow()
    expect(registry.disposeTerminalConnection).toHaveBeenCalledWith('never-opened')
  })
})

describe('useDeleteTerminalSession', () => {
  it('DELETEs the backend PTY for the given session', async () => {
    const f = vi.fn(() => Promise.resolve({ ok: true, status: 200 } as unknown as Response))
    vi.stubGlobal('fetch', f)
    const { result } = renderHookWithProviders(() => useDeleteTerminalSession())
    await act(async () => { await result.current.mutateAsync('pty-42') })
    expect(f).toHaveBeenCalledWith('/api/terminal/sessions/pty-42', { method: 'DELETE' })
  })

  it('surfaces a non-ok response as a mutation error carrying the status', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: false, status: 409 } as unknown as Response)))
    const { result } = renderHookWithProviders(() => useDeleteTerminalSession())
    await expect(
      act(async () => { await result.current.mutateAsync('pty-43') }),
    ).rejects.toThrow('Failed to delete terminal session (409)')
  })
})
