/**
 * At ONE column the page keeps the scroll axis.
 *
 * The gallery only has to own a scroller because `VirtuosoMasonry` can only own
 * one: its entire prop surface is columnCount / data / context / ItemContent /
 * initialItemCount / useWindowScroll, with no `customScrollParent`, and this
 * app's shell is `h-dvh` with `overflow-hidden` on body so `useWindowScroll` has
 * zero travel (measured on a real build: scrollHeight - clientHeight = 0).
 *
 * Handing the axis over is what forced everything else: the pre-gallery region
 * had to be capped into a scroller of its own (a bounded version of the very
 * dead-swipe the handover fixed), the chrome had to hide on scroll to win back
 * the 317px it pinned, and any section rendered AFTER the gallery was left
 * unreachable.
 *
 * None of that is needed at one column, because at one column there is no
 * waterfall to preserve — `Math.floor(clientWidth / 300)` is 1 for every phone
 * in portrait (two columns need ~620px of viewport). A single column of
 * variable-height cards is a LIST, and a list can be windowed against a scroller
 * it does not own: this app's own `useVirtualChat` takes `externalScrollerRef`.
 * So the narrow path points the gallery at the page column and the page column
 * scrolls, which tracks the finger 1:1 and needs no animation, no threshold and
 * no settle window.
 *
 * jsdom has no layout, so `clientWidth` is 0 and the column count comes from the
 * viewport seed in `useColumnCount` — which is what lets these tests drive both
 * layouts by setting `window.innerWidth`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup } from '@testing-library/react'
import ArtifactsPage, { VIRTUALIZE_AT } from '../pages/ArtifactsPage'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

/** Records what the virtualizer was handed, so the test can prove the page's own
 *  column was passed as its scroller rather than only that a list rendered. */
const listProps: { scrollerRef?: { current: HTMLElement | null }; followOutput?: boolean; sessionId?: string; initialPlacement?: string; eagerFirstMeasure?: boolean } = {}
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: {
    externalScrollerRef?: { current: HTMLElement | null }
    followOutput?: boolean
    sessionId?: string
    initialPlacement?: string
    eagerFirstMeasure?: boolean
    items: unknown[]
  }) => {
    // Capture the REF, not its value: refs are populated after commit, and the
    // real hook reads it lazily from its effects for exactly that reason.
    listProps.scrollerRef = opts.externalScrollerRef
    listProps.followOutput = opts.followOutput
    listProps.sessionId = opts.sessionId
    listProps.initialPlacement = opts.initialPlacement
    listProps.eagerFirstMeasure = opts.eagerFirstMeasure
    return {
      scrollerRef: { current: null },
      contentRef: { current: null },
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      // One mounted row, so tests can assert on the wrapper LibraryList
      // renders around a card (measure target, margin containment).
      virtualItems: opts.items.length
        ? [{ data: opts.items[0], index: 0, key: 'row0', mounted: true, height: 260 }]
        : [],
      offsetBefore: 0,
      offsetAfter: 0,
      totalHeight: 0,
      isAtBottom: false,
      scrollToIndex: () => {},
      scrollToBottom: () => {},
      mountIndex: () => false,
      measureRef: () => () => {},
    }
  },
}))

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

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

/** The page's scroll host — anchored on its test id, not on the page-gutter
 *  class, because the gutter lives on an inner wrapper now that the header has
 *  to sit INSIDE the scroller. */
function contentColumn(): HTMLElement {
  return screen.getByTestId('artifacts-scroll-host')
}

function setViewport(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true })
}

async function renderWith(count: number) {
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
    artifacts: Array.from({ length: count }, (_, i) => artifact(i)),
  })
  vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact(0))
  renderWithProviders(<ArtifactsPage />)
  await waitFor(() => expect(contentColumn()).toBeTruthy())
}

const src = () => readFileSync(join(__dirname, '..', 'pages', 'ArtifactsPage.tsx'), 'utf8')

