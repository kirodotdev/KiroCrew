/**
 * FrontmatterCard — the metadata strip shown above a markdown file preview when
 * the file opens with a YAML frontmatter block (#22792). It renders the parsed
 * key/value entries, pluralizes its heading by field count through the i18n
 * catalog, collapses on click, and shows a symbol for an empty value so a blank
 * line is not mistaken for a missing key.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { FrontmatterCard } from '../components/markdown/FrontmatterCard'
import '../i18n/all'

describe('FrontmatterCard', () => {
  it('renders nothing when there are no entries', () => {
    const { container } = render(<FrontmatterCard entries={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('lists each key/value and pluralizes the heading by field count', () => {
    render(
      <FrontmatterCard
        entries={[
          { key: 'okf_version', value: '0.1' },
          { key: 'tags', value: 'retrospective, reliability' },
        ]}
      />,
    )
    expect(screen.getByRole('button', { name: /2 fields/i })).toBeTruthy()
    const strip = screen.getByTestId('md-frontmatter')
    expect(within(strip).getByText('okf_version')).toBeTruthy()
    expect(within(strip).getByText('retrospective, reliability')).toBeTruthy()
  })

  it('uses the singular heading for exactly one field', () => {
    render(<FrontmatterCard entries={[{ key: 'title', value: 'Hello' }]} />)
    expect(screen.getByRole('button', { name: /1 field(?!s)/i })).toBeTruthy()
  })

  it('collapses and expands the entries when the header is clicked', () => {
    render(<FrontmatterCard entries={[{ key: 'title', value: 'Hello' }]} />)
    const toggle = screen.getByRole('button')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByText('title')).toBeTruthy()
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('title')).toBeNull()
  })

  it('shows a symbol marker for an empty value instead of a blank cell', () => {
    render(<FrontmatterCard entries={[{ key: 'draft', value: '' }]} />)
    expect(screen.getByText('∅')).toBeTruthy()
  })
})
