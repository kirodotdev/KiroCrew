import { describe, expect, it } from 'vitest'
import { applyListEdit, listLineBreakEdit, listMarkerBackspaceEdit, type ListLineBreakEdit } from '../components/composerListContinuation'

function apply(value: string, caret: number, edit: ListLineBreakEdit | null): string {
  if (!edit) return `${value.slice(0, caret)}\n${value.slice(caret)}`
  return applyListEdit(value, edit)
}

// Break at the `|` in `marked`, the way the composer would.
function breakAt(marked: string): string {
  const caret = marked.indexOf('|')
  const value = marked.replace('|', '')
  return apply(value, caret, listLineBreakEdit(value, caret))
}

describe('listLineBreakEdit', () => {
  it.each([
    ['- item|', '- item\n- '],
    ['* item|', '* item\n* '],
    ['+ item|', '+ item\n+ '],
    ['  - nested|', '  - nested\n  - '],
    ['\t- tabbed|', '\t- tabbed\n\t- '],
    ['-   wide gap|', '-   wide gap\n-   '],
  ])('continues bullet %j with the same bullet and indent', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it.each([
    ['3. step|', '3. step\n4. '],
    ['3) step|', '3) step\n4) '],
    ['9. step|', '9. step\n10. '],
    ['09. step|', '09. step\n10. '],
    ['007) step|', '007) step\n008) '],
    ['   1. indented|', '   1. indented\n   2. '],
  ])('continues ordered %j with the next number', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it('repeats an ordinal too large for a safe integer', () => {
    expect(breakAt('9007199254740993. big|')).toBe('9007199254740993. big\n9007199254740993. ')
  })

  it.each([
    ['- [ ] todo|', '- [ ] todo\n- [ ] '],
    ['- [x] done|', '- [x] done\n- [ ] '],
    ['- [X] done|', '- [X] done\n- [ ] '],
    ['1. [x] done|', '1. [x] done\n2. [ ] '],
  ])('continues task item %j unchecked', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it.each([
    ['a\n- |', 'a\n'],
    ['a\n-   |', 'a\n'],
    ['a\n  * |', 'a\n'],
    ['a\n4. |', 'a\n'],
    ['a\n- [ ] |', 'a\n'],
    ['a\n- [x]|', 'a\n'],
    ['- |\nnext', '\nnext'],
  ])('ends the list on empty item %j', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it('deletes the whole empty item and puts the caret at its line start', () => {
    expect(listLineBreakEdit('a\n  - ', 6)).toEqual({ start: 2, end: 6, insert: '' })
  })

  it('splits an item mid-line, moving the tail to the next item', () => {
    expect(breakAt('- alpha |beta')).toBe('- alpha \n- beta')
    expect(breakAt('2. alpha |beta\n3. gamma')).toBe('2. alpha \n3. beta\n4. gamma')
  })

  it.each([
    ['|- item'],
    ['-| item'],
    ['  |- item'],
    ['12|. item'],
    ['- [|x] item'],
  ])('does not continue with the caret inside the marker %j', input => {
    const caret = input.indexOf('|')
    expect(listLineBreakEdit(input.replace('|', ''), caret)).toBeNull()
  })

  it.each([
    ['plain text|'],
    ['-no space|'],
    ['2024.|'],
    ['-|'],
    ['1.5 apples|'],
    ['text - not a list|'],
    ['- one\nplain|'],
  ])('does not continue a non-list line %j', input => {
    const caret = input.indexOf('|')
    expect(listLineBreakEdit(input.replace('|', ''), caret)).toBeNull()
  })

  it('does not continue with the caret inside a mention token', () => {
    expect(listLineBreakEdit('- see @src/fo|o.ts'.replace('|', ''), 13)).toBeNull()
    expect(listLineBreakEdit('- run $sk|ill'.replace('|', ''), 9)).toBeNull()
    expect(breakAt('- see (@src/fo|o.ts)')).toBe('- see (@src/fo\no.ts)')
    expect(listLineBreakEdit('- see (@src/fo|o.ts)'.replace('|', ''), 14)).toBeNull()
    expect(listLineBreakEdit('- run "$sk|ill"'.replace('|', ''), 10)).toBeNull()
    // At the end of the token the caret is outside it.
    expect(breakAt('- see @src/foo.ts|')).toBe('- see @src/foo.ts\n- ')
  })

  it('continues when a sigil does not start a mention token', () => {
    expect(breakAt('- email a@b.c|om')).toBe('- email a@b.c\n- om')
    expect(breakAt('- set $PA|TH')).toBe('- set $PA\n- TH')
    // Mention resolution needs the sigil at the word start or after one opening wrapper.
    expect(breakAt('- see x(@src/fo|o')).toBe('- see x(@src/fo\n- o')
  })

  it('handles a long unbroken run on a list line', () => {
    // A raw paste (Cmd+Shift+V) can put an unbroken ~200k-character run on a
    // list line, with the caret inside the run or in a word after it.
    const run = 'x'.repeat(200_000)
    const midRun = `- ${run}`
    const midCaret = 2 + run.length / 2
    const afterRun = `- ${run} @src/foo.ts`
    const tokenCaret = afterRun.length - 3
    expect(listLineBreakEdit(midRun, midCaret)).toEqual({ start: midCaret, end: midCaret, insert: '\n- ' })
    expect(listLineBreakEdit(afterRun, tokenCaret)).toBeNull()
    expect(listLineBreakEdit(afterRun, afterRun.length)).toEqual({
      start: afterRun.length,
      end: afterRun.length,
      insert: '\n- ',
    })
  })

  it('does not continue with the caret inside a protected chip range', () => {
    const value = '- [ Paste #1 · 4 lines ]'
    expect(listLineBreakEdit(value, 5, [{ start: 2, end: value.length }])).toBeNull()
    expect(listLineBreakEdit(value, value.length, [{ start: 2, end: value.length }]))
      .toEqual({ start: value.length, end: value.length, insert: '\n- ' })
  })

  it('rejects an out-of-range caret', () => {
    expect(listLineBreakEdit('- a', -1)).toBeNull()
    expect(listLineBreakEdit('- a', 4)).toBeNull()
  })

  it('reads only the caret line of a multi-line value', () => {
    expect(breakAt('intro\n1. one|\ntrailing')).toBe('intro\n1. one\n2. \ntrailing')
  })
})

describe('listLineBreakEdit renumbers the items below a new one', () => {
  it.each([
    // The reported case: Return between the marker and the text.
    ['1. |foo\n2. bar', '1. \n2. foo\n3. bar'],
    ['1. foo|\n2. bar', '1. foo\n2. \n3. bar'],
    ['1. a|\n2. b\n3. c\n4. d', '1. a\n2. \n3. b\n4. c\n5. d'],
    ['3) a|\n4) b', '3) a\n4) \n5) b'],
    ['09. a|\n10. b', '09. a\n10. \n11. b'],
    ['8. a|\n9. b', '8. a\n9. \n10. b'],
    ['  1. a|\n  2. b', '  1. a\n  2. \n  3. b'],
    ['1. [x] a|\n2. [ ] b', '1. [x] a\n2. [ ] \n3. [ ] b'],
  ])('%j', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it.each([
    // Nested items and wrapped text are passed over, siblings after them move.
    ['1. a|\n   - sub\n   more\n2. b', '1. a\n2. \n   - sub\n   more\n3. b'],
    ['1. a|\n   1. sub\n   2. sub\n2. b', '1. a\n2. \n   1. sub\n   2. sub\n3. b'],
  ])('passes over deeper lines %j', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it.each([
    // A `1.` / `1.` list is numbered by the renderer, not the user: leave it.
    ['1. a|\n1. b\n1. c', '1. a\n2. \n1. b\n1. c'],
    // A deliberate jump ends the run.
    ['1. a|\n2. b\n7. c', '1. a\n2. \n3. b\n7. c'],
    // A blank line, a shallower line, another delimiter or a bullet ends it.
    ['1. a|\n2. b\n\n3. c', '1. a\n2. \n3. b\n\n3. c'],
    ['1. a|\ntext\n2. b', '1. a\n2. \ntext\n2. b'],
    ['1. a|\n2) b', '1. a\n2. \n2) b'],
    ['1. a|\n- b\n2. c', '1. a\n2. \n- b\n2. c'],
    ['  1. a|\n2. b', '  1. a\n  2. \n2. b'],
  ])('stops at the end of the counted run %j', (input, expected) => {
    expect(breakAt(input)).toBe(expected)
  })

  it('does not renumber bullets or the lines above the caret', () => {
    expect(breakAt('- a|\n- b')).toBe('- a\n- \n- b')
    expect(breakAt('1. x\n2. y\n3. a|\n4. b')).toBe('1. x\n2. y\n3. a\n4. \n5. b')
  })

  it('reports each number write as a digit span of the original value', () => {
    expect(listLineBreakEdit('1. a\n2. b\n3. c', 4)).toEqual({
      start: 4, end: 4, insert: '\n2. ',
      renumber: [{ start: 5, end: 6, insert: '3' }, { start: 10, end: 11, insert: '4' }],
    })
    expect(listLineBreakEdit('1. a\n- b', 4)).toEqual({ start: 4, end: 4, insert: '\n2. ' })
  })

  it('stops before a number inside a protected chip range', () => {
    const value = '1. a\n2. b\n3. c'
    expect(listLineBreakEdit(value, 4, [{ start: 10, end: 14 }])?.renumber).toEqual([
      { start: 5, end: 6, insert: '3' },
    ])
  })

  it('moves the items below up when Enter ends an empty item mid-list', () => {
    expect(breakAt('1. a\n2. |\n3. b\n4. c')).toBe('1. a\n\n2. b\n3. c')
    expect(breakAt('1. a\n2. |\n7. b')).toBe('1. a\n\n7. b')
    expect(breakAt('- a\n- |\n- b')).toBe('- a\n\n- b')
  })
})

describe('ordinal increment', () => {
  it.each([
    ['1', '2'],
    ['9', '10'],
    ['99', '100'],
    ['09', '10'],
    ['007', '008'],
    ['0', '1'],
    ['9007199254740991', '9007199254740991'],
    ['99999999999999999999', '99999999999999999999'],
  ])('%s. -> %s.', (input, expected) => {
    expect(breakAt(`${input}. x|`)).toBe(`${input}. x\n${expected}. `)
  })
})

// Backspace at the `|` in `marked`, the way the composer would; returns the
// value and the caret after it.
function backspaceAt(marked: string): [string, number] {
  const caret = marked.indexOf('|')
  const value = marked.replace('|', '')
  const edit = listMarkerBackspaceEdit(value, caret)
  if (!edit) return [value.slice(0, caret - 1) + value.slice(caret), caret - 1]
  return [applyListEdit(value, edit), edit.start + edit.insert.length]
}

describe('listMarkerBackspaceEdit', () => {
  // Every marker the Enter key continues is cleared by ONE Backspace.
  it.each([
    ['1. a\n2. |', '1. a\n'],
    ['9) a\n10) |', '9) a\n'],
    ['09. a\n10. |', '09. a\n'],
    ['- a\n- |', '- a\n'],
    ['* a\n* |', '* a\n'],
    ['+ a\n+ |', '+ a\n'],
    ['- [x] a\n- [ ] |', '- [x] a\n'],
    ['1. [ ] a\n2. [ ] |', '1. [ ] a\n'],
    ['  - a\n  - |', '  - a\n'],
    ['\t1. a\n\t2. |', '\t1. a\n'],
    ['-   a\n-   |', '-   a\n'],
    ['- [ ]|', ''],
  ])('clears the empty item %j in one press', (input, expected) => {
    const [value, caret] = backspaceAt(input)
    expect(value).toBe(expected)
    expect(caret).toBe(expected.length)
  })

  it('keeps the text after the caret on an empty item line', () => {
    expect(backspaceAt('1. a\n2. |\nnext')).toEqual(['1. a\n\nnext', 5])
  })

  it.each([
    ['2. |text', 'text', 0],
    ['- [ ] |todo', 'todo', 0],
    ['  10) |deep', '  deep', 2],
    ['intro\n- |item\nend', 'intro\nitem\nend', 6],
  ])('removes only the marker of %j and keeps indent and text', (input, expected, caret) => {
    expect(backspaceAt(input)).toEqual([expected, caret])
  })

  it.each([
    ['2.| '],
    ['2|. '],
    ['- t|ext'],
    ['plain|'],
    ['2024.|'],
    ['|- a'],
    [' |- a'],
  ])('deletes one character for %j', input => {
    const caret = input.indexOf('|')
    expect(listMarkerBackspaceEdit(input.replace('|', ''), caret)).toBeNull()
  })

  it('does not act with the caret inside a protected chip range', () => {
    expect(listMarkerBackspaceEdit('- abc', 2, [{ start: 1, end: 4 }])).toBeNull()
  })

  it('rejects an out-of-range caret', () => {
    expect(listMarkerBackspaceEdit('- ', 0)).toBeNull()
    expect(listMarkerBackspaceEdit('- ', 3)).toBeNull()
  })

  it.each([
    ['1. a\n2. |\n3. b\n4. c', '1. a\n\n2. b\n3. c', 5],
    ['09. a\n10. |\n11. b', '09. a\n\n10. b', 6],
    ['  1. a\n  2. |\n  3. b', '  1. a\n\n  2. b', 7],
    // Off-count, other delimiter and bullets are left alone, as on Enter.
    ['1. a\n2. |\n7. b', '1. a\n\n7. b', 5],
    ['1. a\n2. |\n3) b', '1. a\n\n3) b', 5],
  ])('moves the items below up when clearing the empty item %j', (input, expected, caret) => {
    expect(backspaceAt(input)).toEqual([expected, caret])
  })

  it('leaves the numbers below when the marker of an item with text goes', () => {
    expect(backspaceAt('1. a\n2. |text\n3. b')).toEqual(['1. a\ntext\n3. b', 5])
  })
})

describe('Enter then Backspace', () => {
  // The two keys cancel out: an accidental Return, cleared, leaves the list as it was.
  it.each([
    ['1. foo|\n2. bar', '1. foo\n2. bar'],
    ['1. a|\n2. b\n3. c', '1. a\n2. b\n3. c'],
    ['8. a|\n9. b', '8. a\n9. b'],
  ])('%j', (input, expected) => {
    const caret = input.indexOf('|')
    const value = input.replace('|', '')
    const edit = listLineBreakEdit(value, caret)!
    const broken = applyListEdit(value, edit)
    const after = edit.start + edit.insert.length
    const [cleared, clearedCaret] = backspaceAt(`${broken.slice(0, after)}|${broken.slice(after)}`)
    // The second Backspace joins the blank line to the item above.
    const joined = cleared.slice(0, clearedCaret - 1) + cleared.slice(clearedCaret)
    expect(joined).toBe(expected)
  })
})
