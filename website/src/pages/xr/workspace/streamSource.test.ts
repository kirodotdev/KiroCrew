/**
 * The live page source: what it tells the gateway, when it says a picture arrived, and what a hand's press becomes.
 *
 * The socket is the real link module with a recording sender; the two input routes are a stub that answers what
 * the gateway would. No picture is decoded for real: `createImageBitmap` is a stand-in that keeps the size.
 */
import { readFileSync } from 'node:fs'
import type { WebGLRenderer } from 'three'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const input = vi.hoisted(() => vi.fn())
const ask = vi.hoisted(() => vi.fn())
vi.mock('../../../api/client', () => ({ api: { browserPanelInput: input, browserPanelAsk: ask } }))

import { applyXrPanelBinary, applyXrPanelFrame, decodeXrPanelPicture, sendXrPanel, setXrPanelSender } from '../../../hooks/xrPanelLink'
import { INPUT_GAP_MS, resetPanelInputLink } from './panelInputLink'
import type { LayerPicture, PageLayer } from './pageLayer'
import { MOST_LIVE_PAGES } from './source'
import { setFullHook } from './fullHook'
import { flatness, hasShape, StreamSource } from './streamSource'

type Sent = { op: string; [key: string]: unknown }
let sent: Sent[] = []
const ops = (op: string) => sent.filter(m => m.op === op)
/** The acks that say a picture is in the texture (or on its layer): what every rule that waits for his sight reads. */
const landedAcks = () => ops('ack').filter(m => m.kind === 'landed')
const renderer = { initTexture: vi.fn() } as unknown as WebGLRenderer
const JPEG = [0xff, 0xd8, 0xff, 0xe0]
const PNG = [0x89, 0x50, 0x4e, 0x47]

/** One binary frame as the gateway sends it: a 4-byte length, a JSON header, the picture's bytes. */
function picture(seq: number, bytes: number[] = JPEG, more: Record<string, unknown> = {}): ArrayBuffer {
  const header = new TextEncoder().encode(JSON.stringify({ type: 'xr_panel_frame', panel: 'a', stream: 's1', seq, w: 2048, h: 1440, css: [1024, 720], mask: [], ...more }))
  const out = new Uint8Array(4 + header.length + bytes.length)
  new DataView(out.buffer).setUint32(0, header.length, false)
  out.set(header, 4)
  out.set(bytes, 4 + header.length)
  return out.buffer
}

/** Lets the decode of the picture that waits finish. */
const decoded = () => vi.waitFor(() => expect((globalThis.createImageBitmap as ReturnType<typeof vi.fn>).mock.results.every(r => r.type === 'return')).toBe(true)).then(() => Promise.resolve())

function live(source: StreamSource): void {
  applyXrPanelFrame({ op: 'opened', id: source.id, handle: 'p1', css: [1024, 720] })
  applyXrPanelFrame({ op: 'status', id: source.id, status: 'live', why: '' })
}

/** One picture with content lands: a new page takes input from then on (D50). */
async function showing(source: StreamSource, seq = 900): Promise<void> {
  applyXrPanelBinary(picture(seq, JPEG, { panel: source.id }))
  await decoded()
  source.beforeDraw?.(renderer, true)
}

beforeEach(() => {
  sent = []
  input.mockReset()
  input.mockResolvedValue({ ok: true, sent: 1, target: null })
  ask.mockReset()
  ask.mockResolvedValue({ ok: true, answer: null })
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 2048, height: 1440, close: vi.fn() })))
  setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
})

