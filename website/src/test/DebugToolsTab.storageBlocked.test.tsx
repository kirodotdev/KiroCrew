import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

/**
 * A browser with storage blocked -- Chrome's "block all cookies", or a sandboxed
 * embedding -- makes the `localStorage` GETTER ITSELF throw `SecurityError`, before
 * any method on it runs. A `useState` initializer that reads it raw therefore throws
 * during render, and the tab becomes an error boundary instead of a toggle that is
 * simply off.
 *
 * `safeGetItem` exists for exactly this and the write side already used
 * `safeSetItem`, so this pins the read to the same floor: the tab renders, and the
 * toggle degrades to off.
 */
describe('DebugToolsTab under blocked storage', () => {
  let original: PropertyDescriptor | undefined

  beforeEach(() => {
    original = Object.getOwnPropertyDescriptor(window, 'localStorage')
    // Throw from the ACCESSOR, not from `getItem`: that is what a locked-down
    // browser does, and it is the case a `try` around a method call would miss.
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('The operation is insecure.', 'SecurityError')
      },
    })
  })

  afterEach(() => {
    if (original) Object.defineProperty(window, 'localStorage', original)
    vi.restoreAllMocks()
  })

  it('renders with the toggle off instead of throwing out of render', async () => {
    const { DebugToolsTab } = await import('../pages/developer/DebugToolsTab')
    expect(() => render(<DebugToolsTab />)).not.toThrow()
    const box = screen.getByRole('switch') as HTMLInputElement | null
      ?? (screen.getByRole('checkbox') as HTMLInputElement)
    expect(box.getAttribute('aria-checked') ?? String(box.checked)).toBe('false')
  })
})

/**
 * The toggle's whole job is two writes and a notification: persist the choice, reflect
 * it, and tell the already-open chat. The overlay lives outside React by design -- the
 * paths it instruments are the ones a re-render would disturb -- so the CustomEvent is
 * the ONLY way a mounted transcript learns the switch flipped. A toggle that stored the
 * flag without dispatching would look correct until someone reloaded the page.
 */
describe('DebugToolsTab toggle', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('persists the flag and announces it, both ways', async () => {
    const { INSPECTOR_KEYS } = await import('../dev/scrollInspector')
    const { DebugToolsTab } = await import('../pages/developer/DebugToolsTab')

    const seen: unknown[] = []
    const onEvent = (e: Event) => seen.push((e as CustomEvent).detail)
    window.addEventListener(INSPECTOR_KEYS.ENABLED_EVENT, onEvent)

    render(<DebugToolsTab />)
    const box = screen.getByRole('switch') as HTMLInputElement | null
      ?? (screen.getByRole('checkbox') as HTMLInputElement)

    fireEvent.click(box)
    expect(localStorage.getItem(INSPECTOR_KEYS.ENABLED_KEY)).toBe('1')
    expect(box.getAttribute('aria-checked') ?? String(box.checked)).toBe('true')

    fireEvent.click(box)
    expect(localStorage.getItem(INSPECTOR_KEYS.ENABLED_KEY)).toBe('0')
    expect(box.getAttribute('aria-checked') ?? String(box.checked)).toBe('false')

    window.removeEventListener(INSPECTOR_KEYS.ENABLED_EVENT, onEvent)
    expect(seen).toEqual([true, false])
  })

  it('starts ON when the flag is already stored', async () => {
    const { INSPECTOR_KEYS } = await import('../dev/scrollInspector')
    localStorage.setItem(INSPECTOR_KEYS.ENABLED_KEY, '1')
    const { DebugToolsTab } = await import('../pages/developer/DebugToolsTab')
    render(<DebugToolsTab />)
    const box = screen.getByRole('switch') as HTMLInputElement | null
      ?? (screen.getByRole('checkbox') as HTMLInputElement)
    expect(box.getAttribute('aria-checked') ?? String(box.checked)).toBe('true')
  })
})
