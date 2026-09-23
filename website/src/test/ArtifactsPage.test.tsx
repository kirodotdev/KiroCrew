import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentType } from 'react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

// The thumbnail frame mints a gateway document instead of building a blob. The
// automock resolves every api method to `undefined`, which it cannot await.
beforeEach(() => {
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})


vi.mock('../api/client')

// VirtuosoMasonry virtualizes against real layout, which jsdom lacks, so it
// renders zero items in tests. Mock it to a plain map through ItemContent so
// the card content + handlers are exercised.
vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: ({ data, context, ItemContent }: {
    data: unknown[]
    context: unknown
    ItemContent: ComponentType<{ data: unknown; index: number; context: unknown }>
  }) => (
    <div data-testid="masonry">
      {data.map((d, i) => (
        <ItemContent key={i} data={d} index={i} context={context} />
      ))}
    </div>
  ),
}))

const mkArtifact = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'widget',
  source: 'chat',
  pinned: true,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  ...overrides,
})

/**
 * The filter-bar dropdowns. `SimpleSelect` wraps a Radix Select, so each is a
 * <button role="combobox"> whose rows exist only while the popup is open.
 */
const kindTrigger = () => screen.getByRole('combobox', { name: 'Filter by kind' })
const tagTrigger = () => screen.getByRole('combobox', { name: 'Filter by tag' })

