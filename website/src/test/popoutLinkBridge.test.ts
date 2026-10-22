/** Executes the popped-out widget tab's link bridge (`popoutLinkBridgeScript`)
 * against a stand-in for that tab's window and navigator over a real document,
 * and pins the same rules the in-dashboard bridge keeps: only its own frame, only
 * a web URL, only right after a click with the frame focused, once per click,
 * and the "Open <host>" button when the click cannot be tied to the frame.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { popoutLinkBridgeScript } from '../lib/widgetSrcdoc'

const OPEN = 'kirocrew-dashboard:open'
const LINK = 'https://code.example.test/reviews/CR-1'

type Listener = (e: { source?: unknown; data?: unknown; target?: unknown }) => void

function setup({ active = true } = {}) {
  const frame = document.createElement('iframe')
  frame.tabIndex = 0
  frame.setAttribute('data-popout-test', '')
  document.body.appendChild(frame)
  const frameWindow = {}
  Object.defineProperty(frame, 'contentWindow', { configurable: true, value: frameWindow })
  const listeners: Record<string, Listener[]> = {}
  const open = vi.fn()
  const win = {
    addEventListener: (type: string, fn: Listener) => { (listeners[type] ??= []).push(fn) },
    open,
  }
  const state = { active }
  const nav = { userActivation: { get isActive() { return state.active } } }
  new Function('window', 'document', 'navigator', popoutLinkBridgeScript('Open %HOST%'))(win, document, nav)
  const fire = (type: string, e: Parameters<Listener>[0] = {}) => listeners[type]?.forEach((fn) => fn(e))
  const send = (data: unknown, source: unknown = frameWindow) => fire('message', { source, data })
  /** Focus moving from the tab into the frame: the tab's window blurs. */
  const enter = () => { frame.focus(); fire('blur') }
  /** A click or key in the tab itself, outside the frame. */
  const tabInput = (type = 'pointerdown', target: unknown = document.body) => fire(type, { target })
  const chip = () => document.querySelector<HTMLButtonElement>('[data-frame-link-chip]')
  return { frame, send, enter, tabInput, chip, open, state, listeners }
}

afterEach(() => {
  vi.useRealTimers()
  document.querySelectorAll('[data-popout-test], [data-frame-link-chip]').forEach((n) => n.remove())
})

describe('popped-out widget link bridge', () => {
  it('opens at once on a click inside the frame, without an opener', () => {
    const { send, enter, open, chip } = setup()
    enter()
    send({ type: OPEN, url: LINK })
    expect(open).toHaveBeenCalledWith(LINK, '_blank', 'noopener,noreferrer')
    expect(chip()).toBeNull()
  })

  it('opens once per click, however many requests the page queues', () => {
    const { send, enter, open } = setup()
    enter()
    send({ type: OPEN, url: LINK })
    send({ type: OPEN, url: LINK })
    send({ type: OPEN, url: 'https://example.test/other' })
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('opens nothing with no activation or with focus elsewhere', () => {
    const quiet = setup({ active: false })
    quiet.enter()
    quiet.send({ type: OPEN, url: LINK })
    expect(quiet.open).not.toHaveBeenCalled()
    expect(quiet.chip()).toBeNull()
    quiet.frame.remove()
    const away = setup()
    away.send({ type: OPEN, url: LINK })
    expect(away.open).not.toHaveBeenCalled()
    expect(away.chip()).toBeNull()
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,x',
    'file:///etc/hosts',
    'mailto:a@example.test',
    '/relative',
    'https://user:pass@example.test/',
  ])('refuses %s', (url) => {
    const { send, enter, open, chip } = setup()
    enter()
    send({ type: OPEN, url })
    expect(open).not.toHaveBeenCalled()
    expect(chip()).toBeNull()
  })

  it('ignores another window and another message type', () => {
    const { send, enter, open } = setup()
    enter()
    send({ type: OPEN, url: LINK }, {})
    send({ type: 'kirocrew-dashboard:act', url: LINK })
    expect(open).not.toHaveBeenCalled()
  })

  it('listens for the tab\'s own clicks and keys and for focus coming into the frame', () => {
    const { listeners } = setup()
    expect(Object.keys(listeners).sort()).toEqual(
      ['blur', 'focus', 'keydown', 'message', 'mousedown', 'pointerdown', 'pointerup', 'touchend'],
    )
  })

  it.each([
    ['a key in the tab itself is still live', (s: ReturnType<typeof setup>) => { s.enter(); s.tabInput('keydown') }],
    ['focus did not come into the frame from the tab', (s: ReturnType<typeof setup>) => { s.frame.focus() }],
  ])('shows the button instead of opening when %s, and its click opens', (_label, arrange) => {
    const s = setup()
    arrange(s)
    s.send({ type: OPEN, url: LINK })
    expect(s.open).not.toHaveBeenCalled()
    const button = s.chip()
    expect(button?.textContent).toBe('Open code.example.test')
    expect(button?.title).toBe(LINK)
    // It takes focus, so the frame cannot ask again on the same click.
    expect(document.activeElement).toBe(button)
    button!.click()
    expect(s.open).toHaveBeenCalledWith(LINK, '_blank', 'noopener,noreferrer')
    expect(s.chip()).toBeNull()
  })

  it('keeps the button however long it waits, and takes it away on input elsewhere or Escape', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const s = setup()
    const ask = () => { s.frame.focus(); s.send({ type: OPEN, url: LINK }); expect(s.chip()).not.toBeNull() }
    ask()
    // No timer: a slow reader still finds it a minute later.
    vi.advanceTimersByTime(60_000)
    expect(s.chip()).not.toBeNull()
    s.tabInput('pointerdown')
    expect(s.chip()).toBeNull()
    ask()
    s.tabInput('pointerdown')
    expect(s.chip()).toBeNull()
    ask()
    s.tabInput('keydown', s.chip())
    expect(s.chip()).not.toBeNull()
    s.chip()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(s.chip()).toBeNull()
    expect(s.open).not.toHaveBeenCalled()
  })

  it('keeps the label inside its script, whatever it holds', () => {
    const script = popoutLinkBridgeScript('</script><b>%HOST%')
    expect(script).not.toContain('</script>')
    expect(script).toContain('\\u003c/script>')
  })
})
