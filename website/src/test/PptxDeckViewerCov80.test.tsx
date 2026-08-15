import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComposeDefs, DeckDetail } from '../apps/pptx-maker/api'

const deck = vi.fn()
const fetchArtifactText = vi.fn()
const fetchArtifactJson = vi.fn()
vi.mock('../apps/pptx-maker/api', async () => {
  const actual = await vi.importActual<typeof import('../apps/pptx-maker/api')>(
    '../apps/pptx-maker/api',
  )
  return { ...actual, pptxMakerApi: { deck }, fetchArtifactText, fetchArtifactJson }
})

const revealPath = vi.fn()
vi.mock('../api/client', () => ({ api: { revealPath } }))

// The three leaf renderers are stubbed: this file's job is tab routing and the
// follow rule, and each leaf (markdown, the sandboxed board frame, the SVG slide)
// has its own tests.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../apps/pptx-maker/BoardFrame', () => ({
  default: ({ html }: { html: string }) => <div data-testid="board">{html}</div>,
}))
vi.mock('../apps/pptx-maker/SlidePreview', () => ({
  default: ({ label, defs }: { label: string; defs: ComposeDefs | null }) => (
    <div data-testid="slide">{`${label}|${defs?.defs ?? 'no-defs'}`}</div>
  ),
}))

const DeckViewer = (await import('../apps/pptx-maker/DeckViewer')).default

function detail(over: Partial<DeckDetail> = {}): DeckDetail {
  return {
    deckId: 'zzq-deck',
    name: 'zzq-deck-name',
    defsUrl: null,
    pptxUrl: null,
    dirPath: '',
    pptxPath: null,
    specs: {},
    updatedAt: {},
    slides: [],
    ...over,
  }
}

function renderViewer(deckId = 'zzq-deck') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}><DeckViewer deckId={deckId} /></QueryClientProvider>,
  )
  return {
    ...view,
    qc,
    /** Stand in for the next poll of `deckId` landing, without waiting on the
     *  real 1.5s interval — the follow rule reacts to the DATA changing. */
    poll: async (id: string, next: DeckDetail) => {
      await act(async () => {
        qc.setQueryData(['pptx-maker', 'deck', id], next)
      })
    },
    switchDeck: (next: string) => view.rerender(
      <QueryClientProvider client={qc}><DeckViewer deckId={next} /></QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  revealPath.mockResolvedValue(undefined)
})

