import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import MarkdownRenderer from '../components/MarkdownRenderer'

const PAKO_URL = 'https://mermaid.live/edit#pako:eNqrVkrOT0lVslJKy8kvT85ILCpRCHGJyVNwVNDVtVNwUqoFALkVCh0'

describe('Mermaid pako links', () => {
  it('keeps the validated address as the rendered link destination', () => {
    render(<MarkdownRenderer content={`[Open diagram](${PAKO_URL})`} />)

    expect(screen.getByRole('link', { name: 'Open diagram' })).toHaveAttribute('href', PAKO_URL)
  })

  it('keeps two links in source order without merging their fragments', () => {
    render(<MarkdownRenderer content={`[First](${PAKO_URL}) then [Second](${PAKO_URL})`} />)

    expect(screen.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      PAKO_URL,
      PAKO_URL,
    ])
  })

  it('keeps sentence punctuation outside a bare diagram link destination', () => {
    render(<MarkdownRenderer content={`See ${PAKO_URL}. Next.`} />)

    expect(screen.getByRole('link')).toHaveAttribute('href', PAKO_URL)
  })
})
