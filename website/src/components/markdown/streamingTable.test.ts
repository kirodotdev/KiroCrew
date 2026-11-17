import { describe, expect, it } from 'vitest'

import { deferIncompleteStreamingTable, holdPartialTableRow } from './streamingTable'

const HEAD = '| Name | Type |\n| --- | --- |\n'
/** What the streaming tail block draws for `text`. */
const shown = (text: string) => holdPartialTableRow(deferIncompleteStreamingTable(text))

describe('holdPartialTableRow', () => {
  it('holds back the row being typed under a table', () => {
    expect(holdPartialTableRow(HEAD + '| a')).toBe(HEAD)
    expect(holdPartialTableRow(HEAD + '| a | string |')).toBe(HEAD)
    expect(holdPartialTableRow(HEAD + '| a | string |\n| b | num')).toBe(HEAD + '| a | string |\n')
  })

  it('returns the same text for every character of one row', () => {
    const row = '| `width` | number |'
    const seen = new Set<string>()
    for (let i = 1; i <= row.length; i++) seen.add(shown(HEAD + '| a | string |\n' + row.slice(0, i)))
    expect([...seen]).toEqual([HEAD + '| a | string |\n'])
    // The row's line break releases it, and the next row starts from that text.
    expect(shown(HEAD + '| a | string |\n' + row + '\n')).toBe(HEAD + '| a | string |\n' + row + '\n')
    expect(shown(HEAD + '| a | string |\n' + row + '\n|')).toBe(HEAD + '| a | string |\n' + row + '\n')
  })

  it('redraws a table once per row while its text arrives one character at a time', () => {
    const rows = ['| a | string |', '| b | number |', '| c | boolean |']
    const body = rows.join('\n') + '\n'
    const seen = new Set<string>()
    for (let i = 0; i <= body.length; i++) seen.add(shown(HEAD + body.slice(0, i)))
    // The bare header, then one text per completed row.
    expect(seen.size).toBe(rows.length + 1)
  })

  it('holds a row under a table that follows a paragraph, indented or not', () => {
    const lead = 'The options:\n\n'
    expect(holdPartialTableRow(lead + HEAD + '| a | str')).toBe(lead + HEAD)
    const indented = '  | Name | Type |\n  | --- | --- |\n'
    expect(holdPartialTableRow(indented + '  | a')).toBe(indented)
  })

  it('leaves text that ends on a line break, or has one line, alone', () => {
    expect(holdPartialTableRow(HEAD)).toBe(HEAD)
    expect(holdPartialTableRow(HEAD + '| a | string |\n')).toBe(HEAD + '| a | string |\n')
    expect(holdPartialTableRow('| Name | Type |')).toBe('| Name | Type |')
    expect(holdPartialTableRow('')).toBe('')
  })

  it('leaves a line that is not a bordered row under a table alone', () => {
    expect(holdPartialTableRow(HEAD + 'plain text')).toBe(HEAD + 'plain text')
    // A blank line ended the table: the next line starts a new block.
    expect(holdPartialTableRow(HEAD + '| a | string |\n\n| Other')).toBe(HEAD + '| a | string |\n\n| Other')
  })

  it('leaves a pipe line under anything but a table with its delimiter row alone', () => {
    // The delimiter row itself, still being typed.
    expect(holdPartialTableRow('| Name | Type |\n| --- | -')).toBe('| Name | Type |\n| --- | -')
    // Bordered lines with no delimiter row are not a table.
    expect(holdPartialTableRow('| a | b |\n| c | d |\n| e')).toBe('| a | b |\n| c | d |\n| e')
    // A delimiter row with no header above it is not a table either.
    expect(holdPartialTableRow('| --- | --- |\n| a')).toBe('| --- | --- |\n| a')
    // Prose, then a line that merely starts with a pipe.
    expect(holdPartialTableRow('Run it like this:\n| grep x')).toBe('Run it like this:\n| grep x')
  })
})

describe('deferIncompleteStreamingTable', () => {
  it('withholds a header run until its delimiter row arrives', () => {
    expect(deferIncompleteStreamingTable('Intro\n\n| Name | Type |')).toBe('Intro\n')
    expect(deferIncompleteStreamingTable('| Name | Type |\n---')).toBe('')
    expect(deferIncompleteStreamingTable(HEAD + '| a')).toBe(HEAD + '| a')
  })

  it('leaves prose with inline pipes alone', () => {
    expect(deferIncompleteStreamingTable('Run `cmd | grep x | wc` to count.')).toBe('Run `cmd | grep x | wc` to count.')
  })
})
