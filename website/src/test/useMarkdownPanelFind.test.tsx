/**
 * The preview find over fenced code blocks, driven through `useMarkdownPanelFind`
 * against a DOM the test builds.
 *
 * Pierre draws a code block into a shadow root a `TreeWalker` cannot enter, and
 * it does so late: `WarmSwap` shows a plain copy of the text, parks the
 * highlighted copy beside it in an `aria-hidden` box, and swaps them when the
 * highlight has painted. A find that ran on the plain copy then holds ranges on
 * nodes that are gone. These tests build both states by hand (see `pierreDom`)
 * and check what the find counts, what it paints, and that it finds again, on the
 * same match, once the swap lands.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the hook is imported,
 * because it captures both into module-level constants at load time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { installHighlightApiStub, paintedText, registeredRanges } from './highlightApiStub'
import { codeBlock, page, para, parked, plainCopy } from './pierreDom'
import { stubManualFrames } from './manualFrames'

installHighlightApiStub()
const { useMarkdownPanelFind } = await import('../components/useMarkdownPanelFind')

let frames: ReturnType<typeof stubManualFrames>
beforeEach(() => { frames = stubManualFrames() })
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.replaceChildren()
})

/** The side-panel preview and the full-screen one, each a root the find can search. */
function mount({ fullscreen = false } = {}) {
  const preview = page()
  const full = page()
  const previewRef = { current: preview } as RefObject<HTMLDivElement>
  const fullscreenPreviewRef = { current: full } as RefObject<HTMLDivElement>
  const hook = renderHook(({ full: isFull }) => useMarkdownPanelFind({
    active: true, editing: false, diffMode: false, isMarkdown: true, fullscreen: isFull, content: '', previewRef, fullscreenPreviewRef,
  }), { initialProps: { full: fullscreen } })
  return { preview, full, previewRef, ...hook }
}
type Mounted = ReturnType<typeof mount>

/** Cmd+F, then the term. The frame the chord asks for (to focus the input) is run here, so none is left waiting. */
function find({ result }: Mounted, term: string) {
  act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true })) })
  act(() => frames.runFrame())
  act(() => result.current.setFindTerm(term))
}

/** A block `WarmSwap` is holding: a plain copy on screen, the highlighted copy parked beside it. */
function held(root: HTMLElement, plain: string, lines: string[][]) {
  const host = codeBlock(lines)
  const box = parked(host)
  const copy = plainCopy(plain)
  root.append(copy, box)
  return { host, box, copy }
}

/** The swap: the plain copy goes, the box is shown, and the frame the find asked for runs. */
async function swap({ box, copy }: { box: HTMLElement; copy: HTMLElement }) {
  box.removeAttribute('aria-hidden')
  copy.remove()
  await Promise.resolve() // happy-dom hands over a mutation's records on a microtask the mutation queued
  act(() => frames.runFrame())
}

/** The source line of the code block a range sits in. */
const lineOf = (range: Range) => (range.startContainer.parentElement!.closest('[data-line]') as HTMLElement).dataset.line

describe('finding in a code block', () => {
  it('finds a phrase that crosses the tokens of a line, and paints exactly that text', () => {
    const m = mount()
    m.preview.append(para('Intro'), codeBlock([['mycli', ' rm', ' NAME', ' --force']]))
    find(m, 'mycli rm NAME')
    expect(m.result.current.findCount).toBe(1)
    expect(paintedText('mc-find-current')).toEqual(['mycli rm NAME'])
  })

  it('reads the prose and the code in document order, and steps through both', () => {
    const m = mount()
    m.preview.append(para('echo first'), codeBlock([['echo', ' one'], ['echo', ' two']]), para('echo last'))
    find(m, 'echo')
    expect(m.result.current.findCount).toBe(4)
    const places: Array<ShadowRoot | Document | null> = []
    for (let i = 0; i < 4; i++) {
      places.push(registeredRanges('mc-find-current')[0].startContainer.getRootNode() as ShadowRoot | Document)
      act(() => m.result.current.stepFind(1))
    }
    const shadow = m.preview.querySelector('diffs-container')!.shadowRoot
    expect(places).toEqual([document, shadow, shadow, document])
  })

  it('does not search the line numbers beside the code', () => {
    const m = mount()
    m.preview.append(para('no numerals here'), codeBlock([['a'], ['b'], ['c']]))
    find(m, '2')
    expect(m.result.current.findCount).toBe(0)
  })

  it('reads the text on screen once while the highlighted copy is parked beside it', () => {
    const m = mount()
    held(m.preview, 'echo one\necho two', [['echo', ' one'], ['echo', ' two']])
    find(m, 'echo')
    expect(m.result.current.findCount).toBe(2)
    expect(registeredRanges('mc-find-current')[0].startContainer.getRootNode()).toBe(document)
  })
})

