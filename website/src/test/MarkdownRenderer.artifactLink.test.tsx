// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import MarkdownRenderer, { artifactSlugFromHref } from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

// The agent emits `[<name>](/artifacts/<slug>)` markdown links;
// the renderer must intercept clicks on those anchors and route the slug to
// onArtifactOpen instead of letting the browser navigate full-page.

describe('artifactSlugFromHref', () => {
  it('extracts the slug from a relative /artifacts/<slug> href', () => {
    expect(artifactSlugFromHref('/artifacts/my-report')).toBe('my-report')
  })

  it('extracts the slug from an absolute origin-prefixed href', () => {
    expect(artifactSlugFromHref('http://localhost:5173/artifacts/my-report')).toBe('my-report')
  })

  it('strips a trailing query string and hash', () => {
    expect(artifactSlugFromHref('/artifacts/my-report?share=1')).toBe('my-report')
    expect(artifactSlugFromHref('/artifacts/my-report#section')).toBe('my-report')
  })

  it('decodes a percent-encoded slug', () => {
    expect(artifactSlugFromHref('/artifacts/my%20report')).toBe('my report')
  })

  it('returns null for non-artifact hrefs', () => {
    expect(artifactSlugFromHref('/files/foo')).toBeNull()
    expect(artifactSlugFromHref('https://example.com/path')).toBeNull()
    expect(artifactSlugFromHref(null)).toBeNull()
    expect(artifactSlugFromHref(undefined)).toBeNull()
    expect(artifactSlugFromHref('')).toBeNull()
  })
})

describe('MarkdownRenderer artifact link interception', () => {
  const ARTIFACT_MD = '[My Report](/artifacts/my-report)'
  const realFetch = globalThis.fetch

  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('invokes onArtifactOpen with the slug when an artifact anchor is clicked', () => {
    const onArtifactOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={ARTIFACT_MD} onArtifactOpen={onArtifactOpen} />
    )
    const anchor = container.querySelector('a[href="/artifacts/my-report"]')
    expect(anchor).not.toBeNull()
    fireEvent.click(anchor!)
    expect(onArtifactOpen).toHaveBeenCalledTimes(1)
    expect(onArtifactOpen).toHaveBeenCalledWith('my-report')
  })

  it('routes a click on an inline child of the anchor (closest walk) to onArtifactOpen', () => {
    const onArtifactOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'[*My Report*](/artifacts/my-report)'} onArtifactOpen={onArtifactOpen} />
    )
    // The <em> is an inline child of the anchor — e.target is the <em>, not <a>.
    const em = container.querySelector('a[href="/artifacts/my-report"] em')
    expect(em).not.toBeNull()
    fireEvent.click(em!)
    expect(onArtifactOpen).toHaveBeenCalledWith('my-report')
  })

  it('does not also open the file panel when artifact and path handlers are wired', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      headers: new Headers({ 'X-Path-Kind': 'file' }),
    } as Response)) as unknown as typeof fetch
    const onArtifactOpen = vi.fn()
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer
        content={ARTIFACT_MD}
        onArtifactOpen={onArtifactOpen}
        onFileOpen={onFileOpen}
      />,
    )

    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()

    const anchor = container.querySelector('a[href="/artifacts/my-report"]')!
    fireEvent.click(anchor)
    await waitFor(() => expect(onArtifactOpen).toHaveBeenCalledWith('my-report'))
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('does not invoke onArtifactOpen on shift+click (lets the navigation happen)', () => {
    const onArtifactOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={ARTIFACT_MD} onArtifactOpen={onArtifactOpen} />
    )
    const anchor = container.querySelector('a[href="/artifacts/my-report"]')!
    fireEvent.click(anchor, { shiftKey: true })
    expect(onArtifactOpen).not.toHaveBeenCalled()
  })

  it('leaves non-artifact links alone', () => {
    const onArtifactOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'[Docs](https://example.com/docs)'} onArtifactOpen={onArtifactOpen} />
    )
    const anchor = container.querySelector('a')!
    fireEvent.click(anchor)
    expect(onArtifactOpen).not.toHaveBeenCalled()
  })

  it('is inert when onArtifactOpen is not provided (no crash, link untouched)', () => {
    const { container } = render(<MarkdownRenderer content={ARTIFACT_MD} />)
    const anchor = container.querySelector('a[href="/artifacts/my-report"]')!
    expect(() => fireEvent.click(anchor)).not.toThrow()
  })
})
