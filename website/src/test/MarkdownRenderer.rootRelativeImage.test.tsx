import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import MarkdownRenderer, { BasePathCtx } from '../components/MarkdownRenderer'
import { siteRootImageCandidates } from '../components/markdown/ImgWithFallback'

/**
 * A root-relative image (`/images/a.png`) in a previewed markdown file means
 * "the root of the repository or site this file belongs to" — GitHub, Zenn and
 * Docusaurus all write images that way — not the root of the disk. The preview
 * used to send it to /api/file-raw verbatim, so every such image rendered as
 * "Image file no longer exists" although the file sat next to the markdown
 * (issue #23623). The absolute reading still goes first, then the directories
 * above the file, nearest first.
 */

const raw = (p: string) => `/api/file-raw?path=${encodeURIComponent(p)}`

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 404 }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPreview(content: string, filePath: string | null) {
  return render(
    <BasePathCtx.Provider value={filePath}>
      <MarkdownRenderer content={content} />
    </BasePathCtx.Provider>,
  )
}

describe('siteRootImageCandidates', () => {
  it('walks the directories above the file, nearest first, without the disk root', () => {
    expect(siteRootImageCandidates('/repo/zenn/articles/post.md', '/images/a.png')).toEqual([
      '/repo/zenn/articles/images/a.png',
      '/repo/zenn/images/a.png',
      '/repo/images/a.png',
    ])
  })

  it('ends at the file’s own drive root on Windows and accepts either separator there', () => {
    // The drive root is kept: the absolute reading tried first resolves on
    // the gateway's drive, which need not be the file's.
    expect(siteRootImageCandidates('D:\\site\\docs\\post.md', '/img/a.png')).toEqual([
      'D:\\site\\docs/img/a.png',
      'D:\\site/img/a.png',
      'D:/img/a.png',
    ])
  })

  it('keeps a backslash as a file-name character on POSIX', () => {
    expect(siteRootImageCandidates('/repo/a\\b/post.md', '/x.png')).toEqual([
      '/repo/a\\b/x.png',
      '/repo/x.png',
    ])
  })

  it('caps how far up it walks', () => {
    const deep = '/' + Array.from({ length: 20 }, (_, i) => `d${i}`).join('/') + '/post.md'
    expect(siteRootImageCandidates(deep, '/a.png')).toHaveLength(8)
  })

  it('returns nothing without a directory or an image path', () => {
    expect(siteRootImageCandidates('post.md', '/a.png')).toEqual([])
    expect(siteRootImageCandidates('/repo/post.md', '/')).toEqual([])
  })
})