describe('ArtifactsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-artifacts-view')
  })

  it('shows empty state when no artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
  })

  it('renders the library with artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue', { tags: ['ops', 'cr'], version: 3 }),
        mkArtifact('pipeline-health', { tags: ['ops'], kind: 'widget' }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(screen.getByText('pipeline health')).toBeInTheDocument()
    expect(screen.getByText('cr-queue')).toBeInTheDocument()
    expect(screen.getByText(/v3/)).toBeInTheDocument()
  })
  it('renders an image artifact card with an <img> from the asset URL and an image badge', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('sunset-photo', {
        kind: 'image',
        image: { mime: 'image/png', ext: 'png', alt: 'A sunset' },
      })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact('sunset-photo', { kind: 'image', image: { mime: 'image/png', ext: 'png', alt: 'A sunset' } }),
    )
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('sunset photo')).toBeInTheDocument())
    // The card's thumbnail streams from the artifact's asset endpoint.
    const img = screen.getByAltText('A sunset') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/api/artifacts/sunset-photo/asset')
    // The kind badge reads "image".
    expect(screen.getByText('image')).toBeInTheDocument()
    // Image cards are non-editable previews: no widget iframe is mounted.
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('reserves an image card box from stored natural dimensions before the bytes arrive', async () => {
    // The save-time header sniff records width/height exactly so the UI can
    // derive an aspect ratio BEFORE the lazy load lands. Without the
    // attributes the card mounts ~16px tall and grows ~280px when the image
    // arrives — in the virtualized gallery that late growth shoves everything
    // below it mid-scroll.
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('sized-photo', {
          kind: 'image',
          image: { mime: 'image/png', ext: 'png', alt: 'Sized', width: 800, height: 600 },
        }),
        mkArtifact('legacy-photo', {
          kind: 'image',
          image: { mime: 'image/png', ext: 'png', alt: 'Legacy' },
        }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByAltText('Sized')).toBeInTheDocument())
    const sized = screen.getByAltText('Sized') as HTMLImageElement
    expect(sized.getAttribute('width')).toBe('800')
    expect(sized.getAttribute('height')).toBe('600')
    // Older payloads without dimensions degrade to no attributes (today's
    // late-growth behavior), never width=0 or NaN.
    const legacy = screen.getByAltText('Legacy') as HTMLImageElement
    expect(legacy.getAttribute('width')).toBeNull()
    expect(legacy.getAttribute('height')).toBeNull()
  })

  it('rejects a non-finite widget height report instead of persisting it', async () => {
    // `typeof NaN === 'number'`, and NaN flows through min/max/round into
    // BOTH the persisted cache and the rendered height. One misbehaving
    // widget must not corrupt the geometry every later mount reserves from.
    const { widgetHeightKey, getWidgetHeight } = await import('../utils/widgetHeights')
    const art = mkArtifact('nan-widget')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [art] })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue({ ...art, content: '<div>w</div>' })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    const iframe = document.querySelector('iframe') as HTMLIFrameElement
    const key = widgetHeightKey('<div>w</div>', 'thumb900')
    const before = getWidgetHeight(key)
    fireEvent(window, new MessageEvent('message', {
      source: iframe.contentWindow,
      data: { type: 'mc-widget-height', height: NaN },
    }))
    expect(getWidgetHeight(key)).toBe(before) // NaN never lands in the cache
    // A finite report through the same handler still works (the guard is
    // narrow, not a broken listener).
    fireEvent(window, new MessageEvent('message', {
      source: iframe.contentWindow,
      data: { type: 'mc-widget-height', height: 240 },
    }))
    expect(getWidgetHeight(key)).toBe(240)
  })

  it('re-reserves from the cache when the lazy content arrives', async () => {
    // The card mounts before its lazy fetch resolves (hasPreview gates on
    // kind), so the one-time useState initializer ran against the EMPTY
    // content's key. The resync effect must re-read the cache for the real
    // content's key, or every mount pays one avoidable height correction.
    const { widgetHeightKey, setWidgetHeight } = await import('../utils/widgetHeights')
    const html = '<div>cached widget</div>'
    // Decoys first: with other entries in the space, the MEDIAN fallback the
    // empty-content initializer lands on (500) differs from the exact cached
    // height (333) — otherwise this test cannot tell resync from fallback.
    setWidgetHeight(widgetHeightKey('<div>decoy-a</div>', 'thumb900'), 500)
    setWidgetHeight(widgetHeightKey('<div>decoy-b</div>', 'thumb900'), 500)
    setWidgetHeight(widgetHeightKey(html, 'thumb900'), 333)
    const art = mkArtifact('cached-widget')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [art] })
    let resolveFull: (a: Artifact) => void = () => {}
    vi.mocked(api).artifact = vi.fn().mockReturnValue(new Promise((r) => { resolveFull = r }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cached widget')).toBeInTheDocument())
    resolveFull({ ...art, content: html } as Artifact)
    // scaledH = round(contentH * colW/BASE_W) = round(333 * 320/900) = 118.
    await waitFor(() => {
      const iframe = document.querySelector('iframe') as HTMLIFrameElement | null
      expect(iframe).not.toBeNull()
      const wrap = iframe!.closest('div[style*="height"]') as HTMLElement
      expect(wrap.style.height).toBe('118px')
    })
  })

  it('learns a legacy image size on load and reserves from it on the next mount', async () => {
    // Legacy artifacts (saved before the header sniff) have no dimensions in
    // metadata, so their first-ever view grows on load. The load records the
    // natural size client-side; every LATER mount — the "same cards bounce on
    // every pass" report — reserves the box up front.
    const legacyArt = mkArtifact('old-shot', {
      kind: 'image',
      image: { mime: 'image/png', ext: 'png', alt: 'Old shot' },
    })
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [legacyArt] })
    const first = renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByAltText('Old shot')).toBeInTheDocument())
    const img = screen.getByAltText('Old shot') as HTMLImageElement
    expect(img.getAttribute('width')).toBeNull() // nothing known yet
    // The image arrives: report a natural size (jsdom defaults these to 0).
    Object.defineProperty(img, 'naturalWidth', { configurable: true, get: () => 1024 })
    Object.defineProperty(img, 'naturalHeight', { configurable: true, get: () => 768 })
    fireEvent.load(img)
    first.unmount()
    // Second visit: the box is reserved before any bytes arrive.
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByAltText('Old shot')).toBeInTheDocument())
    const again = screen.getByAltText('Old shot') as HTMLImageElement
    expect(again.getAttribute('width')).toBe('1024')
    expect(again.getAttribute('height')).toBe('768')
  })


  it('renders Starred/All filter toggle', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // Starred/All toggle group is present (identified by its aria-label)
    const group = screen.getByRole('group', { name: /Filter starred/i })
    expect(group).toBeInTheDocument()
    expect(group.querySelector('button')).toBeInTheDocument()
  })

  it('renders Source column in the table view', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { source: 'dashboard', session_title: 'My Session' })],
    })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // Source column header exists in table view
    expect(screen.getByText('Source')).toBeInTheDocument()
    // Source value renders session_title when available
    expect(screen.getByText('My Session')).toBeInTheDocument()
  })

  it('renders star toggle buttons for each artifact', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: true })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const starBtn = screen.getByLabelText('Remove star from artifact')
    expect(starBtn).toBeInTheDocument()
    expect(starBtn).toHaveAttribute('aria-pressed', 'true')
  })

  // The star used to exist ONLY in the table view, while the Starred filter and
  // the Starred StatCard applied to BOTH views — so in the default gallery you
  // could filter by starred without being able to star anything. `pinned` is
  // also the retention control (prune_auto_widgets only sweeps unpinned
  // records), so the gallery could not keep an artifact either.
  it('exposes the star on masonry cards in the default gallery view', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: false })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: false }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // No table in play — this is the card, not the row.
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    const starBtn = screen.getByLabelText('Star artifact')
    expect(starBtn).toHaveAttribute('aria-pressed', 'false')
  })

  it('stars an artifact from a masonry card without opening it', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: false })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: false }))
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star artifact'))

    await waitFor(() => expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', true))
    // The card body is itself a click target that navigates; the star must
    // stopPropagation so starring never doubles as "open this artifact".
    expect(window.location.pathname).not.toContain('cr-queue')
  })

  it('unstars an already-starred masonry card', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: true })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: true }))
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Remove star from artifact'))

    await waitFor(() => expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', false))
  })

  // Session documents (files badged "Artifact" in the chat transcript) used to
  // render ONLY as table/tree rows, while the DEFAULT gallery view showed
  // nothing for them — so a document could carry the "Artifact" badge in chat
  // yet be invisible on this page until the user discovered the table toggle.
  const mkDoc = (path: string, name: string) => ({
    path,
    name,
    updated_at: '2026-08-07T12:00:00',
    session_key: 'dashboard_chat-1',
    session_title: 'Research session',
    message_ts: 'm1',
    saved: false,
    slug: '',
  })

  it('surfaces unsaved session documents in the default gallery view', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    expect(screen.getByText(/From your chats/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Star document')).toBeInTheDocument()
  })

  it('shows session documents in the gallery even when the library is empty', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    expect(screen.getByText('FINDINGS.md')).toBeInTheDocument()
    // With unsaved docs on screen, the empty state must not point at the chat
    // bookmark — it points at the adjacent star affordance instead.
    expect(screen.getByText(/star a document in .From your chats./i)).toBeInTheDocument()
    expect(screen.queryByText(/Click the bookmark icon/i)).not.toBeInTheDocument()
  })

  // At ≥30 artifacts the masonry virtualizes into a viewport-height scroller;
  // a section rendered after it hides below the fold. The section must precede
  // the gallery grid in DOM order.
  it('renders the session-docs section above the artifact gallery', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    const section = screen.getByText(/From your chats/i)
    const card = screen.getByText('cr queue')
    expect(section.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  // The section sits ABOVE the library, so it must be height-bounded: an
  // uncapped cross-session firehose would bury the saved artifacts — the same
  // failure this section exists to cure, inverted.
  it('caps the session-docs section at 5 with a Show all expander', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: Array.from({ length: 7 }, (_, i) => mkDoc(`/ws/doc-${i}.md`, `doc-${i}.md`)),
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('doc-0.md')).toBeInTheDocument())
    expect(screen.getByText('doc-4.md')).toBeInTheDocument()
    expect(screen.queryByText('doc-5.md')).not.toBeInTheDocument()

    await user.click(screen.getByText(/Show all \(7\)/i))
    expect(screen.getByText('doc-6.md')).toBeInTheDocument()

    await user.click(screen.getByText(/Show less/i))
    expect(screen.queryByText('doc-6.md')).not.toBeInTheDocument()
  })

  // A user with never-to-be-saved docs can put the section away for good;
  // the choice persists across visits via localStorage.
  it('collapses the session-docs section via its header and persists the choice', async () => {
    const user = userEvent.setup()
    localStorage.removeItem('mc-artifacts-session-docs-collapsed')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /From your chats/i }))
    expect(screen.queryByText('FINDINGS.md')).not.toBeInTheDocument()
    expect(localStorage.getItem('mc-artifacts-session-docs-collapsed')).toBe('1')

    await user.click(screen.getByRole('button', { name: /From your chats/i }))
    expect(screen.getByText('FINDINGS.md')).toBeInTheDocument()
  })

  // The section must not pop in after load and shift the gallery under the
  // user's cursor: while the docs query is pending a fixed-height skeleton
  // reserves the slot.
  it('reserves the section slot with a skeleton while session docs load', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    let resolveDocs!: (v: unknown) => void
    vi.mocked(api).artifactSessionDocs = vi.fn().mockReturnValue(new Promise((r) => { resolveDocs = r }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(document.querySelector('[aria-busy="true"]')).toBeInTheDocument()

    resolveDocs({ docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')] })
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    expect(document.querySelector('[aria-busy="true"]')).not.toBeInTheDocument()
  })

  it('materializes a session document from the gallery star', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    const materializeSpy = vi.fn().mockResolvedValue({})
    vi.mocked(api).materializeArtifact = materializeSpy
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))

    await waitFor(() => expect(materializeSpy).toHaveBeenCalledWith('/ws/research/FINDINGS.md', 'dashboard_chat-1'))
  })

  // Session-doc rows used to be dead on click — the ONLY affordance was the
  // star, which SAVES the document into the library. Clicking a row now opens
  // a read-only preview (via the redacting /api/file-read), so the user can
  // read a document before deciding to keep it.
  describe('session-doc preview', () => {
    const stubFileRead = (text: string) =>
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: 'OK',
        text: () => Promise.resolve(text),
      }))
    afterEach(() => vi.unstubAllGlobals())

    it('opens a read-only preview on row click, and its star saves then closes', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      const materializeSpy = vi.fn().mockResolvedValue({})
      vi.mocked(api).materializeArtifact = materializeSpy
      stubFileRead('# Findings headline')
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      await user.click(screen.getByText('FINDINGS.md'))
      const dialog = await screen.findByRole('dialog')
      // Content comes from the same redacting file-read the chat panel uses.
      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/file-read?path=' + encodeURIComponent('/ws/research/FINDINGS.md'),
      )
      await waitFor(() => expect(within(dialog).getByText('Findings headline')).toBeInTheDocument())

      // The header save button is the same materialize affordance the row's
      // star carries — now with its name visible ("Save to artifacts", named
      // after the surface it writes to, not the Apps "Library"); a
      // successful save closes the preview (the doc is a real artifact now)
      // and announces itself in the page's status notice.
      await user.click(within(dialog).getByRole('button', { name: /Save to artifacts/ }))
      await waitFor(() => expect(materializeSpy).toHaveBeenCalledWith('/ws/research/FINDINGS.md', 'dashboard_chat-1'))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      // The save's only other effects are removals (row unmounts, modal
      // closes) — the acknowledgment is what says "saved", not "vanished".
      expect(screen.getByText('Saved “FINDINGS.md” to your artifacts.')).toBeInTheDocument()
    })

    it('starring a row saves without opening the preview', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      stubFileRead('# Findings headline')
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      // The star sits inside the clickable row: saving must not ALSO open the
      // preview (SessionDocStar stops propagation).
      await user.click(screen.getByLabelText('Star document'))
      await waitFor(() => expect(vi.mocked(api).materializeArtifact).toHaveBeenCalled())
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })

    // The preview shares `['file-read', path]` with the chat side panel, whose
    // cold-tab hydration reads `{ text, ok, status }` from the same entry
    // within staleTime. A divergent shape here (say `{ text, missing }`) would
    // poison that consumer: preview a doc, open it in chat within 10s, and the
    // panel sees an entry with `ok`/`status` undefined.
    it('populates the shared file-read cache with the chat panel contract shape', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      stubFileRead('# Findings headline')
      const { queryClient } = renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      await user.click(screen.getByText('FINDINGS.md'))
      const dialog = await screen.findByRole('dialog')
      await waitFor(() => expect(within(dialog).getByText('Findings headline')).toBeInTheDocument())
      expect(queryClient.getQueryData(['file-read', '/ws/research/FINDINGS.md']))
        .toEqual({ text: '# Findings headline', ok: true, status: 200 })
    })

    // The row is a keyboard target too — and the star nested inside it bubbles
    // its own Enter/Space keydown up to the row. Keyboard-starring must save
    // WITHOUT opening the preview (and without the row's preventDefault
    // cancelling the star's native activation).
    it('opens the preview on row Enter; keyboard-starring saves without opening it', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      const materializeSpy = vi.fn().mockResolvedValue({})
      vi.mocked(api).materializeArtifact = materializeSpy
      stubFileRead('# Findings headline')
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      // Keyboard-activate the star first: materializes, no preview.
      screen.getByLabelText('Star document').focus()
      await user.keyboard('{Enter}')
      await waitFor(() => expect(materializeSpy).toHaveBeenCalledWith('/ws/research/FINDINGS.md', 'dashboard_chat-1'))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

      // Then Enter on the row itself opens the preview.
      const row = screen.getByText('FINDINGS.md').closest('[role="button"]') as HTMLElement
      row.focus()
      await user.keyboard('{Enter}')
      await screen.findByRole('dialog')
    })

    // SECURITY: fileReadUrl appends `resolve=1` to a relative path, and the
    // backend resolves that against the CURRENT project directory at request
    // time — not the project the path was recorded under. A relative doc
    // previewed after a project switch would silently read a same-named file
    // in the newly active project. The preview must refuse: no request may
    // leave the browser for a relative path.
    it('refuses to preview a relative-path document without fetching', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('notes/relative-plan.md', 'relative-plan.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      const fetchSpy = vi.fn()
      vi.stubGlobal('fetch', fetchSpy)
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('relative-plan.md')).toBeInTheDocument())

      await user.click(screen.getByText('relative-plan.md'))
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(/can't be opened safely from here/)).toBeInTheDocument()
      // The refusal is client-side and absolute: nothing was requested.
      expect(fetchSpy).not.toHaveBeenCalled()
      // Not retryable — retrying cannot make the path safe.
      expect(within(dialog).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
      // A refusal is not a failure: it renders as a status, not an alert
      // (review r3: danger dress on a deliberate refusal misreports it).
      expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
      expect(within(dialog).getByRole('status')).toBeInTheDocument()
      // And the save is withheld too: the backend materialize allowlist only
      // trusts paths absolute after expansion, and offering a save beside
      // refusal copy invites committing a document sight-unseen.
      expect(within(dialog).getByRole('button', { name: /Save to artifacts/ })).toBeDisabled()
    })

    // SECURITY: `~name` is only absolute if `name` is a real account —
    // expanduser leaves an unknown `~name` unchanged and the backend's
    // resolver then anchors it to the process CWD, the same cross-project
    // disclosure the refusal exists to prevent. Only `~` / `~/` (the
    // gateway user's own home, project-independent) may pass.
    it('refuses a ~name path like a relative one, without fetching', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('~nosuchuser/plan.md', 'plan.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      const fetchSpy = vi.fn()
      vi.stubGlobal('fetch', fetchSpy)
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('plan.md')).toBeInTheDocument())

      await user.click(screen.getByText('plan.md'))
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(/can't be opened safely from here/)).toBeInTheDocument()
      expect(fetchSpy).not.toHaveBeenCalled()
    })

    // The two failure branches must name what actually happened. An HTTP
    // error response means the request DID reach the server — copy blaming
    // the network there names exactly the cause the code has excluded (the
    // defect that got the previous shared copy blocked in review).
    it('names a server error as a server error', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false, status: 500, statusText: 'Internal Server Error',
        text: () => Promise.resolve(''),
      }))
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      await user.click(screen.getByText('FINDINGS.md'))
      const dialog = await screen.findByRole('dialog')
      await waitFor(() => expect(within(dialog).getByText(/the server reported an error/)).toBeInTheDocument())
      expect(within(dialog).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    })

    it('names a network failure as a network failure', async () => {
      const user = userEvent.setup()
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({})
      // fetch REJECTING (not an error status) is the network-failure shape.
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      await user.click(screen.getByText('FINDINGS.md'))
      const dialog = await screen.findByRole('dialog')
      await waitFor(() => expect(within(dialog).getByText(/didn't reach the server/)).toBeInTheDocument())
      expect(within(dialog).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    })
  })

  // The star is the section's ONLY action; a failed materialize used to stop
  // the spinner and change nothing else — indistinguishable from success. And
  // the banner must actually dismiss: the X resets every mutation feeding
  // mutErr, or clicking it re-renders the same error instantly.
  it('surfaces a failed materialize in the error banner, and the banner dismisses', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    vi.mocked(api).materializeArtifact = vi.fn().mockRejectedValue(new Error('materialize blew up'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))
    await waitFor(() => expect(screen.getByText(/materialize blew up/i)).toBeInTheDocument())

    await user.click(screen.getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByText(/materialize blew up/i)).not.toBeInTheDocument())
  })

  // If the response carries the field and the page drops it, the user is told only the
  // new slug -- the exact harm the report exists to prevent.
  it('surfaces a de-duplicated slug after promoting a document', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({
      slug: 'findings-md-2',
      slug_collided_with: 'findings-md',
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))

    // Both slugs must appear: the one it landed at, and the one already taken.
    // The page has other status regions, so pick the one carrying the slug.
    const statuses = await screen.findAllByRole('status')
    const notice = statuses.find((n) => (n.textContent ?? '').includes('findings-md-2'))
    if (!notice) throw new Error('collision notice did not render')
    expect(notice.textContent).toMatch(/findings-md["”']/)

    // The cards render a.name only, so the notice is the only place either slug
    // is reachable: each needs its own control.
    const openNew = screen.getByRole('button', { name: /Open findings-md-2/ })
    const openOld = screen.getByRole('button', { name: /Open older findings-md/ })
    expect(notice).toContainElement(openNew)
    expect(notice).toContainElement(openOld)
    await user.click(openNew)
  })

  // Negative control for the test above: the notice must be driven by the FIELD,
  // not by promoting at all, or it would fire on every successful promote.
  it('shows no de-duplicated-slug notice when the derived slug was free', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    const materializeSpy = vi.fn().mockResolvedValue({ slug: 'findings-md', slug_collided_with: '' })
    vi.mocked(api).materializeArtifact = materializeSpy
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))

    // Positive control on the same render: the promote really did happen, so the
    // absence below is a fact about the notice rather than about a dead click.
    await waitFor(() => expect(materializeSpy).toHaveBeenCalled())
    expect(screen.queryByText(/Saved under a different address/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Open older/ })).not.toBeInTheDocument()
  })

  // The star also lives in table/tree rows, which can sit a viewport below the
  // notice, so it is brought into view rather than assumed to be on screen.
  it('scrolls the de-duplicated-slug notice into view when it appears', async () => {
    const user = userEvent.setup()
    const scrollSpy = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollSpy
    try {
      vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
      vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
        docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
      })
      vi.mocked(api).materializeArtifact = vi.fn().mockResolvedValue({
        slug: 'findings-md-2',
        slug_collided_with: 'findings-md',
      })
      renderWithProviders(<ArtifactsPage />)
      await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

      await user.click(screen.getByLabelText('Star document'))

      await waitFor(() => expect(screen.getByRole('button', { name: /Open findings-md-2/ })).toBeInTheDocument())
      expect(scrollSpy).toHaveBeenCalled()
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  // A later clean promote must not wipe a warning the user has not read: only a
  // colliding promote may replace it, and only the user may dismiss it.
  it('keeps an unread de-duplicated-slug notice when the next promote does not collide', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [
        mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md'),
        mkDoc('/ws/research/NOTES.md', 'NOTES.md'),
      ],
    })
    const materializeSpy = vi.fn()
      .mockResolvedValueOnce({ slug: 'findings-md-2', slug_collided_with: 'findings-md' })
      .mockResolvedValueOnce({ slug: 'notes-md', slug_collided_with: '' })
    vi.mocked(api).materializeArtifact = materializeSpy
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    const stars = screen.getAllByLabelText('Star document')
    await user.click(stars[0])
    await waitFor(() => expect(screen.getByRole('button', { name: /Open findings-md-2/ })).toBeInTheDocument())

    // Second promote lands on a free slug; the first warning is still unread.
    await user.click(screen.getAllByLabelText('Star document')[0])
    await waitFor(() => expect(materializeSpy).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: /Open findings-md-2/ })).toBeInTheDocument()
  })

  it('hides already-saved session documents from the gallery section', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('research-findings')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [{ ...mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md'), saved: true, slug: 'research-findings' }],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('research findings')).toBeInTheDocument())
    expect(screen.queryByText('FINDINGS.md')).not.toBeInTheDocument()
    expect(screen.queryByText(/From your chats/i)).not.toBeInTheDocument()
  })

  it('filters by name search', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue'),
        mkArtifact('ticket-board'),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const search = screen.getByPlaceholderText(/Filter by name/i) as HTMLInputElement
    await userEvent.type(search, 'queue')
    expect(screen.getByText('cr queue')).toBeInTheDocument()
    expect(screen.queryByText('ticket board')).not.toBeInTheDocument()
  })

  it('shows error banner on fetch failure', async () => {
    vi.mocked(api).artifacts = vi.fn().mockRejectedValue(new Error('network down'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/network down/i)).toBeInTheDocument())
  })

  it('calls deleteArtifact when user confirms delete', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const deleteBtn = screen.getByLabelText('Remove from artifacts library')
    await userEvent.click(deleteBtn)
    expect(deleteSpy).toHaveBeenCalledWith('cr-queue')
  })

  it('does not delete when user cancels', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn()
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    await userEvent.click(screen.getByLabelText('Remove from artifacts library'))
    expect(deleteSpy).not.toHaveBeenCalled()
  })

  it('refetches with kind filter when kind dropdown changes', async () => {
    const fetcher = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifacts = fetcher
    renderWithProviders(<ArtifactsPage />)
    // Wait for loading state to clear and the kind dropdown to mount. SimpleSelect
    // renders a <button role="combobox">, so the current selection is text on the
    // trigger rather than a form value, and the rows mount only once it is open.
    await waitFor(() =>
      expect(kindTrigger()).toHaveTextContent(/all kinds/i),
    )
    fireEvent.click(kindTrigger())
    fireEvent.click(await screen.findByRole('option', { name: 'kind: markdown' }))
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: undefined, kind: 'markdown' })
    })
  })

  it('refetches with tag filter when tag dropdown changes', async () => {
    // The tag rows are built from a separate unfiltered query, so this also covers
    // the dynamic half of the dropdown: an "all tags" row on the empty string plus
    // one row per tag in the library.
    const fetcher = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { tags: ['ops'] })],
    })
    vi.mocked(api).artifacts = fetcher
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(tagTrigger()).toHaveTextContent(/all tags/i))
    fireEvent.click(tagTrigger())
    fireEvent.click(await screen.findByRole('option', { name: 'tag: ops' }))
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: 'ops', kind: undefined })
    })
  })

  it('card action button pops the artifact out into its own window (keyboard-reachable)', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const popoutBtn = screen.getByLabelText('Pop out to window')
    expect(popoutBtn).toBeInTheDocument()
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    await userEvent.click(popoutBtn)
    expect(open).toHaveBeenCalledTimes(1)
    expect(String(open.mock.calls[0][0])).toContain('/popout/artifact/cr-queue')
    open.mockRestore()
  })

  it('renders Artifact Deploy button that navigates to /deploy', async () => {
    // Artifact Deploy is a Feature Preview, so the door is only offered to an
    // operator who opted in. The flag is a plain localStorage key.
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('Artifact Deploy')).toBeInTheDocument())
    const btn = screen.getByText('Artifact Deploy').closest('button')!
    expect(btn).toBeInTheDocument()
    await userEvent.click(btn)
    // MemoryRouter means we check the location changed; since there's no
    // matching Route defined in the test wrapper, we verify the button exists
    // and is clickable (navigation intent is covered by the navigate call).
    expect(btn).toBeInTheDocument()
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
  })

  it('does not offer the Artifact Deploy door until the preview is on', async () => {
    // Default OFF is the point: every door behind this flag leads to spending in
    // a real AWS account and to content on the open internet.
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifacts = artifacts
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(artifacts).toHaveBeenCalled())
    expect(screen.queryByText('Artifact Deploy')).toBeNull()
  })
})

