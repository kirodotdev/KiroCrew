import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { codeShadowRoots, matchRanges, searchRuns, shadowSelectionRange, watchText } from './pierreShadow'
import { codeBlock, page, para, parked, plainCopy } from '../test/pierreDom'
import { stubManualFrames } from '../test/manualFrames'

const asText = (runs: Text[][]) => runs.map(run => run.map(node => node.data).join(''))
const lines = (root: Element) => asText(searchRuns(root))

describe('codeShadowRoots', () => {
  it('lists the open roots in document order and skips a host with no root', () => {
    const first = codeBlock([['a']])
    const bare = document.createElement('diffs-container')
    const second = codeBlock([['b']])
    const root = page(first, bare, para('between'), second)
    expect(codeShadowRoots(root)).toEqual([first.shadowRoot, second.shadowRoot])
  })
})

describe('a block parked beside its plain copy', () => {
  it('is not searched, so the text on screen is read once', () => {
    const root = page(plainCopy('echo one'), parked(codeBlock([['echo', ' one']])))
    expect(lines(root)).toEqual(['echo one'])
  })

  it('is searched, from its shadow root, once the swap shows it', () => {
    const host = codeBlock([['echo', ' one']])
    const box = parked(host)
    const copy = plainCopy('echo one')
    const root = page(copy, box)
    box.removeAttribute('aria-hidden')
    copy.remove()
    expect(lines(root)).toEqual(['echo one'])
    expect(searchRuns(root)[0][0].getRootNode()).toBe(host.shadowRoot)
  })

  it('is parked when the host itself is the aria-hidden element', () => {
    const host = codeBlock([['hidden']])
    host.setAttribute('aria-hidden', 'true')
    const root = page(host)
    expect(lines(root)).toEqual([])
    expect(codeShadowRoots(root)).toEqual([])
  })

  it('is shown under any aria-hidden value but true', () => {
    const box = parked(codeBlock([['shown']]))
    box.setAttribute('aria-hidden', 'false')
    expect(lines(page(box))).toEqual(['shown'])
  })

  it('is judged below the root only, so an app hidden behind a dialog is still searched', () => {
    const behindDialog = document.createElement('div')
    behindDialog.setAttribute('aria-hidden', 'true')
    const root = document.createElement('div')
    root.append(codeBlock([['still here']]))
    behindDialog.append(root)
    document.body.append(behindDialog)
    expect(lines(root)).toEqual(['still here'])
    expect(codeShadowRoots(root)).toHaveLength(1)
  })
})

describe('searchRuns', () => {
  it('keeps the light DOM node by node and a code line as one run, in document order', () => {
    const root = page(
      para('Intro'),
      codeBlock([['mycli', ' rm', ' NAME'], ['echo', ' ', 'hi']]),
      document.createTextNode('\n   '),
      para('Outro'),
    )
    expect(lines(root)).toEqual(['Intro', 'mycli rm NAME', 'echo hi', 'Outro'])
  })

  it('never reads the gutter numbers or the style sheet inside the root', () => {
    const root = page(codeBlock([['one'], ['two'], ['three']]))
    const text = lines(root).join('|')
    expect(text).toBe('one|two|three')
    expect(text).not.toContain('color')
  })

  it('keeps the spaces between tokens, which are text nodes of their own', () => {
    const root = page(codeBlock([['a', ' ', 'b']]))
    expect(searchRuns(root)[0].map(node => node.data)).toEqual(['a', ' ', 'b'])
  })

  it('finds a block that is not the root’s direct child', () => {
    const wrapper = document.createElement('section')
    wrapper.append(codeBlock([['deep']]))
    expect(lines(page(para('x'), wrapper))).toEqual(['x', 'deep'])
  })

  it('returns nothing for a root with no text', () => {
    expect(searchRuns(page(document.createElement('div')))).toEqual([])
  })
})

