/**
 * Tests for Obsidian `![[file]]` image embeds in the Notes app.
 *
 * Three halves: the parser splitting target from size or alt, the resolver
 * choosing one file in a fixed order (and refusing to choose between two), and
 * the renderer showing the embed as an image while leaving standard markdown
 * images, links and wikilinks exactly as they were.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { isImageEmbed, parseEmbed, resolveEmbedSrc, resolveEmbedTarget } from '../apps/md-notebook/utils'
import type { EmbedContext } from '../apps/md-notebook/utils'
import { Preview } from '../apps/md-notebook/Preview'

const VAULT = '/vault'

/** A vault the way the page hands it to the renderer: root, note folder, Obsidian setting. */
function ctx(over: Partial<EmbedContext> = {}): EmbedContext {
  return { vaultRoot: VAULT, noteDir: `${VAULT}/customers/acme`, attachmentFolder: 'z-assets', ...over }
}

function fileUrl(rel: string): string {
  return `/api/file-raw?path=${encodeURIComponent(`${VAULT}/${rel}`)}`
}

function previewElement(content: string, embeds?: EmbedContext, onStartEdit = vi.fn()) {
  return (
    <Preview
      content={content}
      noteDir={embeds?.noteDir}
      embeds={embeds}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />
  )
}

function renderPreview(content: string, embeds?: EmbedContext, onStartEdit = vi.fn()) {
  render(previewElement(content, embeds, onStartEdit))
  return onStartEdit
}

describe('md-notebook/parseEmbed', () => {
  it('reads a bare target', () => {
    expect(parseEmbed('Pasted image 20260706094611.png')).toEqual({ target: 'Pasted image 20260706094611.png' })
  })

  it('reads a numeric suffix as the display width', () => {
    expect(parseEmbed('a.png|731')).toEqual({ target: 'a.png', width: 731 })
  })

  it('keeps only the width of a WxH size', () => {
    expect(parseEmbed('a.png|640x480')).toEqual({ target: 'a.png', width: 640 })
  })

  it('honours a small width verbatim, caps a huge one and ignores zero', () => {
    expect(parseEmbed('a.png|20')).toEqual({ target: 'a.png', width: 20 })
    expect(parseEmbed('a.png|99999')).toEqual({ target: 'a.png', width: 2000 })
    expect(parseEmbed('a.png|0')).toEqual({ target: 'a.png' })
  })

  it('reads a non-numeric suffix as the alt text', () => {
    expect(parseEmbed('a.png|Subnet allocation')).toEqual({ target: 'a.png', alt: 'Subnet allocation' })
  })

  it('drops a heading fragment, which addresses a note and not an image', () => {
    expect(parseEmbed('a.png#section|300')).toEqual({ target: 'a.png', width: 300 })
  })
})

describe('md-notebook/isImageEmbed', () => {
  it('accepts the image extensions Obsidian embeds, in any case', () => {
    for (const t of ['a.png', 'a.JPG', 'a.jpeg', 'a.gif', 'a.webp', 'a.SVG', 'a.bmp', 'a.avif', 'dir/a.png']) {
      expect(isImageEmbed(t)).toBe(true)
    }
  })

  it('rejects a note transclusion, a PDF and audio, which Obsidian also embeds', () => {
    for (const t of ['Other note', 'notes/Other note', 'deck.pdf', 'call.mp3', 'a.png.bak']) {
      expect(isImageEmbed(t)).toBe(false)
    }
  })
})

describe('md-notebook/resolveEmbedTarget', () => {
  it('prefers the configured attachment folder for a bare file name', () => {
    expect(resolveEmbedTarget('a.png', ctx())).toBe('z-assets/a.png')
  })

  it('falls back to the note folder, then the vault root, without a setting', () => {
    expect(resolveEmbedTarget('a.png', ctx({ attachmentFolder: null }))).toBe('customers/acme/a.png')
    expect(resolveEmbedTarget('a.png', ctx({ attachmentFolder: null, noteDir: undefined }))).toBe('a.png')
  })

  it('reads the Obsidian ./ forms as relative to the note folder', () => {
    expect(resolveEmbedTarget('a.png', ctx({ attachmentFolder: './' }))).toBe('customers/acme/a.png')
    expect(resolveEmbedTarget('a.png', ctx({ attachmentFolder: './img' }))).toBe('customers/acme/img/a.png')
  })

  it('takes a target that carries a folder as vault-relative', () => {
    expect(resolveEmbedTarget('diagrams/a.png', ctx())).toBe('diagrams/a.png')
    expect(resolveEmbedTarget('./diagrams/a.png', ctx())).toBe('diagrams/a.png')
  })

  it('refuses a target that would leave the vault', () => {
    expect(resolveEmbedTarget('../secret.png', ctx())).toBeNull()
    expect(resolveEmbedTarget('a/../../secret.png', ctx())).toBeNull()
    expect(resolveEmbedTarget('/etc/passwd', ctx())).toBeNull()
    expect(resolveEmbedTarget('C:\\secret.png', ctx())).toBeNull()
    expect(resolveEmbedTarget('', ctx())).toBeNull()
  })

  it('keeps a folder-qualified target where the author put it', () => {
    // A target that names a folder is vault-relative, whatever the setting says.
    expect(resolveEmbedTarget('diagrams/a.png', ctx())).toBe('diagrams/a.png')
    expect(resolveEmbedTarget('one/a.png', ctx({ attachmentFolder: null }))).toBe('one/a.png')
  })

  it('does not derive a note folder from a noteDir outside the vault root', () => {
    expect(resolveEmbedTarget('a.png', ctx({ attachmentFolder: null, noteDir: '/elsewhere/n' }))).toBe('a.png')
  })

  it('serves the resolved path through the file endpoint', () => {
    expect(resolveEmbedSrc('Pasted image.png', ctx())).toBe(fileUrl('z-assets/Pasted image.png'))
    expect(resolveEmbedSrc('a.png', undefined)).toBeNull()
    expect(resolveEmbedSrc('../a.png', ctx())).toBeNull()
  })
})

