/** The artifact page's frame is sized to its document, so its own viewport never
 * scrolls and an in-page `#id` link inside it moved nothing. The frame posts the
 * target's offset (`mc-widget-scroll-to`, lib/widgetFragmentScroll.ts) and the
 * host scrolls the container around the frame there. Pinned: the scroll lands on
 * frame offset + target offset, only this frame's window is heard, the offset
 * stays inside the frame, and a fixed-height frame (the side panel) is left to
 * scroll itself. */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import type { Artifact } from '../types'
import { FRAGMENT_SCROLL_MESSAGE } from '../lib/widgetFragmentScroll'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

vi.mock('../lib/widgetSrcdoc', () => ({
  AGENT_PAGE_SANDBOX: 'allow-scripts',
  THEME_VAR_NAMES: [] as string[],
  readThemeVars: () => ({}) as Record<string, string>,
  buildSrcdoc: (opts: { html: string }) => opts.html,
}))

vi.mock('../api/client', () => ({
  api: { sandboxDocUrl: () => Promise.resolve({ url: '/sandbox-doc/abc/1.mac' }) },
  ApiError: class extends Error {},
}))

import { ArtifactBodyIframe } from '../components/ArtifactBody'

const artifact = { slug: 's', name: 'Doc', kind: 'html', content: '<p>x</p>' } as unknown as Artifact

function rect(top: number): DOMRect {
  return { top, left: 0, right: 0, bottom: top, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) }
}

/** Mount the frame inside a scrolling container whose viewport starts at 100px
 * and whose frame starts at 300px, 4000px tall. */
async function mount(heightStyle?: React.CSSProperties) {
  render(
    <div data-testid="scroller" style={{ overflowY: 'auto' }}>
      <ArtifactBodyIframe artifact={artifact} slug="s" heightStyle={heightStyle} />
    </div>,
  )
  const frame = await waitFor(() => {
    const el = document.querySelector('iframe')
    if (!el) throw new Error('frame never mounted')
    return el as HTMLIFrameElement
  })
  const scroller = screen.getByTestId('scroller')
  let scrollTop = 0
  Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } })
  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 5000 })
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 800 })
  scroller.getBoundingClientRect = () => rect(100)
  Object.defineProperty(frame, 'clientHeight', { configurable: true, value: 4000 })
  frame.getBoundingClientRect = () => rect(300)
  return { frame, scroller }
}

function post(frame: HTMLIFrameElement, top: unknown, from?: unknown): void {
  fireEvent(window, new MessageEvent('message', {
    data: { type: FRAGMENT_SCROLL_MESSAGE, top },
    source: (from ?? frame.contentWindow) as MessageEventSource,
  }))
}

describe('ArtifactBodyIframe scrolls the page to an in-page link target', () => {
  it('scrolls its container to the frame offset plus the target offset', async () => {
    const { frame, scroller } = await mount()
    post(frame, 1200)
    // 300 (frame top) + 1200 (target in frame) - 100 (container top).
    expect(scroller.scrollTop).toBe(1400)
  })

  it('hears only its own frame', async () => {
    const { frame, scroller } = await mount()
    post(frame, 1200, window)
    expect(scroller.scrollTop).toBe(0)
  })

  it('keeps the scroll inside the frame and ignores a non-number', async () => {
    const { frame, scroller } = await mount()
    post(frame, 'far')
    post(frame, Number.NaN)
    expect(scroller.scrollTop).toBe(0)
    post(frame, 1e9)
    expect(scroller.scrollTop).toBe(300 + 4000 - 100)
  })

  it('leaves a fixed-height frame (the side panel) to scroll itself', async () => {
    const { frame, scroller } = await mount({ height: '100%', minHeight: 0 })
    post(frame, 1200)
    expect(scroller.scrollTop).toBe(0)
  })
})
