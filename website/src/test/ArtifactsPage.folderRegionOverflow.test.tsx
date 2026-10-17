/**
 * The folder region of the artifacts page must not scroll horizontally.
 *
 * Once the gallery virtualizes (≥ VIRTUALIZE_AT artifacts, more than one
 * column) the breadcrumb + folder cards live inside a capped `overflow-y-auto`
 * region (#4151). CSS turns that region's overflow-x into `auto` as well, so
 * ANY descendant wider than the region paints a horizontal scrollbar under the
 * folder cards. FolderCardGrid used to be exactly that: a `-mr-3` wrapper — one
 * gutter wider than its parent by design, so the trailing card's `mr-3` would
 * not add page width — which is harmless inside a padded page column and 12px
 * of scrollable overflow inside a scroll container. The bar had been there since
 * #4151; #7677 recoloured every scrollbar thumb from `--border` (blends with the
 * card borders) to `--muted`, which is when it became visible (insider 0.6.0rc1).
 *
 * Measured on a real build (scripts/capture-artifacts-horizontal-overflow.mjs),
 * 36 artifacts + 2 folders, 1280/1440/1920, light+dark:
 *   before: folder region scrollWidth − clientWidth = +12px on every frame
 *   after:  0px on every frame; folder cards keep the (W+12)/N pitch, same
 *           column count as the masonry.
 *
 * happy-dom has no layout, so scrollWidth cannot be asserted here. What these
 * tests pin is the structural contract that produces the pixel result: no
 * negative horizontal margin anywhere inside the folder-region scroller, the
 * gutter expressed as grid `gap`, and the column count divided on the same
 * width the masonry divides.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import ArtifactsPage, { VIRTUALIZE_AT } from '../pages/ArtifactsPage'
import { useColumnCount } from '../hooks/useColumnCount'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

const artifact = (i: number) => ({
  slug: `demo-${i}`,
  name: `Demo ${i}`,
  kind: 'widget',
  version: 1,
  updated_at: new Date().toISOString(),
  created_at: new Date().toISOString(),
  tags: [],
  description: '',
  source: 'chat',
})

const FOLDERS = [
  { id: 'f1', name: 'Reports', parent_id: null, path: 'Reports', item_count: 0 },
  { id: 'f2', name: 'Design', parent_id: null, path: 'Design', item_count: 0 },
]

/** Tailwind negative horizontal margins: `-mr-3`, `-ml-2`, `-mx-4`, with or
 *  without a responsive prefix. */
const NEGATIVE_X_MARGIN = /(^|\s|:)-m[xrl]-/

async function renderVirtualized() {
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
    artifacts: Array.from({ length: VIRTUALIZE_AT + 5 }, (_, i) => artifact(i)),
  })
  vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: FOLDERS })
  vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact(0))
  renderWithProviders(<ArtifactsPage />)
  await waitFor(() => expect(screen.getByTestId('masonry')).toBeTruthy())
  await waitFor(() => expect(screen.getAllByRole('button', { name: /open folder/i }).length).toBe(2))
}

/** The capped folder-region scroller: the closest `overflow-y-auto` ancestor
 *  of a folder card. */
function folderRegion(): HTMLElement {
  const card = screen.getAllByRole('button', { name: /open folder/i })[0]
  let el: HTMLElement | null = card
  while (el && !/overflow-y-auto/.test(el.className)) el = el.parentElement
  if (!el) throw new Error('folder card is not inside an overflow-y-auto region')
  return el
}

describe('ArtifactsPage folder region does not scroll horizontally', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.setItem('mc-artifacts-view', 'grid')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
  })
  afterEach(cleanup)

  it('carries no negative horizontal margin anywhere inside the folder-region scroller', async () => {
    await renderVirtualized()
    const region = folderRegion()
    // The scroller's overflow-x is `auto` by CSS once overflow-y is; a child
    // that is wider than the region by a negative margin is the whole bug.
    const offenders = [region, ...Array.from(region.querySelectorAll<HTMLElement>('*'))]
      .filter(el => NEGATIVE_X_MARGIN.test(el.className))
      .map(el => el.className)
    expect(offenders).toEqual([])
  })

  it('lays the folder gutter out as grid gap, not as a per-card margin', async () => {
    await renderVirtualized()
    const cards = screen.getAllByRole('button', { name: /open folder/i })
    const grid = cards[0].parentElement as HTMLElement
    // Every folder card is a direct grid child and the grid owns the gutter…
    expect(grid.className).toMatch(/\bgrid\b/)
    expect(grid.className).toMatch(/\bgap-x-3\b/)
    expect(grid.style.gridTemplateColumns).toMatch(/^repeat\(\d+, minmax\(0, 1fr\)\)$/)
    for (const card of cards) {
      expect(card.parentElement).toBe(grid)
      // …so a card must not add a trailing margin of its own: with gap that
      // would double the gutter, and it is what made the wrapper necessary.
      expect(card.className).not.toMatch(/(^|\s)mr-3(\s|$)/)
      expect(card.className).toMatch(/(^|\s)mb-3(\s|$)/)
    }
  })
})

/** Column parity: the masonry divides `N·(card+gutter)` (its `-mr-3` wrapper),
 *  a gap grid measures `N·card + (N−1)·gutter`. Both must yield the same N at
 *  every width or the folder cards drop a column a few pixels before the
 *  gallery does. */
describe('useColumnCount gutter compensation', () => {
  let mockClientWidth = 0
  const originalClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')

  class MockResizeObserver {
    constructor(public callback: ResizeObserverCallback) {}
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  function Probe({ gutter }: { gutter: number }) {
    const [ref, cols] = useColumnCount(300, gutter)
    return <div data-testid="cols" ref={ref}>{cols}</div>
  }

  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => mockClientWidth })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    cleanup()
    if (originalClientWidth) Object.defineProperty(HTMLElement.prototype, 'clientWidth', originalClientWidth)
    else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
  })

  it('counts the same columns the margin grid does at the boundary widths where they would disagree', () => {
    // A 1188px content column: the masonry's wrapper is 1200 wide -> 4 columns.
    // Divided on 1188 alone the folder grid would only reach 3.
    mockClientWidth = 1188
    const { rerender } = render(<Probe gutter={0} />)
    expect(screen.getByTestId('cols').textContent).toBe('3')
    rerender(<Probe gutter={12} />)
    expect(screen.getByTestId('cols').textContent).toBe('4')
  })

  it('never reports fewer than one column', () => {
    mockClientWidth = 100
    render(<Probe gutter={12} />)
    expect(screen.getByTestId('cols').textContent).toBe('1')
  })
})