describe('matchRanges', () => {
  const find = (root: Element, term: string, flags = 'g') => matchRanges(searchRuns(root), new RegExp(term, flags))

  it('matches a phrase that crosses the tokens of a highlighted line', () => {
    const [match, ...rest] = find(page(codeBlock([['mycli', ' rm', ' NAME', ' --force']])), 'mycli rm NAME')
    expect(rest).toEqual([])
    expect(match.toString()).toBe('mycli rm NAME')
    expect([(match.startContainer as Text).data, match.startOffset]).toEqual(['mycli', 0])
    expect([(match.endContainer as Text).data, match.endOffset]).toEqual([' NAME', 5])
  })

  it('starts a match on the node it begins in and ends it on the node it finishes in', () => {
    const [match] = find(page(codeBlock([['mycli', ' rm', ' NAME']])), 'rm')
    expect([(match.startContainer as Text).data, match.startOffset]).toEqual([' rm', 1])
    const [first] = find(page(codeBlock([['mycli', ' rm']])), 'mycli')
    expect([(first.endContainer as Text).data, first.endOffset]).toEqual(['mycli', 5]) // not offset 0 of the next token
  })

  it('does not let a match run from one line into the next', () => {
    const root = page(codeBlock([['mycli', ' rm'], ['NAME']]))
    expect(find(root, 'rmNAME')).toEqual([])
    expect(find(root, 'rm NAME')).toEqual([])
  })

  it('finds every occurrence, in document order, across the light DOM and the code', () => {
    const root = page(para('use foo here'), codeBlock([['foo', ' foo'], ['bar']]), para('and foo again'))
    const found = find(root, 'foo')
    expect(found.map(r => r.toString())).toEqual(['foo', 'foo', 'foo', 'foo'])
    expect(found.map(r => (r.startContainer as Text).data)).toEqual(['use foo here', 'foo', ' foo', 'and foo again'])
  })

  it('honours the case flag it is handed', () => {
    const root = page(codeBlock([['Echo', ' ', 'ECHO']]))
    expect(find(root, 'echo', 'gi').map(r => r.toString())).toEqual(['Echo', 'ECHO'])
    expect(find(root, 'echo', 'g')).toEqual([])
  })

  it('restarts the scan for every run, whatever the last one left in lastIndex', () => {
    const root = page(para('xx needle'), para('needle yy'))
    expect(find(root, 'needle').map(r => (r.startContainer as Text).data)).toEqual(['xx needle', 'needle yy'])
  })

  it('starts a match exactly on a token boundary in the token it begins', () => {
    const [match] = find(page(codeBlock([['mycli', 'rm', 'NAME']])), 'rm')
    expect([(match.startContainer as Text).data, match.startOffset]).toEqual(['rm', 0]) // not the end of the token before
  })
})

/** A selection that answers only `getComposedRanges`, as Chrome does for text inside a shadow root. */
function fakeSelection(composed: Array<{ startContainer: Node; startOffset: number; endContainer: Node; endOffset: number }>) {
  const getComposedRanges = vi.fn(() => composed)
  return { sel: { getComposedRanges } as unknown as Selection, getComposedRanges }
}

describe('shadowSelectionRange', () => {
  function tokens(host: HTMLElement) {
    return [...host.shadowRoot!.querySelectorAll('[data-line] span')].map(span => span.firstChild as Text)
  }

  it('builds the live range a selection inside a code block covers', () => {
    const host = codeBlock([['mycli', ' rm', ' NAME']])
    const container = page(para('prose'), host)
    const [a, b] = tokens(host)
    const { sel, getComposedRanges } = fakeSelection([{ startContainer: a, startOffset: 2, endContainer: b, endOffset: 3 }])
    const range = shadowSelectionRange(sel, container)!
    expect(range.toString()).toBe('cli rm')
    expect(getComposedRanges).toHaveBeenCalledWith({ shadowRoots: [host.shadowRoot] })
  })

  it('refuses a selection inside a code block that is not under the container', () => {
    const outside = codeBlock([['elsewhere']])
    page(outside)
    const container = page(codeBlock([['mine']]))
    const [text] = tokens(outside)
    const { sel } = fakeSelection([{ startContainer: text, startOffset: 0, endContainer: text, endOffset: 4 }])
    expect(shadowSelectionRange(sel, container)).toBeNull()
  })

  it('refuses a selection that begins in the light DOM and ends inside a code block', () => {
    const host = codeBlock([['code']])
    const prose = para('prose')
    const container = page(prose, host)
    const [text] = tokens(host)
    const { sel } = fakeSelection([{ startContainer: prose.firstChild!, startOffset: 1, endContainer: text, endOffset: 2 }])
    expect(shadowSelectionRange(sel, container)).toBeNull()
  })

  it('refuses a selection that spans two code blocks', () => {
    const one = codeBlock([['first']])
    const two = codeBlock([['second']])
    const container = page(one, two)
    const { sel } = fakeSelection([{ startContainer: tokens(one)[0], startOffset: 0, endContainer: tokens(two)[0], endOffset: 3 }])
    expect(shadowSelectionRange(sel, container)).toBeNull()
  })

  it('refuses an empty selection', () => {
    const host = codeBlock([['code']])
    const container = page(host)
    const [text] = tokens(host)
    const { sel } = fakeSelection([{ startContainer: text, startOffset: 2, endContainer: text, endOffset: 2 }])
    expect(shadowSelectionRange(sel, container)).toBeNull()
  })

  it('answers null, without asking, when the container holds no code block', () => {
    const { sel, getComposedRanges } = fakeSelection([])
    expect(shadowSelectionRange(sel, page(para('prose')))).toBeNull()
    expect(getComposedRanges).not.toHaveBeenCalled()
  })

  it('answers null, without searching the container, where the engine has no getComposedRanges', () => {
    const container = page(codeBlock([['code']]))
    const search = vi.spyOn(container, 'querySelectorAll')
    expect(shadowSelectionRange({} as Selection, container)).toBeNull()
    expect(search).not.toHaveBeenCalled()
  })

  it('answers null where the engine rejects the call', () => {
    const sel = { getComposedRanges: () => { throw new TypeError('shadowRoots is not iterable') } } as unknown as Selection
    expect(shadowSelectionRange(sel, page(codeBlock([['code']])))).toBeNull()
  })

  it('answers null when the engine reports no ranges', () => {
    expect(shadowSelectionRange(fakeSelection([]).sel, page(codeBlock([['code']])))).toBeNull()
  })
})

