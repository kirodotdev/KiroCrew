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
  BOARD_WIDTH,
  DECK_TABS,
  countBoardSlides,
  filterDecks,
  fuzzyMatch,
  libraryChatToken,
  nameFromFilename,
  prepareBoardHtml,
  tabAvailable,
  tabToFollow,
  templateAccents,
} from '../apps/pptx-maker/lib'
import BoardFrame, { BoardThumb } from '../apps/pptx-maker/BoardFrame'
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

describe('library chat tokens', () => {
  it('uses SDPM mention syntax and quotes names containing whitespace', () => {
    expect(libraryChatToken('styles', 'brand')).toBe('@style:brand')
    expect(libraryChatToken('styles', 'Brand Guide')).toBe('@style:"Brand Guide"')
    expect(libraryChatToken('templates', 'corp')).toBe('@template:corp')
    expect(libraryChatToken('templates', 'Company Theme')).toBe('@template:"Company Theme"')
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

  // `<link rel=preconnect>` is NOT a fetch, so no CSP fetch directive governs it.
  // Measured across engines: WebKit/Safari opens a real TCP connection per distinct
  // host (Chromium and Firefox open none), and `connect-src`/`prefetch-src` do not
  // stop it — so a board naming a bank of attacker-chosen hosts is a script-free
  // side channel that encodes deck content in WHICH hosts it dials. Deleting the
  // element is the only mechanism that closes it.
  it('strips link elements, the one egress the CSP cannot deny', () => {
    const out = prepareBoardHtml(
      '<link rel="preconnect" href="https://attacker.example">'
      + '<div class="slide">deck</div>',
    )
    expect(out).not.toMatch(/<link/i)
    expect(out).not.toContain('attacker.example')
    // The board's own content survives.
    expect(out).toContain('deck')
  })

  it('strips a link SPLICED out of fragments, which a regex strip reassembles', () => {
    // The reason this uses a real DOM parse and not a regex. A single-pass regex
    // over the raw string is defeated by splicing: removing the inner `<link>`
    // joins its neighbours into an intact one. Measured — all three shapes below
    // survived a `/<link\b[^>]*>/gi`-style strip as live `<link rel=preconnect>`.
    const spliced = [
      '<lin<link>k rel="preconnect" href="https://evil.example">',
      '<li<link>nk rel="preconnect" href="https://evil.example">',
      '<<link>link rel="preconnect" href="https://evil.example">',
    ]
    for (const board of spliced) {
      const out = prepareBoardHtml(board)
      // Re-parse the RESULT and ask the parser, rather than pattern-matching the
      // string: the security property is whether the document the preview frame
      // builds contains anything that can DIAL OUT, not whether the text looks
      // clean. A spliced tag ends up as a bogus element name (`<lin<link`) that
      // carries no `rel`/`href`, so the hostname survives only as inert text —
      // which is why the assertion is about elements and attributes, not substrings.
      const doc = new DOMParser().parseFromString(out, 'text/html')
      expect(doc.querySelectorAll('link').length, board).toBe(0)
      const dialers = Array.from(doc.querySelectorAll('body *')).filter(
        (el) => el.hasAttribute('rel') || el.hasAttribute('href'),
      )
      expect(dialers, board).toHaveLength(0)
    }
  })

  it('strips link elements in the forms a parser actually accepts', () => {
    // Any attribute order, uppercase, a quoted `>` inside a value, and an
    // unterminated final tag — a regex that only matched the tidy form would leave
    // the leak reachable by writing the tag slightly differently.
    const out = prepareBoardHtml(
      '<LINK HREF="https://a.example" REL=preconnect>'
      + "<link rel='preconnect' title='a>b' href='https://b.example'>"
      + '<link rel="preconnect" href="https://c.example"',
    )
    const doc = new DOMParser().parseFromString(out, 'text/html')
    expect(doc.querySelectorAll('link')).toHaveLength(0)
    // Nothing left that can reach the network. An unterminated final tag is
    // dropped by the parser and its attributes degrade to inert text, so assert on
    // the parsed tree rather than on the absence of the hostname substring.
    const dialers = Array.from(doc.querySelectorAll('body *')).filter(
      (el) => el.hasAttribute('rel') || el.hasAttribute('href'),
    )
    expect(dialers).toHaveLength(0)
  })

  it('leaves a meta refresh in place, because the sandbox already refuses it', () => {
    // Deliberate: the declarative-refresh navigation is gated on the sandboxed
    // automatic-features flag, which `sandbox=""` sets (no `allow-scripts`) — per
    // the WHATWG shared declarative refresh steps, and verified refused in
    // Chromium, Firefox and WebKit with the CSP removed entirely. Stripping it
    // would be dead code; the real invariant is the empty sandbox, pinned below.
    const out = prepareBoardHtml('<meta http-equiv="refresh" content="0;url=https://x.example">')
    expect(out).toContain('http-equiv="refresh"')
  })

  it('counts slides, defaulting to one', () => {
    expect(countBoardSlides('<div class="slide">a</div><div class="slide">b</div>')).toBe(2)
    expect(countBoardSlides('<p>no slides here</p>')).toBe(1)
  })

  // THE load-bearing invariant for both board frames. Everything the preview
  // relies on to be inert — no script, no form submission, and the declarative
  // meta-refresh navigation left un-stripped above — follows from this attribute
  // being EMPTY. Adding a single token (`allow-scripts` above all) silently
  // re-opens all three at once, and no other assertion in this file would notice.
  it('renders both board frames with a fully empty sandbox', () => {
    const { container, unmount } = render(
      <>
        <BoardFrame html="<div class='slide'>a</div>" title="board" />
        <BoardThumb html="<div class='slide'>a</div>" title="thumb" />
      </>,
    )
    const frames = Array.from(container.querySelectorAll('iframe'))
    // BoardFrame only mounts its iframe once it has measured a width, which
    // jsdom reports as 0 — so assert on what IS rendered and require the thumb.
    expect(frames.length).toBeGreaterThan(0)
    for (const frame of frames) {
      expect(frame.getAttribute('sandbox')).toBe('')
    }
    unmount()
  })

  it('promotes both board frames onto their own compositing layer without losing the scale', () => {
    // BoardFrame only mounts its iframe once it has measured a container
    // width, and jsdom reports 0 — pin the measurement so the scaled frame
    // actually renders and its transform can be asserted.
    const rect = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
      width: 960, height: 540, top: 0, left: 0, right: 960, bottom: 540, x: 0, y: 0,
      toJSON: () => ({}),
    } as DOMRect)
    try {
      const { container, unmount } = render(
        <>
          <BoardFrame html="<div class='slide'>a</div>" title="board" />
          <BoardThumb html="<div class='slide'>a</div>" width={52} title="thumb" />
        </>,
      )
      const frames = Array.from(container.querySelectorAll('iframe'))
      expect(frames.length).toBe(2)
      // `translateZ(0)` must be COMPOSED onto the scale, not replace it: the
      // scale is each frame's whole geometry, and a bare translateZ(0) would
      // render the 1920px document at full size inside the preview box.
      expect(frames[0].style.transform).toBe(`scale(${960 / BOARD_WIDTH}) translateZ(0)`)
      expect(frames[1].style.transform).toBe(`scale(${52 / BOARD_WIDTH}) translateZ(0)`)
      for (const frame of frames) {
        expect(frame.style.transformOrigin).toBe('top left')
      }
      unmount()
    } finally {
      rect.mockRestore()
    }
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
  api: {
    createChatSlot: vi.fn(async () => ({ key: 'pptx-1' })),
    chatSlotContext: vi.fn(async () => ({})),
    revealPath: vi.fn(async () => ({})),
    chatSlots: vi.fn(async () => [{ key: 'pptx-1', title: 'AWS intro deck', running: true, messages: 3 }]),
  },
}))

const copyToClipboardMock = vi.fn(async (_text: string) => true)
vi.mock('../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyToClipboard: (text: string) => copyToClipboardMock(text),
}))