afterEach(() => {
  setXrPanelSender(null)
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('a live page source', () => {
  it('asks the gateway for its page when it is made, and again when the socket comes back', () => {
    const source = new StreamSource('a', '/chat/s1', 1024, 720)
    expect(ops('open')).toEqual([expect.objectContaining({ id: 'a', path: '/chat/s1', w: 1024, h: 720, detail: 2 })])
    expect(source.status?.()).toBe('loading')
    live(source)
    expect(source.status?.()).toBe('live')
    setXrPanelSender(null)
    expect(source.status?.()).toBe('loading')
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    expect(ops('open')).toHaveLength(2)
    source.dispose()
    expect(ops('close')).toEqual([{ op: 'close', id: 'a' }])
  })

  it('is not whole after a link moved it to another address, until that page has shown long enough (small 4)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const source = new StreamSource('a', '/members', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    at += 5000
    expect(source.wasWhole(4000)).toBe(true)
    // A link inside it goes to the schedule: what it showed of the Bots page counts no more.
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/schedule' })
    expect(source.pagePath()).toBe('/schedule')
    expect(source.wasWhole(4000)).toBe(false)
    at += 1000
    source.visible?.(false, true)
    at += 60_000
    expect(source.wasWhole(4000)).toBe(false)
    // Back in view, its new page shows long enough: whole again.
    source.visible?.(true, true)
    applyXrPanelBinary(picture(2))
    await decoded()
    source.beforeDraw?.(renderer, true)
    at += 4500
    expect(source.wasWhole(4000)).toBe(true)
    source.dispose()
    clock.mockRestore()
  })

  it('fills out of view when he never had it whole in view: the gateway hears "nobody sees it" only once it has (D48, A)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const hidden = () => sent.filter(m => m.op === 'visible' && m.visible === false)
    const side = new StreamSource('a', '/schedule', 1024, 720)
    // Its panel stands at 60 degrees, out of view, from its first frame.
    side.visible?.(false, true)
    live(side)
    expect(hidden()).toEqual([])
    applyXrPanelBinary(picture(1))
    await decoded()
    // The panel's frame writes it, in view or not.
    side.beforeDraw?.(renderer, true)
    at += 3000
    applyXrPanelBinary(picture(2, PNG))
    await decoded()
    // The panel's frame writes it, in view or not.
    side.beforeDraw?.(renderer, true)
    // A still 3 s after live is its shell: not yet.
    expect(side.filledOutOfView()).toBe(false)
    expect(hidden()).toEqual([])
    at += 4000
    applyXrPanelBinary(picture(3, PNG))
    await decoded()
    // The panel's frame writes it, in view or not.
    side.beforeDraw?.(renderer, true)
    expect(side.filledOutOfView()).toBe(true)
    expect(hidden()).toEqual([{ op: 'visible', id: 'a', visible: false }])
    side.dispose()
    // A page he had whole in his view for 4 s stops at once: it comes back whole either way.
    sent = []
    const seen = new StreamSource('a', '/schedule', 1024, 720)
    live(seen)
    applyXrPanelBinary(picture(4))
    await decoded()
    seen.beforeDraw?.(renderer, true)
    at += 4500
    seen.visible?.(false, true)
    expect(hidden()).toHaveLength(1)
    seen.dispose()
    // One he only glanced at (a second, while the picture turned) goes on filling out of view, as one never seen.
    sent = []
    const glanced = new StreamSource('a', '/schedule', 1024, 720)
    live(glanced)
    applyXrPanelBinary(picture(5))
    await decoded()
    glanced.beforeDraw?.(renderer, true)
    at += 1000
    glanced.visible?.(false, true)
    expect(hidden()).toEqual([])
    at += 6000
    applyXrPanelBinary(picture(6, PNG))
    await decoded()
    glanced.beforeDraw?.(renderer, true)
    expect(glanced.filledOutOfView()).toBe(true)
    expect(hidden()).toHaveLength(1)
    glanced.dispose()
    clock.mockRestore()
  })

  it('is told "he sees it" when he turns to it, after it was asked for again while out of view (REVIEW-17 M3)', () => {
    const words = () => ops('visible').map(m => m.visible)
    const page = new StreamSource('a', '/schedule', 1024, 720)
    // Out of view while it loads: the hide is held, no word.
    page.visible?.(false, true)
    expect(words()).toEqual([])
    // Asked for again (the socket opens anew; a refusal that freed a warm room and `askAgain` take the same way).
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    expect(words()).toEqual([false])
    // He turns to it: the gateway hears it.
    page.visible?.(true, true)
    expect(words()).toEqual([false, true])
    page.dispose()
  })

  it('is told "nobody sees it" at once when it has no panel, before any picture of it landed (REVIEW-17 M4)', () => {
    vi.useFakeTimers()
    const words = () => ops('visible').map(m => m.visible)
    // A page that opens put away, while it loads.
    const away = new StreamSource('a', '/schedule', 1024, 720)
    away.visible?.(false)
    expect(words()).toEqual([false])
    // That word came before the gateway knew the page: it is said again once the page is opened there.
    applyXrPanelFrame({ op: 'opened', id: 'a', handle: 'p1', css: [1024, 720] })
    expect(words()).toEqual([false, false])
    away.dispose()
    // One that its panel held out of view, live with nothing landed, and is then put away.
    sent = []
    const held = new StreamSource('b', '/schedule', 1024, 720)
    held.visible?.(false, true)
    applyXrPanelFrame({ op: 'opened', id: 'b', handle: 'p2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'b', status: 'live', why: '' })
    expect(words()).toEqual([])
    held.visible?.(false)
    expect(words()).toEqual([false])
    // Its timer is gone with the held hide: no second word, ever.
    vi.advanceTimersByTime(60_000)
    expect(words()).toEqual([false])
    held.dispose()
  })

  it('hides at once after a glance when its pictures were cut before it filled (REVIEW-17 M5, C1 and C2)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const words = () => ops('visible').map(m => m.visible)
    // C1: out of view while it loads; the socket opens anew (the hide is said, the page is cut); a glance of 2 s; away.
    const c1 = new StreamSource('a', '/schedule', 1024, 720)
    c1.visible?.(false, true)
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    live(c1)
    c1.visible?.(true, true)
    await showing(c1)
    at += 2000
    c1.visible?.(false, true)
    // The hide at the new open, said again at "opened", "he sees it" at the glance, and the hide at once after it.
    expect(words()).toEqual([false, false, true, false])
    c1.dispose()
    // C2: its panel held its hide; put away (the hide is said); shown again; a glance of 2 s; away.
    sent = []
    const c2 = new StreamSource('b', '/schedule', 1024, 720)
    c2.visible?.(false, true)
    applyXrPanelFrame({ op: 'opened', id: 'b', handle: 'p2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'b', status: 'live', why: '' })
    c2.visible?.(false)
    c2.visible?.(true, true)
    await showing(c2)
    at += 2000
    c2.visible?.(false, true)
    expect(words()).toEqual([false, true, false])
    c2.dispose()
    clock.mockRestore()
  })

  it('takes input at once after a glance over a held hide, also after a link, and waits after a hide that was sent (D53 1, M7)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const words = () => ops('visible').map(m => m.visible)
    // G1: seen 2 s, away 1 s (its hide is held: it has not filled), back. The gateway never stopped: no wait.
    const g1 = new StreamSource('a', '/schedule', 1024, 720)
    live(g1)
    await showing(g1)
    at += 2000
    g1.visible?.(false, true)
    at += 1000
    g1.visible?.(true, true)
    expect(words()).toEqual([])
    expect(g1.notYet()).toBe(false)
    // Its time in view counts from the return, not across the time it was out of view.
    at += 1500
    expect(g1.wasWhole(3000)).toBe(false)
    at += 600_000
    expect(g1.notYet()).toBe(false)
    g1.dispose()
    // G4: read 30 s, a link moves it, a glance 1.5 s later: the same.
    sent = []
    const g4 = new StreamSource('b', '/schedule', 1024, 720)
    live(g4)
    await showing(g4, 901)
    at += 30_000
    applyXrPanelFrame({ op: 'path', id: 'b', path: '/artifacts' })
    at += 1500
    g4.visible?.(false, true)
    at += 1000
    g4.visible?.(true, true)
    expect(words()).toEqual([])
    expect(g4.notYet()).toBe(false)
    g4.dispose()
    // A socket anew during the glance: the page is asked for again and its held hide is said with it, so the return
    // says "he sees it" and waits for the picture that word brings.
    sent = []
    const g7 = new StreamSource('d', '/schedule', 1024, 720)
    live(g7)
    await showing(g7, 904)
    at += 2000
    g7.visible?.(false, true)
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    live(g7)
    g7.visible?.(true, true)
    expect(words().at(-1)).toBe(true)
    expect(g7.notYet()).toBe(true)
    // Opened anew, no clock ends its waits until a picture of this open landed (D55 1, M8): at 3.6 s it still waits.
    at += 3600
    expect(g7.notYet()).toBe(true)
    await showing(g7, 905)
    expect(g7.notYet()).toBe(false)
    g7.dispose()
    // G6: a hide that was sent (it had filled): back in view it waits for the gateway's fresh picture, 3 s at most.
    sent = []
    const g6 = new StreamSource('c', '/schedule', 1024, 720)
    live(g6)
    await showing(g6, 902)
    at += 16_000
    g6.visible?.(false, true)
    g6.visible?.(true, true)
    expect(words()).toEqual([false, true])
    expect(g6.notYet()).toBe(true)
    applyXrPanelBinary(picture(903, JPEG, { panel: 'c' }))
    await decoded()
    g6.beforeDraw?.(renderer, true)
    expect(g6.notYet()).toBe(false)
    g6.dispose()
    clock.mockRestore()
  })

  it('sends no click for a release whose own down was dropped, nor keeps a press across a wait (D53 6, small 1)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const clicks = () => input.mock.calls.filter(([, e]) => (e as { action?: string }).action === 'click').length
    const source = new StreamSource('a', '/schedule', 1024, 720)
    live(source)
    await showing(source)
    // S1: a down is taken; a wait begins (a warm page taken back) before the release; the release falls in it.
    source.pointer({ type: 'down', pointerId: 1, x: 100, y: 100 })
    expect(source.takesPress?.()).toBe(true)
    source.takenBack?.()
    source.pointer({ type: 'up', pointerId: 1, x: 100, y: 100 })
    // The wait ends; the next release comes with no down of its own that was taken.
    at += 3100
    expect(source.notYet()).toBe(false)
    expect(source.takesPress?.()).toBe(false)
    source.pointer({ type: 'up', pointerId: 1, x: 300, y: 300 })
    expect(clicks()).toBe(0)
    // S2: the plain case still clicks.
    source.pointer({ type: 'down', pointerId: 1, x: 300, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 300, y: 300 })
    expect(clicks()).toBe(1)
    source.dispose()
    clock.mockRestore()
  })

  it('judges a down by the frame he saw before it, not by a word that turned since (D50)', async () => {
    const clicks = () => input.mock.calls.filter(([, e]) => (e as { action?: string }).action === 'click').length
    const source = new StreamSource('a', '/schedule', 1024, 720)
    live(source)
    await showing(source)
    // The frame before showed it dropping a press: the down goes nowhere though the page takes input now.
    source.pointer({ type: 'down', pointerId: 1, x: 100, y: 100, seen: false })
    source.pointer({ type: 'up', pointerId: 1, x: 100, y: 100 })
    expect(clicks()).toBe(0)
    // The frame before showed it taking a press: the down counts.
    source.pointer({ type: 'down', pointerId: 1, x: 100, y: 100, seen: true })
    source.pointer({ type: 'up', pointerId: 1, x: 100, y: 100 })
    expect(clicks()).toBe(1)
    source.dispose()
  })

  it('sends its held hide once its late time passed with no picture landed, and fires no timer after (small 2)', () => {
    vi.useFakeTimers()
    const words = () => ops('visible').map(m => m.visible)
    const page = new StreamSource('a', '/schedule', 1024, 720)
    page.visible?.(false, true)
    live(page)
    expect(words()).toEqual([])
    vi.advanceTimersByTime(15_100)
    expect(words()).toEqual([false])
    const timers = vi.getTimerCount()
    vi.advanceTimersByTime(45_000)
    expect(words()).toEqual([false])
    expect(vi.getTimerCount()).toBeLessThanOrEqual(timers)
    page.dispose()
    vi.useRealTimers()
  })

  it('a warm page told "seen" by a rest on its card is never the watched page (D53 5)', () => {
    const page = new StreamSource('a', '/schedule', 1024, 720)
    live(page)
    page.visible?.(false)
    page.gaze?.(2)
    page.peek?.(true)
    expect(page.watchPage().inView).toBe(false)
    page.takenBack?.()
    expect(page.watchPage().inView).toBe(true)
    page.dispose()
  })

  it('does not count as filled out of view once a link moved it, until its new page has filled (small 1)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const source = new StreamSource('a', '/members', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    at += 60_000
    expect(source.filledOutOfView()).toBe(true)
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/schedule' })
    at += 1000
    expect(source.filledOutOfView()).toBe(false)
    at += 15_000
    expect(source.filledOutOfView()).toBe(true)
    source.dispose()
    clock.mockRestore()
  })

  it('takes no input after it was taken back until its fresh picture landed, 3 s at most (D49)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    source.countWhole?.()
    // Warm: hidden. A picture that arrived before it was hidden waits, decoded, in the tab.
    applyXrPanelBinary(picture(2))
    await decoded()
    source.visible?.(false, true)
    at += 60_000
    source.takenBack?.()
    expect(source.awaitingFresh?.()).toBe(true)
    expect(ops('visible').at(-1)).toEqual({ op: 'visible', id: 'a', visible: true })
    // A press, the wheel and a key while it waits go nowhere.
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    source.scroll({ x: 500, y: 300, dx: 0, dy: 120 })
    source.key?.({ type: 'down', key: 'a' })
    expect(input).not.toHaveBeenCalled()
    // The old decoded picture lands: not fresh, it still waits.
    source.beforeDraw?.(renderer, true)
    expect(source.awaitingFresh?.()).toBe(true)
    // The gateway's first picture after it was told it is seen: fresh. From that frame on it takes input.
    at += 400
    applyXrPanelBinary(picture(3))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.awaitingFresh?.()).toBe(false)
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(input).toHaveBeenCalledWith('p1', { type: 'mouse', action: 'click', x: 500, y: 300 })
    // No fresh picture ever comes: after 3 s it takes input whatever came.
    source.visible?.(false, true)
    source.takenBack?.()
    at += 2900
    expect(source.awaitingFresh?.()).toBe(true)
    at += 200
    expect(source.awaitingFresh?.()).toBe(false)
    source.dispose()
    clock.mockRestore()
  })

  it('takes no input until its first picture with content landed, or 3.5 s after live once a picture landed (D50, F1, D55 3)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    let flat = true
    const probe = vi.spyOn(flatness, 'of').mockImplementation(() => flat)
    const press = (source: StreamSource) => { source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 }); source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 }) }
    const source = new StreamSource('a', '/schedule', 1024, 720)
    expect(source.notYet()).toBe(true)
    live(source)
    // Its first picture is flat: it shows, and the page takes nothing yet.
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.pictureAt()).not.toBeNull()
    expect(source.notYet()).toBe(true)
    press(source)
    source.scroll({ x: 500, y: 300, dx: 0, dy: 120 })
    source.text?.('a')
    expect(input).not.toHaveBeenCalled()
    // The fingers close while it waits, and open after its content landed: judged at the down, nothing is sent.
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    flat = false
    applyXrPanelBinary(picture(2))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.notYet()).toBe(false)
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(input).not.toHaveBeenCalled()
    // From that frame on a press is the page's.
    press(source)
    expect(input).toHaveBeenCalledWith('p1', { type: 'mouse', action: 'click', x: 500, y: 300 })
    // Opened again (a new socket): it waits anew. A flat still is no content, at live or a second after it (D55 3):
    // a page that stays flat opens by the 3.5 s clock alone.
    flat = true
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    live(source)
    expect(source.notYet()).toBe(true)
    applyXrPanelBinary(picture(3, PNG))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.notYet()).toBe(true)
    at += 1000
    applyXrPanelBinary(picture(4, PNG))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.notYet()).toBe(true)
    press(source)
    expect(input).toHaveBeenCalledTimes(1)
    at += 2400
    expect(source.notYet()).toBe(true)
    at += 200
    expect(source.notYet()).toBe(false)
    source.dispose()
    // A page whose picture stays flat takes input 3.5 s after it went live. One with no picture at all takes none,
    // whatever the clock (the next unit, D55 1).
    // Its first picture lands with the live: flat, so it opens 3.5 s after live by the clock alone.
    const blank = new StreamSource('b', '/members', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'b', handle: 'p2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'b', status: 'live', why: '' })
    applyXrPanelBinary(picture(5, JPEG, { panel: 'b' }))
    await decoded()
    blank.beforeDraw?.(renderer, true)
    at += 3400
    expect(blank.notYet()).toBe(true)
    at += 200
    expect(blank.notYet()).toBe(false)
    blank.dispose()
    // B2 (D56 2): the first picture lands 5 s after live, flat (the page's first paint). The clock counts from it: no
    // press before 8.5 s. Content that lands at 5.8 s opens it then.
    for (const content of [false, true]) {
      flat = true
      const late = new StreamSource('l', '/members', 1024, 720)
      applyXrPanelFrame({ op: 'opened', id: 'l', handle: 'p3', css: [1024, 720] })
      applyXrPanelFrame({ op: 'status', id: 'l', status: 'live', why: '' })
      const live0 = at
      at = live0 + 5000
      applyXrPanelBinary(picture(6, JPEG, { panel: 'l' }))
      await decoded()
      late.beforeDraw?.(renderer, true)
      expect(late.notYet()).toBe(true)
      input.mockClear()
      press(late)
      expect(input).not.toHaveBeenCalled()
      if (content) {
        at = live0 + 5800
        flat = false
        applyXrPanelBinary(picture(7, JPEG, { panel: 'l' }))
        await decoded()
        late.beforeDraw?.(renderer, true)
        expect(late.notYet()).toBe(false)
      } else {
        at = live0 + 8400
        expect(late.notYet()).toBe(true)
        at = live0 + 8600
        expect(late.notYet()).toBe(false)
      }
      late.dispose()
    }
    probe.mockRestore()
    clock.mockRestore()
  })

  it('takes no press while no picture of this open has landed, asks again in words the gateway answers at 15 s in view, and fails at 30 s (D55 1, D56 1, M10)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const probe = vi.spyOn(flatness, 'of').mockImplementation(() => false)
    const press = (s: StreamSource) => { s.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 }); s.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 }) }
    const opens = () => ops('open').filter(m => m.id === 'n').length
    // The gateway, as `panel_hub.py` answers (REVIEW-19 M10): an `open` for a panel it does not hold gets "opened" and,
    // once the page loads, "live"; an `open` for a panel it holds gets nothing; a `close` gets "closed". Before this the
    // unit fed "opened" and "live" by hand after the ask again, which the gateway never sends: that hid M10. The stand-in
    // models no `visible` (no picture comes of it) and no "too_many", and the picture at 16 s is fed by hand: enough for
    // M10's own fault, which is the words; the real row `m10.mjs` judges the rest (REVIEW-19 S8).
    const held = new Set<string>()
    const said: Sent[] = []
    const answers: (() => void)[] = []
    setXrPanelSender(payload => {
      const m = (payload as { data: Sent }).data
      sent.push(m)
      said.push(m)
      const id = String(m.id)
      if (m.op === 'open' && !held.has(id)) { held.add(id); answers.push(() => { applyXrPanelFrame({ op: 'opened', id, handle: `h-${id}`, css: [1024, 720] }); applyXrPanelFrame({ op: 'status', id, status: 'live', why: '' }) }) }
      if (m.op === 'close' && held.delete(id)) answers.push(() => applyXrPanelFrame({ op: 'status', id, status: 'closed', why: '' }))
    })
    const answer = () => { while (answers.length) answers.shift()?.() }
    const words = (id: string) => said.filter(m => m.id === id).map(m => (m.op === 'visible' ? `visible:${m.visible}` : String(m.op)))
    // N1: a new page, live, and no picture at all. No clock opens it: at 3.6 s it still takes nothing, and its line says it waits.
    const source = new StreamSource('n', '/schedule', 1024, 720)
    answer()
    expect(source.status()).toBe('live')
    at += 3400
    expect(source.notYet()).toBe(true)
    expect(source.note()).toBe('')
    at += 200
    expect(source.notYet()).toBe(true)
    expect(source.pictureAt()).toBeNull()
    expect(source.note()).toBe('waiting')
    press(source)
    source.scroll({ x: 500, y: 300, dx: 0, dy: 120 })
    expect(input).not.toHaveBeenCalled()
    // At 15 s it asks the gateway once more, in words the gateway answers: "nobody sees it", then "he sees it". It
    // stays live and keeps its handle; no second `open` goes, and it never goes to "loading".
    at += 11_500
    source.notYet()
    answer()
    expect(words('n')).toEqual(['open', 'visible:false', 'visible:true'])
    expect(source.status()).toBe('live')
    expect(source.seen.handle).toBe('h-n')
    at += 1000
    source.notYet()
    expect(words('n')).toEqual(['open', 'visible:false', 'visible:true'])
    // At 16 s a press still sends nothing.
    press(source)
    expect(input).not.toHaveBeenCalled()
    // At 30 s in view with none, it is failed, says so, and gives its panel back: it holds none of the twelve, and the
    // gateway's "closed" for that leaves it failed.
    at += 14_000
    source.notYet()
    answer()
    expect(source.status()).toBe('failed')
    expect(source.note()).toBe('no_picture')
    expect(words('n').at(-1)).toBe('close')
    expect(held.has('n')).toBe(false)
    // A press on its glass asks for it anew: a new `open`, which the gateway answers; a picture then opens it.
    press(source)
    expect(input).not.toHaveBeenCalled()
    expect(words('n').at(-1)).toBe('open')
    answer()
    expect(source.status()).toBe('live')
    expect(source.notYet()).toBe(true)
    await showing(source, 7)
    expect(source.notYet()).toBe(false)
    press(source)
    expect(input).toHaveBeenCalledTimes(1)
    source.dispose()
    input.mockClear()
    // A picture at 16 s, after the ask: it opens the page, and a press is the page's.
    const slow = new StreamSource('s', '/schedule', 1024, 720)
    answer()
    at += 16_000
    slow.notYet()
    answer()
    expect(words('s')).toEqual(['open', 'visible:false', 'visible:true'])
    await showing(slow, 8)
    expect(slow.notYet()).toBe(false)
    press(slow)
    expect(input).toHaveBeenCalledTimes(1)
    slow.dispose()
    input.mockClear()
    // Out of his view a page gets no picture: only his time in view counts, added up (small 1). Away 40 s: not failed,
    // nothing asked. In view 10 s and away 1 s in turn: asked again after 15 s in view, failed after 30 s in view.
    const away = new StreamSource('v', '/schedule', 1024, 720)
    answer()
    away.visible?.(false)
    for (let i = 0; i < 40; i += 1) { at += 1000; away.notYet() }
    expect(away.status()).toBe('live')
    expect(words('v').filter(w => w === 'visible:true')).toEqual([])
    for (let turn = 0; turn < 3 && away.status() === 'live'; turn += 1) {
      away.visible?.(true)
      for (let i = 0; i < 10; i += 1) { at += 1000; away.notYet() }
      away.visible?.(false)
      at += 1000
      away.notYet()
    }
    answer()
    expect(away.status()).toBe('failed')
    expect(words('v').filter(w => w === 'close')).toEqual(['close'])
    away.dispose()
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    // A picture that lands ends the wait as before: from it the page takes a press.
    const late = new StreamSource('n', '/schedule', 1024, 720)
    live(late)
    at += 5000
    expect(late.notYet()).toBe(true)
    await showing(late, 3)
    expect(late.notYet()).toBe(false)
    expect(late.note()).toBe('')
    press(late)
    expect(input).toHaveBeenCalledTimes(1)
    // G7: the socket opens anew after it had shown pictures (a glance, a reconnect). The old picture is no picture of
    // this open: 3.6 s after the new live it takes nothing.
    input.mockClear()
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    live(late)
    at += 3600
    expect(late.notYet()).toBe(true)
    press(late)
    expect(input).not.toHaveBeenCalled()
    await showing(late, 4)
    expect(late.notYet()).toBe(false)
    late.dispose()
    // Refused as one too many, asked again, then live: it holds until its first picture too.
    const full = new StreamSource('n', '/schedule', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'n', status: 'failed', why: 'too_many' })
    expect(full.askAgain()).toBe(true)
    live(full)
    at += 4000
    expect(full.notYet()).toBe(true)
    press(full)
    expect(input).not.toHaveBeenCalled()
    full.dispose()
    // A warm page whose socket opened anew while it was away (the reviewer's small 6): it loads, then it is live with
    // no picture of this open. It takes no press, and says so by the same word that the dim and the dot read.
    const warm = new StreamSource('n', '/schedule', 1024, 720)
    live(warm)
    await showing(warm, 5)
    warm.countWhole?.()
    warm.visible?.(false, true)
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    at += 60_000
    warm.takenBack?.()
    at += 4000
    expect(warm.status()).toBe('loading')
    expect(warm.notYet()).toBe(true)
    live(warm)
    at += 4000
    expect(warm.notYet()).toBe(true)
    press(warm)
    expect(input).not.toHaveBeenCalled()
    warm.dispose()
    probe.mockRestore()
    clock.mockRestore()
  })

  it('tells the gateway again that he watches the page after each "he sees it", which it answers having forgotten the watch (REVIEW-19 S1, C1)', () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const words = () => sent.filter(m => m.id === 'w').map(m => (m.op === 'visible' ? `visible:${m.visible}` : m.op === 'watch' ? `watch:${m.on}` : String(m.op)))
    const source = new StreamSource('w', '/schedule', 1024, 720)
    live(source)
    source.watch(true)
    // The ask at 15 s in view with no picture: "nobody sees it" made the gateway forget the watch; it is said again.
    at += 15_500
    source.notYet()
    expect(words().slice(-3)).toEqual(['visible:false', 'visible:true', 'watch:true'])
    // A glance away and back after a hide that went: the same.
    sent = []
    source.countWhole?.()
    source.visible?.(false)
    source.visible?.(true)
    expect(words()).toEqual(['visible:false', 'visible:true', 'watch:true'])
    // A page he does not watch is told only that he sees it.
    sent = []
    source.watch(false)
    source.visible?.(false)
    source.visible?.(true)
    expect(words()).toEqual(['watch:false', 'visible:false', 'visible:true'])
    source.dispose()
    clock.mockRestore()
  })

  it('lets no answer or picture of the open before end this open: a late "closed", a picture of the old cast (REVIEW-19 S2, S3)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const probe = vi.spyOn(flatness, 'of').mockImplementation(() => false)
    const press = (s: StreamSource) => { s.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 }); s.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 }) }
    // F2: failed at 30 s, `close` sent; a press on the glass opens it anew before the gateway's "closed" for the close came.
    const source = new StreamSource('f', '/schedule', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'f', handle: 'h1', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'f', status: 'live', why: '' })
    at += 30_500
    source.notYet()
    expect(source.note()).toBe('no_picture')
    press(source)
    expect(source.status()).toBe('loading')
    // The late "closed" answers the old close: the new open goes on.
    applyXrPanelFrame({ op: 'status', id: 'f', status: 'closed', why: '' })
    expect(source.status()).toBe('loading')
    applyXrPanelFrame({ op: 'opened', id: 'f', handle: 'h2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'f', status: 'live', why: '' })
    expect(source.status()).toBe('live')
    // A "closed" that answers no close of its own still closes the page, as before.
    applyXrPanelFrame({ op: 'status', id: 'f', status: 'closed', why: '' })
    expect(source.status()).toBe('closed')
    source.dispose()
    // S3: the socket opens anew. A picture of the old cast that comes before the new "opened", or after it from the old
    // cast, is no picture of this open.
    const page = new StreamSource('g', '/schedule', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'g', handle: 'h1', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'g', status: 'live', why: '' })
    applyXrPanelBinary(picture(1, JPEG, { panel: 'g', stream: 'old' }))
    await decoded()
    page.beforeDraw?.(renderer, true)
    expect(page.notYet()).toBe(false)
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    applyXrPanelBinary(picture(2, JPEG, { panel: 'g', stream: 'old' }))
    await decoded()
    page.beforeDraw?.(renderer, true)
    applyXrPanelFrame({ op: 'opened', id: 'g', handle: 'h2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'g', status: 'live', why: '' })
    applyXrPanelBinary(picture(3, JPEG, { panel: 'g', stream: 'new' }))
    applyXrPanelBinary(picture(4, JPEG, { panel: 'g', stream: 'old' }))
    await decoded()
    expect(page.seen.pictures).toBe(2)
    page.beforeDraw?.(renderer, true)
    expect(page.notYet()).toBe(false)
    page.dispose()
    // REVIEW-20 Q3: a `close` sent at 30 s whose socket then went: no answer to it will come on the new socket, so
    // a "closed" there closes the page, as one that answers no close of its own does.
    const owed = new StreamSource('q', '/schedule', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'q', handle: 'h1', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'q', status: 'live', why: '' })
    at += 30_500
    owed.notYet()
    expect(owed.note()).toBe('no_picture')
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    applyXrPanelFrame({ op: 'opened', id: 'q', handle: 'h2', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'q', status: 'live', why: '' })
    applyXrPanelFrame({ op: 'status', id: 'q', status: 'closed', why: '' })
    expect(owed.status()).toBe('closed')
    owed.dispose()
    probe.mockRestore()
    clock.mockRestore()
  })

  it('counts no picture of the open before as one of this open: one that waits to land, or to be decoded (REVIEW-19 small 3, O1)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const probe = vi.spyOn(flatness, 'of').mockImplementation(() => false)
    const press = (s: StreamSource) => { s.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 }); s.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 }) }
    const reopen = () => { setXrPanelSender(null); setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) }) }
    // A picture decoded and waiting for the next frame when the socket opens anew: it does not land after the open.
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source, 1)
    applyXrPanelBinary(picture(2))
    await decoded()
    reopen()
    live(source)
    source.beforeDraw?.(renderer, true)
    expect(source.pictureAt()).toBeNull()
    expect(source.notYet()).toBe(true)
    press(source)
    expect(input).not.toHaveBeenCalled()
    // A picture whose decode is under way at the open: given up when the decode ends.
    applyXrPanelBinary(picture(3))
    reopen()
    live(source)
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.pictureAt()).toBeNull()
    expect(source.notYet()).toBe(true)
    // A picture of this open lands, and the page is its own again.
    await showing(source, 4)
    expect(source.notYet()).toBe(false)
    source.dispose()
    probe.mockRestore()
    clock.mockRestore()
  })

  it('is told early when his dot rests on its room\'s card, a picture after that word is fresh, and the dim lasts while a press is held (D52)', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const words = () => ops('visible').map(m => m.visible)
    const source = new StreamSource('a', '/members', 1024, 720)
    live(source)
    await showing(source)
    source.countWhole?.()
    source.visible?.(false)
    expect(words()).toEqual([false])
    // His dot rests on the card: he may see it; he leaves, and it is hidden again.
    source.peek?.(true)
    source.peek?.(false)
    expect(words()).toEqual([false, true, false])
    // He rests again; a picture arrives before the press; the press takes the room.
    source.peek?.(true)
    at += 300
    applyXrPanelBinary(picture(2))
    await decoded()
    at += 100
    source.takenBack?.()
    expect(source.awaitingFresh?.()).toBe(true)
    source.beforeDraw?.(renderer, true)
    expect(source.notYet()).toBe(false)
    // No picture since it came back into view: dim until the gateway's next picture, and 3 s at most (D53 1: no wait
    // that nothing ends).
    source.visible?.(false, true)
    source.takenBack?.()
    at += 2900
    expect(source.notYet()).toBe(true)
    at += 200
    expect(source.awaitingFresh?.()).toBe(false)
    expect(source.notYet()).toBe(false)
    source.dispose()
    clock.mockRestore()
  })

  it('asks again at once when a warm room made room for it after a "too many", and says "full" only when none could (M1 a)', () => {
    const freed: string[] = []
    let rooms = 1
    setFullHook(s => { if (rooms === 0) return false; rooms -= 1; freed.push(s.id); return true })
    const source = new StreamSource('a', '/settings', 1024, 720)
    expect(ops('open')).toHaveLength(1)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'too_many' })
    expect(freed).toEqual(['a'])
    expect(ops('open')).toHaveLength(2)
    expect(source.status?.()).toBe('loading')
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'too_many' })
    expect(ops('open')).toHaveLength(2)
    expect(source.note?.()).toBe('full')
    setFullHook(null)
    source.dispose()
  })

  it('says whether it was whole: it showed its picture long enough, now or before it left the view', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const source = new StreamSource('a', '/members', 1024, 720)
    live(source)
    expect(source.wasWhole(4000)).toBe(false)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    at += 3000
    expect(source.wasWhole(4000)).toBe(false)
    at += 1500
    expect(source.wasWhole(4000)).toBe(true)
    // It leaves the view and comes back: it was whole, though its new picture is young.
    source.visible?.(false, true)
    at += 60_000
    expect(source.wasWhole(4000)).toBe(true)
    source.visible?.(true, true)
    applyXrPanelBinary(picture(2))
    await decoded()
    source.beforeDraw?.(renderer, true)
    at += 100
    expect(source.wasWhole(4000)).toBe(true)
    source.dispose()
    // A page that left the view half a second after its first picture never filled: not whole, however long ago.
    const young = new StreamSource('a', '/members', 1024, 720)
    live(young)
    applyXrPanelBinary(picture(1))
    await decoded()
    young.beforeDraw?.(renderer, true)
    at += 500
    young.visible?.(false, true)
    at += 60_000
    expect(young.wasWhole(4000)).toBe(false)
    // Asked for again (the socket came back): it starts anew.
    young.visible?.(true, true)
    at += 5000
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    expect(young.wasWhole(4000)).toBe(false)
    young.dispose()
    clock.mockRestore()
  })

  it('says, for a page held out of sight, how long it is live, how old its newest picture is, and whether that is a still', async () => {
    let at = 1000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
    const source = new StreamSource('a', '/members', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'a', handle: 'p1', css: [1024, 720] })
    expect(source.restFacts()).toEqual({ liveForMs: null, lastPictureMs: null, stillAfterLiveMs: null })
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'live', why: '' })
    at += 500
    expect(source.restFacts()).toEqual({ liveForMs: 500, lastPictureMs: null, stillAfterLiveMs: null })
    // A moving picture (JPEG), then the gateway's still (PNG).
    applyXrPanelBinary(picture(1))
    await decoded()
    at += 300
    expect(source.restFacts()).toEqual({ liveForMs: 800, lastPictureMs: 300, stillAfterLiveMs: null })
    at += 4000
    applyXrPanelBinary(picture(2, PNG))
    await decoded()
    at += 2000
    expect(source.restFacts()).toEqual({ liveForMs: 6800, lastPictureMs: 2000, stillAfterLiveMs: 4800 })
    // A moving picture after it (a caret that blinks): the still's time stays.
    applyXrPanelBinary(picture(3))
    await decoded()
    expect(source.restFacts()).toEqual({ liveForMs: 6800, lastPictureMs: 0, stillAfterLiveMs: 4800 })
    // No panel ever drew it: it is not whole by itself, and counts as whole once it is said to have filled.
    expect(source.wasWhole(4000)).toBe(false)
    source.countWhole()
    expect(source.wasWhole(4000)).toBe(true)
    source.dispose()
    clock.mockRestore()
  })

  it('says when the gateway refused to open its page, and not when a page that was shown is ended', () => {
    const source = new StreamSource('a', '/capabilities', 1024, 720)
    expect(source.refusedOpen()).toBe(false)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'address' })
    expect(source.refusedOpen()).toBe(true)
    source.dispose()
    // Any other refusal is not about the address, and a page that was live has been shown.
    const full = new StreamSource('b', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'b', status: 'failed', why: 'too_many' })
    expect(full.refusedOpen()).toBe(false)
    full.dispose()
    const shown = new StreamSource('c', '/chat', 1024, 720)
    live(shown)
    applyXrPanelFrame({ op: 'status', id: 'c', status: 'failed', why: 'address' })
    expect(shown.refusedOpen()).toBe(false)
    shown.dispose()
  })

  it('hands on a window that the gateway lets a panel show, and says its line for one it cannot', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    const opened: string[] = []
    source.windows(path => { opened.push(path) })
    applyXrPanelFrame({ op: 'window', id: 'a', path: '/schedule' })
    applyXrPanelFrame({ op: 'window', id: 'b', path: '/artifacts' })
    expect(opened).toEqual(['/schedule'])
    // A path that no panel may hold is not opened by the XR page either.
    applyXrPanelFrame({ op: 'window', id: 'a', path: '//example.com/x' })
    expect(opened).toEqual(['/schedule'])
    expect(source.note?.()).toBe('refused')
    source.windows(null)
    applyXrPanelFrame({ op: 'window', id: 'a', path: '/schedule' })
    expect(opened).toEqual(['/schedule'])
    source.dispose()
  })

  it('is at the path the gateway says its tab shows, and asks for that page when the socket comes back', () => {
    const source = new StreamSource('a', '/artifacts/report-1', 1024, 720)
    live(source)
    expect(source.pagePath()).toBe('/artifacts/report-1')
    const heard = vi.fn()
    source.subscribe(heard)
    // A link inside the page led to the list of artifacts.
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/artifacts' })
    expect(source.pagePath()).toBe('/artifacts')
    expect(heard).toHaveBeenCalledTimes(1)
    // The same path again, a path that opens in neither of the gateway's ways and a path of another panel change nothing.
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/artifacts' })
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/deploy' })
    applyXrPanelFrame({ op: 'path', id: 'b', path: '/chat' })
    applyXrPanelFrame({ op: 'path', id: 'a', path: 7 })
    expect(source.pagePath()).toBe('/artifacts')
    expect(heard).toHaveBeenCalledTimes(1)
    setXrPanelSender(null)
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    expect(ops('open').at(-1)).toEqual(expect.objectContaining({ id: 'a', path: '/artifacts' }))
    // A page that is off the guarded list is one the gateway told with its guard off: the panel is at it.
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/settings/connections' })
    expect(source.pagePath()).toBe('/settings/connections')
    source.dispose()
  })

  it('keeps the query of its address when the gateway tells the same path with none', () => {
    const source = new StreamSource('a', '/members?member=Mira', 390, 720)
    live(source)
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/members' })
    expect(source.pagePath()).toBe('/members?member=Mira')
    applyXrPanelFrame({ op: 'path', id: 'a', path: '/schedule' })
    expect(source.pagePath()).toBe('/schedule')
    source.dispose()
  })

  it('says a picture is decoded (the pace), and that it landed only once it is in the texture', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    const before = source.frame().texture
    expect(applyXrPanelBinary(picture(1))).toBe(true)
    await decoded()
    expect(ops('ack')).toEqual([{ op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'decoded' }])
    // The readout counts a picture once it is in the texture, not when it arrives.
    expect(source.readout?.().count).toBe(0)
    source.beforeDraw?.(renderer, true)
    expect(ops('ack')).toEqual([{ op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'decoded' }, { op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'landed' }])
    expect(source.readout?.().count).toBe(1)
    // Its bytes are the bytes of the pictures that landed, so kB a picture is of what he saw.
    expect(source.readout?.().bytes).toBeGreaterThan(0)
    // Its bytes are the bytes of the pictures that landed, so kB a picture is of what he saw.
    expect(source.readout?.().bytes).toBeGreaterThan(0)
    expect(renderer.initTexture).toHaveBeenCalledWith(source.frame().texture)
    expect(source.frame().texture).not.toBe(before)
    // Turned the right way up by the texture, not by the decode.
    expect(source.frame().texture.repeat.y).toBe(-1)
    expect(source.frame().texture.offset.y).toBe(1)
    expect(globalThis.createImageBitmap).toHaveBeenCalledWith(expect.any(Blob))
    source.dispose()
  })

  it('writes a picture of the same size into the same texture and tells no one', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    const texture = source.frame().texture
    const changed = vi.fn()
    source.subscribe(changed)
    applyXrPanelBinary(picture(2))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.frame().texture).toBe(texture)
    expect(changed).not.toHaveBeenCalled()
    expect(landedAcks().map(m => m.seq)).toEqual([1, 2])
    source.dispose()
  })

  it('never writes into a kept picture, and frees it only when it is let go (M52, D96 5)', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    const kept = source.keepPicture()
    const image = kept.image
    const freed = vi.spyOn(kept, 'dispose')
    // A picture of the same size, then another of the same size: neither goes into the kept texture.
    for (const seq of [2, 3]) {
      applyXrPanelBinary(picture(seq))
      await decoded()
      source.beforeDraw?.(renderer, true)
    }
    expect(source.frame().texture).not.toBe(kept)
    expect(kept.image).toBe(image)
    expect(freed).not.toHaveBeenCalled()
    // Let go, the live texture takes its next picture of the same size as before. The kept one is the source's
    // picture before this one now, so it is freed with the source's own textures, once.
    source.letGoPicture()
    const live2 = source.frame().texture
    applyXrPanelBinary(picture(4))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.frame().texture).toBe(live2)
    source.dispose()
    expect(freed).toHaveBeenCalledTimes(1)
  })

  it('makes the small copies for a picture that stays, and changes the filter only when the page stops or starts', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    const show = async (seq: number, bytes: number[]) => { applyXrPanelBinary(picture(seq, bytes)); await decoded(); source.beforeDraw?.(renderer, true) }
    await show(1, JPEG)
    expect(source.frame().texture.generateMipmaps).toBe(false)
    await show(2, JPEG)
    await show(3, JPEG)
    expect(source.seen.switches).toBe(0)
    await show(4, PNG)
    expect(source.frame().texture.generateMipmaps).toBe(true)
    expect(source.seen).toMatchObject({ switches: 1, stills: 1, pictures: 4 })
    await show(5, JPEG)
    expect(source.frame().texture.generateMipmaps).toBe(false)
    expect(source.seen.switches).toBe(2)
    source.dispose()
  })

  it('takes a new texture for a picture of another size, and the page size the picture names', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    const first = source.frame().texture
    const changed = vi.fn()
    source.subscribe(changed)
    applyXrPanelBinary(picture(2, JPEG, { w: 2800, h: 1800, css: [1400, 900] }))
    await decoded()
    source.beforeDraw?.(renderer, true)
    expect(source.frame()).toMatchObject({ cssWidth: 1400, cssHeight: 900 })
    expect(source.frame().texture).not.toBe(first)
    expect(changed).toHaveBeenCalledTimes(1)
    source.dispose()
  })

  it('counts a picture that has another shape than its page, and says how many the gateway dropped', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1, JPEG, { misshapen: 2 }))
    expect(source.seen).toMatchObject({ pictures: 1, misshapen: 2, offShape: 0 })
    // A tab with its window's own view, 1280 by 657, scaled into the page's box.
    applyXrPanelBinary(picture(2, JPEG, { w: 2048, h: 1051 }))
    expect(source.seen).toMatchObject({ pictures: 2, offShape: 1 })
    // Scaled into its box and rounded: a pixel off is the page's shape.
    applyXrPanelBinary(picture(3, JPEG, { w: 1618, h: 1137, css: [1156, 812] }))
    expect(source.seen.offShape).toBe(1)
    expect(hasShape(1618, 1139, [1156, 812])).toBe(false)
    expect(hasShape(0, 720, [1024, 720])).toBe(false)
    source.dispose()
  })

  it('drops a picture of another panel, and a frame that is not a panel picture', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelBinary(picture(1, JPEG, { panel: 'b' }))
    expect(source.seen.pictures).toBe(0)
    expect(decodeXrPanelPicture(picture(1, JPEG, { type: 'browser_frame' }))).toBeNull()
    expect(decodeXrPanelPicture(new ArrayBuffer(3))).toBeNull()
    expect(decodeXrPanelPicture(picture(1, JPEG, { w: 0 }))).toBeNull()
    source.dispose()
    // With no panel listening a binary frame is left for the live browser card.
    expect(applyXrPanelBinary(picture(1))).toBe(false)
  })

  it('sends a press and its release together, as one click where the hand let go', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    expect(input).not.toHaveBeenCalled()
    source.pointer({ type: 'up', pointerId: 1, x: 502.4, y: 301 })
    expect(input).toHaveBeenCalledWith('p1', { type: 'mouse', action: 'click', x: 502, y: 301 })
    // A press that became a scroll was cancelled: its release is no click.
    input.mockClear()
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'cancel', pointerId: 1, x: 500, y: 330 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 400 })
    expect(input).not.toHaveBeenCalled()
    source.dispose()
  })

  it('has a press on an ask\'s panel carry where the page last drew the controls that answer an ask, and hands on where they are now when it was not sent', async () => {
    const source = new StreamSource('a', '/members', 390, 720)
    live(source)
    await showing(source)
    const heard: string[] = []
    let word: string | null = '[[30,649,96,29]]'
    source.answersBy({ heard: () => word, moved: places => { heard.push(places) } })
    input.mockResolvedValue({ ok: true, sent: 0, moved: true, grants: [[30, 601, 96, 29], [132, 601, 78, 29]] })
    source.pointer({ type: 'down', pointerId: 1, x: 60, y: 660 })
    source.pointer({ type: 'up', pointerId: 1, x: 60, y: 660 })
    expect(input).toHaveBeenCalledWith('p1', { type: 'mouse', action: 'click', x: 60, y: 660, grants: [[30, 649, 96, 29]] })
    await vi.waitFor(() => expect(heard).toEqual(['[[30,601,96,29],[132,601,78,29]]']))
    // Nothing heard yet, or the person's page now: the press carries no such word, and is sent as any press.
    word = null
    input.mockResolvedValue({ ok: true, sent: 1 })
    source.pointer({ type: 'down', pointerId: 1, x: 60, y: 660 })
    source.pointer({ type: 'up', pointerId: 1, x: 60, y: 660 })
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'click', x: 60, y: 660 })
    source.dispose()
  })

  it('lights what the gateway says a pinch would take, and says so when a pinch was not passed on', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const allow = { rect: [480, 290, 90, 24], kind: 'button', label: 'Allow once', guarded: true }
    input.mockResolvedValue({ ok: true, sent: 1, target: allow })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.target?.()).toEqual({ rect: [480, 290, 90, 24], kind: 'button', guarded: true }))
    expect(source.note?.()).toBe('')
    input.mockResolvedValue({ ok: true, sent: 0, guarded: true, target: allow })
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.note?.()).toBe('guarded'))
    source.pointer({ type: 'leave', pointerId: 1, x: 0, y: 0 })
    expect(source.target?.()).toBeNull()
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'leave' })
    source.dispose()
  })

  it('lets the pointer leave a page that leaves his view under it, and lets a page with no pointer go with no word (N6)', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, sent: 1 })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 500, y: 300 }))
    const leaves = () => input.mock.calls.filter(([, e]) => (e as { type?: string }).type === 'leave').length
    source.visible?.(false)
    expect(leaves()).toBe(1)
    // Back in view: no word until the pointer moves on it again, and then a press clicks as before.
    source.visible?.(true)
    source.visible?.(false)
    expect(leaves()).toBe(1)
    source.visible?.(true)
    // Its fresh picture after the return (D49): then it takes a press.
    await showing(source, 901)
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(input).toHaveBeenLastCalledWith('p1', expect.objectContaining({ type: 'mouse', action: 'click', x: 500, y: 300 })))
    source.dispose()
  })

  it('forgets a press across a hide: a release after it clicks nothing, and a pinch in view still clicks (R1)', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, sent: 1 })
    const clicks = () => input.mock.calls.filter(([, e]) => (e as { action?: string }).action === 'click').length
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.visible?.(false)
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(clicks()).toBe(0)
    // Shown again with its fresh picture: a pinch that begins and ends on it clicks once.
    source.visible?.(true)
    await showing(source, 901)
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(clicks()).toBe(1))
    source.dispose()
  })

  it('has a press carry the last answer\'s box for its place while a move to that place is on its way, and none for a place with no answer yet', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const field = { rect: [480, 290, 90, 24], kind: 'text' }
    input.mockResolvedValue({ ok: true, sent: 1, target: field })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.target?.()?.rect).toEqual([480, 290, 90, 24]))
    // A new picture sends a move to the same place; its answer is on its way when he presses.
    let answer: (value: unknown) => void = () => {}
    input.mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    await vi.waitFor(() => expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 500, y: 300 }))
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'click', x: 500, y: 300, aimed: [480, 290, 90, 24] })
    answer({ ok: true, target: field })
    // He went to another place, and its answer has not come: that press carries no box.
    input.mockImplementationOnce(() => new Promise(() => {}))
    source.pointer({ type: 'move', pointerId: 1, x: 700, y: 500 })
    await vi.waitFor(() => expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 700, y: 500 }))
    source.pointer({ type: 'down', pointerId: 1, x: 700, y: 500 })
    source.pointer({ type: 'up', pointerId: 1, x: 700, y: 500 })
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'click', x: 700, y: 500 })
    source.dispose()
  })

  it('has a press carry the label of the control it aimed at beside its box, cut to 80 characters', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, sent: 1, target: { rect: [480, 290, 90, 24], kind: 'button', label: 'Allow once', guarded: false } })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.target?.()?.rect).toEqual([480, 290, 90, 24]))
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'click', x: 500, y: 300, aimed: [480, 290, 90, 24, 'Allow once'] })
    input.mockResolvedValue({ ok: true, sent: 1, target: { rect: [480, 290, 91, 24], kind: 'button', label: 'y'.repeat(100), guarded: false } })
    source.pointer({ type: 'move', pointerId: 1, x: 501, y: 300 })
    await vi.waitFor(() => expect(source.target?.()?.rect).toEqual([480, 290, 91, 24]))
    source.pointer({ type: 'down', pointerId: 1, x: 501, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 501, y: 300 })
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'click', x: 501, y: 300, aimed: [480, 290, 91, 24, 'y'.repeat(80)] })
    source.dispose()
  })

  it('says its own line when the gateway held a press back because a picker of the browser was open', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, sent: 0, picker: true })
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.note?.()).toBe('picker'))
    source.dispose()
  })

  it.each([
    ['text', (source: StreamSource) => source.text('hunter2'), { type: 'text', text: 'hunter2' }],
    ['a key', (source: StreamSource) => source.key({ type: 'down', key: 'Enter' }), { type: 'key', key: 'Enter' }],
  ] as const)('says so when %s was not passed on, because the page\'s focus is in a guarded surface', async (_what, send, event) => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, sent: 0, guarded: true })
    send(source)
    expect(input).toHaveBeenLastCalledWith('p1', event)
    await vi.waitFor(() => expect(source.note?.()).toBe('guarded'))
    source.dispose()
  })

  it('says nothing when text was not sent because the page could not be asked', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    let settled = false
    input.mockImplementation(async () => { settled = true; return { ok: true, sent: 0, unread: true } })
    source.text('hello')
    await vi.waitFor(() => expect(settled).toBe(true))
    await Promise.resolve()
    expect(source.note?.()).toBe('')
    source.dispose()
  })

  it('sends one move at a time, and the newest place after it', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    let answer: (value: unknown) => void = () => {}
    input.mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
    source.pointer({ type: 'move', pointerId: 1, x: 10, y: 10 })
    source.pointer({ type: 'move', pointerId: 1, x: 20, y: 20 })
    source.pointer({ type: 'move', pointerId: 1, x: 30, y: 30 })
    expect(input).toHaveBeenCalledTimes(1)
    answer({ ok: true, target: null })
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(2))
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 30, y: 30 })
    source.dispose()
  })

  it('sends no move for a pointer that rests, and asks once more what is under it after the page scrolled', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, target: null })
    const moves = () => input.mock.calls.filter(([, e]) => (e as { action?: string }).action === 'move').length
    // A move at every frame, at one place of the page (within the rounding to a CSS pixel).
    for (let i = 0; i < 20; i += 1) { source.pointer({ type: 'move', pointerId: 1, x: 500 + (i % 2) * 0.2, y: 300 }); await Promise.resolve() }
    await vi.waitFor(() => expect(moves()).toBe(1))
    source.pointer({ type: 'move', pointerId: 1, x: 510, y: 300 })
    await vi.waitFor(() => expect(moves()).toBe(2))
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 510, y: 300 })
    // The wheel turns the page under it: one move to the same place follows the wheel's answer.
    source.scroll({ x: 510, y: 300, dx: 0, dy: 100 })
    await vi.waitFor(() => expect(moves()).toBe(3))
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 510, y: 300 })
    source.pointer({ type: 'move', pointerId: 1, x: 510, y: 300 })
    // It left, and comes back to the same place: that is sent again.
    source.pointer({ type: 'leave', pointerId: 1, x: 0, y: 0 })
    source.pointer({ type: 'move', pointerId: 1, x: 510, y: 300 })
    await vi.waitFor(() => expect(moves()).toBe(4))
    source.dispose()
  })

  it('sends one move to the resting place after each new picture, so the page\'s own hover follows a page that changed', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    input.mockResolvedValue({ ok: true, target: null })
    const moves = () => input.mock.calls.filter(([, e]) => (e as { action?: string }).action === 'move')
    source.pointer({ type: 'move', pointerId: 1, x: 400, y: 300 })
    await vi.waitFor(() => expect(moves()).toHaveLength(1))
    // Two pictures of a page that changed under the resting pointer: one move after each.
    for (const seq of [1, 2]) {
      applyXrPanelBinary(picture(seq))
      await decoded()
      source.beforeDraw?.(renderer, true)
      await vi.waitFor(() => expect(moves()).toHaveLength(1 + seq))
    }
    expect(moves().at(-1)?.[1]).toEqual({ type: 'mouse', action: 'move', x: 400, y: 300 })
    // A pointer that left: a picture sends no move.
    source.pointer({ type: 'leave', pointerId: 1, x: 0, y: 0 })
    applyXrPanelBinary(picture(3))
    await decoded()
    source.beforeDraw?.(renderer, true)
    await Promise.resolve()
    expect(moves()).toHaveLength(3)
    source.dispose()
  })

  it('adds up the scrolls that wait behind one, so the page ends where the hand did', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    let answer: (value: unknown) => void = () => {}
    input.mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
    source.scroll({ x: 500, y: 300, dx: 0, dy: 40 })
    source.scroll({ x: 500, y: 310, dx: 0, dy: 30 })
    source.scroll({ x: 500, y: 320, dx: 0, dy: 20 })
    expect(input).toHaveBeenCalledTimes(1)
    answer({ ok: true })
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(2))
    expect(input).toHaveBeenLastCalledWith('p1', { type: 'wheel', x: 500, y: 320, dx: 0, dy: 50 })
    source.dispose()
  })

  it('lays the page out for the newest size only', () => {
    vi.useFakeTimers()
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    source.resize(1100, 760)
    source.resize(1250.4, 820)
    source.resize(1400, 900)
    expect(ops('size')).toEqual([])
    vi.advanceTimersByTime(200)
    expect(ops('size')).toEqual([{ op: 'size', id: 'a', w: 1400, h: 900 }])
    source.dispose()
  })

  it('tells the gateway when the panel leaves the view, and a new detail only when its step changes', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    // It was whole in his view (one that never was goes on filling first, D48 A, below).
    applyXrPanelBinary(picture(1))
    await decoded()
    source.beforeDraw?.(renderer, true)
    source.countWhole?.()
    source.visible?.(true, true)
    source.visible?.(false, true)
    source.visible?.(false, true)
    expect(ops('visible')).toEqual([{ op: 'visible', id: 'a', visible: false }])
    source.detail?.(1.96)
    source.detail?.(1.38)
    expect(ops('detail')).toEqual([])
    source.detail?.(0.98)
    source.detail?.(1.0)
    expect(ops('detail')).toEqual([{ op: 'detail', id: 'a', detail: 1.4 }])
    source.dispose()
  })

  it('tells the gateway which page he looks at once a change, again on a new socket, and never after it is gone', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    source.gaze?.(4, 1.75)
    expect(source.watchPage()).toEqual({ id: 'a', inView: true, gazeDeg: 4, far: 1.75 })
    source.watch(true)
    source.watch(true)
    expect(ops('watch')).toEqual([{ op: 'watch', id: 'a', on: true }])
    // The socket drops and a new one opens: the page is opened again, and the word is sent again after it.
    setXrPanelSender(null)
    sent = []
    setXrPanelSender(payload => { sent.push((payload as { data: Sent }).data) })
    expect(sent.map(m => m.op)).toEqual(['open', 'watch'])
    source.watch(false)
    expect(ops('watch').at(-1)).toEqual({ op: 'watch', id: 'a', on: false })
    source.visible?.(false, true)
    expect(source.watchPage().inView).toBe(false)
    source.dispose()
    sent = []
    source.watch(true)
    expect(ops('watch')).toEqual([])
  })

  it('has no picture and sends no input when the gateway does not know the caller, or live pages are off', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'off' })
    expect(source.note?.()).toBe('off')
    source.pointer({ type: 'down', pointerId: 1, x: 5, y: 5 })
    source.pointer({ type: 'up', pointerId: 1, x: 5, y: 5 })
    source.text?.('hello')
    expect(input).not.toHaveBeenCalled()
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'signed-out', why: 'gate_refused' })
    expect(source.status?.()).toBe('signed-out')
    expect(source.note?.()).toBe('signed-out')
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'owner_only' })
    expect(source.note?.()).toBe('failed')
    source.dispose()
  })

  describe('on a composition layer', () => {
    /** A layer that takes what it is offered, as `PageLayer` does, and writes it when the test says a frame came. */
    function fakeLayer(width = 2048, height = 1440) {
      const offered: LayerPicture[] = []
      const layer = {
        width, height,
        offer: (p: LayerPicture) => { if (!p.flipped || p.width !== width || p.height !== height) return false; offered.push(p); return true },
        frame: () => { const p = offered.shift(); p?.done?.(); return !!p },
      }
      return { layer: layer as unknown as PageLayer, offered, frame: layer.frame }
    }
    const decodes = () => (globalThis.createImageBitmap as ReturnType<typeof vi.fn>).mock.calls

    it('decodes a picture the layer\'s way up, hands it to the layer, and says it arrived only once the layer has it', async () => {
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      const { layer, offered, frame } = fakeLayer()
      source.toLayer?.(layer)
      ;(renderer.initTexture as ReturnType<typeof vi.fn>).mockClear()
      const texture = source.frame().texture
      applyXrPanelBinary(picture(1))
      await decoded()
      // A bitmap decoded with no options would stand on its head on a layer.
      expect(decodes().at(-1)).toEqual([expect.any(Blob), { imageOrientation: 'flipY' }])
      source.beforeDraw?.(renderer, true)
      expect(offered).toHaveLength(1)
      expect(offered[0]).toMatchObject({ width: 2048, height: 1440, flipped: true })
      // Handed over, not yet written: the gateway was told only that it is decoded.
      expect(ops('ack')).toEqual([{ op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'decoded' }])
      frame()
      expect(ops('ack')).toEqual([{ op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'decoded' }, { op: 'ack', id: 'a', seq: 1, stream: 's1', kind: 'landed' }])
      // The texture of the mesh is left alone: nothing is written twice.
      expect(renderer.initTexture).not.toHaveBeenCalled()
      expect(source.frame().texture).toBe(texture)
      expect(source.seen.layered).toBe(true)
      source.dispose()
    })

    it('ends no wait with a picture decoded again for the layer: only a picture of the gateway does (D55 2, M9)', async () => {
      let at = 1000
      const clock = vi.spyOn(performance, 'now').mockImplementation(() => at)
      const probe = vi.spyOn(flatness, 'of').mockImplementation(() => false)
      const press = (s: StreamSource) => { s.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 }); s.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 }) }
      // L1: read for 6 s, whole; he looks away for 5 s (the hide goes) and back. No picture since he looked back.
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      await showing(source, 1)
      at += 6000
      source.visible?.(false, true)
      at += 5000
      source.visible?.(true, true)
      expect(source.notYet()).toBe(true)
      // The page is picked for a layer again: its last picture is decoded once more and lands as "again".
      const { layer, frame } = fakeLayer()
      source.toLayer?.(layer)
      await decoded()
      at += 40
      source.beforeDraw?.(renderer, true)
      expect(frame()).toBe(true)
      expect(source.notYet()).toBe(true)
      press(source)
      expect(input).not.toHaveBeenCalled()
      // The gateway's fresh picture ends the wait.
      applyXrPanelBinary(picture(2))
      await decoded()
      source.beforeDraw?.(renderer, true)
      frame()
      expect(source.notYet()).toBe(false)
      press(source)
      expect(input).toHaveBeenCalledTimes(1)
      source.dispose()
      // L2: a warm page taken back waits for its fresh picture; a picture decoded again for a layer is not one.
      input.mockClear()
      const warm = new StreamSource('a', '/chat', 1024, 720)
      live(warm)
      await showing(warm, 3)
      warm.countWhole?.()
      warm.visible?.(false, true)
      at += 60_000
      warm.takenBack?.()
      const second = fakeLayer()
      warm.toLayer?.(second.layer)
      await decoded()
      warm.beforeDraw?.(renderer, true)
      second.frame()
      expect(warm.awaitingFresh?.()).toBe(true)
      expect(warm.notYet()).toBe(true)
      warm.dispose()
      probe.mockRestore()
      clock.mockRestore()
    })

    it('says how big its newest picture is, so that a layer of that size can be made', async () => {
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      expect(source.pictureSize?.()).toBeNull()
      applyXrPanelBinary(picture(1, JPEG, { w: 1434, h: 1008 }))
      await decoded()
      expect(source.pictureSize?.()).toEqual([1434, 1008])
      source.dispose()
    })

    it('keeps a picture that the layer cannot take, and hands it over when a layer of its size is there', async () => {
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      const small = fakeLayer(1434, 1008)
      source.toLayer?.(small.layer)
      applyXrPanelBinary(picture(1))
      await decoded()
      source.beforeDraw?.(renderer, true)
      source.beforeDraw?.(renderer, true)
      expect(small.offered).toHaveLength(0)
      const right = fakeLayer()
      source.toLayer?.(right.layer)
      await decoded()
      source.beforeDraw?.(renderer, true)
      expect(right.offered).toHaveLength(1)
      right.frame()
      expect(landedAcks().map(m => m.seq)).toEqual([1])
      source.dispose()
    })

    it('decodes the picture that is shown once more when the page goes onto a layer, and again when it comes back to its mesh, and answers the gateway once', async () => {
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      applyXrPanelBinary(picture(1))
      await decoded()
      source.beforeDraw?.(renderer, true)
      expect(landedAcks().map(m => m.seq)).toEqual([1])
      const before = { ...source.seen }
      // Onto a layer: the same picture, the other way up.
      const { layer, offered, frame } = fakeLayer()
      source.toLayer?.(layer)
      await decoded()
      expect(decodes().at(-1)).toEqual([expect.any(Blob), { imageOrientation: 'flipY' }])
      source.beforeDraw?.(renderer, true)
      expect(offered).toHaveLength(1)
      frame()
      // Back to the mesh: decoded with no options again, and written into the texture.
      ;(renderer.initTexture as ReturnType<typeof vi.fn>).mockClear()
      source.toLayer?.(null)
      await decoded()
      expect(decodes().at(-1)).toEqual([expect.any(Blob)])
      source.beforeDraw?.(renderer, true)
      expect(renderer.initTexture).toHaveBeenCalledTimes(1)
      // One picture arrived, and one answer went out: the pictures decoded again are not counted or answered.
      expect(landedAcks().map(m => m.seq)).toEqual([1])
      expect(source.seen.pictures).toBe(before.pictures)
      expect(source.seen.layered).toBe(false)
      source.dispose()
    })

    it('decodes a picture again that was decoded for the place the page has just left', async () => {
      const source = new StreamSource('a', '/chat', 1024, 720)
      live(source)
      const { layer, offered, frame } = fakeLayer()
      source.toLayer?.(layer)
      applyXrPanelBinary(picture(1))
      await decoded()
      // The layer is taken away before the picture was handed over.
      source.toLayer?.(null)
      await decoded()
      source.beforeDraw?.(renderer, true)
      await decoded()
      source.beforeDraw?.(renderer, true)
      expect(offered).toHaveLength(0)
      frame()
      expect(decodes().at(-1)).toEqual([expect.any(Blob)])
      // The picture is in the texture, and the gateway heard of it once.
      expect(landedAcks().map(m => m.seq)).toEqual([1])
      source.dispose()
    })
  })

  it('says that no more pages can be open when the gateway refuses one page too many', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'too_many' })
    expect(source.status?.()).toBe('failed')
    expect(source.note?.()).toBe('full')
    source.dispose()
  })

  it('asks for its page once more only when the gateway had no room for it', () => {
    const source = new StreamSource('a', '/chat/s-1', 1024, 720)
    const opens = () => sent.filter(m => m.op === 'open').length
    const before = opens()
    expect(source.askAgain()).toBe(false)
    live(source)
    expect(source.askAgain()).toBe(false)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'address' })
    expect(source.askAgain()).toBe(false)
    expect(opens()).toBe(before)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'too_many' })
    expect(source.askAgain()).toBe(true)
    expect(opens()).toBe(before + 1)
    expect(sent.at(-1)).toMatchObject({ op: 'open', id: 'a', path: '/chat/s-1' })
    // It waits for the gateway's answer again: asked twice in a row, it asks once.
    expect(source.status?.()).toBe('loading')
    expect(source.askAgain()).toBe(false)
    source.dispose()
    expect(source.askAgain()).toBe(false)
  })

  it('hands a tab the stored preferences, but none of the keys that are the XR page\'s own', () => {
    localStorage.setItem('theme', 'dark')
    localStorage.setItem('kirocrew:xr-room', '{"v":1,"pages":[]}')
    localStorage.setItem('kirocrew:xr-anything', '1')
    try {
      const source = new StreamSource('a', '/chat', 1024, 720)
      const prefs = (sent.find(m => m.op === 'open') as { prefs?: Record<string, string> }).prefs ?? {}
      expect(prefs.theme).toBe('dark')
      expect(Object.keys(prefs).filter(k => k.startsWith('kirocrew:xr-'))).toEqual([])
      source.dispose()
    } finally {
      for (const key of ['theme', 'kirocrew:xr-room', 'kirocrew:xr-anything']) localStorage.removeItem(key)
    }
  })

  it('says a line of its own for a browser dialog that the gateway answered with a no, and the old one for a window or an address', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    applyXrPanelFrame({ op: 'refused', id: 'a', why: 'dialog' })
    expect(source.note?.()).toBe('dialog')
    applyXrPanelFrame({ op: 'refused', id: 'a', why: 'window' })
    expect(source.note?.()).toBe('refused')
    applyXrPanelFrame({ op: 'refused', id: 'a', why: 'address' })
    expect(source.note?.()).toBe('refused')
    source.dispose()
  })

  it.each(['host_attach_open', 'host_attach_unknown'])('says that the host is the reason when the gateway does not start its browser there (%s)', why => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why })
    expect(source.note?.()).toBe('host')
    source.dispose()
  })

  it('says a line of its own when the page browser runs for the gateway\'s other local address', () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'failed', why: 'other_address' })
    expect(source.note?.()).toBe('elsewhere')
    // It is not a page for which there was no room: it is not asked for again.
    expect(source.askAgain()).toBe(false)
    source.dispose()
  })

  it('names the gateway\'s own limit in that line', () => {
    const hub = readFileSync('../src/kiro_crew/browser_harness/panel_hub.py', 'utf-8')
    expect(Number(/^MAX_PANELS_PER_SOCKET = (\d+)$/m.exec(hub)?.[1])).toBe(MOST_LIVE_PAGES)
  })

  it('sends what is typed one at a time, in the order it was typed, and joins the letters that wait', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const answers: (() => void)[] = []
    input.mockImplementation(() => new Promise(done => { answers.push(() => done({ ok: true, sent: 1 })) }))
    source.text('h')
    source.text('e')
    source.text('y')
    source.key({ type: 'down', key: 'Enter' })
    source.text('!')
    // One is on its way. The two letters behind it wait as one text; the key and what follows keep their place.
    expect(input.mock.calls).toEqual([['p1', { type: 'text', text: 'h' }]])
    answers[0]()
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(2))
    expect(input.mock.calls[1]).toEqual(['p1', { type: 'text', text: 'ey' }])
    answers[1]()
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(3))
    expect(input.mock.calls[2]).toEqual(['p1', { type: 'key', key: 'Enter' }])
    answers[2]()
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(4))
    expect(input.mock.calls[3]).toEqual(['p1', { type: 'text', text: '!' }])
    answers[3]()
    // After the last one the page is asked once where its focus is now.
    await vi.waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    expect(ask).toHaveBeenCalledWith('p1', 'focus')
    source.dispose()
  })

  it('drops what was typed behind a text that the gateway did not pass on', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const answers: ((answer: unknown) => void)[] = []
    input.mockImplementation(() => new Promise(done => { answers.push(done) }))
    source.text('pass')
    source.key({ type: 'down', key: 'Enter' })
    answers[0]({ ok: true, sent: 0, guarded: true })
    await vi.waitFor(() => expect(source.note?.()).toBe('guarded'))
    await vi.waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    expect(input).toHaveBeenCalledTimes(1)
    source.dispose()
  })

  it('drops what was typed behind a text whose send was cut, so no words reach the page with a gap', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const answers: ((answer: unknown) => void)[] = []
    input.mockImplementation(() => new Promise(done => { answers.push(done) }))
    source.text('hello')
    source.key({ type: 'down', key: 'Enter' })
    source.text('world')
    answers[0]({ ok: true, sent: 0, lost: true })
    await vi.waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    expect(input).toHaveBeenCalledTimes(1)
    source.dispose()
  })

  it('keeps the aim of the last answer after a move whose send was cut, so the next press there is not stale', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    const allow = { rect: [480, 290, 90, 24], kind: 'button', label: 'Allow once', guarded: false }
    input.mockResolvedValue({ ok: true, sent: 1, target: allow })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.target?.()?.rect).toEqual([480, 290, 90, 24]))
    // The hub's answer to a move whose send was cut: no target in it.
    input.mockResolvedValueOnce({ ok: true, sent: 0, lost: true })
    source.pointer({ type: 'move', pointerId: 1, x: 501, y: 300 })
    await vi.waitFor(() => expect(input).toHaveBeenLastCalledWith('p1', { type: 'mouse', action: 'move', x: 501, y: 300 }))
    await vi.waitFor(() => expect(source.target?.()?.rect).toEqual([480, 290, 90, 24]))
    input.mockClear()
    // A press at the lost move's place: no answer is known for it, so it carries none, never "nothing stood here".
    source.pointer({ type: 'down', pointerId: 1, x: 501, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 501, y: 300 })
    await vi.waitFor(() => expect(input).toHaveBeenCalledTimes(1))
    expect(input.mock.calls[0][1]).toMatchObject({ action: 'click', x: 501 })
    expect(input.mock.calls[0][1]).not.toHaveProperty('aimed')
    source.dispose()
  })

  it('knows where the page\'s focus is from a press\'s answer, and asks again when told to', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    expect(source.focus()).toBeNull()
    input.mockResolvedValue({ ok: true, sent: 2, focus: { editable: true, kind: 'text', rect: [10, 600, 800, 40], guarded: false } })
    source.pointer({ type: 'down', pointerId: 1, x: 300, y: 620 })
    source.pointer({ type: 'up', pointerId: 1, x: 300, y: 620 })
    await vi.waitFor(() => expect(source.focus()).toEqual({ editable: true, guarded: false, secret: false }))
    // The same answer again is the same object: a panel that compares it sees no change.
    const first = source.focus()
    ask.mockResolvedValue({ ok: true, answer: { editable: true, kind: 'text', rect: [10, 600, 800, 40], guarded: false } })
    source.refocus()
    await vi.waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    await Promise.resolve()
    expect(source.focus()).toBe(first)
    // The page moved its focus by itself, into a surface that an agent raised.
    ask.mockResolvedValue({ ok: true, answer: { editable: true, kind: 'text', rect: null, guarded: true } })
    source.refocus()
    await vi.waitFor(() => expect(source.focus()).toEqual({ editable: true, guarded: true, secret: false }))
    // The focus is in a password field: the phone's field is a password box for it.
    ask.mockResolvedValue({ ok: true, answer: { editable: true, kind: 'password', rect: [10, 300, 300, 40], guarded: false } })
    source.refocus()
    await vi.waitFor(() => expect(source.focus()).toEqual({ editable: true, guarded: false, secret: true }))
    ask.mockResolvedValue({ ok: true, answer: { editable: true, kind: 'text', rect: null, guarded: true } })
    source.refocus()
    await vi.waitFor(() => expect(source.focus()).toEqual({ editable: true, guarded: true, secret: false }))
    // A press that was not sent says nothing of the focus: what was heard last stands.
    input.mockResolvedValue({ ok: true, sent: 0, guarded: true, target: null })
    source.pointer({ type: 'down', pointerId: 1, x: 300, y: 620 })
    source.pointer({ type: 'up', pointerId: 1, x: 300, y: 620 })
    await vi.waitFor(() => expect(source.note?.()).toBe('guarded'))
    expect(source.focus()).toEqual({ editable: true, guarded: true, secret: false })
    // A page that is not live has no focus to type into.
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'signed-out', why: 'gate_refused' })
    expect(source.focus()).toBeNull()
    source.dispose()
  })

  it('types text into the page, and sends nothing without a socket', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    source.text?.('hello')
    expect(input).toHaveBeenCalledWith('p1', { type: 'text', text: 'hello' })
    source.dispose()
    setXrPanelSender(null)
    expect(sendXrPanel({ op: 'open' })).toBe(false)
  })
})