describe('ArtifactsPage — New Artifact split button', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-artifacts-view')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
  })

  it('creates a blank document and opens it', async () => {
    // No `kind` is sent: the store defaults a blank document to markdown and
    // marks the kind auto-assigned so the first real save can re-type it.
    vi.mocked(api).createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled', { kind: 'markdown' }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /New Artifact/i }))
    await waitFor(() => {
      expect(vi.mocked(api).createArtifact).toHaveBeenCalledWith({ name: 'Untitled', content: '' })
    })
  })

  it('keeps file import available under the caret', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    // The import entry lives in the dropdown, not the toolbar, so it must not
    // be reachable until the caret is opened.
    expect(screen.queryByText(/Import from a file/i)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /More ways to add an artifact/i }))
    expect(await screen.findByText(/Import from a file/i)).toBeInTheDocument()
  })
})

/**
 * A failed list fetch must not masquerade as an empty library (#10867).
 *
 * When the artifacts query errors (gateway restart 403s, connection refused),
 * `data` is undefined and the derived list is [] — indistinguishable, before
 * this fix, from a genuinely empty library. The contract under test:
 *
 * - The page-level <ErrorNotice> banner carries the actual error + the agent
 *   hand-off (errors-use-error-notice), AND the gallery renders a truthful
 *   "couldn't load" placeholder instead of "No artifacts yet". Both together:
 *   the placeholder does not replace the banner, it replaces the lie.
 * - The placeholder's Retry refetches in place and the library recovers.
 */