// Keep the real router (MemoryRouter, the route hooks the page relies on) but
// make `useNavigate` observable, so a test can assert that a failed deck-start
// does NOT navigate. Everything else resolves to the actual module.
const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual =
    await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

// The animated SVG renderer fetches and mutates real DOM; the page tests care
// that a slide slot renders, not how the SVG is assembled. The sanitiser helper
// this module also exports is covered by SlidePreviewSanitize.test.tsx, which does
// not mock it (importing the real module HERE re-enters the hoisted api mock).
// The studio docks the real native chat pane, which needs the whole Redux chat
// store. The page tests care which slot it is handed, not how it renders.
vi.mock('../components/ChatPane', () => ({
  default: ({ slotKey, onOpenFull }: { slotKey: string; onOpenFull?: () => void }) => (
    <div data-testid="studio-chat-pane" data-slot={slotKey}>
      <button type="button" onClick={onOpenFull}>pane-open-full</button>
    </div>
  ),
}))

vi.mock('../apps/pptx-maker/SlidePreview', () => ({
  default: ({ label }: { label: string }) => <div data-testid="slide-preview">{label}</div>,
}))

const READY_ENGINE = {
  ready: true,
  clone: true,
  venv: true,
  pinnedTag: 'v0.10.1',
  installedTag: 'v0.10.1',
  updateRequired: false,
  agentReady: true,
  provision: { state: 'done' as const, log: '', elapsed: 0 },
}

