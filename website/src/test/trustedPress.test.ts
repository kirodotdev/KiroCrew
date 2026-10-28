/** `isTrustedPress` accepts only real input on the confirm button itself. */
import { describe, it, expect, vi } from 'vitest'
import { isTrustedPress } from '../components/trustedPress'

function button(): HTMLButtonElement {
  const b = document.createElement('button')
  document.body.appendChild(b)
  return b
}

describe('isTrustedPress', () => {
  it('refuses a click dispatched from script', () => {
    const b = button()
    let seen: Event | null = null
    b.addEventListener('click', e => { seen = e })
    b.click()
    expect(seen).not.toBeNull()
    expect(isTrustedPress({ nativeEvent: seen }, b)).toBe(false)
  })

  it('refuses a Proxy around a real event that lies about trust and target', () => {
    const b = button()
    const native = new MouseEvent('click')
    const proxy = new Proxy(native, {
      get(target, property, receiver) {
        if (property === 'isTrusted') return true
        if (property === 'target') return b
        return Reflect.get(target, property, receiver)
      },
    })
    expect(proxy instanceof Event).toBe(true)
    expect(isTrustedPress({ nativeEvent: proxy }, b)).toBe(false)
  })

  it('refuses a plain object that claims to be trusted input', () => {
    const b = button()
    expect(isTrustedPress({ nativeEvent: { type: 'click', isTrusted: true, target: b } }, b)).toBe(false)
  })

  it('refuses a missing event or a missing button', () => {
    expect(isTrustedPress(undefined, button())).toBe(false)
    expect(isTrustedPress({ nativeEvent: new MouseEvent('click') }, null)).toBe(false)
  })
})

/**
 * jsdom cannot produce a trusted event, so the accept path is reached by
 * loading the module while `Event` hands it a stand-in `isTrusted` getter. The
 * `type` and `target` getters stay the real ones, so what this covers is the
 * module's own logic: a trusted click counts only when it lands inside the
 * button it is checked against.
 */
describe('isTrustedPress with a trusted click', () => {
  async function loadWithTrusted(trusted: WeakSet<object>) {
    const RealEvent = globalThis.Event
    function StandIn(this: unknown) {
      const probe = Object.create(RealEvent.prototype)
      Object.defineProperty(probe, 'isTrusted', { get(this: object) { return trusted.has(this) } })
      return probe
    }
    StandIn.prototype = RealEvent.prototype
    vi.resetModules()
    globalThis.Event = StandIn as unknown as typeof Event
    try {
      return (await import('../components/trustedPress')).isTrustedPress
    } finally {
      globalThis.Event = RealEvent
    }
  }

  function clickOn(el: Element): MouseEvent {
    const e = new MouseEvent('click', { bubbles: true })
    el.dispatchEvent(e)
    return e
  }

  it('accepts a trusted click inside the button and refuses one outside it', async () => {
    const trusted = new WeakSet<object>()
    const check = await loadWithTrusted(trusted)
    const b = button()
    const label = document.createElement('span')
    b.appendChild(label)
    const inside = clickOn(label)
    const outside = clickOn(button())
    trusted.add(inside)
    trusted.add(outside)
    expect(check({ nativeEvent: inside }, b)).toBe(true)
    expect(check({ nativeEvent: outside }, b)).toBe(false)
  })

  it('refuses a trusted event that is not a click', async () => {
    const trusted = new WeakSet<object>()
    const check = await loadWithTrusted(trusted)
    const b = button()
    const e = new MouseEvent('mousedown', { bubbles: true })
    b.dispatchEvent(e)
    trusted.add(e)
    expect(check({ nativeEvent: e }, b)).toBe(false)
  })
})
