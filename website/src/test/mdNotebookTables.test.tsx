/**
 * Tests for markdown tables in the Notes app.
 *
 * Two halves, because the feature has two failure modes: the parser deciding
 * that prose containing pipes is a table (or missing a real one), and the
 * renderer breaking the click-to-edit contract by mapping the table to the
 * wrong source lines.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { parseTable, splitTableRow, tableAlignments } from '../apps/md-notebook/utils'
import { Preview } from '../apps/md-notebook/Preview'

const SIMPLE = ['| Service | Region |', '| --- | --- |', '| S3 | eu-west-1 |']

function renderPreview(content: string, onStartEdit = vi.fn()) {
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return onStartEdit
}

describe('md-notebook/splitTableRow', () => {
  it('drops the optional leading and trailing pipes', () => {
    expect(splitTableRow('| a | b |')).toEqual(['a', 'b'])
    expect(splitTableRow('a | b')).toEqual(['a', 'b'])
  })

  it('keeps an escaped pipe inside the cell instead of splitting on it', () => {
    expect(splitTableRow('| a \\| b | c |')).toEqual(['a | b', 'c'])
  })

  it('keeps interior empty cells', () => {
    expect(splitTableRow('| a |  | c |')).toEqual(['a', '', 'c'])
  })
})

describe('md-notebook/tableAlignments', () => {
  it('reads the colons as alignment', () => {
    expect(tableAlignments('| :-- | :-: | --: | --- |', 4)).toEqual([
      'left',
      'center',
      'right',
      null,
    ])
  })

  it('refuses a delimiter row whose width differs from the header', () => {
    expect(tableAlignments('| --- | --- |', 3)).toBeNull()
  })

  it('refuses cells that are not made of hyphens and colons', () => {
    expect(tableAlignments('| --- | x |', 2)).toBeNull()
  })
})

describe('md-notebook/parseTable', () => {
  it('reads header, alignment and body rows', () => {
    const t = parseTable(SIMPLE, 0)
    expect(t).not.toBeNull()
    expect(t!.header).toEqual(['Service', 'Region'])
    expect(t!.rows).toEqual([['S3', 'eu-west-1']])
    expect(t!.end).toBe(2)
  })

  it('stops at the first blank line and reports the last line it used', () => {
    const t = parseTable([...SIMPLE, '| EC2 | eu-west-3 |', '', 'after'], 0)
    expect(t!.rows).toHaveLength(2)
    expect(t!.end).toBe(3)
  })

  it('pads a short row and drops the overflow of a long one', () => {
    const t = parseTable(['| a | b | c |', '| - | - | - |', '| 1 |', '| 1 | 2 | 3 | 4 |'], 0)
    expect(t!.rows).toEqual([
      ['1', '', ''],
      ['1', '2', '3'],
    ])
  })

  it('is not fooled by prose that merely contains a pipe', () => {
    expect(parseTable(['a | b', 'plain text'], 0)).toBeNull()
    expect(parseTable(['no pipe here', '| --- | --- |'], 0)).toBeNull()
  })

  it('leaves a bare rule under a line of prose alone', () => {
    // `---` carries no pipe, so it stays a horizontal rule rather than turning
    // the paragraph above it into a one-column table.
    expect(parseTable(['a | b', '---'], 0)).toBeNull()
  })

  it('returns null at the end of the note where no delimiter row can follow', () => {
    expect(parseTable(['| a | b |'], 0)).toBeNull()
  })
})

describe('md-notebook/Preview tables', () => {
  it('renders a table instead of the literal pipes', () => {
    renderPreview(SIMPLE.join('\n'))
    expect(screen.getByRole('table')).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Service' })).toBeTruthy()
    expect(screen.getByRole('cell', { name: 'eu-west-1' })).toBeTruthy()
    expect(screen.queryByText(/\| --- \|/)).toBeNull()
  })

  it('applies the requested column alignment', () => {
    renderPreview(['| a | b |', '| :-: | --: |', '| 1 | 2 |'].join('\n'))
    expect(screen.getByRole('columnheader', { name: 'a' })).toHaveStyle({ textAlign: 'center' })
    expect(screen.getByRole('cell', { name: '2' })).toHaveStyle({ textAlign: 'right' })
  })

  it('renders inline markup inside cells, wikilinks included', () => {
    renderPreview(['| a | b |', '| - | - |', '| **bold** | [[Note]] |'].join('\n'))
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('Note')).toBeTruthy()
  })

  it('opens the whole table source when clicked, not just the header line', async () => {
    const onStartEdit = renderPreview(`intro\n${SIMPLE.join('\n')}`)
    await userEvent.click(screen.getByRole('table'))
    expect(onStartEdit).toHaveBeenCalledWith(1, 3)
  })

  it('leaves a fenced code block containing pipes as code', () => {
    renderPreview(['```', '| a | b |', '| - | - |', '```'].join('\n'))
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.getByText(/\| a \| b \|/)).toBeTruthy()
  })

  it('keeps rendering the blocks that follow the table', () => {
    renderPreview([...SIMPLE, '', '# After'].join('\n'))
    expect(screen.getByRole('heading', { name: 'After' })).toBeTruthy()
  })
})
