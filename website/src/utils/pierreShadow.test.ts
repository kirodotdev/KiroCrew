import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { codePassagePosition, codeQuoteRange, codeShadowRoots, matchRanges, searchRuns, shadowSelectionRange, watchText } from './pierreShadow'
import { codeBlock, codeBlockUnit, diffFence, multiFileDiffFence, page, para, parked, plainCopy } from '../test/pierreDom'
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

describe('codePassagePosition', () => {
  /** A range in the nth rendered line, from `offset` of its first text node to the end of the `endLine`th line's last one. */
  function rangeAt(host: HTMLElement, lineIndex = 0, offset = 0, endLine = lineIndex): Range {
    const lines = host.shadowRoot!.querySelectorAll('[data-line]')
    const text = lines[lineIndex].querySelector('span')!.firstChild as Text
    const spans = lines[endLine].querySelectorAll('span')
    const last = spans[spans.length - 1].firstChild as Text
    const range = document.createRange()
    range.setStart(text, offset)
    range.setEnd(last, last.length)
    return range
  }

  function plainRangeAt(block: HTMLElement, lineIndex = 0, offset = 0, endLine = lineIndex): Range {
    const pre = block.matches('pre') ? block : block.querySelector('pre')!
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT)
    const text = walker.nextNode() as Text
    const sourceLines = text.data.split('\n')
    const lineStart = (index: number) => sourceLines.slice(0, index).reduce((start, line) => start + line.length + 1, 0)
    const range = document.createRange()
    range.setStart(text, lineStart(lineIndex) + offset)
    range.setEnd(text, lineStart(endLine) + sourceLines[endLine].length)
    return range
  }

  it('uses the shown-copy key for the first line of a plain code fence', () => {
    const plain = codeBlockUnit([['mycli', ' rm', ' NAME']], 'plain')
    const shown = codeBlockUnit([['mycli', ' rm', ' NAME']])
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host)))
  })

  it('uses the shown-copy key for a later line of a plain code fence', () => {
    const source = [['first'], ['second'], ['mycli', ' rm', ' NAME']]
    const plain = codeBlockUnit(source, 'plain')
    const shown = codeBlockUnit(source)
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper, 2)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host, 2)))
  })

  it('uses the first visible character after leading whitespace in a plain code fence', () => {
    const source = [['  mycli', ' rm', ' NAME']]
    const plain = codeBlockUnit(source, 'plain')
    const shown = codeBlockUnit(source)
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host)))
  })

  it('moves leading whitespace past plain row boundaries to the next visible character', () => {
    const source = [['previous'], ['  '], ['  target']]
    const plain = codeBlockUnit(source, 'plain')
    const shown = codeBlockUnit(source)
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper, 0, 'previous'.length, 2)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host, 0, 'previous'.length, 2)))
  })

  it('uses the shown-copy ordinal for the second of two plain code fences', () => {
    const firstPlain = codeBlockUnit([['same']], 'plain')
    const secondPlain = codeBlockUnit([['mycli', ' rm', ' NAME']], 'plain')
    const firstShown = codeBlockUnit([['same']])
    const secondShown = codeBlockUnit([['mycli', ' rm', ' NAME']])
    expect(codePassagePosition(page(firstPlain.wrapper, secondPlain.wrapper), plainRangeAt(secondPlain.wrapper)))
      .toBe(codePassagePosition(page(firstShown.wrapper, secondShown.wrapper), rangeAt(secondShown.host)))
  })

  it('ignores a trailing newline that the highlighted rows do not draw', () => {
    const plain = codeBlockUnit([['first'], ['mycli', ' rm', ' NAME']], 'plain')
    const text = plain.wrapper.querySelector('code')!.firstChild as Text
    text.data += '\n'
    const shown = codeBlockUnit([['first'], ['mycli', ' rm', ' NAME']])
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper, 1)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host, 1)))
  })

  it('gives no position to a range after the trailing newline of a plain code fence', () => {
    const plain = codeBlockUnit([['a'], ['b']], 'plain')
    const text = plain.wrapper.querySelector('code')!.firstChild as Text
    text.data += '\n'
    const afterLastRow = document.createRange()
    afterLastRow.setStart(text, text.length)
    afterLastRow.setEnd(text, text.length)
    expect(codePassagePosition(page(plain.wrapper), afterLastRow)).toBeNull()
  })

  it('places a whitespace-only range to the end of a plain row where the shown copy does', () => {
    const source = [['code  '], ['next']]
    const plain = codeBlockUnit(source, 'plain')
    const shown = codeBlockUnit(source)
    expect(codePassagePosition(page(plain.wrapper), plainRangeAt(plain.wrapper, 0, 'code'.length)))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host, 0, 'code'.length)))
  })

  it('uses the shown-copy key for a range that starts on the plain copy element itself', () => {
    const plain = codeBlockUnit([['mycli', ' rm', ' NAME']], 'plain')
    const shown = codeBlockUnit([['mycli', ' rm', ' NAME']])
    const pre = plain.wrapper.querySelector('pre')!
    const fromElement = document.createRange()
    fromElement.setStart(pre, 0)
    fromElement.setEnd(pre.querySelector('code')!.firstChild as Text, 'mycli rm NAME'.length)
    expect(codePassagePosition(page(plain.wrapper), fromElement))
      .toBe(codePassagePosition(page(shown.wrapper), rangeAt(shown.host)))
  })

  it('gives no position to a plain code fence whose wrapper lies above the root', () => {
    const plain = codeBlockUnit([['same']], 'plain')
    page(plain.wrapper)
    const inner = plain.wrapper.querySelector('.pierre-surface')!
    expect(codePassagePosition(inner, plainRangeAt(plain.wrapper))).toBeNull()
  })

  it('gives no position to a plain code fence when the root is its own wrapper', () => {
    const plain = codeBlockUnit([['same']], 'plain')
    page(plain.wrapper)
    expect(codePassagePosition(plain.wrapper, plainRangeAt(plain.wrapper))).toBeNull()
  })

  it('returns null for a range in a plain diff body', () => {
    const diff = diffFence([['-old'], ['+new']], false)
    expect(codePassagePosition(page(diff.wrapper), plainRangeAt(diff.wrapper))).toBeNull()
  })

  it('keeps a later unit stable while an earlier unit changes from plain to shown', () => {
    const first = codeBlockUnit([['same']], 'plain')
    const second = codeBlockUnit([['same']])
    const root = page(first.wrapper, second.wrapper)
    const withFirstPlain = codePassagePosition(root, rangeAt(second.host))
    expect(codePassagePosition(root, rangeAt(second.host))).toBe(withFirstPlain)

    first.wrapper.replaceChildren(first.host)
    const firstPosition = codePassagePosition(root, rangeAt(first.host))
    const withFirstShown = codePassagePosition(root, rangeAt(second.host))
    expect(withFirstShown).toBe(withFirstPlain)
    expect(withFirstShown).not.toBe(firstPosition)
  })

  it('counts a parked host and its plain copy as one unit', () => {
    const first = codeBlockUnit([['same']], 'parked')
    const second = codeBlockUnit([['same']])
    const root = page(first.wrapper, second.wrapper)
    const copy = first.wrapper.querySelector('pre')!

    first.wrapper.replaceChildren(copy)
    const withFirstPlain = codePassagePosition(root, rangeAt(second.host))
    first.wrapper.replaceChildren(copy, parked(first.host))
    const withFirstParked = codePassagePosition(root, rangeAt(second.host))
    first.wrapper.replaceChildren(first.host)
    const withFirstShown = codePassagePosition(root, rangeAt(second.host))

    expect(withFirstParked).toBe(withFirstPlain)
    expect(withFirstShown).toBe(withFirstPlain)
  })

  it('counts a bare diffs-container outside a code-block as its own unit', () => {
    const wrapped = codeBlockUnit([['wrapped']])
    const bare = codeBlock([['bare']])
    const root = page(wrapped.wrapper, bare)
    expect(codePassagePosition(root, rangeAt(wrapped.host))).toBe(1e5)
    expect(codePassagePosition(root, rangeAt(bare))).toBe(1e12 + 1e5)
  })

  it('counts a diff fence as one unit whether or not Pierre drew a host in it', () => {
    const diff = diffFence([['-old'], ['+new']])
    const later = codeBlockUnit([['same']])
    const root = page(diff.wrapper, later.wrapper)
    const withHost = codePassagePosition(root, rangeAt(later.host))

    diff.section.replaceChildren(plainCopy('-old\n+new'))
    const withoutHost = codePassagePosition(root, rangeAt(later.host))

    expect(withHost).toBe(1e12 + 1e5)
    expect(withoutHost).toBe(withHost)
  })

  it('gives identical rows in two files of one diff fence different keys', () => {
    const diff = multiFileDiffFence([[['same']], [['same']]], [true, true])
    const root = page(diff.wrapper)

    expect(codePassagePosition(root, rangeAt(diff.hosts[0]))).toBe(1e5)
    expect(codePassagePosition(root, rangeAt(diff.hosts[1]))).toBe(1e12 + 1e5)
  })

  it('keeps the second file key stable when the first file gains a host', () => {
    const diff = multiFileDiffFence([[['same']], [['same']]], [false, true])
    const root = page(diff.wrapper)
    const withoutFirstHost = codePassagePosition(root, rangeAt(diff.hosts[1]))

    diff.sections[0].replaceChildren(diff.hosts[0])
    const withFirstHost = codePassagePosition(root, rangeAt(diff.hosts[1]))

    expect(withoutFirstHost).toBe(1e12 + 1e5)
    expect(withFirstHost).toBe(withoutFirstHost)
  })

  it('keeps a later code-block key stable as earlier diff files gain and lose hosts', () => {
    const diff = multiFileDiffFence([[['first']], [['second']]], [false, false])
    const later = codeBlockUnit([['same']])
    const root = page(diff.wrapper, later.wrapper)
    const withoutHosts = codePassagePosition(root, rangeAt(later.host))

    diff.sections[0].replaceChildren(diff.hosts[0])
    const withFirstHost = codePassagePosition(root, rangeAt(later.host))
    diff.sections[1].replaceChildren(diff.hosts[1])
    const withBothHosts = codePassagePosition(root, rangeAt(later.host))
    diff.sections[0].replaceChildren(plainCopy('first'))
    const withSecondHost = codePassagePosition(root, rangeAt(later.host))

    expect(withoutHosts).toBe(2e12 + 1e5)
    expect([withFirstHost, withBothHosts, withSecondHost]).toEqual([
      withoutHosts,
      withoutHosts,
      withoutHosts,
    ])
  })

  it('places a selection inside a diff fence under the diff fence\'s own ordinal', () => {
    const first = codeBlockUnit([['same']])
    const diff = diffFence([['-old'], ['+new']])
    const root = page(first.wrapper, diff.wrapper)
    expect(codePassagePosition(root, rangeAt(diff.host, 1))).toBe(1e12 + 2e5)
    expect(codePassagePosition(root, rangeAt(first.host))).toBe(1e5)
  })

  it('gives no position to a block whose wrapper lies above the root', () => {
    const unit = codeBlockUnit([['same']])
    const inner = document.createElement('div')
    inner.append(unit.host)
    unit.wrapper.replaceChildren(inner)
    page(unit.wrapper)
    expect(codePassagePosition(inner, rangeAt(unit.host))).toBeNull()
  })

  it('keeps a passage stable across row windows and distinguishes repeated text on different rows', () => {
    const full = codeBlock([['first'], ['same'], ['same']])
    const windowed = codeBlock([['same'], ['fourth'], ['fifth']], 3)
    const fullRoot = page(full)
    const windowedRoot = page(windowed)

    expect(codePassagePosition(fullRoot, rangeAt(full, 2))).toBe(codePassagePosition(windowedRoot, rangeAt(windowed)))
    expect(codePassagePosition(fullRoot, rangeAt(full, 1))).not.toBe(codePassagePosition(fullRoot, rangeAt(full, 2)))
  })

  it('counts across the tokens of a line, from a text node or from the line itself', () => {
    const host = codeBlock([['mycli', ' rm', ' NAME']])
    const root = page(host)
    const line = host.shadowRoot!.querySelector('[data-line]')!
    const third = line.querySelectorAll('span')[2].firstChild as Text
    const inThird = document.createRange()
    inThird.setStart(third, 1)
    inThird.setEnd(third, third.length)
    expect(codePassagePosition(root, inThird)).toBe(1e5 + 'mycli rm'.length + 1)
    const beforeThird = document.createRange()
    beforeThird.setStart(line, 2)
    beforeThird.setEnd(third, third.length)
    expect(codePassagePosition(root, beforeThird)).toBe(1e5 + 'mycli rm'.length + 1)
  })

  it('uses the first visible character across row boundaries', () => {
    const host = codeBlock([['previous'], ['  '], ['  target']])
    const root = page(host)
    const previous = host.shadowRoot!.querySelectorAll('[data-line] span')[0].firstChild as Text
    const target = host.shadowRoot!.querySelectorAll('[data-line] span')[2].firstChild as Text
    const fromPreviousEnd = document.createRange()
    fromPreviousEnd.setStart(previous, previous.length)
    fromPreviousEnd.setEnd(target, target.length)
    const fromIndent = codePassagePosition(root, rangeAt(host, 2))

    expect(fromIndent).toBe(3e5 + 2)
    expect(codePassagePosition(root, rangeAt(host, 2, 2))).toBe(fromIndent)
    expect(codePassagePosition(root, fromPreviousEnd)).toBe(fromIndent)

    const exact = codeBlock([['first  '], ['target']])
    const exactRoot = page(exact)
    expect(codePassagePosition(exactRoot, rangeAt(exact, 0, 5, 1))).toBe(2e5)
  })

  it('uses an absolute line number for a windowed whole-file code surface', () => {
    const host = codeBlock([['same']], 2901)
    expect(codePassagePosition(page(host), rangeAt(host))).toBe(2901 * 1e5)
  })

  it('refuses a shown-copy column that would spill into the next packed field', () => {
    const host = codeBlock([['x'.repeat(100_000), 'target'], ['target']])
    const root = page(host)
    const first = codePassagePosition(root, rangeAt(host, 0, 100_000))
    const second = codePassagePosition(root, rangeAt(host, 1))

    expect(first).toBeNull()
    expect(first).not.toBe(second)
  })

  it('refuses a plain-copy column that would spill into the next packed field', () => {
    const unit = codeBlockUnit([['x'.repeat(100_000), 'target'], ['target']], 'plain')
    const root = page(unit.wrapper)
    const first = codePassagePosition(root, plainRangeAt(unit.wrapper, 0, 100_000))
    const second = codePassagePosition(root, plainRangeAt(unit.wrapper, 1))

    expect(first).toBeNull()
    expect(first).not.toBe(second)
  })

  it('accepts the last column in its field and refuses the first column outside it', () => {
    const host = codeBlock([['x'.repeat(100_000), 'target']])
    const root = page(host)

    expect(codePassagePosition(root, rangeAt(host, 0, 99_999))).toBe(199_999)
    expect(codePassagePosition(root, rangeAt(host, 0, 100_000))).toBeNull()
  })

  it('accepts the last line in its field and refuses the first line outside it', () => {
    const last = codeBlock([['target']], 9_999_999)
    const outside = codeBlock([['target']], 10_000_000)

    expect(codePassagePosition(page(last), rangeAt(last))).toBe(999_999_900_000)
    expect(codePassagePosition(page(outside), rangeAt(outside))).toBeNull()
  })

  it('refuses a block ordinal whose packed position is not a safe integer', () => {
    const wrappers = Array.from({ length: 9_007 }, () => {
      const wrapper = document.createElement('div')
      wrapper.className = 'code-block'
      return wrapper
    })
    const unit = codeBlockUnit([['target']])
    const root = page(...wrappers, unit.wrapper)

    expect(codePassagePosition(root, rangeAt(unit.host))).toBe(9_007_000_000_100_000)
    const overflow = document.createElement('div')
    overflow.className = 'code-block'
    root.insertBefore(overflow, unit.wrapper)
    expect(codePassagePosition(root, rangeAt(unit.host))).toBeNull()
  })

  it('stays on the last row when leading whitespace runs to its end', () => {
    const host = codeBlock([['code'], ['   ']])
    const whitespace = host.shadowRoot!.querySelectorAll('[data-line] span')[1].firstChild as Text
    const range = document.createRange()
    range.setStart(whitespace, 0)
    range.setEnd(whitespace, whitespace.length)
    expect(codePassagePosition(page(host), range)).toBe(2e5 + 3)
  })

  it('does not count whitespace that lies beyond the last row', () => {
    const host = codeBlock([['code']])
    const tail = document.createElement('div')
    tail.textContent = '    '
    host.shadowRoot!.append(tail)
    const text = host.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    const range = document.createRange()
    range.setStart(text, text.length)
    range.setEnd(tail.firstChild!, 4)
    expect(range.toString()).toBe('    ')
    expect(codePassagePosition(page(host), range)).toBe(1e5 + 'code'.length)
  })

  it('returns null when a reached source line number is missing, invalid or non-positive', () => {
    const missing = codeBlock([['same']])
    const missingRoot = page(missing)
    const missingRange = rangeAt(missing)
    missing.shadowRoot!.querySelector('[data-line]')!.removeAttribute('data-line')
    expect(codePassagePosition(missingRoot, missingRange)).toBeNull()

    const invalid = codeBlock([['same']])
    const invalidRoot = page(invalid)
    const invalidRange = rangeAt(invalid)
    invalid.shadowRoot!.querySelector('[data-line]')!.setAttribute('data-line', 'not-a-number')
    expect(codePassagePosition(invalidRoot, invalidRange)).toBeNull()

    const nonPositive = codeBlock([['same']])
    const nonPositiveRoot = page(nonPositive)
    const nonPositiveRange = rangeAt(nonPositive)
    nonPositive.shadowRoot!.querySelector('[data-line]')!.setAttribute('data-line', '0')
    expect(codePassagePosition(nonPositiveRoot, nonPositiveRange)).toBeNull()

    const reached = codeBlock([['first'], ['  target']])
    const reachedRoot = page(reached)
    const reachedRange = rangeAt(reached, 0, 'first'.length, 1)
    reached.shadowRoot!.querySelectorAll('[data-line]')[1].setAttribute('data-line', 'invalid')
    expect(codePassagePosition(reachedRoot, reachedRange)).toBeNull()
  })

  it('places only a row whose data-line is a positive integer', () => {
    const positionWithLine = (value: string) => {
      const host = codeBlock([['same']])
      const root = page(host)
      const range = rangeAt(host)
      host.shadowRoot!.querySelector('[data-line]')!.setAttribute('data-line', value)
      return codePassagePosition(root, range)
    }

    expect(positionWithLine('1.5')).toBeNull()
    expect(positionWithLine('abc')).toBeNull()
    expect(positionWithLine('0')).toBeNull()
    expect(positionWithLine('-1')).toBeNull()
    expect(positionWithLine('3')).toBe(300_000)
  })

  it('returns null for a range that starts outside a shown code line', () => {
    const host = codeBlock([['code']])
    const prose = para('prose')
    const root = page(host, prose)
    const inProse = document.createRange()
    inProse.setStart(prose.firstChild as Text, 0)
    inProse.setEnd(prose.firstChild as Text, 1)
    expect(codePassagePosition(root, inProse)).toBeNull()
    const inWrapper = document.createRange()
    inWrapper.setStart(host.shadowRoot!.querySelector('[data-content]')!, 0)
    inWrapper.setEnd((host.shadowRoot!.querySelector('[data-line] span') as HTMLElement).firstChild!, 2)
    expect(codePassagePosition(root, inWrapper)).toBeNull()
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

describe('codeQuoteRange', () => {
  const fence = (state: 'plain' | 'parked' | 'shown' = 'shown') => codeBlockUnit([['mycli', ' rm', ' NAME'], ['  mycli', ' rm', ' OTHER']], state)
  /** The packed position of characters `start`.. of the `lineIndex`th line of `host` under `root`. */
  function positionAt(root: Element, host: HTMLElement, lineIndex: number, start: number): number {
    const line = host.shadowRoot!.querySelectorAll('[data-line]')[lineIndex]
    const range = document.createRange()
    const node = line.querySelector('span')!.firstChild as Text
    range.setStart(node, start)
    range.setEnd(node, node.length)
    return codePassagePosition(root, range)!
  }

  it('places the quote at its position in the block it was selected in, not the first block with it', () => {
    const first = fence()
    const second = fence()
    const root = page(para('mycli rm NAME'), first.wrapper, second.wrapper)
    const range = codeQuoteRange(root, 'mycli rm NAME', positionAt(root, second.host, 0, 0))
    expect(range?.toString()).toBe('mycli rm NAME')
    expect(range?.startContainer.getRootNode()).toBe(second.host.shadowRoot)
  })

  it('places a quote that runs onto the next line across both lines', () => {
    const block = fence()
    const root = page(block.wrapper)
    const range = codeQuoteRange(root, 'NAME\n  mycli rm', positionAt(root, block.host, 0, 0) + 'mycli rm '.length)
    expect(range?.toString()).toBe('NAME  mycli rm')
  })

  it('places nothing while the positioned block shows only its plain or parked copy', () => {
    const shown = fence()
    const position = positionAt(page(shown.wrapper), shown.host, 0, 0)
    document.body.replaceChildren()
    expect(codeQuoteRange(page(fence('plain').wrapper, fence().wrapper), 'mycli rm NAME', position)).toBeNull()
    expect(codeQuoteRange(page(fence('parked').wrapper, fence().wrapper), 'mycli rm NAME', position)).toBeNull()
  })

  it('without a position, places a quote found once in the code and never in the prose', () => {
    const block = fence()
    const root = page(para('rm OTHER'), block.wrapper)
    expect(codeQuoteRange(root, 'rm OTHER')?.startContainer.getRootNode()).toBe(block.host.shadowRoot)
  })

  it('without a position, places nothing for a quote in two blocks, a plain copy counting as one', () => {
    expect(codeQuoteRange(page(fence().wrapper, fence().wrapper), 'rm NAME')).toBeNull()
    document.body.replaceChildren()
    expect(codeQuoteRange(page(fence().wrapper, fence('plain').wrapper), 'rm NAME')).toBeNull()
  })

  it('does not use a column past the named row when the remaining quote is ambiguous', () => {
    const block = codeBlockUnit([['echo hello'], ['hello'], ['hello']])
    const root = page(block.wrapper)
    const position = positionAt(root, block.host, 0, 5)
    expect(codeQuoteRange(root, 'hello', position)?.startContainer)
      .toBe(block.host.shadowRoot!.querySelector('[data-line] span')!.firstChild)

    const first = block.host.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    first.data = 'true'
    expect(codeQuoteRange(root, 'hello', position)).toBeNull()
  })

  it('falls back to the unique match when the text at the position has changed', () => {
    const block = fence()
    const root = page(block.wrapper)
    expect(codeQuoteRange(root, 'rm OTHER', positionAt(root, block.host, 0, 0))?.toString()).toBe('rm OTHER')
  })
})
