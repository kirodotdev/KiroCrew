/**
 * PPTX Maker page + helper tests.
 *
 * Two halves:
 *
 * 1. **Pure helpers** — the tab-follow rule and the deck filter are the two bits
 *    of real logic on this page. `tabToFollow` is what makes the viewer narrate a
 *    deck being built, and it has to return null on the FIRST poll or opening a
 *    finished deck would yank the user to whatever was last touched.
 * 2. **The page** — rendered against a mocked API surface, asserting the layout
 *    contract (PageHeader, stat row), that the engine banner appears only when the
 *    engine is missing, and that deck selection drives the viewer.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import {
  DECK_TABS,
  countBoardSlides,
  filterDecks,
  fuzzyMatch,
  nameFromFilename,
  prepareBoardHtml,
  tabAvailable,
  tabToFollow,
  templateAccents,
} from '../apps/pptx-maker/lib'
import type { DeckDetail, DeckSummary } from '../apps/pptx-maker/api'

// ── pure helpers ────────────────────────────────────────────────────────────

function deck(over: Partial<DeckSummary> = {}): DeckSummary {
  return {
    deckId: '20260101-demo',
    name: 'Quarterly Review',
    slideCount: 3,
    thumbnailUrl: null,
    pptxUrl: null,
    brief: '',
    ...over,
  }
}

describe('fuzzyMatch', () => {
  it('matches a subsequence, not just a substring', () => {
    // Deck names are timestamped, so a strict substring filter makes the
    // initials a user actually remembers match nothing.
    expect(fuzzyMatch('Quarterly Review', 'qr')).toBe(true)
    expect(fuzzyMatch('Quarterly Review', 'rq')).toBe(false)
  })

  it('is case-insensitive and matches everything on an empty query', () => {
    expect(fuzzyMatch('Quarterly', 'QUART')).toBe(true)
    expect(fuzzyMatch('anything', '')).toBe(true)
  })
})

describe('filterDecks', () => {
  it('returns every deck for a blank query', () => {
    const decks = [deck(), deck({ deckId: 'b', name: 'Other' })]
    expect(filterDecks(decks, '  ')).toHaveLength(2)
  })

  it('matches on the brief as well as the name', () => {
    const decks = [deck({ name: 'Untitled', brief: 'migration plan for storage' })]
    expect(filterDecks(decks, 'storage')).toHaveLength(1)
  })

  it('drops non-matching decks', () => {
    expect(filterDecks([deck({ name: 'Alpha' })], 'zzzz')).toHaveLength(0)
  })
})

describe('tabToFollow', () => {
  it('returns null on the first poll', () => {
    // Critical: with no baseline, following the newest timestamp would drag the
    // user to whatever a finished deck last touched, days ago.
    expect(tabToFollow(null, { brief: 100, outline: 200 })).toBeNull()
  })

  it('follows the deliverable that just changed', () => {
    expect(tabToFollow({ brief: 100 }, { brief: 100, outline: 200 })).toBe('outline')
  })

  it('picks the newest when several changed at once', () => {
    expect(
      tabToFollow({ brief: 1 }, { brief: 10, outline: 20, artDirection: 30 }),
    ).toBe('artDirection')
  })

  it('returns null when nothing moved', () => {
    expect(tabToFollow({ brief: 100, slides: 200 }, { brief: 100, slides: 200 })).toBeNull()
  })

  it('follows slides when a recompose lands', () => {
    expect(tabToFollow({ slides: 100 }, { slides: 400 })).toBe('slides')
  })

  it('ignores keys that are not deliverable tabs', () => {
    expect(tabToFollow({ brief: 1 }, { brief: 1, somethingElse: 999 })).toBeNull()
  })
})

describe('tabAvailable', () => {
  const detail = { specs: { brief: 'preview/x/specs/brief.md' } } as unknown as DeckDetail

  it('always allows slides', () => {
    expect(tabAvailable(undefined, 'slides')).toBe(true)
  })

  it('allows a deliverable only once it exists', () => {
    expect(tabAvailable(detail, 'brief')).toBe(true)
    expect(tabAvailable(detail, 'outline')).toBe(false)
  })

  it('covers every declared tab', () => {
    // Guards a tab added to DECK_TABS without a corresponding availability rule.
    for (const tab of DECK_TABS) expect(typeof tabAvailable(detail, tab)).toBe('boolean')
  })
})

describe('nameFromFilename', () => {
  it('strips the extension and unsafe characters', () => {
    expect(nameFromFilename('My Deck (v2).pptx')).toBe('My-Deck-v2')
  })

  it('trims leading and trailing separators', () => {
    expect(nameFromFilename('--weird--.html')).toBe('weird')
  })

  it('bounds the length', () => {
    expect(nameFromFilename(`${'a'.repeat(200)}.html`).length).toBeLessThanOrEqual(64)
  })
})

describe('board helpers', () => {
  it('injects a reset so the board is not shown with its own page padding', () => {
    expect(prepareBoardHtml('<html><head></head><body/></html>')).toContain(
      'data-preview-reset',
    )
  })

  it('injects the reset even without a head element', () => {
    expect(prepareBoardHtml('<div class="slide"/>')).toContain('data-preview-reset')
  })

  // A board document is agent-authored, and `sandbox=""` denies script but NOT
  // passive subresource loads — so an `<img src="https://…">` was a GET carrying
  // deck content off-origin. `srcDoc` also means the server's response CSP never
  // applies. These pin the policy that closes it.
  it('denies network egress by default', () => {
    const out = prepareBoardHtml('<div class="slide"/>')
    expect(out).toContain("default-src 'none'")
    expect(out).toContain('http-equiv="Content-Security-Policy"')
  })

  it('grants no http(s) image source, so an image beacon cannot fire', () => {
    const policy = prepareBoardHtml('<div class="slide"/>')
      .match(/content="([^"]*)"/)?.[1] ?? ''
    expect(policy).toContain('img-src data:')
    expect(policy).not.toMatch(/img-src[^;]*https?:/)
    // No bare scheme or origin anywhere in the policy.
    expect(policy).not.toMatch(/https?:\/\//)
  })

  it('emits the policy BEFORE any document byte', () => {
    // A CSP that appears after the markup it governs does not govern it — and a
    // board whose </head> sits after an <img> would have leaked before the meta
    // was parsed, which is why this is prepended rather than spliced in.
    const out = prepareBoardHtml('<html><head></head><body><img src="x"></body></html>')
    expect(out.indexOf('Content-Security-Policy')).toBeLessThan(out.indexOf('<img'))
    expect(out.indexOf('Content-Security-Policy')).toBeLessThan(out.indexOf('<html'))
  })

  it('keeps inline data: art usable', () => {
    // The engine re-encodes embedded raster to data:image/webp, so a blanket image
    // ban would blank every board while looking secure.
    const policy = prepareBoardHtml('<div/>').match(/content="([^"]*)"/)?.[1] ?? ''
    expect(policy).toMatch(/img-src[^;]*data:/)
  })

  it('counts slides, defaulting to one', () => {
    expect(countBoardSlides('<div class="slide">a</div><div class="slide">b</div>')).toBe(2)
    expect(countBoardSlides('<p>no slides here</p>')).toBe(1)
  })

  it('collects theme accents in order and skips gaps', () => {
    expect(templateAccents({ accent1: '#111', accent3: '#333' })).toEqual(['#111', '#333'])
    expect(templateAccents(undefined)).toEqual([])
  })
})

// ── page ────────────────────────────────────────────────────────────────────

const mockApi = {
  engine: vi.fn(),
  provisionEngine: vi.fn(),
  deps: vi.fn(),
  assets: vi.fn(),
  provisionAssets: vi.fn(),
  config: vi.fn(),
  setDeckRoot: vi.fn(),
  decks: vi.fn(),
  deck: vi.fn(),
  styles: vi.fn(),
  style: vi.fn(),
  importStyle: vi.fn(),
  renameStyle: vi.fn(),
  pinStyle: vi.fn(),
  deleteStyle: vi.fn(),
  templates: vi.fn(),
  importTemplate: vi.fn(),
  renameTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
}

vi.mock('../apps/pptx-maker/api', async () => {
  const actual = await vi.importActual<typeof import('../apps/pptx-maker/api')>(
    '../apps/pptx-maker/api',
  )
  return {
    ...actual,
    pptxMakerApi: mockApi,
    fetchArtifactText: vi.fn(async () => '# The brief\n\nSome content.'),
    fetchArtifactJson: vi.fn(async () => ({ defs: '' })),
  }
})

vi.mock('../api/client', () => ({
  api: { createChatSlot: vi.fn(async () => ({ key: 'pptx-1' })), revealPath: vi.fn(async () => ({})) },
}))

// The animated SVG renderer fetches and mutates real DOM; the page tests care
// that a slide slot renders, not how the SVG is assembled. The sanitiser helper
// this module also exports is covered by SlidePreviewSanitize.test.tsx, which does
// not mock it (importing the real module HERE re-enters the hoisted api mock).
vi.mock('../apps/pptx-maker/SlidePreview', () => ({
  default: ({ label }: { label: string }) => <div data-testid="slide-preview">{label}</div>,
}))

const READY_ENGINE = {
  ready: true,
  clone: true,
  venv: true,
  pinnedTag: 'v0.3.8',
  provision: { state: 'done' as const, log: '', elapsed: 0 },
}

async function renderPage() {
  const { default: PptxMakerPage } = await import('../apps/pptx-maker/PptxMakerPage')
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <PptxMakerPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('PptxMakerPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.engine.mockResolvedValue(READY_ENGINE)
    mockApi.deps.mockResolvedValue({ labels: {}, present: {}, missing: [] })
    mockApi.config.mockResolvedValue({ deckRoot: '/home/u/decks', default: '~/.config/sdpm/decks' })
    mockApi.decks.mockResolvedValue({ decks: [deck()] })
    mockApi.deck.mockResolvedValue({
      deckId: '20260101-demo',
      name: 'Quarterly Review',
      defsUrl: null,
      pptxUrl: 'preview/20260101-demo/output.pptx',
      dirPath: '/home/u/decks/20260101-demo',
      pptxPath: '/home/u/decks/20260101-demo/output.pptx',
      specs: { brief: 'preview/20260101-demo/specs/brief.md' },
      updatedAt: { brief: 100 },
      slides: [{ slug: 'intro', previewUrl: null, composeUrl: 'preview/x/compose/intro_1.json' }],
    })
    mockApi.styles.mockResolvedValue({ styles: [{ name: 'brand', source: 'user' }] })
    mockApi.templates.mockResolvedValue({ templates: [{ name: 'corp', source: 'builtin' }] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the standard page header and the stat row', async () => {
    await renderPage()
    expect(await screen.findByTestId('page-header')).toBeTruthy()
    expect(screen.getByTestId('page-title').textContent).toBe('PPTX Maker')
    // The stat row is part of the required page-layout pattern. Matched by
    // testid, not by label text: "Decks" is deliberately also a view tab and a
    // card title, so a text query would be ambiguous.
    await waitFor(() => expect(screen.getAllByTestId('stat-card').length).toBe(5))
    expect(screen.getByText('Finished files')).toBeTruthy()
  })

  it('does not show the engine banner when the engine is ready', async () => {
    await renderPage()
    await screen.findByTestId('page-header')
    await waitFor(() => expect(mockApi.engine).toHaveBeenCalled())
    expect(screen.queryByText(/is not installed yet/i)).toBeNull()
  })

  it('shows the engine banner with an install action when the engine is missing', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.3.8',
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    await renderPage()
    expect(await screen.findByText(/is not installed yet/i)).toBeTruthy()
    expect(screen.getByText('Install engine')).toBeTruthy()
  })

  it('starts the engine install when the banner action is used', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.3.8',
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    mockApi.provisionEngine.mockResolvedValue({ state: 'running' })
    await renderPage()
    await userEvent.click(await screen.findByText('Install engine'))
    await waitFor(() => expect(mockApi.provisionEngine).toHaveBeenCalled())
  })

  it('narrates a running install instead of offering the action again', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: true,
      venv: false,
      pinnedTag: 'v0.3.8',
      provision: { state: 'running', log: 'resolving engine dependencies…', elapsed: 42 },
    })
    await renderPage()
    expect(await screen.findByText(/42s/)).toBeTruthy()
    expect(screen.queryByText('Install engine')).toBeNull()
  })

  it('notes a missing optional dependency without blocking anything', async () => {
    mockApi.deps.mockResolvedValue({
      labels: { soffice: 'LibreOffice' },
      present: { soffice: false },
      missing: ['soffice'],
    })
    await renderPage()
    expect(await screen.findByText(/LibreOffice is not installed/)).toBeTruthy()
  })

  it('lists decks and opens the first one in the viewer', async () => {
    await renderPage()
    expect(await screen.findByText('Quarterly Review')).toBeTruthy()
    // The first deck auto-selects, so the viewer's tabs are reachable at once.
    await waitFor(() => expect(mockApi.deck).toHaveBeenCalledWith('20260101-demo'))
    expect(await screen.findByText('3 slides')).toBeTruthy()
  })

  it('filters the deck list', async () => {
    mockApi.decks.mockResolvedValue({
      decks: [deck(), deck({ deckId: '20260202-other', name: 'Board Update' })],
    })
    await renderPage()
    expect(await screen.findByText('Board Update')).toBeTruthy()
    await userEvent.type(screen.getByPlaceholderText('Search decks…'), 'Board')
    await waitFor(() => expect(screen.queryByText('Quarterly Review')).toBeNull())
    expect(screen.getByText('Board Update')).toBeTruthy()
  })

  it('shows an empty state when there are no decks', async () => {
    mockApi.decks.mockResolvedValue({ decks: [] })
    await renderPage()
    expect(await screen.findByText('No decks yet')).toBeTruthy()
  })

  it('offers a chat session per mode rather than embedding a chat', async () => {
    // Deck generation belongs in the real chat surface, so the page links into it.
    await renderPage()
    expect(await screen.findByText('Spec mode')).toBeTruthy()
    expect(screen.getByText('Vibe mode')).toBeTruthy()
    expect(screen.getByText('Style creator')).toBeTruthy()
  })

  it('creates a chat session on the app agent when a mode is chosen', async () => {
    const { api } = await import('../api/client')
    await renderPage()
    await userEvent.click(await screen.findByText('Spec mode'))
    await waitFor(() =>
      expect(api.createChatSlot).toHaveBeenCalledWith(undefined, 'pptx-maker/sdpm-spec'),
    )
  })

  it('switches to the library view and lists styles', async () => {
    await renderPage()
    await screen.findByTestId('page-header')
    await userEvent.click(screen.getByText('Library'))
    expect(await screen.findByText('brand')).toBeTruthy()
  })

  it('shows the deck output directory in settings', async () => {
    await renderPage()
    await screen.findByTestId('page-header')
    await userEvent.click(screen.getByText('Settings'))
    await waitFor(() => expect(mockApi.config).toHaveBeenCalled())
    expect(await screen.findByDisplayValue('/home/u/decks')).toBeTruthy()
  })

  it('saves a new deck output directory', async () => {
    mockApi.setDeckRoot.mockResolvedValue({ saved: true, deckRoot: '/tmp/decks' })
    await renderPage()
    await screen.findByTestId('page-header')
    await userEvent.click(screen.getByText('Settings'))
    const input = await screen.findByDisplayValue('/home/u/decks')
    await userEvent.clear(input)
    await userEvent.type(input, '/tmp/decks')
    await userEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.setDeckRoot).toHaveBeenCalledWith('/tmp/decks'))
  })
})