describe('a live page source whose gateway takes a wheel and a move on the socket', () => {
  it('sends them on the socket, the next one 16 ms later and not after an answer; a press keeps its request', async () => {
    resetPanelInputLink()
    const source = new StreamSource('a', '/chat', 1024, 720)
    live(source)
    await showing(source)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    // The first wheel asks the gateway what it takes, and goes by its request.
    source.scroll({ x: 500, y: 300, dx: 0, dy: 10 })
    expect(ops('hello')).toEqual([{ op: 'hello' }])
    expect(input).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(0)
    applyXrPanelFrame({ op: 'hello', input: true })
    input.mockClear()
    // From here on wheels ride the socket. Three in one frame: the first goes, the others add up behind it.
    source.scroll({ x: 500, y: 300, dx: 0, dy: 40 })
    source.scroll({ x: 500, y: 310, dx: 0, dy: 30 })
    source.scroll({ x: 500, y: 320, dx: 0, dy: 20 })
    expect(ops('input').map(m => m.event)).toEqual([{ type: 'wheel', x: 500, y: 300, dx: 0, dy: 40 }])
    // No answer came, and 16 ms later the sum goes all the same.
    await vi.advanceTimersByTimeAsync(INPUT_GAP_MS)
    expect(ops('input').map(m => m.event)).toEqual([{ type: 'wheel', x: 500, y: 300, dx: 0, dy: 40 }, { type: 'wheel', x: 500, y: 320, dx: 0, dy: 50 }])
    expect(ops('input').every(m => m.handle === 'p1')).toBe(true)
    expect(input).not.toHaveBeenCalled()
    // A press is a request, as before.
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'up', pointerId: 1, x: 500, y: 300 })
    expect(input).toHaveBeenCalledWith('p1', expect.objectContaining({ type: 'mouse', action: 'click' }))
    source.dispose()
    resetPanelInputLink()
  })
})