describe('watchText', () => {
  let frames: ReturnType<typeof stubManualFrames>
  beforeEach(() => { frames = stubManualFrames() })
  afterEach(() => vi.unstubAllGlobals())

  /** happy-dom hands a mutation's records over on a microtask queued by the mutation itself. */
  const delivered = () => Promise.resolve()

  it('reports a block swapped into the tree', async () => {
    const root = page(plainCopy('echo one'))
    const onChange = vi.fn()
    watchText(root, onChange)
    root.append(codeBlock([['echo', ' one']]))
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('reports a change anywhere below the root', async () => {
    const section = document.createElement('section')
    const root = page(section)
    const onChange = vi.fn()
    watchText(root, onChange)
    section.append(para('deeper'))
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('reports the fill of a shown block’s shadow root, which a tree observer never sees', async () => {
    const host = document.createElement('diffs-container')
    const shadow = host.attachShadow({ mode: 'open' })
    const root = page(host)
    const onChange = vi.fn()
    watchText(root, onChange)
    shadow.append(document.createElement('div'))
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('leaves the fill of a parked block’s shadow root alone', async () => {
    const host = document.createElement('diffs-container')
    const shadow = host.attachShadow({ mode: 'open' })
    const root = page(parked(host))
    const onChange = vi.fn()
    watchText(root, onChange)
    shadow.append(document.createElement('div'))
    await delivered()
    frames.runFrame()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('watches the shadow root of a block that arrived after it started', async () => {
    const root = page(para('first'))
    const onChange = vi.fn()
    watchText(root, onChange)
    const host = document.createElement('diffs-container')
    const shadow = host.attachShadow({ mode: 'open' })
    root.append(host)
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(1)
    shadow.append(document.createElement('div'))
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('calls back once for the changes of a frame, even delivered apart', async () => {
    const root = page(para('first'))
    const onChange = vi.fn()
    watchText(root, onChange)
    root.append(para('second'))
    await delivered()
    root.append(para('third'))
    await delivered()
    expect(frames.pending.size).toBe(1)
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('asks for a new frame for a change that follows the last callback', async () => {
    const root = page(para('first'))
    const onChange = vi.fn()
    watchText(root, onChange)
    root.append(para('second'))
    await delivered()
    frames.runFrame()
    root.append(para('third'))
    await delivered()
    frames.runFrame()
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('stops for good, cancelling a frame it was waiting for', async () => {
    const root = page(para('first'))
    const onChange = vi.fn()
    const stop = watchText(root, onChange)
    root.append(para('second'))
    await delivered()
    expect(frames.pending.size).toBe(1)
    stop()
    expect(frames.pending.size).toBe(0)
    root.append(para('third'))
    await delivered()
    frames.runFrame()
    expect(frames.pending.size).toBe(0)
    expect(onChange).not.toHaveBeenCalled()
  })
})
