import { afterEach, describe, expect, it, vi } from 'vitest'
import { DomSource, addressOf, domPagesWork } from './domSource'
import { staysOut } from './streamPath'

afterEach(() => { vi.useRealTimers() })

describe('a page drawn by the XR tab itself', () => {
  it('is drawn only where the browser has HTML-in-Canvas; this one has not, so the page is streamed', () => {
    expect(domPagesWork()).toBe(false)
  })

  it('keeps the address the page stands at: its path, and the Bots page with its one Bot', () => {
    expect(addressOf({ pathname: '/chat', search: '?x=1' })).toBe('/chat')
    expect(addressOf({ pathname: '/members', search: '?member=Mira' })).toBe('/members?member=Mira')
    expect(addressOf({ pathname: '/members', search: '' })).toBe('/members')
  })

  it('lets no page go to one that stays out of XR, the XR page itself among them', () => {
    for (const out of ['/xr', '/XR/', '/%78r', '/deploy', '/settings/people', '/instances/a']) expect(staysOut(out)).toBe(true)
    for (const fine of ['/chat', '/settings', '/members', '/schedule']) expect(staysOut(fine)).toBe(false)
  })

  it('keeps the browser\'s own dialogs on a desk, and answers them for the page in a session', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const browser = { confirm: vi.fn(() => true), alert: vi.fn(), prompt: vi.fn(() => 'typed') }
    const win = Object.assign(new EventTarget(), browser) as unknown as Window & typeof globalThis
    // The page's own "leave this page?".
    win.addEventListener('beforeunload', ev => ev.preventDefault())
    const leave = () => { const ev = new Event('beforeunload', { cancelable: true }); win.dispatchEvent(ev); return ev.defaultPrevented }
    const room = source as unknown as { dialogs: (doc: Document) => void; inXr: boolean }
    room.dialogs({ defaultView: win } as unknown as Document)
    expect(win.confirm('Delete Mira?')).toBe(true)
    expect(leave()).toBe(true)
    expect(browser.confirm).toHaveBeenCalledWith('Delete Mira?')
    expect(source.note()).toBe('')
    room.inXr = true
    expect(win.confirm('Delete   Mira?')).toBe(false)
    expect(win.prompt('Name?', 'x')).toBeNull()
    win.alert('Done')
    expect(browser.confirm).toHaveBeenCalledTimes(1)
    expect(browser.prompt).not.toHaveBeenCalled()
    expect(browser.alert).not.toHaveBeenCalled()
    expect(source.note()).toBe('dialog')
    expect(source.noteWords()).toContain('Done')
    expect(leave()).toBe(false)
    vi.advanceTimersByTime(6000)
    expect(source.note()).toBe('')
    source.dispose()
  })

  it('opens no browser window for the page, on a desk too: a page of this origin goes to a panel, another says the line', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const own = vi.fn(() => null)
    const win = Object.assign(new EventTarget(), { confirm: vi.fn(), alert: vi.fn(), prompt: vi.fn(), open: own }) as unknown as Window & typeof globalThis
    const room = source as unknown as { dialogs: (doc: Document) => void }
    room.dialogs({ defaultView: win } as unknown as Document)
    const opened: string[] = []
    source.windows(path => { opened.push(path) })
    expect(win.open(`${location.origin}/schedule`, '_blank')).toBeNull()
    expect(win.open(`${location.origin}/members?member=Mira`)).toBeNull()
    expect(opened).toEqual(['/schedule', '/members?member=Mira'])
    expect(source.note()).toBe('')
    // Another origin, a path that stays out, nothing at all: no panel, and the page's line says so for a while.
    for (const url of ['https://example.com/x', `${location.origin}/deploy`, undefined]) win.open(url)
    expect(opened).toHaveLength(2)
    expect(source.note()).toBe('refused')
    expect(source.noteWords()).toBe('')
    expect(own).not.toHaveBeenCalled()
    vi.advanceTimersByTime(6000)
    expect(source.note()).toBe('')
    // At his press on the desk (the page's transient activation), a window of another origin is the browser's own tab.
    const pressed = { isActive: true }
    ;(source as unknown as { downAt: number }).downAt = performance.now()
    ;(source as unknown as { el: unknown }).el = { contentWindow: { location: { href: `${location.origin}/chat` }, navigator: { userActivation: pressed } }, style: {}, remove: () => {}, removeEventListener: () => {} }
    win.open('https://example.com/x', '_blank')
    expect(own).toHaveBeenCalledWith('https://example.com/x', '_blank', undefined)
    expect(source.note()).toBe('')
    // In a session, or with no press of his, it opens nothing and says the line.
    ;(source as unknown as { inXr: boolean }).inXr = true
    win.open('https://example.com/y')
    ;(source as unknown as { inXr: boolean }).inXr = false
    pressed.isActive = false
    win.open('https://example.com/z')
    expect(own).toHaveBeenCalledTimes(1)
    expect(source.note()).toBe('refused')
    // A window of this origin goes to a panel either way.
    pressed.isActive = true
    win.open(`${location.origin}/logs`)
    expect(opened.at(-1)).toBe('/logs')
  })

  it('sends nothing for a press, a scroll, a key or text: the browser hands those to the page', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    expect(() => {
      source.pointer({ type: 'down', pointerId: 1, x: 10, y: 10 })
      source.pointer({ type: 'up', pointerId: 1, x: 10, y: 10 })
      source.scroll({ x: 10, y: 10, dx: 0, dy: 100 })
    }).not.toThrow()
    expect('key' in source).toBe(false)
    expect('text' in source).toBe(false)
    expect('answerPlaces' in source).toBe(false)
    expect(source.readout()).toMatchObject({ way: 'dom', count: 0, bytes: 0, gateway: null })
    expect(source.status()).toBe('loading')
    expect(source.pagePath()).toBe('/chat')
    source.dispose()
    expect(source.status()).toBe('closed')
  })

  it('hears the person\'s own press, notch and key in every document the page loads, and stops none of them', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const el = (source as unknown as { el: HTMLIFrameElement }).el
    // The page's own document, as the browser has it once the page loaded (this test's DOM loads no frame).
    const doc = document.implementation.createHTMLDocument('page')
    Object.defineProperty(el, 'contentDocument', { value: doc })
    const added = vi.spyOn(doc, 'addEventListener')
    const heard: string[] = []
    source.listen(input => { heard.push(input) })
    el.dispatchEvent(new Event('load'))
    const kinds = added.mock.calls.map(([type, , opts]) => `${String(type)}:${JSON.stringify(opts)}`)
    for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'wheel', 'keydown']) expect(kinds).toContain(`${type}:{"capture":true,"passive":true}`)
    // A key that a script of the page made is not the person's.
    const key = new KeyboardEvent('keydown', { key: 'a', cancelable: true })
    doc.dispatchEvent(key)
    expect(heard).toEqual([])
    expect(key.defaultPrevented).toBe(false)
    source.dispose()
  })

  it('copies a page out of view not at all, and once it is back, the paint it missed, before that frame draws', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const el = (source as unknown as { el: HTMLIFrameElement }).el
    const canvas = Object.assign(document.createElement('canvas'), { getElementImageDefaultSize: () => ({ width: 2048, height: 1440 }) })
    const copy = vi.fn()
    const gl = { TEXTURE_2D: 1, SRGB8_ALPHA8: 2, RGBA: 3, UNSIGNED_BYTE: 4, TEXTURE_MAG_FILTER: 5, TEXTURE_MIN_FILTER: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, LINEAR: 9, CLAMP_TO_EDGE: 10, UNPACK_FLIP_Y_WEBGL: 11, createTexture: () => ({}), texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), texElementSubImage2D: copy }
    const renderer = { domElement: canvas, xr: { isPresenting: false }, getContext: () => gl, state: { bindTexture: vi.fn() } } as unknown as Parameters<DomSource['beforeDraw']>[0]
    const paint = () => { const ev = new Event('paint'); Object.defineProperty(ev, 'changedElements', { value: [el] }); canvas.dispatchEvent(ev) }
    source.beforeDraw(renderer)
    paint()
    source.visible(false)
    source.beforeDraw(renderer)
    source.beforeDraw(renderer)
    expect(copy).not.toHaveBeenCalled()
    source.visible(true)
    source.beforeDraw(renderer)
    expect(copy).toHaveBeenCalledTimes(1)
    source.beforeDraw(renderer)
    expect(copy).toHaveBeenCalledTimes(1)
    source.dispose()
  })

  it('copies at most one page a room frame: three in view, the middle one moving and looked at, gets every frame', () => {
    vi.useFakeTimers()
    const canvas = Object.assign(document.createElement('canvas'), { getElementImageDefaultSize: () => ({ width: 2048, height: 1440 }) })
    const copied: Element[] = []
    const gl = { TEXTURE_2D: 1, SRGB8_ALPHA8: 2, RGBA: 3, UNSIGNED_BYTE: 4, TEXTURE_MAG_FILTER: 5, TEXTURE_MIN_FILTER: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, LINEAR: 9, CLAMP_TO_EDGE: 10, UNPACK_FLIP_Y_WEBGL: 11, createTexture: () => ({}), texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), texElementSubImage2D: (_t: number, _l: number, _x: number, _y: number, el: Element) => { copied.push(el) } }
    const info = { render: { frame: 0 } }
    const renderer = { domElement: canvas, xr: { isPresenting: false }, getContext: () => gl, state: { bindTexture: vi.fn() }, info } as unknown as Parameters<DomSource['beforeDraw']>[0]
    const pages = ['left', 'mid', 'right'].map(id => new DomSource(id, '/chat', 1024, 720))
    const els = pages.map(p => (p as unknown as { el: HTMLIFrameElement }).el)
    pages.forEach((p, i) => p.gaze([28, 1, 28][i]))
    const paint = (list: Element[]) => { const ev = new Event('paint'); Object.defineProperty(ev, 'changedElements', { value: list }); canvas.dispatchEvent(ev) }
    const frame = () => { for (const p of pages) p.beforeDraw(renderer); info.render.frame += 1 }
    frame()
    // All three painted once; then only the middle one, at every frame.
    paint(els)
    const perFrame: number[] = []
    for (let f = 0; f < 30; f += 1) {
      const before = copied.length
      frame()
      perFrame.push(copied.length - before)
      // The middle page changes its own document (an answer streaming in), then the browser paints it.
      ;(pages[1] as unknown as { onTouch: () => void }).onTouch()
      paint([els[1]])
    }
    expect(Math.max(...perFrame)).toBe(1)
    // The middle one went first, and has every frame after the two still pages had their one copy each.
    expect(copied[0]).toBe(els[1])
    expect(copied.filter(e => e === els[0]).length).toBe(1)
    expect(copied.filter(e => e === els[2]).length).toBe(1)
    expect(copied.filter(e => e === els[1]).length).toBe(28)
    for (const p of pages) p.dispose()
  })

  it('marks a page changed by its own paint only: the paint that our copy causes marks nothing', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const el = (source as unknown as { el: HTMLIFrameElement }).el
    const canvas = Object.assign(document.createElement('canvas'), { getElementImageDefaultSize: () => ({ width: 2048, height: 1440 }) })
    const copy = vi.fn()
    const gl = { TEXTURE_2D: 1, SRGB8_ALPHA8: 2, RGBA: 3, UNSIGNED_BYTE: 4, TEXTURE_MAG_FILTER: 5, TEXTURE_MIN_FILTER: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, LINEAR: 9, CLAMP_TO_EDGE: 10, UNPACK_FLIP_Y_WEBGL: 11, createTexture: () => ({}), texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), texElementSubImage2D: copy }
    const renderer = { domElement: canvas, xr: { isPresenting: false }, getContext: () => gl, state: { bindTexture: vi.fn() } } as unknown as Parameters<DomSource['beforeDraw']>[0]
    const paint = () => { const ev = new Event('paint'); Object.defineProperty(ev, 'changedElements', { value: [el] }); canvas.dispatchEvent(ev) }
    // He looks at another page: this one is held to the 250 ms rule.
    const other = new DomSource('o', '/crons', 1024, 720)
    other.gaze(1)
    source.gaze(30)
    other.beforeDraw(renderer)
    source.beforeDraw(renderer)
    paint()
    source.beforeDraw(renderer)
    expect(copy).toHaveBeenCalledTimes(1)
    // The browser names the page again after our copy; nothing in it changed: no second copy, frame after frame.
    for (let f = 0; f < 5; f += 1) { vi.advanceTimersByTime(16); paint(); source.beforeDraw(renderer) }
    expect(copy).toHaveBeenCalledTimes(1)
    // The page itself changes (its document, a scroll, an input), then paints: it is copied at once.
    ;(source as unknown as { onTouch: () => void }).onTouch()
    paint()
    source.beforeDraw(renderer)
    expect(copy).toHaveBeenCalledTimes(2)
    // A page that keeps painting with no change in its document (a CSS loop) is still copied, four times a second.
    for (let f = 0; f < 60; f += 1) { vi.advanceTimersByTime(16.7); paint(); source.beforeDraw(renderer) }
    expect(copy.mock.calls.length - 2).toBeGreaterThanOrEqual(3)
    expect(copy.mock.calls.length - 2).toBeLessThanOrEqual(4)
    other.dispose()
    source.dispose()
  })

  it('marks the page he looks at at once, and a dropped paint of another page 250 ms after its last copy', () => {
    vi.useFakeTimers()
    const canvas = Object.assign(document.createElement('canvas'), { getElementImageDefaultSize: () => ({ width: 2048, height: 1440 }) })
    const copied: Element[] = []
    const gl = { TEXTURE_2D: 1, SRGB8_ALPHA8: 2, RGBA: 3, UNSIGNED_BYTE: 4, TEXTURE_MAG_FILTER: 5, TEXTURE_MIN_FILTER: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, LINEAR: 9, CLAMP_TO_EDGE: 10, UNPACK_FLIP_Y_WEBGL: 11, createTexture: () => ({}), texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), texElementSubImage2D: (_t: number, _l: number, _x: number, _y: number, el: Element) => { copied.push(el) } }
    const info = { render: { frame: 0 } }
    const renderer = { domElement: canvas, xr: { isPresenting: false }, getContext: () => gl, state: { bindTexture: vi.fn() }, info } as unknown as Parameters<DomSource['beforeDraw']>[0]
    const [watched, aside] = [new DomSource('w', '/chat', 1024, 720), new DomSource('a', '/crons', 1024, 720)]
    const [we, ae] = [watched, aside].map(p => (p as unknown as { el: HTMLIFrameElement }).el)
    watched.gaze(1)
    aside.gaze(30)
    const paint = (list: Element[]) => { const ev = new Event('paint'); Object.defineProperty(ev, 'changedElements', { value: list }); canvas.dispatchEvent(ev) }
    const frame = () => { watched.beforeDraw(renderer); aside.beforeDraw(renderer); info.render.frame += 1; vi.advanceTimersByTime(16.7) }
    frame()
    paint([we, ae]); frame(); frame()
    expect(copied.filter(e => e === we).length).toBe(1)
    expect(copied.filter(e => e === ae).length).toBe(1)
    // Both keep painting with no change in their documents (a spinner): the watched page is copied at every paint it
    // gets the frame for; the other is held to the 250 ms rule.
    for (let f = 0; f < 30; f += 1) { paint([we, ae]); frame() }
    expect(copied.filter(e => e === we).length).toBeGreaterThan(12)
    expect(copied.filter(e => e === ae).length).toBeLessThanOrEqual(4)
    // A paint of the other page that was dropped, with no paint after it: it is copied once 250 ms are over.
    const before = copied.filter(e => e === ae).length
    paint([ae])
    for (let f = 0; f < 20; f += 1) frame()
    expect(copied.filter(e => e === ae).length).toBe(before + 1)
    watched.dispose(); aside.dispose()
  })

  it('lets the page that took his input go first in the next frame, and a page that is not drawn holds no pick', () => {
    vi.useFakeTimers()
    const canvas = Object.assign(document.createElement('canvas'), { getElementImageDefaultSize: () => ({ width: 2048, height: 1440 }) })
    const copied: Element[] = []
    const gl = { TEXTURE_2D: 1, SRGB8_ALPHA8: 2, RGBA: 3, UNSIGNED_BYTE: 4, TEXTURE_MAG_FILTER: 5, TEXTURE_MIN_FILTER: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, LINEAR: 9, CLAMP_TO_EDGE: 10, UNPACK_FLIP_Y_WEBGL: 11, createTexture: () => ({}), texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), texElementSubImage2D: (_t: number, _l: number, _x: number, _y: number, el: Element) => { copied.push(el) } }
    const info = { render: { frame: 0 } }
    const renderer = { domElement: canvas, xr: { isPresenting: false }, getContext: () => gl, state: { bindTexture: vi.fn() }, info } as unknown as Parameters<DomSource['beforeDraw']>[0]
    const pages = ['gaze', 'typed', 'ghost'].map(id => new DomSource(id, '/chat', 1024, 720))
    const els = pages.map(p => (p as unknown as { el: HTMLIFrameElement }).el)
    pages.forEach((p, i) => p.gaze([1, 30, 0.5][i]))
    const paint = (list: Element[]) => { const ev = new Event('paint'); Object.defineProperty(ev, 'changedElements', { value: list }); canvas.dispatchEvent(ev) }
    const touch = (p: DomSource) => (p as unknown as { onTouch: () => void }).onTouch()
    const frame = (drawn = pages) => { for (const p of drawn) p.beforeDraw(renderer); info.render.frame += 1; vi.advanceTimersByTime(16.7) }
    frame()
    // The ghost page stops being drawn (its panel holds it, no draw) but keeps its mark and its place nearest his look.
    paint(els); frame([pages[0], pages[1]]); frame([pages[0], pages[1]])
    const drawnTwo = [pages[0], pages[1]]
    // The looked-at page moves at every frame; he types into the other page.
    for (let f = 0; f < 20; f += 1) { touch(pages[0]); paint([els[0]]); frame(drawnTwo) }
    ;(pages[1] as unknown as { onInput: () => void }).onInput()
    paint([els[1]])
    const n = copied.length
    frame(drawnTwo)
    expect(copied[n]).toBe(els[1])
    // The ghost never cut the others to a copy in many frames: the looked-at page had nearly every frame.
    expect(copied.filter(e => e === els[0]).length).toBeGreaterThanOrEqual(19)
    // It is not drawn after the first frame, so it is copied not at all, and takes no frame from the others.
    expect(copied.filter(e => e === els[2]).length).toBe(0)
    for (const p of pages) p.dispose()
  })

  it('lays the page out at the panel\'s size when the panel is sized by hand', () => {
    vi.useFakeTimers()
    const source = new DomSource('d', '/chat', 1024, 720)
    const told = vi.fn()
    source.subscribe(told)
    source.resize(800, 600)
    expect(source.frame()).toMatchObject({ cssWidth: 800, cssHeight: 600 })
    expect(told).toHaveBeenCalledTimes(1)
    source.resize(800, 600)
    expect(told).toHaveBeenCalledTimes(1)
    source.dispose()
  })
})