let lastClient: QueryClient | null = null

async function renderPage() {
  const { default: PptxMakerPage } = await import('../apps/pptx-maker/PptxMakerPage')
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  lastClient = client
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <PptxMakerPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('PptxMakerPage', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    // The studio chat is remembered across visits; each test starts with none.
    localStorage.clear()
    const { api } = await import('../api/client')
    vi.mocked(api.chatSlots).mockResolvedValue([{ key: 'pptx-1', title: 'AWS intro deck', running: true, messages: 3 } as never])
    copyToClipboardMock.mockResolvedValue(true)
    mockApi.engine.mockResolvedValue(READY_ENGINE)
    mockApi.deps.mockResolvedValue({
      labels: {},
      present: {},
      managed: {},
      missing: [],
      hints: {},
    })
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
      pinnedTag: 'v0.10.1',
      installedTag: null,
      updateRequired: false,
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    await renderPage()
    expect(await screen.findByText(/is not installed yet/i)).toBeTruthy()
    expect(screen.getByText('Install engine')).toBeTruthy()
  })

  it('reports a failed update as an error notice, not a muted banner line', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: true,
      pinnedTag: 'v0.10.4',
      installedTag: 'v0.3.8',
      updateRequired: true,
      provision: { state: 'error', log: 'archive digest mismatch', elapsed: 0 },
    })
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The presentation engine could not be updated.')
    expect(alert.textContent).toContain('archive digest mismatch')
    await userEvent.click(screen.getByText('Update engine'))
    await waitFor(() => expect(mockApi.provisionEngine).toHaveBeenCalled())
  })

  it('points the start hint at Finish setup when only the agent is missing', async () => {
    mockApi.engine.mockResolvedValue({ ...READY_ENGINE, agentReady: false })
    await renderPage()
    expect(await screen.findByText('Finish setup above to start a deck.')).toBeTruthy()
  })

  it('reports a failed agent registration as an error notice with Finish setup', async () => {
    mockApi.engine.mockResolvedValue({
      ...READY_ENGINE,
      agentReady: false,
      provision: { state: 'error', log: 'agent registration failed', elapsed: 0 },
    })
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Deck chat couldn't be set up.")
    expect(alert.textContent).toContain('agent registration failed')
    expect(screen.getByText('Finish setup')).toBeTruthy()
  })

  it('reports a provision request that never reached the job', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.10.4',
      installedTag: null,
      updateRequired: false,
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    mockApi.provisionEngine.mockRejectedValueOnce(new Error('gateway unreachable'))
    await renderPage()
    await userEvent.click(await screen.findByText('Install engine'))
    const notice = await screen.findByTestId('engine-request-error')
    expect(notice.textContent).toContain("Couldn't start the presentation engine setup.")
    expect(notice.textContent).toContain('gateway unreachable')
  })

  it('reports a failed install as an error notice with a retry', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.10.1',
      installedTag: null,
      updateRequired: false,
      provision: { state: 'error', log: 'resolving engine dependencies…\nuv sync failed (exit 1)', elapsed: 0 },
    })
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The presentation engine could not be installed.')
    expect(alert.textContent).toContain('uv sync failed (exit 1)')
    await userEvent.click(screen.getByText('Try the install again'))
    await waitFor(() => expect(mockApi.provisionEngine).toHaveBeenCalled())
  })

  it('offers an explicit update when an older engine is installed', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.10.1',
      installedTag: 'v0.3.8',
      updateRequired: true,
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    await renderPage()
    expect(await screen.findByText(/v0\.3\.8.*v0\.10\.1/)).toBeTruthy()
    expect(screen.getByText('Update engine')).toBeTruthy()
    // The start hint names the control the banner offers, not an install.
    expect(screen.getByText('Update the presentation engine above to start a deck.')).toBeTruthy()
    expect(screen.queryByText('Install the presentation engine above to start a deck.')).toBeNull()
  })

  it('starts the engine install when the banner action is used', async () => {
    mockApi.engine.mockResolvedValue({
      ready: false,
      clone: false,
      venv: false,
      pinnedTag: 'v0.10.1',
      installedTag: null,
      updateRequired: false,
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
      pinnedTag: 'v0.10.1',
      installedTag: null,
      updateRequired: false,
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
      managed: { soffice: false },
      missing: ['soffice'],
      hints: {},
    })
    await renderPage()
    expect(await screen.findByText(/LibreOffice is not installed/)).toBeTruthy()
  })

  it('shows the install command for a dependency it cannot install itself', async () => {
    // The warning used to name the tool and stop, leaving no next step.
    mockApi.deps.mockResolvedValue({
      labels: { soffice: 'LibreOffice' },
      present: { soffice: false },
      managed: { soffice: false },
      missing: ['soffice'],
      hints: { soffice: 'brew install --cask libreoffice' },
    })
    await renderPage()
    // The command is interpolated into the one whole-sentence key (the i18n gate
    // rejects a sentence split across keys), so match the substring.
    expect(await screen.findByText(/brew install --cask libreoffice/)).toBeTruthy()
  })

  it('says nothing once a tool resolves from the app-managed install', async () => {
    // pdftoppm ships with the app now, so warning about it would be a false alarm.
    mockApi.deps.mockResolvedValue({
      labels: { soffice: 'LibreOffice', pdftoppm: 'poppler' },
      present: { soffice: true, pdftoppm: true },
      managed: { soffice: false, pdftoppm: true },
      missing: [],
      hints: {},
    })
    await renderPage()
    expect(screen.queryByText(/is not installed/)).toBeNull()
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

  it.each([
    ['Spec mode', 'Interaction mode: dialogue'],
    ['Vibe mode', 'Interaction mode: fast'],
    ['Style creator', 'The user wants to create a reusable style. Call start_style() first.'],
  ])('opens the studio chat with %s context without leaving the page', async (label, context) => {
    const { api } = await import('../api/client')
    await renderPage()
    await userEvent.click(await screen.findByText(label))
    const pane = await screen.findByTestId('studio-chat-pane')
    expect(pane.getAttribute('data-slot')).toBe('pptx-1')
    expect(navigateSpy).not.toHaveBeenCalled()
    expect(api.createChatSlot).toHaveBeenCalledWith(
      undefined, 'pptx-maker', undefined, undefined, 'persistent',
    )
    expect(api.chatSlotContext).toHaveBeenCalledWith(
      'pptx-1', context, { source: 'pptx-maker', ephemeral: true },
    )
  })

  it('reports a lost mode context beside the studio chat instead of swallowing it', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.chatSlotContext).mockRejectedValueOnce(new Error('context unavailable'))
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    expect(await screen.findByTestId('studio-chat-pane')).toBeTruthy()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The chat opened without the selected mode. Tell the agent which mode you want in the chat.')
    expect(alert.textContent).toContain('context unavailable')
  })

  it('explains the disabled start buttons when the engine status cannot be read', async () => {
    mockApi.engine.mockRejectedValue(new Error('engine status unavailable'))
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Couldn't check the presentation engine.")
    expect(alert.textContent).toContain('engine status unavailable')
    const vibe = screen.getByText('Vibe mode').closest('button') as HTMLButtonElement
    expect(vibe.disabled).toBe(true)
  })

  it('reports an unreadable engine status in the studio layout too, without a hand-off', async () => {
    localStorage.setItem('kc:pptx-maker:studio-chat', JSON.stringify({ slot: 'pptx-1', open: true }))
    mockApi.engine.mockRejectedValue(new Error('engine status unavailable'))
    await renderPage()
    expect(screen.getByTestId('studio-chat-pane')).toBeTruthy()
    const notice = await screen.findByTestId('engine-status-error')
    expect(notice.textContent).toContain("Couldn't check the presentation engine.")
    // The ChatPane beside it may hold a draft, so the notice does not navigate away.
    expect(notice.textContent).not.toContain('Ask the agent')
  })

  it('keeps the deck preview beside the studio chat', async () => {
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    // The decks card (list + viewer) stays mounted next to the chat.
    expect(screen.getByLabelText('Search decks…')).toBeTruthy()
    // The start card and stat row are hidden while the studio is open.
    expect(screen.queryByText('Vibe mode')).toBeNull()
  })

  it('hands the studio chat to the full chat surface on request', async () => {
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    await userEvent.click(screen.getByRole('button', { name: 'Open in full chat' }))
    expect(navigateSpy).toHaveBeenCalledWith('/chat?sid=pptx-1')
  })

  it('comes back with the studio chat the user left open, from the first render', async () => {
    const first = await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    // Leaving for another app or the main chat unmounts the page and drops ?chat=.
    first.unmount()
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot).mockClear()
    await renderPage()
    // Synchronously present: no decks-layout frame before the studio appears.
    const pane = screen.getByTestId('studio-chat-pane')
    expect(pane.getAttribute('data-slot')).toBe('pptx-1')
    expect(screen.queryByText('Start a deck')).toBeNull()
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('forgets a remembered studio chat whose session is gone', async () => {
    const first = await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    first.unmount()
    const { api } = await import('../api/client')
    vi.mocked(api.chatSlots).mockResolvedValue([])
    await renderPage()
    await waitFor(() => expect(screen.queryByTestId('studio-chat-pane')).toBeNull())
    expect(localStorage.getItem('kc:pptx-maker:studio-chat')).toBeNull()
  })

  it('shows the way back at once even when the session list predates the chat', async () => {
    const { api } = await import('../api/client')
    // The list the page fetched first knows nothing of the chat about to start.
    vi.mocked(api.chatSlots).mockResolvedValueOnce([])
    localStorage.setItem('kc:pptx-maker:studio-chat', JSON.stringify({ slot: 'older', open: false }))
    await renderPage()
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    await userEvent.click(screen.getByRole('button', { name: 'Close the chat' }))
    // Not hidden behind the stale list while it refetches.
    expect(screen.getByRole('button', { name: /^Reopen the chat/ })).toBeTruthy()
  })

  it('names the closed-chat entry as a chat in visible text', async () => {
    localStorage.setItem('kc:pptx-maker:studio-chat', JSON.stringify({ slot: 'pptx-1', open: false, title: 'tei' }))
    await renderPage()
    const entry = screen.getByRole('button', { name: 'Reopen the chat “tei”' })
    expect(entry.textContent).toContain('Reopen chat')
    expect(entry.textContent).toContain('tei')
  })

  it('reports a failed lookup of the remembered chat instead of hiding it', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.chatSlots).mockRejectedValue(new Error('gateway unavailable'))
    localStorage.setItem('kc:pptx-maker:studio-chat', JSON.stringify({ slot: 'pptx-1', open: false }))
    await renderPage()
    const notice = await screen.findByTestId('studio-chat-lookup-error')
    expect(notice.textContent).toContain("Couldn't check the deck chat you closed.")
    // The entry still offers the chat back from memory.
    expect(screen.getByRole('button', { name: /^Reopen the chat/ })).toBeTruthy()
  })

  it('offers a closed studio chat back, by name, from the decks card', async () => {
    const first = await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    await userEvent.click(screen.getByRole('button', { name: 'Close the chat' }))
    await waitFor(() => expect(screen.queryByTestId('studio-chat-pane')).toBeNull())
    first.unmount()
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot).mockClear()
    // Hold the session list back: the entry must not wait for it.
    vi.mocked(api.chatSlots).mockClear()
    let releaseSlots: (slots: never[]) => void = () => {}
    vi.mocked(api.chatSlots).mockImplementationOnce(() => new Promise((resolve) => { releaseSlots = resolve }))
    await renderPage()
    expect(screen.queryByTestId('studio-chat-pane')).toBeNull()
    const resume = screen.getByRole('button', { name: 'Reopen the chat “AWS intro deck”' })
    expect(resume.textContent).not.toContain('Working')
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    releaseSlots([{ key: 'pptx-1', title: 'AWS intro deck', running: true, messages: 3 } as never])
    // Re-queried: the entry moves into the deck list once the decks load.
    const named = { name: 'Reopen the chat “AWS intro deck”' }
    await waitFor(() => expect(screen.getByRole('button', named).textContent).toContain('Working'))
    await userEvent.click(screen.getByRole('button', named))
    const pane = await screen.findByTestId('studio-chat-pane')
    expect(pane.getAttribute('data-slot')).toBe('pptx-1')
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('copies the deck path, the deck_id a new chat can name', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'More deck actions' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /Copy deck path/ }))
    expect(copyToClipboardMock).toHaveBeenCalledWith('/home/u/decks/20260101-demo')
    expect(await screen.findByText('Copied')).toBeTruthy()
  })

  it('shows the deck path when the clipboard refuses the copy', async () => {
    copyToClipboardMock.mockResolvedValue(false)
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'More deck actions' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /Copy deck path/ }))
    const notice = await screen.findByTestId('deck-viewer-copy-error')
    expect(notice.textContent).toContain("Couldn't copy the deck path")
    expect(notice.textContent).toContain('/home/u/decks/20260101-demo')
  })

  it('closes the studio chat back to the deck page', async () => {
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    await userEvent.click(screen.getByRole('button', { name: 'Close the chat' }))
    await waitFor(() => expect(screen.queryByTestId('studio-chat-pane')).toBeNull())
    expect(await screen.findByText('Vibe mode')).toBeTruthy()
  })

  it('follows the deck the studio chat creates, once', async () => {
    const older = deck({ deckId: '20260101-older', name: 'Older deck' })
    const other = deck({ deckId: '20260102-other', name: 'Other deck' })
    mockApi.decks.mockResolvedValue({ decks: [other, older] })
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    await screen.findByTestId('studio-chat-pane')
    await userEvent.click(await screen.findByText('Older deck'))
    await waitFor(() => expect(mockApi.deck).toHaveBeenLastCalledWith('20260101-older'))

    // The chat creates its deck: the preview jumps to it without a click.
    const created = deck({ deckId: '20260930-created', name: 'Created deck' })
    mockApi.decks.mockResolvedValue({ decks: [created, other, older] })
    await lastClient!.invalidateQueries({ queryKey: ['pptx-maker', 'decks'] })
    await waitFor(() => expect(mockApi.deck).toHaveBeenLastCalledWith('20260930-created'))

    // After that the user's own choice wins over later polls.
    await userEvent.click(screen.getByText('Older deck'))
    await waitFor(() => expect(mockApi.deck).toHaveBeenLastCalledWith('20260101-older'))
    await lastClient!.invalidateQueries({ queryKey: ['pptx-maker', 'decks'] })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(mockApi.deck).toHaveBeenLastCalledWith('20260101-older')
  })

  it('does not start a chat while the agent is still unregistered', async () => {
    const { api } = await import('../api/client')
    mockApi.engine.mockResolvedValue({ ...READY_ENGINE, agentReady: false })
    await renderPage()
    expect(
      await screen.findByText("Deck chat isn't ready yet. Finish setup to start a deck."),
    ).toBeTruthy()
    expect(screen.getByText('Finish setup')).toBeTruthy()
    const vibe = screen.getByText('Vibe mode').closest('button') as HTMLButtonElement
    expect(vibe.disabled).toBe(true)
    await userEvent.click(vibe)
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('does not start a chat until the engine is installed', async () => {
    const { api } = await import('../api/client')
    mockApi.engine.mockResolvedValue({
      ...READY_ENGINE,
      ready: false,
      clone: false,
      venv: false,
      installedTag: null,
      provision: { state: 'idle', log: '', elapsed: 0 },
    })
    await renderPage()
    expect(
      await screen.findByText('Install the presentation engine above to start a deck.'),
    ).toBeTruthy()
    const vibe = screen.getByText('Vibe mode').closest('button') as HTMLButtonElement
    expect(vibe.disabled).toBe(true)
    await userEvent.click(vibe)
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('surfaces a visible error and does not navigate when the deck-start create fails', async () => {
    // The reporter of the flash-and-return symptom inferred that a failed
    // create was swallowed. It is not: a rejected createChatSlot sets the
    // mutation's error state, which renders an ErrorNotice under the buttons,
    // and onSuccess (the only navigate site) never runs. This pins that guard so
    // the visible-error path cannot be quietly removed and reopen the report.
    const { api } = await import('../api/client')
    ;(api.createChatSlot as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('deck start blew up'),
    )
    await renderPage()
    await userEvent.click(await screen.findByText('Vibe mode'))
    // The error is shown to the user (ErrorNotice renders role="alert").
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('deck start blew up')
    // And the bounce the reporter described (navigate away with nothing shown)
    // does not happen: the only navigate call is in onSuccess.
    expect(navigateSpy).not.toHaveBeenCalled()
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
