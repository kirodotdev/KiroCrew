/**
 * The artifact library's FILTER toolbar on a phone.
 *
 * The controls used to live on one `flex-wrap` row: search, kind, tag, a
 * `ml-auto` Deploy button, and the Starred/All toggle. At 390px that row broke
 * wherever the widths happened to land — search + kind on line 1, the
 * right-floated Deploy alone on line 2, the toggle left-aligned on line 3 — so
 * every line started and ended at a different x and the toolbar read as a
 * scatter rather than a stack.
 *
 * The narrow shape gives each row the full content width and one shared pair of
 * edges: search, then the two selects half-and-half, then Starred/All justified
 * against the view switcher. Deploy is the one control that leaves the page, so
 * it moves into the add menu instead of holding a line of its own.
 *
 * Every assertion here is width-dependent behaviour (which branch renders
 * where), not a restatement of a class string, except the two that pin the
 * mobile grouping wrappers — those ARE the layout, and a mutation test proved
 * each one fails when its class is dropped.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

/** The row that owns the filters. The search box is its first child. */
function toolbar(): HTMLElement {
  const search = screen.getByPlaceholderText(/filter by name/i)
  // input -> relative wrapper -> toolbar
  return search.parentElement!.parentElement as HTMLElement
}

const kindTrigger = () => screen.getByRole('combobox', { name: /filter by kind/i })
const tagTrigger = () => screen.getByRole('combobox', { name: /filter by tag/i })

describe('ArtifactsPage filter toolbar at phone width', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mobile = true
    localStorage.setItem('mc-artifacts-view', 'grid')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  })
  afterEach(cleanup)

  it('stacks the toolbar into rows below md and restores the row from md up', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(toolbar()).toBeTruthy())
    // Narrow is the baseline (bare classes), desktop is the `md:` addition —
    // the repo's mobile-first convention. A bare `flex-wrap` here is the defect
    // this replaces, so it must not be reachable without the breakpoint.
    expect(toolbar().className).toContain('flex-col')
    expect(toolbar().className).toContain('md:flex-row')
    expect(toolbar().className).not.toMatch(/(^|\s)flex-wrap/)
  })

  it('gives the two selects one row and equal halves of it', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(kindTrigger()).toBeTruthy())
    const kindCell = kindTrigger().parentElement!.parentElement as HTMLElement
    const tagCell = tagTrigger().parentElement!.parentElement as HTMLElement
    // Same parent = same line; flex-1 + min-w-0 on each = equal halves that can
    // actually shrink. Without min-w-0 a long tag name floors the cell and the
    // pair runs past the viewport.
    expect(kindCell.parentElement).toBe(tagCell.parentElement)
    for (const cell of [kindCell, tagCell]) {
      expect(cell.className).toContain('flex-1')
      expect(cell.className).toContain('min-w-0')
    }
  })

  it('keeps the 180px tag-trigger floor for desktop only', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(tagTrigger()).toBeTruthy())
    const tagCell = tagTrigger().parentElement!.parentElement as HTMLElement
    // The floor exists so the popup (which is exactly the trigger's width) can
    // show real tag names. On a phone the half-row already gives ~179px, and an
    // unconditional floor would push the pair past a 320px viewport instead.
    expect(tagCell.className).toContain('md:min-w-[180px]')
    expect(tagTrigger().closest('[style*="min-width"]')).toBeNull()
  })

  it('pairs the Starred toggle with the view switcher on one justified row', async () => {
    renderWithProviders(<ArtifactsPage />)
    const starred = await waitFor(() => screen.getByRole('group', { name: /filter starred/i }))
    const gallery = screen.getByRole('button', { name: /gallery/i })
    const row = starred.parentElement as HTMLElement
    // Both answer "what am I looking at". One justified row gives the toolbar's
    // last line a left AND a right edge; alone, either control strands one side.
    expect(row.contains(gallery)).toBe(true)
    expect(row.className).toContain('justify-between')
  })

  it('moves Deploy out of the toolbar and into the add menu', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(toolbar()).toBeTruthy())
    expect(screen.queryByRole('button', { name: /artifact deploy/i })).toBeNull()
    const trigger = screen.getByRole('button', { name: /more actions/i })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('menuitem', { name: /artifact deploy/i })).toBeTruthy())
  })

  it('keeps Deploy a visible button on desktop, and out of the menu', async () => {
    mobile = false
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /artifact deploy/i })).toBeTruthy())
    const trigger = screen.getByRole('button', { name: /more ways to add an artifact/i })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('menuitem', { name: /import from a file/i })).toBeTruthy())
    expect(screen.queryByRole('menuitem', { name: /artifact deploy/i })).toBeNull()
  })

  it('makes the split button caret take the labelled half\'s height', async () => {
    renderWithProviders(<ArtifactsPage />)
    const caret = await waitFor(() => screen.getByRole('button', { name: /more actions/i }))
    // Measured on a real build: the caret holds only a 13px icon, so its own
    // content box is 23px against the labelled half's 30.14px (whose text
    // line-height sets the height). The parent centres it, which reads as a gap
    // above AND below the caret and a broken seam.
    //
    // This is a class assertion rather than a height one on purpose: jsdom has no
    // layout, so it cannot compute the 23 -> 30.14 change. `self-stretch` (not a
    // literal height) is the thing being pinned — a fixed height would clip once
    // the label's font grows.
    expect(caret.className).toContain('self-stretch')
    // Not viewport-gated: the same seam exists at every width, and the labelled
    // half is the height source at all of them.
    expect(caret.className).not.toMatch(/md:self-stretch/)
  })

  it('keeps the view switcher in the header row on desktop', async () => {
    mobile = false
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /gallery/i })).toBeTruthy())
    const title = screen.getByRole('heading', { name: /your artifacts/i })
    // Desktop shape is unchanged: the switcher rides with the create actions,
    // not with the filters. Queried through the container rather than held as a
    // node — the switcher re-renders once it has measured its parent, and a node
    // captured before that is detached from the tree.
    expect(within(title.parentElement as HTMLElement).getByRole('button', { name: /gallery/i })).toBeTruthy()
    expect(within(toolbar()).queryByRole('button', { name: /gallery/i })).toBeNull()
  })
})
