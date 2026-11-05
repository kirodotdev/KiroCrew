/**
 * #23547: the outline flyout (MarkdownToc) and the skill file list
 * (SkillDirectoryBrowser) are capped lists. With the scrollbar hidden until
 * scroll, a mouse user saw a list that just ended. Each now carries the
 * MORE_BELOW_MASK fade while `useScrollEdgesY` reports rows below the fold, and
 * drops it when the list fits.
 *
 * The fade sits on an inner scroller, not on the bordered box: a mask clips
 * everything outside the masked element, so on the box it would also erase the
 * border (and the flyout's drop shadow).
 *
 * jsdom does no layout, so the scroller's geometry is stubbed by test id.
 */
import { useRef } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({ skillTree: vi.fn(), skillFile: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ code }: { code: string }) => <pre>{code}</pre>,
}))

import MarkdownOutlineRail from '../components/MarkdownToc'
import SkillDirectoryBrowser from '../components/SkillDirectoryBrowser'
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'

const MASK_CLASS = MORE_BELOW_MASK.split(' ')[0]

class InertObserver { observe() {} unobserve() {} disconnect() {} }

/** Hidden rows below the fold, per scroller test id. */
let hiddenBelow: Record<string, number> = {}

const proto = HTMLElement.prototype
const saved = {
  scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
}

beforeEach(() => {
  hiddenBelow = {}
  vi.stubGlobal('IntersectionObserver', InertObserver)
  vi.stubGlobal('ResizeObserver', InertObserver)
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get() { return 200 } })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) { return 200 + (hiddenBelow[this.dataset.testid ?? ''] ?? 0) },
  })
  mockApi.skillTree.mockReset()
  mockApi.skillFile.mockReset()
})
afterEach(() => {
  vi.unstubAllGlobals()
  for (const [k, d] of Object.entries(saved)) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
})

function Doc() {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div style={{ position: 'relative' }}>
      <div ref={ref}>
        <h1>Title</h1>
        <h2>Second</h2>
        <h3>Third</h3>
      </div>
      <MarkdownOutlineRail containerRef={ref} />
    </div>
  )
}

function renderBrowser() {
  mockApi.skillTree.mockResolvedValue({
    name: 'demo', root: '/x',
    entries: [
      { path: 'SKILL.md', type: 'file', size: 10 },
      { path: 'helper.sh', type: 'file', size: 10 },
    ],
  })
  mockApi.skillFile.mockResolvedValue({ name: 'demo', path: 'SKILL.md', content: '# Demo' })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><SkillDirectoryBrowser skillKey="demo" /></QueryClientProvider>)
}

describe('MarkdownToc flyout scroll cue', () => {
  it('fades the bottom while outline rows sit below the fold', async () => {
    hiddenBelow['markdown-toc-flyout-list'] = 120
    render(<Doc />)
    const list = await screen.findByTestId('markdown-toc-flyout-list')
    await waitFor(() => expect(list.className).toContain(MASK_CLASS))
    // The bordered, shadowed flyout box itself is never masked.
    expect((list.parentElement as HTMLElement).className).not.toContain(MASK_CLASS)
  })

  it('shows no fade when the outline fits', async () => {
    render(<Doc />)
    const list = await screen.findByTestId('markdown-toc-flyout-list')
    expect(list.className).not.toContain(MASK_CLASS)
  })
})

describe('SkillDirectoryBrowser file list scroll cue', () => {
  it('fades the bottom while files sit below the fold', async () => {
    hiddenBelow['skill-tree-list'] = 80
    renderBrowser()
    await waitFor(() => expect(screen.getByText('helper.sh')).toBeInTheDocument())
    const list = screen.getByTestId('skill-tree-list')
    await waitFor(() => expect(list.className).toContain(MASK_CLASS))
    expect((list.parentElement as HTMLElement).className).not.toContain(MASK_CLASS)
  })

  it('shows no fade when the file list fits', async () => {
    renderBrowser()
    await waitFor(() => expect(screen.getByText('helper.sh')).toBeInTheDocument())
    expect(screen.getByTestId('skill-tree-list').className).not.toContain(MASK_CLASS)
  })
})
