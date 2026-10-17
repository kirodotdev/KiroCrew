/**
 * haptic — one best-effort tap per platform.
 *
 *  - Android (navigator.vibrate present): each kind maps to its own pattern.
 *  - iOS (no vibrate API, iPhone UA, engine reflects `input.switch`): a hidden
 *    `<input type=checkbox switch>` is clicked once per call and created only
 *    once per document. An iPhone UA whose engine lacks the property is a no-op.
 *  - desktop / jsdom (neither): a silent no-op that touches nothing.
 *  - a throwing engine never propagates: feedback must not break the action.
 *  - the switch's click is a hardware trigger, not a tap on the page: no page
 *    listener sees it, while the control still activates.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

async function freshHaptic() {
  vi.resetModules()
  return (await import('./haptic')).haptic
}

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15'

function setUserAgent(value: string) {
  Object.defineProperty(navigator, 'userAgent', { value, configurable: true })
}

// jsdom's HTMLInputElement has no `switch` reflection (it predates the WebKit
// attribute), so the iOS engine is stood up by defining the property the probe
// looks for, and torn down again after each test.
function withSwitchEngine() {
  Object.defineProperty(HTMLInputElement.prototype, 'switch', { value: false, configurable: true, writable: true })
}

describe('haptic', () => {
  const originalUA = navigator.userAgent

  beforeEach(() => {
    delete (navigator as unknown as { vibrate?: unknown }).vibrate
  })

  afterEach(() => {
    setUserAgent(originalUA)
    delete (navigator as unknown as { vibrate?: unknown }).vibrate
    delete (HTMLInputElement.prototype as unknown as { switch?: unknown }).switch
    document.body.replaceChildren()
  })

  it('vibrates with a per-kind pattern where the API exists', async () => {
    const vibrate = vi.fn(() => true)
    Object.defineProperty(navigator, 'vibrate', { value: vibrate, configurable: true, writable: true })
    const haptic = await freshHaptic()
    haptic()
    haptic('medium')
    haptic('success')
    haptic('error')
    expect(vibrate.mock.calls.map(c => c[0])).toEqual([10, 20, [10, 40, 10], [30, 40, 30]])
  })

  it('is a silent no-op on a device with no engine', async () => {
    const haptic = await freshHaptic()
    expect(() => haptic('error')).not.toThrow()
    expect(document.body.querySelector('input[switch]')).toBeNull()
  })

  it('clicks one hidden switch on iOS and reuses it', async () => {
    setUserAgent(IPHONE_UA)
    withSwitchEngine()
    const haptic = await freshHaptic()
    haptic('light')
    haptic('success')
    const switches = document.body.querySelectorAll('input[type=checkbox][switch]')
    expect(switches).toHaveLength(1)
    expect(switches[0].closest('label')?.getAttribute('aria-hidden')).toBe('true')
    expect((switches[0] as HTMLInputElement).tabIndex).toBe(-1)
  })

  it('keeps the switch click away from page listeners while the control still activates', async () => {
    setUserAgent(IPHONE_UA)
    withSwitchEngine()
    const haptic = await freshHaptic()
    // Registered AFTER the module, the way a component's effect or a drawer's
    // click swallower would be: capture on window, plain on document and body.
    const windowCapture = vi.fn()
    const documentBubble = vi.fn()
    const bodyBubble = vi.fn()
    window.addEventListener('click', windowCapture, { capture: true })
    document.addEventListener('click', documentBubble)
    document.body.addEventListener('click', bodyBubble)
    try {
      haptic('light')
      const input = document.body.querySelector<HTMLInputElement>('input[switch]')
      expect(input?.checked).toBe(true)
      expect(windowCapture).not.toHaveBeenCalled()
      expect(documentBubble).not.toHaveBeenCalled()
      expect(bodyBubble).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('click', windowCapture, { capture: true })
      document.removeEventListener('click', documentBubble)
      document.body.removeEventListener('click', bodyBubble)
    }
  })

  it('adds nothing to the document on an iPhone whose engine lacks the switch', async () => {
    setUserAgent(IPHONE_UA)
    const haptic = await freshHaptic()
    expect(() => haptic('light')).not.toThrow()
    expect(document.body.querySelector('input[switch]')).toBeNull()
  })

  it('swallows a throwing engine', async () => {
    Object.defineProperty(navigator, 'vibrate', {
      value: () => { throw new Error('no motor') },
      configurable: true,
      writable: true,
    })
    const haptic = await freshHaptic()
    expect(() => haptic('medium')).not.toThrow()
  })
})
