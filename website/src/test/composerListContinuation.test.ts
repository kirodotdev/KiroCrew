import { describe, expect, it } from 'vitest'
import { listLineBreakEdit, type ListLineBreakEdit } from '../components/composerListContinuation'

function apply(value: string, caret: number, edit: ListLineBreakEdit | null): string {
  if (!edit) return `${value.slice(0, caret)}\n${value.slice(caret)}`
  return value.slice(0, edit.start) + edit.insert + value.slice(edit.end)
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
    expect(breakAt('2. alpha |beta\n3. gamma')).toBe('2. alpha \n3. beta\n3. gamma')
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
