import { describe, expect, it } from 'vitest'
import { hasListLine, listIndentEdit } from '../components/composerListIndent'

/** Apply Tab or Shift+Tab with a collapsed caret marked by `|`. */
function press(marked: string, outdent = false): string | null {
  const caret = marked.indexOf('|')
  const value = marked.replace('|', '')
  const edit = listIndentEdit(value, caret, caret, outdent)
  if (!edit) return null
  return edit.value.slice(0, edit.caret) + '|' + edit.value.slice(edit.caret)
}

/** Whether Tab at the end of `line` counts it as a list line. */
const indents = (line: string) => listIndentEdit(line, line.length, line.length, false) !== null

describe('list line detection', () => {
  it.each(['- a', '* a', '+ a', '1. a', '12) a', '- [ ] task', '- [x] done', '  - nested', '\t1. tabbed', '- '])(
    'treats %j as a list line', line => expect(indents(line)).toBe(true),
  )

  // Same rule as list continuation: a bare marker ("-", "2024.") is ordinary text.
  it.each(['', 'plain text', '-5 degrees', '*bold*', '1.5 litres', 'a - b', '#1 item', '-', '3.', '2024.'])(
    'does not treat %j as a list line', line => expect(indents(line)).toBe(false),
  )
})

describe('listIndentEdit: Tab', () => {
  it('adds two spaces at the start of the line and keeps the caret on its character', () => {
    expect(press('- ite|m')).toBe('  - ite|m')
  })

  it('indents only the caret line of a multi-line draft', () => {
    expect(press('intro\n- one\n- tw|o\nend')).toBe('intro\n- one\n  - tw|o\nend')
  })

  it('indents an already-indented item one more level', () => {
    expect(press('  1. fi|rst')).toBe('    1. fi|rst')
  })

  it('works with the caret at the very start of the draft', () => {
    expect(press('|- a')).toBe('  |- a')
  })

  it('works on a task item', () => {
    expect(press('- [ ] do |it')).toBe('  - [ ] do |it')
  })

  it('leaves a non-list line alone so Tab moves focus', () => {
    expect(press('just te|xt')).toBeNull()
    expect(press('- one\nplain|')).toBeNull()
  })

  it('leaves a ranged selection alone', () => {
    expect(listIndentEdit('- item', 1, 4, false)).toBeNull()
  })
})

describe('listIndentEdit: Shift+Tab', () => {
  it('removes two leading spaces', () => {
    expect(press('  - ite|m', true)).toBe('- ite|m')
  })

  it('removes one leading tab', () => {
    expect(press('\t- ite|m', true)).toBe('- ite|m')
  })

  it('removes a lone leading space', () => {
    expect(press(' - ite|m', true)).toBe('- ite|m')
  })

  it('removes only one level from a deeper item', () => {
    expect(press('    - dee|p', true)).toBe('  - dee|p')
  })

  it('parks a caret inside the removed indentation at the line start', () => {
    expect(press('a\n |  - x', true)).toBe('a\n| - x')
  })

  it('is a no-op on an unindented list line so Shift+Tab keeps moving focus', () => {
    expect(press('- to|p', true)).toBeNull()
  })

  it('is a no-op on a non-list line', () => {
    expect(press('  plain inde|nted', true)).toBeNull()
  })

  it('round-trips with Tab', () => {
    const indented = press('x\n- b|')!
    expect(press(indented, true)).toBe('x\n- b|')
  })
})

describe('hasListLine', () => {
  it('is true when any line of the draft is a list line', () => {
    expect(hasListLine('intro\n  - item')).toBe(true)
    expect(hasListLine('plain\ntext')).toBe(false)
    expect(hasListLine('')).toBe(false)
  })
})
