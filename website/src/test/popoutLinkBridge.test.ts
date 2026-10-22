/** Executes the popped-out widget tab's link bridge (`popoutLinkBridgeScript`)
 * against stand-ins for that tab's window, document and navigator, and pins the
 * same rules the in-dashboard bridge keeps: only its own frame, only a web URL,
 * only right after a click in the frame, and only once the person confirms a
 * prompt that shows the URL.
 */
import { describe, it, expect, vi } from 'vitest'
import { popoutLinkBridgeScript } from '../lib/widgetSrcdoc'

const OPEN = 'kirocrew-dashboard:open'
const LINK = 'https://code.example.test/reviews/CR-1'

function setup({ active = true, focused = true, answer = true } = {}) {
  const frameWindow = {}
  const frame = { contentWindow: frameWindow }
  let onMessage: ((e: { source: unknown; data: unknown }) => void) | null = null
  const confirm = vi.fn(() => answer)
  const open = vi.fn()
  const win = {
    addEventListener: (type: string, fn: (e: { source: unknown; data: unknown }) => void) => {
      if (type === 'message') onMessage = fn
    },
    confirm,
    open,
  }
  const doc = {
    querySelector: (sel: string) => (sel === 'iframe' ? frame : null),
    get activeElement() { return focused ? frame : {} },
  }
  const nav = { userActivation: { isActive: active } }
  new Function('window', 'document', 'navigator', popoutLinkBridgeScript('Open this link?'))(win, doc, nav)
  const send = (data: unknown, source: unknown = frameWindow) => onMessage?.({ source, data })
  return { send, confirm, open }
}

describe('popped-out widget link bridge', () => {
  it('asks with the URL shown and opens it on yes, without an opener', () => {
    const { send, confirm, open } = setup()
    send({ type: OPEN, url: LINK })
    expect(confirm).toHaveBeenCalledWith(`Open this link?\n\n${LINK}`)
    expect(open).toHaveBeenCalledWith(LINK, '_blank', 'noopener,noreferrer')
  })

  it('opens nothing on no', () => {
    const { send, open } = setup({ answer: false })
    send({ type: OPEN, url: LINK })
    expect(open).not.toHaveBeenCalled()
  })

  it.each([
    ['no activation', { active: false }],
    ['focus elsewhere', { focused: false }],
  ])('does not ask with %s', (_label, opts) => {
    const { send, confirm } = setup(opts)
    send({ type: OPEN, url: LINK })
    expect(confirm).not.toHaveBeenCalled()
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,x',
    'file:///etc/hosts',
    'mailto:a@example.test',
    '/relative',
    'https://user:pass@example.test/',
  ])('refuses %s', (url) => {
    const { send, confirm } = setup()
    send({ type: OPEN, url })
    expect(confirm).not.toHaveBeenCalled()
  })

  it('ignores another window and another message type', () => {
    const { send, confirm } = setup()
    send({ type: OPEN, url: LINK }, {})
    send({ type: 'kirocrew-dashboard:act', url: LINK })
    expect(confirm).not.toHaveBeenCalled()
  })

  it('cannot be broken out of by its prompt text', () => {
    const script = popoutLinkBridgeScript('</script><script>alert(1)</script>')
    expect(script).not.toContain('</script>')
  })
})
