import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setXrPanelSender } from '../../../hooks/xrPanelLink'
import { INPUT_ANSWER_MS, panelInputClosed, panelInputHeard, panelInputOpened, resetPanelInputLink, sendPanelInput, socketTakesInput } from './panelInputLink'

const WHEEL = { type: 'wheel', x: 10, y: 10, dx: 0, dy: 40 } as const
const MOVE = { type: 'mouse', action: 'move', x: 5, y: 6 } as const

describe('a wheel and a move on the socket', () => {
  let sent: { type: string; data: Record<string, unknown> }[] = []
  /** A socket opens; `answers` says whether its gateway knows the word `hello`. */
  const socket = (answers: boolean) => {
    setXrPanelSender(payload => { sent.push(payload as never) })
    panelInputOpened()
    // The first wheel asks the gateway, and goes by its request.
    expect(sendPanelInput('h1', WHEEL)).toBeNull()
    if (answers) expect(panelInputHeard({ op: 'hello', input: true })).toBe(true)
  }
  const hear = (data: Record<string, unknown>) => panelInputHeard(data)

  beforeEach(() => { vi.useFakeTimers(); sent = []; resetPanelInputLink() })
  afterEach(() => { setXrPanelSender(null); resetPanelInputLink(); vi.useRealTimers() })

  it('asks a socket at its first wheel what it takes, and uses it only after the gateway said so', () => {
    socket(false)
    expect(sent.map(m => m.data)).toEqual([{ op: 'hello' }])
    // An older gateway drops the word and never answers: every event keeps its request.
    expect(socketTakesInput()).toBe(false)
    expect(sendPanelInput('h1', WHEEL)).toBeNull()
    expect(sent).toHaveLength(1)
    hear({ op: 'hello', input: true })
    expect(socketTakesInput()).toBe(true)
    // Asked once a socket.
    expect(sent).toHaveLength(1)
  })

  it('sends a wheel and a move with the panel\'s handle and a count, and gives each its own answer', async () => {
    socket(true)
    const wheel = sendPanelInput('h1', WHEEL)
    const move = sendPanelInput('h1', MOVE)
    expect(sent.slice(1).map(m => m.data)).toEqual([
      { op: 'input', handle: 'h1', n: 1, event: WHEEL },
      { op: 'input', handle: 'h1', n: 2, event: MOVE },
    ])
    // The answers come in any order, each named by its count.
    hear({ op: 'input', handle: 'h1', n: 2, sent: 1, target: { rect: [1, 2, 3, 4] } })
    hear({ op: 'input', handle: 'h1', n: 1, sent: 1 })
    expect(await move).toMatchObject({ ok: true, sent: 1, target: { rect: [1, 2, 3, 4] } })
    expect(await wheel).toMatchObject({ ok: true, sent: 1 })
  })

  it('never sends a press, a key or a text this way', () => {
    socket(true)
    expect(sendPanelInput('h1', { type: 'mouse', action: 'click', x: 1, y: 1 })).toBeNull()
    expect(sendPanelInput('h1', { type: 'key', key: 'Enter' })).toBeNull()
    expect(sendPanelInput('h1', { type: 'text', text: 'hi' })).toBeNull()
    expect(sendPanelInput('h1', { type: 'leave' })).toBeNull()
    expect(sendPanelInput('', WHEEL)).toBeNull()
    expect(sent).toHaveLength(1)
  })

  it('an event that the gateway merged, dropped or refused has no answer; nor has one that is never answered', async () => {
    socket(true)
    const [a, b, c, d] = [sendPanelInput('h1', WHEEL), sendPanelInput('h1', WHEEL), sendPanelInput('h1', WHEEL), sendPanelInput('h1', WHEEL)]
    hear({ op: 'input', handle: 'h1', n: 1, merged: true })
    hear({ op: 'input', handle: 'h1', n: 2, dropped: true })
    hear({ op: 'input', handle: 'h1', n: 3, error: 'not_your_panel' })
    expect(await a).toBeNull()
    expect(await b).toBeNull()
    expect(await c).toBeNull()
    vi.advanceTimersByTime(INPUT_ANSWER_MS)
    expect(await d).toBeNull()
  })

  it('a socket that closes ends every wait, and the next socket is asked anew', async () => {
    socket(true)
    const wheel = sendPanelInput('h1', WHEEL)
    setXrPanelSender(null)
    panelInputClosed()
    expect(await wheel).toBeNull()
    expect(socketTakesInput()).toBe(false)
    sent = []
    socket(false)
    expect(sent.map(m => m.data)).toEqual([{ op: 'hello' }])
    expect(sendPanelInput('h1', WHEEL)).toBeNull()
  })
})