describe('DeckViewer — shell states', () => {
  it('says it is loading before the first poll answers', () => {
    deck.mockImplementation(() => new Promise(() => {}))
    renderViewer()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('reports a deck that does not resolve', async () => {
    deck.mockResolvedValue(undefined)
    renderViewer()
    expect(await screen.findByText('Deck not found')).toBeInTheDocument()
  })

  it('offers only the tabs the deck actually has, plus Slides', async () => {
    deck.mockResolvedValue(detail({ specs: { brief: 'zzq/brief.md' } }))
    renderViewer()
    expect(await screen.findByText('Brief')).toBeInTheDocument()
    expect(screen.getByText('Slides')).toBeInTheDocument()
    expect(screen.queryByText('Outline')).toBeNull()
    expect(screen.queryByText('Art direction')).toBeNull()
  })
})

describe('DeckViewer — slides tab', () => {
  it('opens on an empty state when nothing has been composed', async () => {
    deck.mockResolvedValue(detail())
    renderViewer()
    expect(await screen.findByText('No slides yet')).toBeInTheDocument()
  })

  it('renders one numbered preview per composed slide, sharing the deck defs', async () => {
    deck.mockResolvedValue(detail({
      defsUrl: 'zzq/defs.json',
      slides: [
        { slug: 'zzq-title', previewUrl: null, composeUrl: 'zzq/1.json' },
        { slug: 'zzq-body', previewUrl: null, composeUrl: 'zzq/2.json' },
      ],
    }))
    fetchArtifactJson.mockResolvedValue({ defs: 'zzq-shared-defs' })
    renderViewer()

    await waitFor(() =>
      expect(screen.getAllByTestId('slide')[0].textContent).toContain('zzq-shared-defs'))
    const slides = screen.getAllByTestId('slide')
    expect(slides.map((s) => s.textContent?.split('|')[0])).toEqual([
      '1. zzq-title', '2. zzq-body',
    ])
    expect(fetchArtifactJson).toHaveBeenCalledWith('zzq/defs.json')
  })

  it('holds an aspect-ratio placeholder for a slide with no compose payload yet', async () => {
    deck.mockResolvedValue(detail({
      slides: [{ slug: 'zzq-pending', previewUrl: null, composeUrl: null }],
    }))
    renderViewer()
    // The slug row is drawn either way; the preview itself is not.
    expect(await screen.findByText('1. zzq-pending')).toBeInTheDocument()
    expect(screen.queryByTestId('slide')).toBeNull()
    // Never fetched — there is no defs URL on this deck.
    expect(fetchArtifactJson).not.toHaveBeenCalled()
  })
})

describe('DeckViewer — deliverable tabs', () => {
  it('reads a markdown deliverable and renders it', async () => {
    deck.mockResolvedValue(detail({ specs: { outline: 'zzq/outline.md' } }))
    fetchArtifactText.mockResolvedValue('zzq-outline-body')
    renderViewer()

    await userEvent.click(await screen.findByText('Outline'))
    expect(await screen.findByTestId('md')).toHaveTextContent('zzq-outline-body')
    expect(fetchArtifactText).toHaveBeenCalledWith('zzq/outline.md')
  })

  it('says a markdown deliverable could not be read rather than drawing nothing', async () => {
    deck.mockResolvedValue(detail({ specs: { brief: 'zzq/brief.md' } }))
    fetchArtifactText.mockRejectedValue(new Error('zzq-read-failed'))
    renderViewer()

    await userEvent.click(await screen.findByText('Brief'))
    expect(await screen.findByText('This deliverable could not be read.')).toBeInTheDocument()
  })

  it('hands the art-direction document to the board frame', async () => {
    deck.mockResolvedValue(detail({ specs: { artDirection: 'zzq/board.html' } }))
    fetchArtifactText.mockResolvedValue('<p>zzq-board-markup</p>')
    renderViewer()

    await userEvent.click(await screen.findByText('Art direction'))
    expect(await screen.findByTestId('board')).toHaveTextContent('zzq-board-markup')
  })

  it('says so when the art-direction document could not be read', async () => {
    deck.mockResolvedValue(detail({ specs: { artDirection: 'zzq/board.html' } }))
    fetchArtifactText.mockRejectedValue(new Error('zzq-board-failed'))
    renderViewer()

    await userEvent.click(await screen.findByText('Art direction'))
    expect(await screen.findByText('This deliverable could not be read.')).toBeInTheDocument()
  })
})

describe('DeckViewer — header actions', () => {
  it('reveals the deck folder on the host', async () => {
    deck.mockResolvedValue(detail({ dirPath: '/zzq/decks/one' }))
    renderViewer()
    await userEvent.click(await screen.findByText('Reveal folder'))
    expect(revealPath).toHaveBeenCalledWith('/zzq/decks/one')
  })

  it('swallows a failed reveal rather than throwing into the render', async () => {
    revealPath.mockRejectedValue(new Error('zzq-no-file-manager'))
    deck.mockResolvedValue(detail({ dirPath: '/zzq/decks/one' }))
    renderViewer()
    await userEvent.click(await screen.findByText('Reveal folder'))
    await waitFor(() => expect(revealPath).toHaveBeenCalled())
    expect(screen.getByText('Reveal folder')).toBeInTheDocument()
  })

  it('offers the download only once a pptx exists, named after the deck', async () => {
    deck.mockResolvedValue(detail({ pptxUrl: 'decks/zzq/out.pptx', dirPath: '' }))
    renderViewer()
    const link = await screen.findByText('Download .pptx')
    const anchor = link.closest('a') as HTMLAnchorElement
    expect(anchor.getAttribute('href')).toBe('/api/apps/pptx-maker/decks/zzq/out.pptx')
    expect(anchor.getAttribute('download')).toBe('zzq-deck-name.pptx')
    expect(screen.queryByText('Reveal folder')).toBeNull()
  })
})

describe('DeckViewer — following the agent', () => {
  it('does not move the tab on the first poll of an existing deck', async () => {
    // Otherwise opening a finished deck yanks the user to whatever was last
    // touched days ago.
    deck.mockResolvedValue(detail({
      specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 5000 },
    }))
    renderViewer()
    await screen.findByText('Brief')
    expect(await screen.findByText('No slides yet')).toBeInTheDocument()
  })

  it('follows the deliverable that changed between polls', async () => {
    fetchArtifactText.mockResolvedValue('zzq-brief-body')
    deck.mockResolvedValue(detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 1 } }))
    const { poll } = renderViewer()
    await screen.findByText('Brief')

    // Second poll sees brief's mtime move, so the viewer switches to it.
    await poll('zzq-deck', detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 9 } }))
    expect(await screen.findByTestId('md')).toHaveTextContent('zzq-brief-body')
  })

  it('resets the follow baseline when the user switches decks', async () => {
    fetchArtifactText.mockResolvedValue('zzq-brief-body')
    deck.mockResolvedValue(detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 1 } }))
    const { poll, qc, switchDeck } = renderViewer('zzq-deck')
    await screen.findByText('Brief')
    await poll('zzq-deck', detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 9 } }))
    await screen.findByTestId('md')

    // Another deck whose brief carries a HIGHER mtime than the one just followed.
    qc.setQueryData(['pptx-maker', 'deck', 'zzq-other'], detail({
      deckId: 'zzq-other', specs: { brief: 'zzq/other.md' }, updatedAt: { brief: 99 },
    }))
    switchDeck('zzq-other')

    // Back on Slides: the new deck's first observation must not be diffed against
    // the previous deck's timestamps.
    expect(await screen.findByText('No slides yet')).toBeInTheDocument()
  })

  it('falls back to Slides when the followed tab loses its content', async () => {
    fetchArtifactText.mockResolvedValue('zzq-brief-body')
    deck.mockResolvedValue(detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 1 } }))
    const { poll } = renderViewer()
    await screen.findByText('Brief')
    await poll('zzq-deck', detail({ specs: { brief: 'zzq/brief.md' }, updatedAt: { brief: 9 } }))
    await screen.findByTestId('md')

    // The deliverable is gone from a later poll; the shell must not render a tab
    // with nothing behind it.
    await poll('zzq-deck', detail({ specs: {}, updatedAt: { brief: 9 } }))
    expect(await screen.findByText('No slides yet')).toBeInTheDocument()
  })
})
