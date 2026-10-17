/**
 * Scroll position memory against the REAL `@pierre/trees` widget.
 *
 * `PierreWorkspaceTreeImpl.scrollMemory.test.tsx` pins the policy against a
 * recording fake. This file mounts the real `<FileTree>` and checks the two
 * library contracts the feature rests on: the capture listener finds the
 * widget's shadow root and sees its scroller's `scroll` events, and a
 * `scrollToPath` issued by the remounted host is consumed by the widget and
 * moves its scroller. A `@pierre/trees` bump that broke either would leave
 * every fake-backed test green.
 *
 * happy-dom does no layout, so the scroller's `clientHeight` is stubbed to a
 * real viewport height and the restored `scrollTop` is asserted as the value
 * the widget writes, not as pixels on screen.
 */
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FILE_TREE_DEFAULT_ITEM_HEIGHT } from '@pierre/trees'

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
}))
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { api } from '../api/client'
import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { __recallTreeScrollAnchorForTests, __resetTreeScrollAnchorsForTests } from '../pierre/treeScrollAnchor'

const ROOT = '/repo/project'
const FILES = Array.from({ length: 60 }, (_, i) => `f${String(i).padStart(2, '0')}.ts`)
const SCROLLER = '[data-file-tree-virtualized-scroll]'

async function mountTree() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const view = render(<PierreWorkspaceTreeImpl projectDir={ROOT} rememberScroll />, { wrapper })
  const scroller = await waitFor(() => {
    const el = view.container.querySelector('file-tree-container')?.shadowRoot?.querySelector<HTMLElement>(SCROLLER)
    expect(el).toBeTruthy()
    return el as HTMLElement
  })
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 300 })
  return { view, scroller }
}

beforeEach(() => {
  __resetTreeScrollAnchorsForTests()
  vi.mocked(api.projectTree).mockResolvedValue({ root: ROOT, paths: FILES, repo: true })
  vi.mocked(api.projectGitStatus).mockResolvedValue({ repo: true, repoRoot: '/repo', files: [] })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('scroll position memory against the real @pierre/trees widget', () => {
  it('captures the real scroller and scrolls the remounted widget back to that row', async () => {
    const first = await mountTree()
    // The widget's own row height at the default density the host uses.
    const itemHeight = FILE_TREE_DEFAULT_ITEM_HEIGHT
    // Waiting on the hook's attach, which may take a frame after mount.
    await waitFor(() => {
      first.scroller.scrollTop = 20 * itemHeight + 3
      first.scroller.dispatchEvent(new Event('scroll'))
      expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe('f20.ts')
    })
    first.view.unmount()

    const second = await mountTree()
    await act(async () => {})
    expect(second.scroller.scrollTop).toBe(20 * itemHeight)
  })

  it('lands on the anchor in the last screenful once the view measures a short viewport', async () => {
    // The view applies the first request against its 420 px initial estimate
    // and only clamps after measuring, so in a 300 px panel the last
    // screenful's row needs the request again after that measurement.
    const observers: Array<{ cb: ResizeObserverCallback; el: Element | null }> = []
    vi.stubGlobal('ResizeObserver', class {
      private entry: { cb: ResizeObserverCallback; el: Element | null }
      constructor(cb: ResizeObserverCallback) {
        this.entry = { cb, el: null }
        observers.push(this.entry)
      }
      observe(el: Element) { this.entry.el = el }
      unobserve() {}
      disconnect() { this.entry.el = null }
    })
    try {
      const itemHeight = FILE_TREE_DEFAULT_ITEM_HEIGHT
      const viewport = 300
      const lastScreenful = FILES.length * itemHeight - viewport
      const first = await mountTree()
      await waitFor(() => {
        first.scroller.scrollTop = lastScreenful
        first.scroller.dispatchEvent(new Event('scroll'))
        expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe('f50.ts')
      })
      first.view.unmount()
      observers.length = 0

      const second = await mountTree()
      // The first request was applied against the estimate.
      expect(second.scroller.scrollTop).toBe(FILES.length * itemHeight - 420)
      // The real viewport height arrives: to the view's observer on its
      // scroller, and to the hook's on the widget host the scroller fills.
      await act(async () => {
        for (const o of observers) {
          if (!o.el) continue
          o.cb([{
            target: o.el,
            borderBoxSize: [{ blockSize: viewport, inlineSize: 200 }],
            contentRect: { height: viewport },
          } as unknown as ResizeObserverEntry], {} as ResizeObserver)
        }
      })
      await waitFor(() => expect(second.scroller.scrollTop).toBe(lastScreenful))
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
