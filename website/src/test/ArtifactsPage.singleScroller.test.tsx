/**
 * One vertical scroller owns the artifacts page.
 *
 * The virtualized gallery brings its own scroller. While the page column ALSO
 * scrolled, a gesture resolved to whichever of the two it landed in, and the
 * page-level one had only ~113px of travel once the gallery was on screen — so
 * a gesture that landed there stopped dead after a few pixels.
 *
 * This does NOT explain a card-origin swipe behaving differently from a
 * gap-origin one: both land inside the same containers. That asymmetry is the
 * drag sensor's (see ArtifactsPage.dragSensors.test.tsx). Two same-axis
 * scrollers are a separate defect on the same page.
 *
 * Measured on a real build at 390px with 42 artifacts:
 *   before: 2 scrollers — page column 706/819 (113px travel), gallery 608/12485
 *   after:  1 scroller  — gallery 489/12485; card-origin swipe 0->205px,
 *           gap-origin swipe 0->259px, both moving the same element
 *
 * These are wiring assertions, not geometry ones: jsdom has no layout, so the
 * pixel claims above come from the pod probe. What the tests pin is that the
 * two independent decisions — "does the gallery virtualize" and "who owns the
 * scroll axis" — stay in agreement, since disagreeing is what puts a second
 * scroller back on the page.
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
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true }))

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

function makeArtifacts(n: number) {
  return Array.from({ length: n }, (_, i) => artifact(i))
}

/** The page's scroll host — the element whose `overflow` decides who owns the
 *  axis.
 *
 *  Anchored on its own test id rather than on the page-gutter class: the gutter
 *  moved to an inner wrapper once the header had to live INSIDE the scroller
 *  (`PageHeader` brings the gutter with it, and two nested paddings would put
 *  the title 32px in while its cards stay at 16px). Matching the gutter class
 *  now finds that inner wrapper, which carries no overflow at all — so the
 *  assertions below would read as "the column does not scroll" rather than as
 *  "the helper is pointing at the wrong element". */
function contentColumn(): HTMLElement {
  const host = screen.getByTestId('artifacts-scroll-host')
  return host as HTMLElement
}

async function renderWith(count: number) {
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: makeArtifacts(count) })
  vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact(0))
  renderWithProviders(<ArtifactsPage />)
  await waitFor(() => expect(contentColumn()).toBeTruthy())
}

describe('ArtifactsPage owns exactly one vertical scroll axis', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.setItem('mc-artifacts-view', 'grid')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
  })
  afterEach(cleanup)

  it('hands the axis to the gallery once it virtualizes', async () => {
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('masonry')).toBeTruthy())
    const col = contentColumn()
    // The page column must NOT scroll: it is the one with almost no travel, and
    // a gesture landing in it is the reported dead swipe.
    expect(col.className).not.toMatch(/overflow-y-auto/)
    expect(col.className).toMatch(/overflow-hidden/)
    // ...and it must be a flex column, or the gallery below cannot be sized to
    // the room that is left and would overflow the page again.
    expect(col.className).toMatch(/flex-col/)
    expect(col.className).toMatch(/min-h-0/)
  })

  it('sizes the virtualized gallery to the leftover room, not a viewport fraction', async () => {
    await renderWith(VIRTUALIZE_AT + 5)
    const masonryRoot = (await waitFor(() => screen.getByTestId('masonry'))).parentElement as HTMLElement
    // flex-1 claims the room the toolbar and folder rows did not take; min-h-0
    // is what lets it shrink below its 12000px of content instead of pushing
    // the column open (which is how the second scroller appeared).
    expect(masonryRoot.className).toMatch(/flex-1/)
    expect(masonryRoot.className).toMatch(/min-h-0/)
  })

  it('caps the pre-gallery region so it cannot squeeze the gallery to nothing', async () => {
    await renderWith(VIRTUALIZE_AT + 5)
    await waitFor(() => expect(screen.getByTestId('masonry')).toBeTruthy())
    // Breadcrumb + folder cards + the chats section are plain flex siblings of a
    // `flex-1 min-h-0` gallery inside an `overflow-hidden` column. Uncapped, a
    // tall stack of folder cards is absorbed by flex-shrink with nothing able to
    // scroll it into view — measured at 390x844 with a 700px stand-in: it
    // rendered at 562px, the gallery collapsed to 0px, and no ancestor scroller
    // could reach either. `shrink-0` stops the squeeze, `max-h` leaves the
    // gallery a floor.
    const src = readFileSync(join(__dirname, '..', 'pages', 'ArtifactsPage.tsx'), 'utf8')
    expect(src).toMatch(/galleryOwnsScroll \? 'shrink-0 max-h-\[\d+%\] overflow-y-auto' : ''/)
  })

  it('leaves the page column scrolling while the gallery is content-sized', async () => {
    await renderWith(VIRTUALIZE_AT - 5)
    // Below the threshold there is no inner scroller at all, so the page column
    // is the only candidate and must keep the axis.
    expect(screen.queryByTestId('masonry')).toBeNull()
    const col = contentColumn()
    expect(col.className).toMatch(/overflow-y-auto/)
    expect(col.className).not.toMatch(/overflow-hidden/)
  })

  it('keeps both decisions on the same threshold constant', async () => {
    // Exactly at the threshold the gallery virtualizes, so the page must already
    // have handed over the axis. An off-by-one between the two call sites is
    // invisible at every other count and puts two scrollers back on this page.
    await renderWith(VIRTUALIZE_AT)
    await waitFor(() => expect(screen.getByTestId('masonry')).toBeTruthy())
    expect(contentColumn().className).not.toMatch(/overflow-y-auto/)
  })
})