describe('ArtifactsPage keeps the axis on the page at one column', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listProps.scrollerRef = undefined
    mobile = true
    // Cleared first: the page reads four `mc-artifacts-*` keys (`view`,
    // `pinned-only`, `sort`, `session-docs-collapsed`) and this fixture seeds two,
    // so without the clear the other two carry over from whatever ran before —
    // the fixture would be only partly determined by itself.
    localStorage.clear()
    localStorage.setItem('mc-artifacts-view', 'grid')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
  })
  afterEach(cleanup)

  it('renders the gallery as a list, pointed at the page column', async () => {
    setViewport(390)
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('artifacts-gallery-list')).toBeTruthy())
    // Not the masonry: at one column the two render the same thing, and only a
    // list can be windowed against a scroller it does not own.
    expect(screen.queryByTestId('masonry')).toBeNull()
    // The crux. Without an external scroller the virtualizer creates its own and
    // the page is back to two same-axis scrollers.
    expect(listProps.scrollerRef?.current).toBe(contentColumn())
    // A gallery is not a transcript: an append must not pull the viewport down.
    expect(listProps.followOutput).toBe(false)
    // And it opens at the HEAD. The hook's default is the chat contract —
    // tail window + bottom pin on slot entry — which lands the gallery at its
    // last card and, with every unmeasured card then sitting ABOVE the
    // viewport, turns each height measurement into a scrollTop compensation
    // write (visible as mount-time flicker).
    expect(listProps.initialPlacement).toBe('top')
    // Mixed-height cards (HTML/GIF/image) around one estimate: first
    // measurements must not ride the starvable debounce, or every spacer
    // handoff during a downward scroll bounces by (real − estimate).
    expect(listProps.eagerFirstMeasure).toBe(true)
    // The measured wrapper must contain its card's bottom margin (flow-root =
    // a BFC). Without it the mb-3 gap collapses through the wrapper, every
    // row measures 12px short, and the accumulated offset error surfaces as a
    // fixed-position bounce on engines without scroll anchoring (iOS Safari).
    const wrapper = screen.getByTestId('artifacts-gallery-list')
      .querySelector('div[class*="flow-root"]')
    expect(wrapper).not.toBeNull()
  })

  it('leaves the page column scrolling, so the chrome scrolls away by physics', async () => {
    setViewport(390)
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('artifacts-gallery-list')).toBeTruthy())
    const col = contentColumn()
    expect(col.className).toMatch(/overflow-y-auto/)
    expect(col.className).not.toMatch(/overflow-hidden/)
  })

  it('drops the pre-gallery cap, so no sibling scroller is left behind', async () => {
    setViewport(390)
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('artifacts-gallery-list')).toBeTruthy())
    // The cap existed only to stop a tall folder stack from squeezing a
    // `flex-1` gallery to 0px inside an `overflow-hidden` column. With the
    // column scrolling there is nothing to squeeze, and the cap would be a
    // ~64px-travel scroller sitting in the top 45% of the viewport.
    expect(document.querySelector('[class*="max-h-["]')).toBeNull()
  })

  it('keeps the masonry, and its scroller, once there are real columns', async () => {
    setViewport(1280)
    mobile = false
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('masonry')).toBeTruthy())
    // The waterfall is real from two columns up, so nothing changes there: the
    // masonry owns its scroller and the page column yields, exactly as before.
    expect(screen.queryByTestId('artifacts-gallery-list')).toBeNull()
    const col = contentColumn()
    expect(col.className).toMatch(/overflow-hidden/)
    expect(col.className).not.toMatch(/overflow-y-auto/)
    expect(document.querySelector('[class*="max-h-["]')).toBeTruthy()
  })

  it('puts the page header inside the scroller, without doubling the gutter', async () => {
    setViewport(390)
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('artifacts-gallery-list')).toBeTruthy())
    const host = contentColumn()
    const header = screen.getByTestId('page-header')
    // Outside the scroller the title cannot scroll away, and with the collapse
    // gated off at one column it would pin ~130px of a 844px viewport forever —
    // worse than the collapse it replaces.
    expect(host.contains(header)).toBe(true)
    // `PageHeader` carries the page gutter itself, so the scroll host must not:
    // nested paddings put the title 32px in while the cards it labels stay at 16.
    expect(host.className).not.toMatch(/px-4/)
    expect(header.className).toMatch(/px-4/)
  })

  it('reserves the cached height for a thumbnail instead of the ceiling', async () => {
    // The seed is what decides whether a card corrects on arrival. Seeding from
    // VIEWPORT_H makes every thumbnail start at the MAXIMUM and shrink when the
    // iframe reports, so the error is one-way and the corrections accumulate
    // into a scroller that keeps getting shorter as you scroll. Measured over
    // eight swipes: 4530px of height change on the pre-change build, 1188px with
    // the cache warm, and the scroller's total drift 1332px -> 181px.
    const text = src()
    const thumb = text.slice(text.indexOf('function WidgetThumb('), text.indexOf('function GridCard('))
    // Cache first, then the median of the SAME key space, then the ceiling.
    expect(thumb).toMatch(/getWidgetHeight\(heightKey\) \?\? Math\.min\(VIEWPORT_H, estimateWidgetHeight\(THUMB_HEIGHT_SPACE, VIEWPORT_H\)\)/)
    // ...and the measurement has to be written back, or the cache never warms.
    expect(thumb).toMatch(/setWidgetHeight\(heightKey, next\)/)
    // A separate key space: thumbnails measure at BASE_W and clamp to
    // VIEWPORT_H, a full-width frame does neither, so sharing entries would
    // reserve frame-sized boxes for thumbnails.
    expect(text).toMatch(/const THUMB_HEIGHT_SPACE = 'thumb900'/)
  })

  it('reserves ONE box for a thumbnail, before and after the iframe exists', async () => {
    // A different placeholder height that is swapped once the blob URL resolves
    // is a SECOND height change per card, on top of the report — and in a
    // virtualized list every one of those re-lays out everything below it.
    const text = src()
    const thumb = text.slice(text.indexOf('function WidgetThumb('), text.indexOf('function GridCard('))
    expect(thumb).toMatch(/style=\{\{ height: scaledH \}\}/)
    expect(thumb).not.toMatch(/height: blobUrl \? scaledH/)
  })

  it('does not let the gallery measure the width a second time', async () => {
    // `FolderCardGrid` legitimately measures its own element — it aligns folder
    // cards column-for-column with the gallery — and it decides nothing about
    // scrolling. What must not happen is the GALLERY measuring independently of
    // the page: the page reads the count to decide who owns the axis, so a
    // disagreement at a boundary width would have the page yield while the
    // gallery renders a list that also scrolls, which is two same-axis
    // scrollers again at that one width.
    const text = src()
    const body = text.slice(
      text.indexOf('function LibraryMasonry('),
      text.indexOf('function MasonryGridItem('),
    )
    expect(body).not.toMatch(/useColumnCount\(/)
    expect(body).toMatch(/cols: number/)
    expect(text).toMatch(/const \[galleryWidthRef, cols\] = useColumnCount\(/)
  })

  it('ties the axis handover to the column count, not just the item count', async () => {
    // `galleryOwnsScroll` gates the cap, the collapse hook and the column's own
    // overflow. Dropping `cols > 1` re-enables all three on a phone.
    expect(src()).toMatch(/galleryOwnsScroll =[^\n]*VIRTUALIZE_AT && cols > 1/)
  })
})
