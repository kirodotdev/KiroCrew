import { describe, it, expect, afterEach, onTestFinished, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { decorClipPath, useThemeDecorKeepClear } from './themeDecorKeepClear'
import { GUIDE_KEEP_CLEAR_ATTR } from '../guide/trustRoot'

function rect(top: number, left: number, bottom: number, right: number): DOMRect {
  return { top, left, bottom, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) } as DOMRect
}

describe('decorClipPath', () => {
  it('cuts each hole out of the viewport as one even-odd polygon', () => {
    expect(decorClipPath([{ top: 400, left: 50.4, bottom: 590.2, right: 1600 }], 1634, 608)).toBe(
      'polygon(evenodd, 0px 0px, 1634px 0px, 1634px 608px, 0px 608px, 0px 0px, '
      + '50px 400px, 1600px 400px, 1600px 590px, 50px 590px, 50px 400px, 0px 0px)',
    )
  })

  it('gives two boxes touching at a fractional edge one shared edge, so no strip is painted back', () => {
    const clip = decorClipPath([
      { top: 400, left: 50, bottom: 520.6, right: 1000 },
      { top: 520.6, left: 50, bottom: 560, right: 1000 },
    ], 1634, 608)!
    expect(clip).toContain('50px 400px, 1000px 400px, 1000px 521px, 50px 521px')
    expect(clip).toContain('50px 521px, 1000px 521px, 1000px 560px, 50px 560px')
  })

  it('clamps a hole to the viewport and drops an empty one', () => {
    expect(decorClipPath([{ top: 500, left: -10, bottom: 900, right: 300 }], 400, 600)).toContain('0px 500px, 300px 500px, 300px 600px')
    expect(decorClipPath([{ top: 10, left: 10, bottom: 10, right: 50 }], 400, 600)).toBeUndefined()
    expect(decorClipPath([], 400, 600)).toBeUndefined()
  })
})

function Probe({ slot }: { slot: HTMLElement }) {
  useThemeDecorKeepClear(slot)
  return null
}

