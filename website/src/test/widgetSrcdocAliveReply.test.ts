/** Executes the REAL alive reply, extracted from buildSrcdoc's own output, and
 * pins its contract: it answers a ping from its parent, and nothing else. */
import { describe, it, expect, beforeAll, beforeEach } from 'vitest'
import { buildSrcdoc, FRAME_ALIVE_MESSAGE_TYPE, FRAME_PING_MESSAGE_TYPE } from '../lib/widgetSrcdoc'

function replyScript(out: string): string | undefined {
  const doc = new DOMParser().parseFromString(out, 'text/html')
  return Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes(FRAME_PING_MESSAGE_TYPE))
}

const posted: unknown[] = []
const parentStub = { postMessage: (msg: unknown) => posted.push(msg) }

function send(source: unknown, data: unknown) {
  const ev = new MessageEvent('message', { data })
  Object.defineProperty(ev, 'source', { value: source })
  window.dispatchEvent(ev)
}

describe('frame alive reply', () => {
  beforeAll(() => {
    const script = replyScript(buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', aliveReply: true }))
    if (!script) throw new Error('alive reply not found in srcdoc')
    new Function('parent', script)(parentStub)
  })
  beforeEach(() => {
    posted.length = 0
  })

  it('answers a ping from its parent', () => {
    send(parentStub, { type: FRAME_PING_MESSAGE_TYPE })
    expect(posted).toEqual([{ type: FRAME_ALIVE_MESSAGE_TYPE }])
  })

  it('ignores a ping from any other window, and any other message', () => {
    send({}, { type: FRAME_PING_MESSAGE_TYPE })
    send(parentStub, { type: 'something-else' })
    send(parentStub, null)
    expect(posted).toEqual([])
  })

  it('is absent unless asked for', () => {
    expect(replyScript(buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark' }))).toBeUndefined()
  })
})
