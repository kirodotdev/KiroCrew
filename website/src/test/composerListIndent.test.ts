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

/** Apply Tab or Shift+Tab over a selection marked by `[` and `]`. */
function pressRange(marked: string, outdent = false): string | null {
  const start = marked.indexOf('[')
  const end = marked.indexOf(']') - 1
  const edit = listIndentEdit(marked.replace('[', '').replace(']', ''), start, end, outdent)
  if (!edit) return null
  return edit.value.slice(0, edit.caret) + '[' + edit.value.slice(edit.caret, edit.end) + ']' + edit.value.slice(edit.end)
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

  it('nests under a tab-indented sibling after its tab, not before it', () => {
    expect(press('\t1. a\n\t2. |b')).toBe('\t1. a\n\t   1. |b')
    expect(press('\t- a|')).toBe('\t  - a|')
  })
})

describe('listIndentEdit: a selection over several lines', () => {
  it('indents every list line in it, skips plain lines, and keeps the selection', () => {
    expect(pressRange('intro\n- [one\nnote\n- tw]o')).toBe('intro\n  - [one\nnote\n  - tw]o')
  })

  it('moves a run of siblings by the first one\'s step', () => {
    expect(pressRange('1. a\n[2. b\n3. c]')).toBe('1. a\n   [1. b\n   2. c]')
    expect(pressRange('1. x\n[- a\n- b]')).toBe('1. x\n   [- a\n   - b]')
    expect(pressRange('[1. a\n2. b]')).toBe('  [1. a\n  2. b]')
  })

  it('moves a selected item\'s children by the item\'s own step', () => {
    expect(pressRange('1. a\n[2. b\n   1. c]')).toBe('1. a\n   [1. b\n      1. c]')
    expect(pressRange('9. x\n[10. a\n    1. c]')).toBe('9. x\n   [1. a\n      1. c]')
    expect(pressRange('- a\n[  - b\n    - c]', true)).toBe('- a\n[- b\n  - c]')
    expect(pressRange('- x\n[  1. a\n     - c]', true)).toBe('- x\n[1. a\n   - c]')
  })

  it('takes back what Tab added on lines indented with tabs', () => {
    expect(pressRange(pressRange('[- a\n\t- c]')!, true)).toBe('[- a\n\t- c]')
    expect(pressRange('[  - a\n\t\t- c]', true)).toBe('[- a\n\t- c]')
  })

  it('leaves the line a selection ends at the start of', () => {
    expect(pressRange('[- a\n- b\n]- c')).toBe('  [- a\n  - b\n]- c')
    expect(pressRange('[- a\n]- b')).toBe('  [- a\n]- b')
  })

  it('is left alone when no line in it can move', () => {
    expect(pressRange('[plain\ntext]')).toBeNull()
    expect(pressRange('[- a\n- b]', true)).toBeNull()
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

  it('parks a caret inside the removed indentation where it was removed', () => {
    expect(press('a\n  | - x', true)).toBe('a\n |- x')
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
    expect(press(press('\t- a|')!, true)).toBe('\t- a|')
  })

  it('outdents back to the parent item indent', () => {
    expect(press('1. one\n   - su|b', true)).toBe('1. one\n- su|b')
    expect(press('- a\n  - b\n    - c|', true)).toBe('- a\n  - b\n  - c|')
  })
})

describe('listIndentEdit: ordered parents', () => {
  // CommonMark nests an item only at or past its parent's text column, so two
  // spaces under "1. " would render as a sibling.
  it('indents under an ordered sibling to its text column', () => {
    expect(press('1. one\n2. tw|o')).toBe('1. one\n   1. tw|o')
    expect(press('10. ten\n11. ele|ven')).toBe('10. ten\n    1. ele|ven')
  })

  it('uses the nested sibling column, skipping plain lines between', () => {
    expect(press('1. one\n   1. a\n   more text\n   2. |b')).toBe('1. one\n   1. a\n   more text\n      1. |b')
  })

  it('keeps two spaces under a bullet sibling', () => {
    expect(press('- one\n- tw|o')).toBe('- one\n  - tw|o')
  })

  it('round-trips with Shift+Tab under an ordered parent', () => {
    const indented = press('1. one\n2. tw|o')!
    expect(press(indented, true)).toBe('1. one\n2. tw|o')
    expect(press(press('\t1. a\n\t2. |b')!, true)).toBe('\t1. a\n\t2. |b')
  })
})

// A numbered item that changes level is counted at its new level: CommonMark
// starts a nested list at its first item's number, so a `2.` moved under `1.`
// would render as a nested list that begins at 2.
describe('listIndentEdit: numbering an ordered item that changes level', () => {
  it('Tab starts the nested level at 1 and closes the gap below', () => {
    expect(press('1. a\n2. b|\n3. c\n4. d')).toBe('1. a\n   1. b|\n2. c\n3. d')
  })

  it('Tab continues the count of a nested sibling right above', () => {
    expect(press('1. a\n   1. x\n   2. y\n2. b|\n3. c')).toBe('1. a\n   1. x\n   2. y\n   3. b|\n2. c')
  })

  it('Tab starts at 1 after a nested bullet or another delimiter', () => {
    expect(press('1. a\n   - x\n2. b|')).toBe('1. a\n   - x\n   1. b|')
    expect(press('1. a\n   1) x\n2. b|')).toBe('1. a\n   1) x\n   1. b|')
  })

  it('keeps zero padding', () => {
    expect(press('01. a\n02. b|\n03. c')).toBe('01. a\n    01. b|\n02. c')
  })

  it('leaves the numbers below that the user did not count in sequence', () => {
    expect(press('1. a\n2. b|\n7. c')).toBe('1. a\n   1. b|\n7. c')
    expect(press('1. a\n1. b|\n1. c')).toBe('1. a\n   1. b|\n1. c')
    expect(press('1. a\n2. b|\n\n3. c')).toBe('1. a\n   1. b|\n\n3. c')
  })

  it('keeps the number of an item that stays first in its own list', () => {
    expect(press('1. a\n   3. b|')).toBe('1. a\n     3. b|')
    expect(press('3. a|')).toBe('  3. a|')
    expect(press('   2. b|', true)).toBe(' 2. b|')
  })

  it('keeps a caret in front of the marker there, and one inside the number behind it', () => {
    expect(press('1. a\n|2. b')).toBe('1. a\n   |1. b')
    expect(press('1. a\n1|2. b')).toBe('1. a\n   1|. b')
    expect(press('1. a\n   |1. b', true)).toBe('1. a\n|2. b')
  })

  it('Shift+Tab follows the parent level and moves the items below it down', () => {
    expect(press('1. a\n   1. b|\n2. c', true)).toBe('1. a\n2. b|\n3. c')
  })

  it('Shift+Tab restarts the nested items below, which become its children', () => {
    expect(press('1. a\n   1. b\n   2. c|\n   3. d\n   4. e\n2. f', true))
      .toBe('1. a\n   1. b\n2. c|\n   1. d\n   2. e\n3. f')
  })

  it('Shift+Tab starts at 1 beside a bullet or another delimiter', () => {
    expect(press('- a\n  1. b\n  2. c|', true)).toBe('- a\n  1. b\n1. c|')
    expect(press('1) a\n   1. b\n   2. c|', true)).toBe('1) a\n   1. b\n1. c|')
  })

  it('Shift+Tab leaves the items below that do not reach its text column', () => {
    expect(press('- a\n  1. b|\n  2. c', true)).toBe('- a\n1. b|\n  2. c')
  })

  it('round-trips a run with the items below it', () => {
    const indented = press('1. a\n2. b|\n3. c')!
    expect(indented).toBe('1. a\n   1. b|\n2. c')
    expect(press(indented, true)).toBe('1. a\n2. b|\n3. c')
  })

  it('leaves the numbers alone when a bullet moves', () => {
    expect(press('1. a\n- b|\n2. c')).toBe('1. a\n   - b|\n2. c')
    expect(pressRange('1. a\n[- b\n2. c]')).toBe('1. a\n   [- b\n   2. c]')
  })

  it('counts a numbered item under a bullet from 1 and closes the gap below', () => {
    expect(press('- a\n- b\n1. c|\n2. d')).toBe('- a\n- b\n  1. c|\n1. d')
  })
})

describe('listIndentEdit: numbering a selection that changes level', () => {
  it('Tab counts the selected siblings from 1 and closes the gap below', () => {
    expect(pressRange('1. a\n[2. b\n3. c]\n4. d')).toBe('1. a\n   [1. b\n   2. c]\n2. d')
  })

  it('Tab leaves the numbers of selected children', () => {
    expect(pressRange('1. a\n[2. b\n   1. x\n   2. y\n3. c]\n4. d'))
      .toBe('1. a\n   [1. b\n      1. x\n      2. y\n   2. c]\n2. d')
  })

  it('Shift+Tab counts the selected siblings after the parent level', () => {
    expect(pressRange('1. a\n[   1. b\n   2. c]\n   3. d\n2. e', true)).toBe('1. a\n[2. b\n3. c]\n   1. d\n4. e')
  })

  it('renumbers the unselected line a selection ends at the start of', () => {
    expect(pressRange('1. a\n[2. b\n]3. c')).toBe('1. a\n   [1. b\n]2. c')
  })

  it('stops counting at a selected sibling that is off the count', () => {
    expect(pressRange('1. a\n[2. b\n7. c\n8. d]\n9. e')).toBe('1. a\n   [1. b\n   7. c\n   8. d]\n9. e')
  })

  it('does not renumber a selected line as an item below another selected one', () => {
    expect(pressRange('1. a\n   1. b\n      [1. c\n   2. d]', true)).toBe('1. a\n   1. b\n   [2. c\n2. d]')
  })

  // Shift+Tab cannot move a line with no indentation; such a line is counted
  // like an unselected one.
  it('counts a selected line that cannot move as an item below the run', () => {
    expect(pressRange('1. a\n   1. b\n   [2. c\n2. d]', true)).toBe('1. a\n   1. b\n[2. c\n3. d]')
    expect(pressRange('[1. a\n   1. b\n   2. c\n2. d]', true)).toBe('[1. a\n2. b\n3. c\n4. d]')
  })

  it('counts on from the numbers this key press already gave the lines above', () => {
    expect(pressRange('1. a\n   1. x\n   [2. y\n2. b]\n3. c')).toBe('1. a\n   1. x\n      [1. y\n   2. b]\n2. c')
    expect(pressRange('1. a\n   1. b\n   [2. c\n2. d\n   1. f]', true)).toBe('1. a\n   1. b\n[2. c\n3. d\n4. f]')
  })

  // The items below are read as the user typed them, whatever number this key
  // press gave the item they follow.
  it('moves the items below a later run that the user counted from the line above', () => {
    expect(pressRange('1. a\n   1. b\n   [2. c\n2. d\n   1. f]\n3. e', true)).toBe('1. a\n   1. b\n[2. c\n3. d\n4. f]\n5. e')
    expect(pressRange('1. a\n      [2. x\n   1. q]\n2. e', true)).toBe('1. a\n[2. x\n3. q]\n4. e')
    expect(pressRange('1) a\n      [2. x\n   1. q]\n2) e', true)).toBe('1) a\n[1. x\n2. q]\n2) e')
  })

  // A child nests only at or past its parent's text column, which moves when
  // the parent's number gains or loses a digit.
  it('keeps selected children on the text column when the number changes width', () => {
    expect(pressRange('9. x\n   [1. a\n      1. c]', true)).toBe('9. x\n[10. a\n    1. c]')
    expect(pressRange(pressRange('9. x\n[10. a\n    1. c\n       - d]')!, true)).toBe('9. x\n[10. a\n    1. c\n       - d]')
    // A number that loses more columns than Tab adds takes the rest from the child's own indentation.
    expect(pressRange('- a\n[1000000. b\n         1. c]')).toBe('- a\n  [1. b\n     1. c]')
  })
})

// The items below are renumbered by the rule Enter uses, which keeps the lines
// nested under an item on its text column when its number changes width.
describe('listIndentEdit: lines nested under an item below whose number changes width', () => {
  it('Shift+Tab moves them in when the item below gains a digit', () => {
    expect(press('8. h\n   1. x|\n9. i\n   - ii', true)).toBe('8. h\n9. x|\n10. i\n    - ii')
    expect(press('98. h\n    1. x|\n99. i\n    1. ii\n       - deep\ntext', true))
      .toBe('98. h\n99. x|\n100. i\n     1. ii\n        - deep\ntext')
  })

  it('Tab moves them back when the item below loses a digit', () => {
    expect(press('8. h\n9. x|\n10. i\n    - ii\n    wrapped')).toBe('8. h\n   1. x|\n9. i\n   - ii\n   wrapped')
  })

  it('round-trips', () => {
    expect(press(press('8. h\n9. x|\n10. i\n    - ii')!, true)).toBe('8. h\n9. x|\n10. i\n    - ii')
  })

  it('leaves them when the number keeps its width', () => {
    expect(press('1. h\n   1. x|\n2. i\n   - ii', true)).toBe('1. h\n2. x|\n3. i\n   - ii')
  })
})

describe('hasListLine', () => {
  it('is true when any line of the draft is a list line', () => {
    expect(hasListLine('intro\n  - item')).toBe(true)
    expect(hasListLine('plain\ntext')).toBe(false)
    expect(hasListLine('')).toBe(false)
  })
})
