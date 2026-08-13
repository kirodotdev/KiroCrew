/**
 * The receiving half of the library's "New Artifact" action.
 *
 * The library creates the document empty and navigates here with
 * `state.justCreatedBlank`. Three behaviours hang off that handover and are
 * asserted below: the editor opens focused, the title can be renamed inline
 * (create-first naming is useless without it), and an untouched blank is
 * discarded on leave so the library does not fill up with empty Untitled
 * documents.
 *
 * `ContentRenderer` is mocked down to a textarea for the same reason as
 * ArtifactDetailPage.dirtyDelete.test.tsx: the real editor is Monaco, which
 * renders no accessible input under jsdom. The subject here is the page's
 * lifecycle logic, not the editor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route, Link } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { __resetArtifactEditing } from '../utils/artifactEditGuard'
import { markJustCreatedBlank, __resetJustCreatedBlank } from '../lib/blankHandoff'
import { beginArtifactWrite, __resetArtifactWrites } from '../lib/artifactWrites'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
vi.mock('../components/ContentRenderer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/ContentRenderer')>()),
  ContentRenderer: ({ editing, displayContent, onChange }: {
    editing: boolean; displayContent: string; onChange: (v: string) => void
  }) => editing
    ? <textarea aria-label="editor" defaultValue={displayContent} onChange={e => onChange(e.target.value)} />
    : <div>{displayContent}</div>,
}))

const mkArtifact = (o: Partial<Artifact> = {}): Artifact => ({
  slug: 'untitled', name: 'Untitled', kind: 'markdown', source: 'chat', description: '',
  tags: [], version: 1, created_at: '2026-07-31T05:00:00.000000+00:00',
  updated_at: '2026-07-31T05:00:00.000000+00:00', content: '', ...o,
})

/** Render the detail page, optionally arriving with the blank handover flag. */
function renderPage({ blank = true, artifact = mkArtifact() } = {}) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  // `blank` = the library just created this and handed it over. The hand-off is
  // module-scoped and consumed once, so a "reload" is simply not arming it.
  if (blank) markJustCreatedBlank(artifact.slug, artifact.name)
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
    </Routes>,
    { route: `/artifacts/${artifact.slug}` },
  )
}

