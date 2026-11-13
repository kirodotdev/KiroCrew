/**
 * AppIcon inline-SVG path: a first-party `/app-assets/` icon is fetched, sanitised,
 * has its element ids scoped per instance, and is cached per URL. These tests cover
 * the fetch success branch, the id rewrite, the cache hit, the non-SVG reply and a
 * failed fetch that must degrade to the lucide glyph.
 */
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AppIcon from '../components/AppIcon'

const theme = { current: 'light' as 'light' | 'dark' }
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: theme.current }) }))

const SVG = '<svg xmlns="http://www.w3.org/2000/svg">'
  + '<defs><linearGradient id="g1"/></defs>'
  + '<rect id="r1" fill="url(#g1)" width="10" height="10"/>'
  + '<script>window.__pwned = true</script></svg>'

const okSvg = (text: string) => ({ ok: true, text: async () => text })

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  theme.current = 'light'
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AppIcon inline SVG path', () => {
  it('fetches a first-party icon, strips scripts and scopes its ids to the instance', async () => {
    const url = '/app-assets/cov-scope/icon-a.svg'
    fetchMock.mockResolvedValue(okSvg(SVG))
    const { container } = render(<AppIcon iconUrl={url} />)

    await waitFor(() => expect(container.querySelector('span.app-icon')).not.toBeNull())
    expect(fetchMock).toHaveBeenCalledWith(url)

    const span = container.querySelector('span.app-icon') as HTMLElement
    expect(span.querySelector('script')).toBeNull()
    const gradient = span.querySelector('linearGradient')
    const rect = span.querySelector('rect')
    const gradientId = gradient?.getAttribute('id') ?? ''
    expect(gradientId).toMatch(/^ai[a-zA-Z0-9]+-g1$/)
    expect(rect?.getAttribute('fill')).toBe(`url(#${gradientId})`)
    expect(rect?.getAttribute('id')).toMatch(/^ai[a-zA-Z0-9]+-r1$/)
  })

  it('reuses the cached markup on a second mount without fetching again', async () => {
    const url = '/app-assets/cov-cache/icon-b.svg'
    fetchMock.mockResolvedValue(okSvg(SVG))
    const first = render(<AppIcon iconUrl={url} />)
    await waitFor(() => expect(first.container.querySelector('span.app-icon')).not.toBeNull())
    first.unmount()

    const second = render(<AppIcon iconUrl={url} />)
    expect(second.container.querySelector('span.app-icon')).not.toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('keeps the reserved box and paints nothing when the reply is not an SVG document', async () => {
    const url = '/app-assets/cov-text/icon-c.svg'
    const text = vi.fn(async () => 'just some text')
    fetchMock.mockResolvedValue({ ok: true, text })
    const { container } = render(<AppIcon iconUrl={url} size={32} />)

    await waitFor(() => expect(text).toHaveBeenCalled())
    // Let the state update that follows the reply's text() run before judging the DOM.
    await act(async () => {})
    expect(fetchMock).toHaveBeenCalledWith(url)
    expect(container.querySelector('span.app-icon')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    const reserved = container.querySelector('span') as HTMLElement
    expect(reserved.style.width).toBe('32px')
    expect(reserved.style.height).toBe('32px')
  })

  it('degrades to the lucide glyph when the fetch fails', async () => {
    const url = '/app-assets/cov-fail/icon-d.svg'
    fetchMock.mockResolvedValue({ ok: false, text: async () => '' })
    const { container } = render(<AppIcon iconUrl={url} icon="Shield" />)

    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull())
    expect(container.querySelector('span.app-icon')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })
})