describe('md-notebook/Preview embeds', () => {
  it('renders an embed as an image served from the attachment folder', () => {
    renderPreview('![[Pasted image 20260706094611.png]]', ctx())
    const img = screen.getByAltText('Pasted image 20260706094611.png')
    expect(img.getAttribute('src')).toBe(fileUrl('z-assets/Pasted image 20260706094611.png'))
    // The bang and the wikilink span, the pre-feature rendering, are gone.
    expect(document.body.textContent).not.toContain('!')
    expect(document.body.textContent).not.toContain('Pasted image')
  })

  it('names a bare embed by its file, so the image is not marked decorative', () => {
    // An empty alt hides the image from a screen reader; a pasted screenshot is
    // the content of its line, so its name is the least the alt can carry.
    renderPreview('![[a.png]]', ctx())
    expect(screen.getByRole('img', { name: 'a.png' })).toBeTruthy()
  })

  it('leaves a note transclusion as accent text instead of calling it a missing image', () => {
    renderPreview('see ![[Other note]] and ![[deck.pdf|the deck]]', ctx())
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('svg')).toBeNull()
    expect(screen.getByText('Other note')).toBeTruthy()
    expect(screen.getByText('the deck')).toBeTruthy()
    expect(document.body.textContent).not.toContain('!')
  })

  it('renders a standard markdown image and an embed side by side in one note', () => {
    renderPreview(['![New diagram](assets/new.png)', '', '![[legacy.png]]'].join('\n'), ctx())
    expect(screen.getByAltText('New diagram').getAttribute('src')).toBe(fileUrl('customers/acme/assets/new.png'))
    const imgs = document.querySelectorAll('img')
    expect(imgs).toHaveLength(2)
    expect(imgs[1].getAttribute('src')).toBe(fileUrl('z-assets/legacy.png'))
  })

  it('applies a requested width and keeps the column cap', () => {
    renderPreview('![[a.png|731]]', ctx())
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.style.width).toBe('731px')
    expect(img.style.maxWidth).toBe('100%')
  })

  it('uses a non-numeric suffix as the alt text', () => {
    renderPreview('![[a.png|Subnet allocation]]', ctx())
    expect(screen.getByAltText('Subnet allocation')).toBeTruthy()
  })

  it('still recognises an embed without any vault context and shows its file name', () => {
    renderPreview('![[legacy.png]]')
    expect(document.querySelector('img')).toBeNull()
    const label = screen.getByText('legacy.png')
    expect(label.querySelector('svg')).not.toBeNull()
  })

  it('falls back to the file name when the file has gone', () => {
    renderPreview('![[gone.png]]', ctx())
    fireEvent.error(document.querySelector('img') as HTMLImageElement)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('gone.png')).toBeTruthy()
  })

  it('requests the corrected source after the provisional one failed, without a remount', () => {
    // The page hands the renderer a context with no setting on first paint and
    // the same embed keeps its positional key when the setting arrives, so the
    // provisional URL (the note's folder) and the corrected one (the attachment
    // folder) reach the SAME NoteImage as two successive `src` props. A 404 on
    // the first must not pin the block on the failure fallback: the reader's
    // file is in the vault, one folder over.
    const view = render(previewElement('![[shot.png]]', ctx({ attachmentFolder: undefined })))
    const provisional = screen.getByAltText('shot.png') as HTMLImageElement
    expect(provisional.getAttribute('src')).toBe(fileUrl('customers/acme/shot.png'))
    fireEvent.error(provisional)
    expect(document.querySelector('img')).toBeNull()
    // Same tree, new props: the setting names the folder the file is in.
    view.rerender(previewElement('![[shot.png]]', ctx()))
    const corrected = screen.getByAltText('shot.png') as HTMLImageElement
    expect(corrected.getAttribute('src')).toBe(fileUrl('z-assets/shot.png'))
    // And a failure on the corrected source is still honoured: the reset is
    // per source, not a one-shot retry.
    fireEvent.error(corrected)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('shot.png')).toBeTruthy()
  })

  it('leaves an ordinary wikilink alone', () => {
    renderPreview('see [[architecture-note|the design]]', ctx())
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('the design')).toBeTruthy()
  })

  it('leaves an ordinary link alone, so the added groups did not shift the others', () => {
    renderPreview('see [the doc](https://example.com/doc)', ctx())
    expect(screen.getByRole('link', { name: 'the doc' }).getAttribute('href')).toBe('https://example.com/doc')
  })

  it('renders an embed inside a table cell', () => {
    renderPreview(['| Shape | Picture |', '| --- | --- |', '| Flow | ![[f.png]] |'].join('\n'), ctx())
    expect(document.querySelector('td img')?.getAttribute('src')).toBe(fileUrl('z-assets/f.png'))
  })

  it('keeps click-to-edit on the line holding the embed', async () => {
    const onStartEdit = renderPreview('![[a.png]]', ctx())
    await userEvent.click(document.querySelector('img') as HTMLImageElement)
    expect(onStartEdit).toHaveBeenCalledWith(0, 0)
  })
})
