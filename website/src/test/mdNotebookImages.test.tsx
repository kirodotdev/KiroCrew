/**
 * Tests for markdown images in the Notes app.
 *
 * Two halves, matching the feature's two failure modes: the resolver handing a
 * source to the browser it should have refused (or refusing one it should have
 * served), and the renderer losing the click-to-edit contract or swallowing the
 * `!` back into a link the way it did before this feature existed.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { noteDirPath, resolveNoteImageSrc } from '../apps/md-notebook/utils'
import { Preview } from '../apps/md-notebook/Preview'

function renderPreview(content: string, noteDir?: string, onStartEdit = vi.fn()) {
  render(
    <Preview
      content={content}
      noteDir={noteDir}
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

describe('md-notebook/resolveNoteImageSrc', () => {
  it('passes an https source through untouched', () => {
    expect(resolveNoteImageSrc('https://example.com/a.png')).toBe('https://example.com/a.png')
  })

  it('refuses a script source so a note cannot smuggle one into an img', () => {
    expect(resolveNoteImageSrc('javascript:alert(1)')).toBeNull()
  })

  it('refuses a data source rather than inlining note-supplied bytes', () => {
    expect(resolveNoteImageSrc('data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=')).toBeNull()
  })

  it('serves an absolute local path through the file endpoint', () => {
    expect(resolveNoteImageSrc('/Users/me/vault/a.png')).toBe(
      '/api/file-raw?path=%2FUsers%2Fme%2Fvault%2Fa.png',
    )
  })

  it('treats a Windows drive letter as a path, not a URL scheme', () => {
    expect(resolveNoteImageSrc('C:\\vault\\a.png')).toBe('/api/file-raw?path=C%3A%5Cvault%5Ca.png')
  })

  it('resolves a relative source against the directory of the note', () => {
    expect(resolveNoteImageSrc('assets/a.png', '/vault/aws')).toBe(
      '/api/file-raw?path=%2Fvault%2Faws%2Fassets%2Fa.png',
    )
  })

  it('drops a leading ./ instead of doubling it into the path', () => {
    expect(resolveNoteImageSrc('./a.png', '/vault')).toBe('/api/file-raw?path=%2Fvault%2Fa.png')
  })

  it('leaves a relative source unresolved when no note directory is known', () => {
    expect(resolveNoteImageSrc('assets/a.png')).toBeNull()
  })

  it('ignores an empty source', () => {
    expect(resolveNoteImageSrc('   ')).toBeNull()
  })
})

describe('md-notebook/noteDirPath', () => {
  it('joins the vault content root with the note folder', () => {
    expect(noteDirPath({ localPath: '/vault' }, 'aws/certs/note.md')).toBe('/vault/aws/certs')
  })

  it('honours the vault subfolder', () => {
    expect(noteDirPath({ localPath: '/vault', subfolder: 'docs' }, 'a/note.md')).toBe('/vault/docs/a')
  })

  it('returns the root for a note sitting at the top level', () => {
    expect(noteDirPath({ localPath: '/vault' }, 'note.md')).toBe('/vault')
  })

  it('has no directory without a vault or without a note', () => {
    expect(noteDirPath(null, 'note.md')).toBeUndefined()
    expect(noteDirPath({ localPath: '/vault' }, null)).toBeUndefined()
  })
})

describe('md-notebook/Preview images', () => {
  it('renders a remote image with its alt text', () => {
    renderPreview('![Architecture](https://example.com/arch.png)')
    const img = screen.getByAltText('Architecture')
    expect(img.getAttribute('src')).toBe('https://example.com/arch.png')
  })

  it('no longer leaves the bang behind as a link, the pre-feature behaviour', () => {
    renderPreview('![Architecture](https://example.com/arch.png)')
    expect(screen.queryByRole('link')).toBeNull()
    expect(document.body.textContent).not.toContain('!')
  })

  it('still renders an ordinary link, so the added group did not shift the others', () => {
    renderPreview('see [the doc](https://example.com/doc)')
    expect(screen.getByRole('link', { name: 'the doc' }).getAttribute('href')).toBe(
      'https://example.com/doc',
    )
  })

  it('serves a vault-relative image through the file endpoint', () => {
    renderPreview('![Diagram](assets/d.png)', '/vault/aws')
    expect(screen.getByAltText('Diagram').getAttribute('src')).toBe(
      '/api/file-raw?path=%2Fvault%2Faws%2Fassets%2Fd.png',
    )
  })

  it('falls back to the alt text when the source is refused', () => {
    renderPreview('![Broken](javascript:alert(1))')
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('Broken')).toBeTruthy()
  })

  it('falls back to the alt text when the image fails to load', () => {
    renderPreview('![Missing picture](https://example.com/gone.png)')
    fireEvent.error(screen.getByAltText('Missing picture'))
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText('Missing picture')).toBeTruthy()
  })

  it('falls back to the file name when the note gave no alt text', () => {
    renderPreview('![](https://example.com/deep/path/photo.png?v=2)')
    // An empty alt marks the image decorative, so it carries no img role and is
    // queried as an element: the query itself pins that accessibility choice.
    const img = document.querySelector('img')
    expect(img).not.toBeNull()
    fireEvent.error(img as HTMLImageElement)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('photo.png')).toBeTruthy()
  })

  it('marks a failed image as an image, not as prose', () => {
    // The muted colour alone cannot carry that meaning: alt text reads as a
    // sentence the author wrote. The glyph is what says an image failed.
    renderPreview('![Subnet allocation per zone](https://example.com/gone.png)')
    fireEvent.error(screen.getByAltText('Subnet allocation per zone'))
    const label = screen.getByText('Subnet allocation per zone')
    expect(label.querySelector('svg')).not.toBeNull()
  })

  it('marks a refused source the same way as one that failed to load', () => {
    // Both are one event to the reader: the picture is not there. A refused
    // source taking a different presentation would read as ordinary prose.
    renderPreview('![Untrusted](javascript:void 0)')
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('Untrusted').querySelector('svg')).not.toBeNull()
  })

  it('holds a height floor until the bytes decode, then releases it', () => {
    // A click in this app edits the line under the cursor, so an image that
    // lays out at nothing and then snaps to its natural height would move the
    // block being clicked. The floor bounds that shift; keeping it after load
    // would leave a small image padded forever.
    renderPreview('![Diagram](https://example.com/d.png)')
    const img = screen.getByAltText('Diagram') as HTMLImageElement
    expect(img.style.minHeight).toBe('120px')
    fireEvent.load(img)
    expect(img.style.minHeight).toBe('')
    expect(img.style.height).toBe('auto')
  })

  it('caps a tall image at one screenful', () => {
    renderPreview('![Whiteboard](https://example.com/tall.png)')
    const img = screen.getByAltText('Whiteboard') as HTMLImageElement
    expect(img.style.maxHeight).toBe('60vh')
    expect(img.style.objectFit).toBe('contain')
  })

  it('gives an SVG a definite width instead of a height floor', () => {
    // An SVG authored with only a viewBox has no intrinsic size and would
    // collapse under a max-width cap; the width basis lets the ratio derive it.
    renderPreview('![Chart](https://example.com/chart.svg)')
    const img = screen.getByAltText('Chart') as HTMLImageElement
    expect(img.style.width).toBe('100%')
    expect(img.style.minHeight).toBe('')
  })

  it('resolves an image inside a table cell', () => {
    renderPreview(
      ['| Shape | Picture |', '| --- | --- |', '| Flow | ![Flow](assets/f.png) |'].join('\n'),
      '/vault',
    )
    expect(screen.getByAltText('Flow').getAttribute('src')).toBe(
      '/api/file-raw?path=%2Fvault%2Fassets%2Ff.png',
    )
  })

  it('keeps click-to-edit on the line holding the image', async () => {
    const onStartEdit = renderPreview('![Diagram](assets/d.png)', '/vault')
    await userEvent.click(screen.getByAltText('Diagram'))
    expect(onStartEdit).toHaveBeenCalledWith(0, 0)
  })
})