describe('ArtifactDetailPage — a freshly created blank document', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    __resetArtifactEditing()
    __resetJustCreatedBlank()
    __resetArtifactWrites()
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'untitled', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'untitled', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).deleteArtifact = vi.fn().mockResolvedValue(undefined)
    vi.mocked(api).settleBlankArtifact = vi.fn().mockResolvedValue({ outcome: 'deleted' })
  })

  it('opens the editor straight away so the user can start typing', async () => {
    renderPage()
    expect(await screen.findByLabelText('editor')).toBeInTheDocument()
  })

  it('leaves an existing document alone when arriving without the flag', async () => {
    renderPage({ blank: false, artifact: mkArtifact({ content: '# already written' }) })
    await screen.findByText('# already written')
    expect(screen.queryByLabelText('editor')).not.toBeInTheDocument()
  })

  it('does not force the editor open on a blank flag if content already exists', async () => {
    // Reloading the page after typing keeps the flag in history state; the
    // emptiness guard is what stops it yanking the user back into edit mode.
    renderPage({ artifact: mkArtifact({ content: '# typed since' }) })
    await screen.findByText('# typed since')
    expect(screen.queryByLabelText('editor')).not.toBeInTheDocument()
  })

  it('renames the document from the title, without bumping the version', async () => {
    renderPage({ blank: false })
    fireEvent.click(await screen.findByTitle('Rename this artifact'))
    const input = await screen.findByLabelText('Artifact name')
    fireEvent.change(input, { target: { value: 'Launch plan' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith('untitled', { name: 'Launch plan' })
    })
  })

  it('returns keyboard focus to the title after committing a rename', async () => {
    // The input unmounts on commit, which would drop focus to the body and lose
    // the user's place mid-keyboard-flow.
    renderPage()
    const trigger = await screen.findByTitle('Rename this artifact')
    fireEvent.click(trigger)
    const input = await screen.findByLabelText('Artifact name')
    fireEvent.change(input, { target: { value: 'Release plan' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(screen.getByTitle('Rename this artifact')).toHaveFocus(),
    )
  })

  it('abandons a rename on Escape', async () => {
    renderPage({ blank: false })
    fireEvent.click(await screen.findByTitle('Rename this artifact'))
    const input = await screen.findByLabelText('Artifact name')
    fireEvent.change(input, { target: { value: 'Discard me' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Artifact name')).not.toBeInTheDocument())
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  it('ignores a rename that changes nothing', async () => {
    renderPage({ blank: false })
    fireEvent.click(await screen.findByTitle('Rename this artifact'))
    fireEvent.keyDown(await screen.findByLabelText('Artifact name'), { key: 'Enter' })
    await waitFor(() => expect(screen.queryByLabelText('Artifact name')).not.toBeInTheDocument())
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  it('never discards a document it was not handed as a fresh blank', async () => {
    const { unmount } = renderPage({ blank: false })
    await screen.findByText('Untitled')
    unmount()
    expect(vi.mocked(api).deleteArtifact).not.toHaveBeenCalled()
  })

  // ── Leaving a just-created blank ────────────────────────────────────────────
  // The keep / save / delete DECISION is made atomically in the store (see
  // settle_blank in artifacts.py) because deciding here would race a concurrent
  // save from a popout window or an agent. So what this page is responsible for
  // is narrower, and that is what these assert: it asks the store to settle, it
  // passes the unsaved buffer along, and it does NOT ask at all once the user has
  // done something through this page.
  const settled = () => vi.mocked(api).settleBlankArtifact

  it('asks the store to settle an untouched blank on the way out', async () => {
    const { unmount } = renderPage()
    await screen.findByLabelText('editor')
    unmount()
    await waitFor(() => {
      expect(settled()).toHaveBeenCalledWith('untitled', {
        untitled_name: 'Untitled',
        draft: '',
        allow_delete: true,
      })
    })
  })

  it('hands the unsaved draft to the store rather than writing it itself', async () => {
    // The store writes it only after confirming, under its lock, that the stored
    // copy is still empty -- so a concurrent save cannot be clobbered.
    const { unmount } = renderPage()
    const editor = await screen.findByLabelText('editor')
    fireEvent.change(editor, { target: { value: '# a draft never saved' } })
    unmount()
    await waitFor(() => {
      expect(settled()).toHaveBeenCalledWith('untitled', {
        untitled_name: 'Untitled',
        draft: '# a draft never saved',
        allow_delete: true,
      })
    })
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
    expect(vi.mocked(api).deleteArtifact).not.toHaveBeenCalled()
  })

  it.each([
    ['renamed it', async () => {
      fireEvent.click(screen.getByTitle('Rename this artifact'))
      const input = await screen.findByLabelText('Artifact name')
      fireEvent.change(input, { target: { value: 'Release plan' } })
      fireEvent.keyDown(input, { key: 'Enter' })
    }],
    ['picked a type', async () => {
      // SimpleSelect wraps a Radix Select: open the trigger, then click the row.
      fireEvent.click(screen.getByLabelText('Document type'))
      fireEvent.click(await screen.findByRole('option', { name: 'text' }))
    }],
    ['tagged it', async () => {
      fireEvent.click(screen.getByRole('button', { name: /Add a tag/i }))
      const tagInput = await screen.findByRole('textbox', { name: /Add a tag/i })
      fireEvent.change(tagInput, { target: { value: 'ops' } })
      fireEvent.keyDown(tagInput, { key: 'Enter' })
    }],
  ])('asks the store not to delete once the user has %s', async (_label, act) => {
    // A write issued through this page is the one thing the store cannot see yet,
    // so deletion is taken off the table. It must NOT skip the call: the editor may
    // still hold text the user typed, and naming a document should not cost them
    // their first paragraph. Asserted with the write still in flight -- the case
    // the earlier designs kept getting wrong.
    // The real transport counts the request; this mock stands in for it, leaving
    // the page reading exactly the state it reads in production.
    vi.mocked(api).updateArtifact = vi.fn((slug: string) => {
      beginArtifactWrite(slug)
      return new Promise(() => {}) as Promise<Artifact>
    })
    vi.mocked(api).setArtifactPinned = vi.fn((slug: string) => {
      beginArtifactWrite(slug)
      return new Promise(() => {}) as Promise<Artifact>
    })
    const { unmount } = renderPage()
    const editor = await screen.findByLabelText('editor')
    fireEvent.change(editor, { target: { value: '# my first paragraph' } })
    await act()
    unmount()
    await waitFor(() => {
      expect(settled()).toHaveBeenCalledWith('untitled', {
        untitled_name: 'Untitled',
        draft: '# my first paragraph',
        allow_delete: false,
      })
    })
    expect(vi.mocked(api).deleteArtifact).not.toHaveBeenCalled()
  })

  it('still settles when a rename was abandoned rather than issued', async () => {
    // Escape writes nothing, so the blank is still litter.
    const { unmount } = renderPage()
    await screen.findByLabelText('editor')
    fireEvent.click(screen.getByTitle('Rename this artifact'))
    const input = await screen.findByLabelText('Artifact name')
    fireEvent.change(input, { target: { value: 'Changed my mind' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    unmount()
    await waitFor(() => expect(settled()).toHaveBeenCalled())
  })

  it('sends the name the document was CREATED with, not a fresh translation', async () => {
    // The untitled placeholder is localised. Re-deriving it on departure means a
    // language change in between makes the stored name look like a rename, and the
    // store keeps the document while silently dropping the draft.
    const { unmount } = renderPage({ artifact: mkArtifact({ name: 'Sans titre' }) })
    await screen.findByLabelText('editor')
    unmount()
    await waitFor(() => {
      expect(settled()).toHaveBeenCalledWith('untitled', {
        untitled_name: 'Sans titre',
        draft: '',
        allow_delete: true,
      })
    })
  })

  it('settles the blank when navigating straight to another artifact', async () => {
    // This route is REUSED for the next artifact, so the new document renders
    // before the cleanup runs. An ungated snapshot would be replaced by it and the
    // blank's unsaved draft would be stranded -- no settle, no rescue.
    const other = mkArtifact({ slug: 'other-doc', name: 'Other doc', content: '# unrelated' })
    vi.mocked(api).artifact = vi.fn(async (slug: string) =>
      slug === 'other-doc' ? other : mkArtifact(),
    )
    markJustCreatedBlank('untitled', 'Untitled')
    const { unmount } = renderWithProviders(
      <Routes>
        <Route
          path="/artifacts/:slug"
          element={
            <>
              <ArtifactDetailPage />
              <Link to="/artifacts/other-doc">open the other document</Link>
            </>
          }
        />
      </Routes>,
      { route: '/artifacts/untitled' },
    )
    const editor = await screen.findByLabelText('editor')
    fireEvent.change(editor, { target: { value: '# typed then navigated' } })

    // Same route pattern, different slug -- the component is reused, not remounted.
    fireEvent.click(screen.getByText('open the other document'))

    await waitFor(() => {
      expect(vi.mocked(api).settleBlankArtifact).toHaveBeenCalledWith('untitled', {
        untitled_name: 'Untitled',
        draft: '# typed then navigated',
        allow_delete: true,
      })
    })
    unmount()
  })

  it('does not offer an in-flight rename up for deletion across a route change', async () => {
    // The nastiest ordering in this page: rename a fresh blank, then open another
    // artifact before the PATCH lands. The per-slug reset effect runs before the
    // departing document's cleanup, so an unscoped flag reads false by then and the
    // cleanup would tell the server the still-pristine document is safe to delete
    // -- destroying the document the user just named.
    const other = mkArtifact({ slug: 'other-doc', name: 'Other doc', content: '# unrelated' })
    vi.mocked(api).artifact = vi.fn(async (slug: string) =>
      slug === 'other-doc' ? other : mkArtifact(),
    )
    // Never resolves: the rename is still in flight when the route changes.
    vi.mocked(api).updateArtifact = vi.fn((s2: string) => {
      beginArtifactWrite(s2)
      return new Promise(() => {}) as Promise<Artifact>
    })
    markJustCreatedBlank('untitled', 'Untitled')
    const { unmount } = renderWithProviders(
      <Routes>
        <Route
          path="/artifacts/:slug"
          element={
            <>
              <ArtifactDetailPage />
              <Link to="/artifacts/other-doc">open the other document</Link>
            </>
          }
        />
      </Routes>,
      { route: '/artifacts/untitled' },
    )
    await screen.findByLabelText('editor')
    fireEvent.click(screen.getByTitle('Rename this artifact'))
    const input = await screen.findByLabelText('Artifact name')
    fireEvent.change(input, { target: { value: 'Release checklist' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    fireEvent.click(screen.getByText('open the other document'))

    await waitFor(() => expect(vi.mocked(api).settleBlankArtifact).toHaveBeenCalled())
    expect(vi.mocked(api).settleBlankArtifact).toHaveBeenCalledWith(
      'untitled',
      expect.objectContaining({ allow_delete: false }),
    )
    unmount()
  })

  it('never settles on a revisit, because the hand-off is one-shot', async () => {
    // A reload keeps the URL but not the module-scoped hand-off. Without this,
    // router state would survive the reload and re-arm cleanup on a document the
    // user has since come back to.
    const { unmount } = renderPage({ blank: false, artifact: mkArtifact({ tags: ['ops'] }) })
    await screen.findByText('Untitled')
    unmount()
    expect(settled()).not.toHaveBeenCalled()
  })

  it('survives the settle request failing', async () => {
    vi.mocked(api).settleBlankArtifact = vi.fn().mockRejectedValue(new Error('offline'))
    const { unmount } = renderPage()
    await screen.findByLabelText('editor')
    unmount()
    await waitFor(() => expect(settled()).toHaveBeenCalled())
  })

  // ── Document type control ───────────────────────────────────────────────────
  it('lets the user switch a document to plain text', async () => {
    // Markdown and text accept the same bytes, so this cannot be auto-detected:
    // "# Notes" is a heading in one and a literal hash in the other.
    renderPage({ blank: false })
    const trigger = await screen.findByLabelText('Document type')
    expect(trigger).toHaveTextContent('markdown')
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'text' }))
    await waitFor(() => {
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith('untitled', { kind: 'text' })
    })
  })

  it('offers only the types that keep an editor', async () => {
    renderPage({ blank: false })
    // The rows exist only while the popup is open, and Radix rows carry no value
    // attribute — the visible label is the option value here either way.
    fireEvent.click(await screen.findByLabelText('Document type'))
    const offered = (await screen.findAllByRole('option')).map(o => o.textContent?.trim())
    expect(offered).toEqual(['markdown', 'text', 'json', 'svg'])
    // widget / html render in a sandboxed iframe with no editor.
    expect(offered).not.toContain('widget')
    expect(offered).not.toContain('html')
  })

  it('shows a read-only type badge when the document is not editable', async () => {
    renderPage({ blank: false, artifact: mkArtifact({ kind: 'widget', content: '<div>x</div>' }) })
    await screen.findByText('widget')
    expect(screen.queryByLabelText('Document type')).not.toBeInTheDocument()
  })

})
