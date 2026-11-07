/**
 * The frontmatter strip must render OUTSIDE the preview root (`previewRef`).
 *
 * Comment capture (`anchorFromRange`) and highlight lookup (`findBestOccurrence`)
 * both measure text offsets under that root. When the strip lived inside it,
 * collapsing the strip removed its rows from the offset space, so a comment on
 * a heading that repeats a frontmatter value was redrawn on the wrong occurrence.
 */
import { describe, it, expect } from 'vitest'
import { createRef } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { ContentRenderer } from '../components/ContentRenderer'
import '../i18n/all'

const DOC = ['---', 'title: Hello', 'tags: [a, b]', '---', '# Hello', '', 'Hello'].join('\n')

function preview() {
  const previewRef = createRef<HTMLElement>()
  render(
    <ContentRenderer
      isRichType={false}
      fileType="markdown"
      filePath="/w/notes.md"
      content={DOC}
      editing={false}
      lang="markdown"
      lineNums={false}
      wordWrap
      onChange={() => {}}
      previewRef={previewRef}
      displayContent={DOC}
      isMarkdown
    />,
  )
  return previewRef
}

describe('ContentRenderer frontmatter strip and the comment offset root', () => {
  it('renders the strip outside the preview root', () => {
    const root = preview().current!
    const strip = screen.getByTestId('md-frontmatter')
    expect(root).toBeTruthy()
    expect(root.contains(strip)).toBe(false)
    expect(root.textContent).not.toContain('tags')
  })

  it('keeps the preview root text identical when the strip collapses', () => {
    const root = preview().current!
    const before = root.textContent
    fireEvent.click(screen.getByRole('button', { expanded: true }))
    expect(screen.getByRole('button', { expanded: false })).toBeTruthy()
    expect(root.textContent).toBe(before)
  })
})