describe('useThemeDecorKeepClear', () => {
  afterEach(() => { document.body.replaceChildren() })

  it('clips the composer out of the slot while the slot holds a decoration', async () => {
    const slot = document.createElement('div')
    const composer = document.createElement('div')
    composer.setAttribute(GUIDE_KEEP_CLEAR_ATTR, '')
    composer.getBoundingClientRect = () => rect(400, 50, 590, 1000)
    document.body.append(slot, composer)
    render(<Probe slot={slot} />)
    // An empty slot is left alone.
    expect(slot.style.clipPath).toBe('')

    const overlay = document.createElement('div')
    slot.appendChild(overlay)
    await waitFor(() => expect(slot.style.clipPath).toContain('50px 400px, 1000px 400px, 1000px 590px'))

    // The composer leaving takes its hole with it.
    composer.remove()
    await waitFor(() => expect(slot.style.clipPath).toBe(''))

    // An emptied slot drops the clip.
    document.body.appendChild(composer)
    await waitFor(() => expect(slot.style.clipPath).not.toBe(''))
    overlay.remove()
    await waitFor(() => expect(slot.style.clipPath).toBe(''))
  })

  it('observes each marked box once, so an idle page stops measuring', async () => {
    // A browser ResizeObserver reports a box's size as soon as it is observed.
    const observeCalls: Element[] = []
    class ReportingObserver {
      constructor(private cb: ResizeObserverCallback) {}
      observe(el: Element) {
        observeCalls.push(el)
        this.cb([], this as unknown as ResizeObserver)
      }
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', ReportingObserver)
    // Frames run only when the test runs them, so "nothing changed" is a state, not a wait.
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    onTestFinished(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

    const slot = document.createElement('div')
    slot.appendChild(document.createElement('div'))
    const composer = document.createElement('div')
    composer.setAttribute(GUIDE_KEEP_CLEAR_ATTR, '')
    composer.getBoundingClientRect = () => rect(400, 50, 590, 1000)
    document.body.append(slot, composer)
    const { unmount } = render(<Probe slot={slot} />)
    // Run up to ten frames; an idle page must stop asking for more well before that.
    let ran = 0
    while (frames.length > 0 && ran < 10) { frames.shift()!(0); ran += 1 }
    expect(slot.style.clipPath).toContain('50px 400px, 1000px 400px, 1000px 590px')
    expect(frames).toHaveLength(0)
    expect(ran).toBeLessThanOrEqual(2)
    // The composer, once; its parent is <body>, which is never observed.
    expect(observeCalls).toEqual([composer])
    unmount()
  })

  it('re-measures when a container resizes, so a composer that moves at the same size is followed', () => {
    const observers: Array<{ cb: ResizeObserverCallback, targets: Set<Element> }> = []
    class RecordingObserver {
      targets = new Set<Element>()
      constructor(cb: ResizeObserverCallback) { observers.push({ cb, targets: this.targets }) }
      observe(el: Element) { this.targets.add(el) }
      unobserve(el: Element) { this.targets.delete(el) }
      disconnect() { this.targets.clear() }
    }
    vi.stubGlobal('ResizeObserver', RecordingObserver)
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    onTestFinished(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

    const slot = document.createElement('div')
    slot.appendChild(document.createElement('div'))
    // main > dock > composer: a taller bottom terminal shrinks `main` and
    // moves the composer up without resizing it.
    const main = document.createElement('main')
    const dock = document.createElement('div')
    const composer = document.createElement('div')
    composer.setAttribute(GUIDE_KEEP_CLEAR_ATTR, '')
    let top = 400
    composer.getBoundingClientRect = () => rect(top, 50, top + 190, 1000)
    dock.appendChild(composer)
    main.appendChild(dock)
    document.body.append(slot, main)
    const { unmount } = render(<Probe slot={slot} />)
    while (frames.length > 0) frames.shift()!(0)
    expect(slot.style.clipPath).toContain('50px 400px, 1000px 400px')
    const [{ cb, targets }] = observers
    expect([...targets]).toEqual(expect.arrayContaining([composer, dock, main]))
    expect(targets.has(document.body)).toBe(false)

    top = 250
    cb([{ target: main } as unknown as ResizeObserverEntry], {} as ResizeObserver)
    while (frames.length > 0) frames.shift()!(0)
    expect(slot.style.clipPath).toContain('50px 250px, 1000px 250px')
    unmount()
  })

  it('does not re-measure while a reply streams, only when a marked box comes or goes', async () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    onTestFinished(() => { vi.restoreAllMocks() })
    const settle = () => { while (frames.length > 0) frames.shift()!(0) }

    const slot = document.createElement('div')
    slot.appendChild(document.createElement('div'))
    const composer = document.createElement('div')
    composer.setAttribute(GUIDE_KEEP_CLEAR_ATTR, '')
    composer.getBoundingClientRect = () => rect(400, 50, 590, 1000)
    const transcript = document.createElement('div')
    const reply = document.createElement('p')
    reply.textContent = 'The'
    transcript.appendChild(reply)
    document.body.append(slot, transcript, composer)
    const { unmount } = render(<Probe slot={slot} />)
    settle()
    expect(slot.style.clipPath).not.toBe('')

    // Mutation observers are told in the order they were created, so this one
    // hears a batch only after the hook's observer has.
    let delivered: () => void = () => {}
    const probe = new MutationObserver(() => delivered())
    probe.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true })
    onTestFinished(() => { probe.disconnect() })
    const nextBatch = () => new Promise<void>(resolve => { delivered = resolve })

    // A streaming reply: text grows, new paragraphs and code rows arrive.
    let batch = nextBatch()
    reply.firstChild!.textContent = 'The theme character'
    reply.append(' sits on the composer')
    const block = document.createElement('pre')
    block.appendChild(document.createElement('code')).textContent = 'npm run build'
    transcript.appendChild(block)
    reply.setAttribute('data-streaming', 'true')
    await batch
    expect(frames).toHaveLength(0)

    // A notice row marked keep-clear appearing does need a read.
    batch = nextBatch()
    const notice = document.createElement('div')
    notice.setAttribute(GUIDE_KEEP_CLEAR_ATTR, '')
    document.body.appendChild(notice)
    await batch
    expect(frames).toHaveLength(1)
    unmount()
  })
})
