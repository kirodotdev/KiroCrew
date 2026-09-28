// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

// An MD_COMPONENTS override rebuilds its element to attach a className, and a
// rebuild forwards only what it names. Naming just `sp(node)` dropped every
// attribute the sanitize schema (TAG_ATTRS) had already decided was safe — so
// `<ol start>` renumbered a fence-split step list back to 1, and a raw-HTML
// table with `colspan` was admitted by sanitize and then flattened by the
// override. `spa` derives the forward from that same table.
//
// These tests are the anti-drift half: they assert the attributes reach the
// DOM for every override that has a TAG_ATTRS entry, so re-introducing a
// hand-written forward (or a bare `sp`) in one of them fails here.

describe('MarkdownRenderer — overrides forward sanitize-admitted attributes', () => {
  it('keeps colspan / rowspan / scope on table cells', () => {
    const html = [
      '<table>',
      '<thead><tr><th colspan="2" scope="col">Span</th></tr></thead>',
      '<tbody><tr><td rowspan="2">Tall</td><td>Plain</td></tr></tbody>',
      '</table>',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={html} />)
    const th = container.querySelector('th')
    const td = container.querySelector('td')
    expect(th).not.toBeNull()
    expect(th!.getAttribute('colspan')).toBe('2')
    expect(th!.getAttribute('scope')).toBe('col')
    expect(td!.getAttribute('rowspan')).toBe('2')
    // The override's own styling is untouched by the forward.
    expect(th!.className).toContain('border-b')
    expect(td!.className).toContain('px-3')
  })

  it('keeps an explicit value on a list item', () => {
    const { container } = render(<MarkdownRenderer content={'<ol><li value="5">five</li><li>six</li></ol>'} />)
    const li = container.querySelector('li')
    expect(li!.getAttribute('value')).toBe('5')
    expect(li!.className).toContain('leading-relaxed')
  })

  it('never forwards class from the source, so the override owns styling', () => {
    // `class` is globally admitted by the sanitize schema, so it survives into
    // the hast node — but the override sets className itself and must win.
    const { container } = render(<MarkdownRenderer content={'<ol><li class="attacker">x</li></ol>'} />)
    const li = container.querySelector('li')
    expect(li!.className).toContain('leading-relaxed')
    expect(li!.className).not.toContain('attacker')
  })

  it('omits a boolean attribute that is not set rather than forwarding false', () => {
    const { container } = render(<MarkdownRenderer content={'1. plain\n2. list'} />)
    const ol = container.querySelector('ol')
    expect(ol!.hasAttribute('reversed')).toBe(false)
    expect(ol!.hasAttribute('type')).toBe(false)
  })

  it('forwards reversed when it is set', () => {
    const { container } = render(<MarkdownRenderer content={'<ol reversed><li>a</li><li>b</li></ol>'} />)
    const ol = container.querySelector('ol')
    expect(ol!.hasAttribute('reversed')).toBe(true)
  })
})