describe('finding again once the swap lands', () => {
  it('stays on the same match and paints it on the highlighted copy', async () => {
    const m = mount()
    const block = held(m.preview, 'echo one\necho two', [['echo', ' one'], ['echo', ' two']])
    find(m, 'echo')
    act(() => m.result.current.stepFind(1))
    expect(m.result.current.findIdx).toBe(1)
    await swap(block)
    expect(m.result.current.findCount).toBe(2)
    expect(m.result.current.findIdx).toBe(1)
    const [current] = registeredRanges('mc-find-current')
    expect(current.toString()).toBe('echo')
    expect(current.startContainer.getRootNode()).toBe(block.host.shadowRoot)
    expect(lineOf(current)).toBe('2')
    expect(registeredRanges('mc-find').map(lineOf)).toEqual(['1'])
  })

  it('keeps a match that is left and no more, when the highlighted copy has fewer', async () => {
    const m = mount()
    const block = held(m.preview, 'echo one\necho two\necho three', [['echo', ' one'], ['echo', ' two']])
    find(m, 'echo')
    act(() => m.result.current.stepFind(1))
    act(() => m.result.current.stepFind(1))
    expect(m.result.current.findIdx).toBe(2)
    await swap(block)
    expect(m.result.current.findCount).toBe(2)
    expect(m.result.current.findIdx).toBe(1)
    expect(lineOf(registeredRanges('mc-find-current')[0])).toBe('2')
  })

  it('paints nothing, and says no results, when the highlighted copy has none', async () => {
    const m = mount()
    const block = held(m.preview, 'echo one', [['other']])
    find(m, 'echo')
    expect(m.result.current.findCount).toBe(1)
    await swap(block)
    expect(m.result.current.findCount).toBe(0)
    expect(m.result.current.findIdx).toBe(0)
    expect(registeredRanges('mc-find-current')).toEqual([])
  })

  it('keeps the case setting through the swap', async () => {
    const m = mount()
    const block = held(m.preview, 'Echo echo', [['Echo', ' echo']])
    find(m, 'echo')
    act(() => m.result.current.setFindCase(true))
    expect(m.result.current.findCount).toBe(1)
    await swap(block)
    expect(m.result.current.findCount).toBe(1)
  })

  it('does not scroll the reader back to the match', async () => {
    const scroll = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    const m = mount()
    const block = held(m.preview, 'echo one\necho two', [['echo', ' one'], ['echo', ' two']])
    find(m, 'echo')
    expect(scroll).toHaveBeenCalledTimes(1)
    await swap(block)
    expect(scroll).toHaveBeenCalledTimes(1)
  })

  it('starts over at the first match for a new term', () => {
    const m = mount()
    m.preview.append(codeBlock([['echo', ' one'], ['echo', ' two']]))
    find(m, 'echo')
    act(() => m.result.current.stepFind(1))
    expect(m.result.current.findIdx).toBe(1)
    act(() => m.result.current.setFindTerm('ech'))
    expect(m.result.current.findIdx).toBe(0)
    expect(lineOf(registeredRanges('mc-find-current')[0])).toBe('1')
  })

  it('looks at the full-screen preview while full screen, not the side panel’s', async () => {
    const m = mount({ fullscreen: true })
    const block = held(m.full, 'echo one\necho two', [['echo', ' one'], ['echo', ' two']])
    m.preview.append(para('echo in the panel behind'))
    find(m, 'echo')
    expect(m.result.current.findCount).toBe(2)
    await swap(block)
    expect(m.result.current.findCount).toBe(2)
    expect(registeredRanges('mc-find-current')[0].startContainer.getRootNode()).toBe(block.host.shadowRoot)
  })
})

describe('watching the preview', () => {
  it('stops when the find closes', async () => {
    const m = mount()
    const block = held(m.preview, 'echo one', [['echo', ' one']])
    find(m, 'echo')
    act(() => m.result.current.closeFind())
    await swap(block)
    expect(frames.pending.size).toBe(0)
    expect(registeredRanges('mc-find')).toEqual([])
    expect(registeredRanges('mc-find-current')).toEqual([])
  })

  /** Every observer the hook (or the module under it) creates from here on. */
  function observersCreated() {
    const created: unknown[] = []
    const RealObserver = globalThis.MutationObserver
    vi.stubGlobal('MutationObserver', class extends RealObserver {
      constructor(callback: MutationCallback) { super(callback); created.push(this) }
    })
    return created
  }

  it('starts only once an open find has a term to look for', () => {
    const watching = observersCreated()
    const m = mount()
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true })) })
    expect(m.result.current.findOpen).toBe(true)
    expect(watching).toHaveLength(0)
    act(() => m.result.current.setFindTerm('echo'))
    expect(watching.length).toBeGreaterThan(0)
  })

  it('does not watch a find that is closed, whatever term it holds', () => {
    const watching = observersCreated()
    const m = mount()
    act(() => m.result.current.setFindTerm('echo'))
    expect(m.result.current.findOpen).toBe(false)
    expect(watching).toHaveLength(0)
  })

  it('does nothing, and does not throw, with no preview mounted', () => {
    const m = mount()
    m.previewRef.current = null
    find(m, 'echo')
    expect(m.result.current.findCount).toBe(0)
  })
})
