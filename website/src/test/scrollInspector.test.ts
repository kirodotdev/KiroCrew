import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

/** The inspector's contract is not what it SHOWS -- it is what it costs when it is
 *  off. It sits on the transcript's hottest paths (a per-frame correction loop, a
 *  per-render counter, every scroll burst), so "disabled" has to mean no element,
 *  no timer, and nothing retained -- not merely "cheap". Each test here fails if
 *  the gate is moved below any of those.
 *
 *  Loaded fresh per test: the module reads the flag ONCE at import and keeps its
 *  state in module scope, so a shared instance would let one test's toggle decide
 *  the next test's answer. */
const load = async () => await import('../dev/scrollInspector')

const overlay = () => document.querySelector('[data-scroll-inspector]')

describe('scroll inspector: disabled costs nothing', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    document.body.replaceChildren()
  })

  it('creates no element and arms no timer while off', async () => {
    const setInterval = vi.spyOn(window, 'setInterval')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(false)

    insp.devLog('TAG', 'detail')
    insp.devWatchScroller(document.createElement('div'), 12)
    insp.devWatchMessages(100, 7000)

    expect(overlay()).toBeNull()
    expect(setInterval).not.toHaveBeenCalled()
  })

  it('retains nothing logged while off, so enabling later shows no backlog', async () => {
    const insp = await load()
    insp.devLog('BEFORE', 'x')
    insp.setInspectorEnabled(true)
    // Only the reading that arrives AFTER enabling may appear. A buffer that
    // filled while disabled would both cost memory and mislead: those lines
    // carry timestamps from a window nobody was watching.
    insp.devLog('AFTER', 'y')
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('AFTER')
    expect(text).not.toContain('BEFORE')
  })

  it('reads the persisted flag at load, so a reload keeps it on', async () => {
    localStorage.setItem('mc-scroll-inspector', '1')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(true)
  })

  it('survives storage being unavailable instead of taking the app down', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('private mode')
    })
    const insp = await load()
    // An inspector nobody can turn on is the safe answer on a path the product
    // depends on; a throw here would break every chat that imports it.
    expect(insp.inspectorOn()).toBe(false)
    getItem.mockRestore()
  })
})

describe('scroll inspector: enabling and disabling', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    document.body.replaceChildren()
  })

  it('paints a line only once there is something to show', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    // Enabling alone must not hang an empty box over the page: a surface that
    // reports nothing should stay invisible.
    expect(overlay()).toBeNull()
    insp.devLog('RESTORE.OK', 'idx=4')
    expect(overlay()?.textContent).toContain('RESTORE.OK')
  })

  it('removes the element and clears the timer when switched off', async () => {
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    insp.devWatchScroller(document.createElement('div'), 3)
    expect(overlay()).not.toBeNull()

    insp.setInspectorEnabled(false)
    expect(overlay()).toBeNull()
    expect(clearInterval).toHaveBeenCalled()
    expect(insp.inspectorOn()).toBe(false)
  })

  it('picks up the panel toggle event without a reload', async () => {
    const insp = await load()
    expect(insp.inspectorOn()).toBe(false)
    window.dispatchEvent(new CustomEvent('mc-scroll-inspector-changed', { detail: true }))
    expect(insp.inspectorOn()).toBe(true)
    window.dispatchEvent(new CustomEvent('mc-scroll-inspector-changed', { detail: false }))
    expect(insp.inspectorOn()).toBe(false)
  })

  it('follows another tab turning it off', async () => {
    localStorage.setItem('mc-scroll-inspector', '1')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(true)
    localStorage.setItem('mc-scroll-inspector', '0')
    window.dispatchEvent(new StorageEvent('storage', { key: 'mc-scroll-inspector' }))
    expect(insp.inspectorOn()).toBe(false)
  })

  it('lets a tap through everywhere except the drag grip', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    // The box must never swallow a tap meant for the transcript it reports on;
    // the grip is the one place that opts back in so it can be dragged.
    expect(box.style.pointerEvents).toBe('none')
    const grip = box.firstElementChild as HTMLElement
    expect(grip.style.pointerEvents).toBe('auto')
  })

  it('restores a dragged position from storage', async () => {
    localStorage.setItem('mc-scroll-inspector-pos', JSON.stringify({ x: 120, y: 260 }))
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    expect(box.style.left).toBe('120px')
    expect(box.style.top).toBe('260px')
  })

  it('ignores a corrupt persisted position instead of vanishing off-screen', async () => {
    localStorage.setItem('mc-scroll-inspector-pos', '{not json')
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    expect(parseFloat(box.style.left)).toBeGreaterThanOrEqual(0)
    expect(parseFloat(box.style.top)).toBeGreaterThanOrEqual(0)
  })
})

describe('scroll inspector: reading helpers', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
  })

  it('keyShape shows the prefix, which is the part that identifies the vocabulary', async () => {
    const { keyShape } = await load()
    // The persisted anchor is a stable row id (`a-` prefix); the per-render key
    // is not. Printing only the tail hid exactly that difference and cost a
    // round of wrong diagnosis.
    expect(keyShape('a-abc123def456')).toBe('a-\u2026def456')
    expect(keyShape('turn-abc123def456')).toBe('tu\u2026def456')
  })

  it('shortId keeps two sessions distinguishable and tolerates absence', async () => {
    const { shortId } = await load()
    expect(shortId('chat-17-1788561823590')).toBe('3590')
    expect(shortId('abc')).toBe('abc')
    expect(shortId(null)).toBe('-')
    expect(shortId(undefined)).toBe('-')
  })
})
