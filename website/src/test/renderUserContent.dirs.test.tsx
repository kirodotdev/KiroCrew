import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'

const noop = () => {}

/** The folder chip carries the full path in its title — that title is the
 *  stable hook these tests select on. */
function dirChip(container: HTMLElement, fullPath: string): HTMLElement | null {
  return container.querySelector(`[title="${fullPath}"]`)
}

describe('renderUserContent — folder references', () => {
  it('fresh message: @rel/ token + meta.dirs renders a folder chip, not plain text', () => {
    const { container } = render(
      <>{renderUserContent('look in @src/pages/ please', { dirs: ['/repo/src/pages'] }, noop)}</>,
    )
    const chip = dirChip(container, '/repo/src/pages')
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveTextContent('@src/pages/')
  })

  it('clicking the folder chip opens the directory via onFolderOpen', () => {
    const onFolderOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent('look in @src/pages/ please', { dirs: ['/repo/src/pages'] }, noop, onFolderOpen)}</>,
    )
    const chip = dirChip(container, '/repo/src/pages')
    // Clickable contract: a real focusable button, not a div with a listener.
    expect(chip?.getAttribute('role')).toBe('button')
    fireEvent.click(chip!)
    expect(onFolderOpen).toHaveBeenCalledWith('/repo/src/pages')
  })

  it('degrades to an inert chip when no folder handler is provided', () => {
    const { container } = render(
      <>{renderUserContent('look in @src/pages/ please', { dirs: ['/repo/src/pages'] }, noop)}</>,
    )
    expect(dirChip(container, '/repo/src/pages')?.getAttribute('role')).toBeNull()
  })

  it('history replay: [attached_dir N] marker resolves to a chip and never leaks raw', () => {
    const { container } = render(
      <>{renderUserContent('look in [attached_dir 1] /repo/docs please', undefined, noop)}</>,
    )
    expect(container.textContent).not.toContain('[attached_dir')
    const chip = dirChip(container, '/repo/docs')
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveTextContent('@docs/')
  })

  it('meta.dirs wins over the marker fallback for path resolution', () => {
    const { container } = render(
      <>{renderUserContent('see [attached_dir 1] /repo/my docs now', { dirs: ['/repo/my docs'] }, noop)}</>,
    )
    // Lossless: the spaced path resolves via the meta index, not the \S+ cut.
    expect(dirChip(container, '/repo/my docs')).toBeInTheDocument()
  })

  it('file and folder references coexist and resolve to their own paths', () => {
    const { container } = render(
      <>{renderUserContent(
        'compare [attached_file 1] /repo/a.ts with [attached_dir 1] /repo/docs now',
        { files: ['/repo/a.ts'], dirs: ['/repo/docs'] },
        noop,
      )}</>,
    )
    expect(dirChip(container, '/repo/docs')).toBeInTheDocument()
    const fileChip = container.querySelector('[title="/repo/a.ts"]')
    expect(fileChip).toBeInTheDocument()
    expect(container.textContent).not.toContain('[attached_file')
    expect(container.textContent).not.toContain('[attached_dir')
  })

  it('renders the folder chip in a segment adjacent to a paste token', () => {
    const pastes = [{ id: 'p1', seq: 1, lines: 5, content: 'line1\nline2\nline3\nline4\nline5' }]
    const { container } = render(
      <>{renderUserContent(
        '[ Paste #1 · 5 lines ]\ncheck [attached_dir 1] /repo/docs too',
        { pastes, dirs: ['/repo/docs'] },
        noop,
      )}</>,
    )
    expect(dirChip(container, '/repo/docs')).toBeInTheDocument()
    expect(container.textContent).not.toContain('[attached_dir')
  })

  it('a plain sentence ending a clause with slash-less mentions is untouched', () => {
    const { container } = render(
      <>{renderUserContent('email a@b.c/ and read @notes.md now', undefined, noop)}</>,
    )
    expect(container.querySelector('[title]')).toBeNull()
  })
})
