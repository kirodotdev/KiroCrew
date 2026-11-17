import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

/**
 * A streaming table is drawn one whole row at a time, and a frame that leaves
 * the drawn text unchanged does not parse the block again.
 *
 * react-markdown is wrapped, not replaced: the wrapper counts its renders (one
 * render is one parse of the block's text) and then renders the real component,
 * so the DOM assertions below read real output.
 */
let parses = 0
vi.mock('react-markdown', async importOriginal => {
  const actual = await importOriginal<typeof import('react-markdown')>()
  const Counted = (props: Parameters<typeof actual.default>[0]) => {
    parses++
    return createElement(actual.default, props)
  }
  return { ...actual, default: Counted }
})

import MarkdownRenderer from '../components/MarkdownRenderer'

afterEach(() => { parses = 0 })

const STREAM = { streaming: true, glow: true, smooth: true } as const
const TABLE = '| Name | Type |\n| --- | --- |\n| alpha | string |\n'
const bodyRows = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('tbody tr')).map((tr) => (tr.textContent || '').replace(/\s+/g, ' ').trim())

describe('streaming table rows', () => {
  it('does not draw the row being typed until its line break arrives', () => {
    const { container, rerender } = render(<MarkdownRenderer content={TABLE + '| be'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring'])
    rerender(<MarkdownRenderer content={TABLE + '| beta | number |'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring'])
    rerender(<MarkdownRenderer content={TABLE + '| beta | number |\n'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
  })

  it('does not parse the table again for the characters of the row being typed', () => {
    const { rerender } = render(<MarkdownRenderer content={TABLE + '|'} {...STREAM} />)
    const before = parses
    const row = '| beta | number |'
    for (let i = 2; i <= row.length; i++) rerender(<MarkdownRenderer content={TABLE + row.slice(0, i)} {...STREAM} />)
    expect(parses).toBe(before)
    // The completed row is parsed, once.
    rerender(<MarkdownRenderer content={TABLE + row + '\n'} {...STREAM} />)
    expect(parses).toBe(before + 1)
  })

  it('still parses prose on every change, so its text keeps arriving by the character', () => {
    const { container, rerender } = render(<MarkdownRenderer content={'The first wor'} {...STREAM} />)
    const before = parses
    rerender(<MarkdownRenderer content={'The first word'} {...STREAM} />)
    rerender(<MarkdownRenderer content={'The first words'} {...STREAM} />)
    expect(parses).toBe(before + 2)
    expect(container.textContent).toContain('The first words')
  })

  it('draws every row once the stream has ended, line break or not', () => {
    const { container, rerender } = render(<MarkdownRenderer content={TABLE + '| beta | number |'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring'])
    rerender(<MarkdownRenderer content={TABLE + '| beta | number |'} streaming={false} glow={false} smooth />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
  })

  it('draws a table that is not streaming whole', () => {
    const { container } = render(<MarkdownRenderer content={TABLE + '| beta | number |'} />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
  })

  it('draws the last row of a table whose block a code fence closed, while the fence streams', () => {
    // The assembler closes the markdown block at the fence and joins its lines
    // with no trailing line break. It is still the last markdown block, but no
    // more text will reach it, so nothing is held back.
    const { container, rerender } = render(<MarkdownRenderer content={TABLE + '| beta | number |\n```py\nx = 1'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
    rerender(<MarkdownRenderer content={TABLE + '| beta | number |\n```py\nx = 1\ny = 2'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
  })

  it('draws the last row of a table whose block a widget closed, while the widget streams', () => {
    const { container } = render(<MarkdownRenderer content={TABLE + '| beta | number |\n<mcwidget title="Board">\n<div>'} {...STREAM} />)
    expect(bodyRows(container)).toEqual(['alphastring', 'betanumber'])
  })

  it('draws a header line that a code fence closed before any delimiter row', () => {
    // No delimiter row can arrive any more, so the line is drawn as the text it is.
    const { container } = render(<MarkdownRenderer content={'| Name | Type |\n```py\nx = 1'} {...STREAM} />)
    expect(container.textContent).toContain('| Name | Type |')
  })
})
