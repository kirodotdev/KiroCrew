// @vitest-environment happy-dom
/**
 * A staged dashboard's link in the chat opens in the side panel, not a browser
 * tab: the renderer takes a PLAIN click on `/api/members/<slug>/dashboard?preview=1`
 * into the host's artifact action under a `dashboard-preview:` reference, and that
 * action opens a panel tab without treating the reference as an artifact. Every
 * modified click keeps the real href, so the browser can still open the URL.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, renderHook } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import type { usePanelTabs } from '../hooks/usePanelTabs'
import {
  dashboardPreviewRef,
  dashboardPreviewSlugFromHref,
  dashboardPreviewSlugFromRef,
} from '../utils/dashboardPreview'

const HREF = '/api/members/atlas/dashboard?preview=1'

describe('dashboardPreviewSlugFromHref', () => {
  it('reads the slug from the relative link dashboard_preview hands out', () => {
    expect(dashboardPreviewSlugFromHref(HREF)).toBe('atlas')
  })

  it('reads the same link written with this origin', () => {
    expect(dashboardPreviewSlugFromHref(`${window.location.origin}${HREF}`)).toBe('atlas')
  })

  it('declines another origin, the live read, and a malformed slug', () => {
    expect(dashboardPreviewSlugFromHref(`https://elsewhere.test${HREF}`)).toBeNull()
    expect(dashboardPreviewSlugFromHref('/api/members/atlas/dashboard')).toBeNull()
    expect(dashboardPreviewSlugFromHref('/api/members/atlas/dashboard?preview=0')).toBeNull()
    expect(dashboardPreviewSlugFromHref('/api/members/At%20las/dashboard?preview=1')).toBeNull()
    expect(dashboardPreviewSlugFromHref('/api/members/atlas/dashboard/x?preview=1')).toBeNull()
    expect(dashboardPreviewSlugFromHref(null)).toBeNull()
  })
})

describe('dashboardPreviewRef', () => {
  it('round-trips a slug, and no artifact slug reads as a reference', () => {
    expect(dashboardPreviewSlugFromRef(dashboardPreviewRef('atlas'))).toBe('atlas')
    expect(dashboardPreviewSlugFromRef('atlas')).toBeNull()
    expect(dashboardPreviewSlugFromRef('dashboard-preview-atlas')).toBeNull()
  })

})

describe('MarkdownRenderer staged-dashboard link', () => {
  const realFetch = globalThis.fetch
  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  function mount(onArtifactOpen = vi.fn()) {
    const { container } = render(
      <MarkdownRenderer content={`[See the page](${HREF})`} onArtifactOpen={onArtifactOpen} onFileOpen={vi.fn()} />,
    )
    const anchor = container.querySelector(`a[href="${HREF}"]`) as HTMLAnchorElement | null
    expect(anchor).not.toBeNull()
    return { anchor: anchor!, onArtifactOpen }
  }

  it('opens a plain click in the side panel and keeps the browser from navigating', () => {
    const { anchor, onArtifactOpen } = mount()
    const notCancelled = fireEvent.click(anchor)
    expect(onArtifactOpen).toHaveBeenCalledTimes(1)
    expect(onArtifactOpen).toHaveBeenCalledWith('dashboard-preview:atlas')
    expect(notCancelled).toBe(false)
  })

  it.each([
    ['Cmd', { metaKey: true }],
    ['Ctrl', { ctrlKey: true }],
    ['Shift', { shiftKey: true }],
    ['Alt', { altKey: true }],
  ])('leaves a %s-click to the browser, on the real URL', (_name, mods) => {
    const { anchor, onArtifactOpen } = mount()
    const notCancelled = fireEvent.click(anchor, mods)
    expect(onArtifactOpen).not.toHaveBeenCalled()
    expect(notCancelled).toBe(true)
    expect(anchor.getAttribute('href')).toBe(HREF)
    expect(anchor.getAttribute('target')).toBe('_blank')
  })

  it('does not probe the link as a file path', async () => {
    const fetchSpy = vi.fn(() => Promise.resolve(new Response('{}')))
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    mount()
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('usePanelDocumentActions.openArtifact with a staged-dashboard reference', () => {
  const realFetch = globalThis.fetch
  afterEach(() => { globalThis.fetch = realFetch })

  it('opens a titled panel tab and reads no artifact', async () => {
    const fetchSpy = vi.fn(() => Promise.resolve(new Response('{}')))
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    const openArtifact = vi.fn()
    const onOpened = vi.fn()
    const tabsCtl = { openArtifact } as unknown as ReturnType<typeof usePanelTabs>
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl,
      slotRef: { current: 'chat-1' },
      queryClient: new QueryClient(),
      showActionError: vi.fn(),
      onOpened,
    }))
    await result.current.openArtifact('dashboard-preview:atlas')
    expect(openArtifact).toHaveBeenCalledWith(
      { slug: 'dashboard-preview:atlas', kind: 'html', title: 'atlas (preview)' },
      '',
      'chat-1',
    )
    expect(onOpened).toHaveBeenCalledTimes(1)
    // No artifact read and no involvement breadcrumb: there is no artifact.
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('titles the tab with the roster name when the roster is cached', async () => {
    const queryClient = new QueryClient()
    queryClient.setQueryData(['kirocrew-agents', 'members-roster'], [{ name: 'Atlas', slug: 'atlas' }])
    const openArtifact = vi.fn()
    const tabsCtl = { openArtifact } as unknown as ReturnType<typeof usePanelTabs>
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'chat-1' }, queryClient, showActionError: vi.fn(),
    }))
    await result.current.openArtifact('dashboard-preview:atlas')
    expect(openArtifact.mock.calls[0][0].title).toBe('Atlas (preview)')
  })

  it('re-reads the staged page on every open, so a restaged page is never shown stale', async () => {
    const queryClient = new QueryClient()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const tabsCtl = { openArtifact: vi.fn() } as unknown as ReturnType<typeof usePanelTabs>
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'chat-1' }, queryClient, showActionError: vi.fn(),
    }))
    await result.current.openArtifact('dashboard-preview:atlas')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['member-dashboard', 'atlas'] })
  })
})