describe('root-relative image in a previewed file', () => {
  it('tries the absolute path, then each directory above the file, until one loads', () => {
    const { container } = renderPreview('![shot](/images/post/shot.png)', '/repo/articles/post.md')
    const img = () => container.querySelector('img')!
    expect(img().getAttribute('src')).toBe(raw('/images/post/shot.png'))
    fireEvent.error(img())
    expect(img().getAttribute('src')).toBe(raw('/repo/articles/images/post/shot.png'))
    fireEvent.error(img())
    expect(img().getAttribute('src')).toBe(raw('/repo/images/post/shot.png'))
    fireEvent.load(img())
    expect(container.textContent).not.toContain('Image file no longer exists')
    expect(container.textContent).not.toContain('Image failed to load')
  })

  it('shows the broken-image chip, naming the path as written, once every candidate fails', async () => {
    const { container, findByText } = renderPreview('![](/images/gone.png)', '/repo/post.md')
    fireEvent.error(container.querySelector('img')!) // /images/gone.png
    fireEvent.error(container.querySelector('img')!) // /repo/images/gone.png
    expect(container.querySelector('img')).toBeNull()
    await findByText('/images/gone.png')
  })

  it('says the file is gone when the path as written answers 404, probing only that path', async () => {
    const { container, findByText } = renderPreview('![](/images/gone.png)', '/repo/post.md')
    fireEvent.error(container.querySelector('img')!)
    fireEvent.error(container.querySelector('img')!)
    await findByText('Image file no longer exists')
    const fetchMock = vi.mocked(globalThis.fetch)
    expect(fetchMock.mock.calls.map(c => c[0])).toEqual([raw('/images/gone.png')])
  })

  it('keeps the generic wording when the as-written file is refused (403) and the fallbacks 404', async () => {
    const probes: Promise<Response>[] = []
    vi.mocked(globalThis.fetch).mockImplementation((input) => {
      const p = Promise.resolve({ status: String(input) === raw('/home/u/photo.avif') ? 403 : 404 } as Response)
      probes.push(p)
      return p
    })
    const { container, findByText, queryByText } = renderPreview('![](/home/u/photo.avif)', '/repo/post.md')
    fireEvent.error(container.querySelector('img')!) // /home/u/photo.avif (403)
    fireEvent.error(container.querySelector('img')!) // /repo/home/u/photo.avif (404)
    await findByText('Image failed to load')
    await waitFor(() => expect(probes).toHaveLength(1))
    // Settle the probes the chip started, so its verdict has been applied.
    await act(async () => { await Promise.all(probes) })
    expect(queryByText('Image file no longer exists')).toBeNull()
    expect(queryByText('Image failed to load')).not.toBeNull()
  })

  it('leaves a root-relative image outside a file preview on the absolute path alone', async () => {
    const { container, findByText } = renderPreview('![](/tmp/shot.png)', null)
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/tmp/shot.png'))
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()
    await findByText('/tmp/shot.png')
  })

  it('starts over from the absolute path when the image source changes', () => {
    const { container, rerender } = renderPreview('![](/images/a.png)', '/repo/post.md')
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/repo/images/a.png'))
    rerender(
      <BasePathCtx.Provider value="/repo/post.md">
        <MarkdownRenderer content="![](/images/b.png)" />
      </BasePathCtx.Provider>,
    )
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/images/b.png'))
  })

  it('starts over from the absolute path when the previewed file changes', () => {
    const { container, rerender } = renderPreview('![](/images/a.png)', '/repo/one/post.md')
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/repo/one/images/a.png'))
    rerender(
      <BasePathCtx.Provider value="/other/two/post.md">
        <MarkdownRenderer content="![](/images/a.png)" />
      </BasePathCtx.Provider>,
    )
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/images/a.png'))
  })

  it('keeps a loaded image visible when another file in the same folder shows the same image', () => {
    const skeleton = (c: HTMLElement) => c.querySelector('.animate-pulse')
    const { container, rerender } = renderPreview('![](images/logo.png)', '/repo/docs/a.md')
    fireEvent.load(container.querySelector('img')!)
    expect(skeleton(container)).toBeNull()
    rerender(
      <BasePathCtx.Provider value="/repo/docs/b.md">
        <MarkdownRenderer content="![](images/logo.png)" />
      </BasePathCtx.Provider>,
    )
    // Same resolved src, so the browser fires no new load event: the image must
    // not fall back under the loading skeleton.
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/repo/docs/images/logo.png'))
    expect(skeleton(container)).toBeNull()
  })

  it('tries the new file’s candidates after every candidate failed for the previous file', () => {
    const { container, rerender } = renderPreview('![](/images/a.png)', '/one/post.md')
    fireEvent.error(container.querySelector('img')!) // /images/a.png
    fireEvent.error(container.querySelector('img')!) // /one/images/a.png
    expect(container.querySelector('img')).toBeNull()
    rerender(
      <BasePathCtx.Provider value="/two/post.md">
        <MarkdownRenderer content="![](/images/a.png)" />
      </BasePathCtx.Provider>,
    )
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/images/a.png'))
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')!.getAttribute('src')).toBe(raw('/two/images/a.png'))
  })
})