describe('ArtifactsPage list-fetch failure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-artifacts-view')
  })

  it('renders the error banner AND the truthful placeholder together — never "No artifacts yet"', async () => {
    vi.mocked(api).artifacts = vi.fn().mockRejectedValue(new Error('boom 403'))
    renderWithProviders(<ArtifactsPage />)

    // Truthful placeholder in the gallery slot…
    await waitFor(() => expect(screen.getByTestId('artifacts-error-state')).toBeInTheDocument())
    expect(screen.getByText(/Couldn’t load your artifacts/i)).toBeInTheDocument()
    // …while the banner still surfaces the real error (the placeholder
    // supplements the ErrorNotice contract, it does not replace it).
    expect(screen.getByText(/boom 403/)).toBeInTheDocument()
    // The lie this fix removes:
    expect(screen.queryByText(/No artifacts yet/i)).not.toBeInTheDocument()
  })

  it('Retry refetches in place and the library recovers', async () => {
    let fail = true
    vi.mocked(api).artifacts = vi.fn().mockImplementation(() =>
      fail ? Promise.reject(new Error('boom 403')) : Promise.resolve({ artifacts: [mkArtifact('recovered-artifact')] }),
    )
    // The folder list fails under the same trigger (every endpoint 403s).
    vi.mocked(api).artifactFolders = vi.fn().mockImplementation(() =>
      fail ? Promise.reject(new Error('boom 403')) : Promise.resolve({ folders: [] }),
    )
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByTestId('artifacts-error-state')).toBeInTheDocument())
    const foldersCallsBefore = vi.mocked(api.artifactFolders).mock.calls.length

    fail = false
    // Target the placeholder's own action: aux-read rows render their own
    // Retry buttons for unrelated failures, so the click must be scoped.
    await userEvent.click(within(screen.getByTestId('artifacts-error-state-action')).getByRole('button'))

    // mkArtifact renders the slug de-hyphenated as the card title.
    await waitFor(() => expect(screen.getByText('recovered artifact')).toBeInTheDocument())
    expect(screen.queryByTestId('artifacts-error-state')).not.toBeInTheDocument()
    // One click heals the co-failed folder read too — not just the list.
    await waitFor(() => expect(vi.mocked(api.artifactFolders).mock.calls.length).toBeGreaterThan(foldersCallsBefore))
  })

  it('renders the truthful placeholder in the unfiltered Table view too — not an empty tree', async () => {
    // #10867's misread must not survive in the other view mode: a user whose
    // persisted view is Table (no filters) gets the same unknown-not-absent
    // placeholder, never LibraryTree rendered over zero items.
    localStorage.setItem('mc-artifacts-view', 'table')
    vi.mocked(api).artifacts = vi.fn().mockRejectedValue(new Error('boom 403'))
    renderWithProviders(<ArtifactsPage />)

    await waitFor(() => expect(screen.getByTestId('artifacts-error-state')).toBeInTheDocument())
    expect(screen.queryByText(/No artifacts yet/i)).not.toBeInTheDocument()
  })
})
