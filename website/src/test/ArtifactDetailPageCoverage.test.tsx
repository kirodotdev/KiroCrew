/**
 * Coverage suite for `ArtifactDetailPage` — the write-side behaviour the existing
 * suites do not reach.
 *
 * The sibling suites (`ArtifactDetailPage.test.tsx`,
 * `.anchoredComments`, `.companionChat`, `.dirtyDelete`, `.newBlank`,
 * `.popoutNav`) cover rendering, panel state and the blank hand-off. What was
 * left cold is everything that MUTATES: save / snapshot / revert, kind + tag +
 * rename edits, the download export, comment-thread mutations, and the upstream
 * sync banner. Those are the paths where a silent failure loses a user's work,
 * so each is pinned here together with its error branch.
 *
 * Kiro Crew convention: this suite mirrors `ArtifactDetailPage.test.tsx` —
 * automocked api client, `renderWithProviders` on the real `/artifacts/:slug`
 * route so `useParams` and `navigate` run for real.
 *
 * Two harness substitutions, both deliberate:
 *  1. `ArtifactBodyNative` is replaced with a plain textarea. The real editor is
 *     a lazily-imported Monaco instance that never mounts under jsdom, so
 *     `editedContent` — and therefore `dirty`, and therefore every save path —
 *     is otherwise unreachable from the UI.
 *  2. `PublishHub` and `useArtifactPopouts` are stubbed so the toolbar's publish
 *     and pop-out branches can be exercised without their own dependency graphs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactComment } from '../types'

// The sandboxed frame mints its document URL through the api client. The
// automock resolves every method to `undefined`, which the component cannot
// await — without this stub the frame throws instead of rendering.
beforeEach(() => {
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})


vi.mock('../api/client')

// The embedded companion chat page — its own suites cover it; here it only has
// to mount without ChatPage's full hook graph.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

// The publish panel is a separate surface with its own provider queries; the
// page's contract is only that toggling the toolbar button mounts it.
vi.mock('../components/PublishHub', () => ({
  PublishHub: ({ onClose }: { onClose: () => void }) => (
    <div>
      <span>publish hub stub</span>
      <button type="button" onClick={onClose}>close hub</button>
    </div>
  ),
}))

// Pop-out state is driven by a real cross-window registry. A module-level flag
// lets one test assert the popped-out toolbar without a second window.
let poppedOut = false
const popoutCalls: string[] = []
vi.mock('../hooks/useArtifactPopouts', () => ({
  useArtifactPopouts: () => ({
    isPoppedOut: () => poppedOut,
    open: (s: string) => { popoutCalls.push(`open:${s}`) },
    focus: (s: string) => { popoutCalls.push(`focus:${s}`) },
    bringBack: (s: string) => { popoutCalls.push(`back:${s}`) },
  }),
}))

// Monaco never mounts under jsdom, so stand in a textarea for the editable body
// and one activate button per comment (the real overlay's click target).
vi.mock('../components/ArtifactBody', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ArtifactBody')>()
  return {
    ...actual,
    ArtifactBodyNative: ({
      content, editing, onChange, previewRef, comments, onActivateComment,
    }: {
      content: string
      editing: boolean
      onChange: (v: string) => void
      previewRef: React.RefObject<HTMLDivElement | null>
      comments?: ArtifactComment[]
      onActivateComment?: (id: string) => void
    }) => (
      editing ? (
        <textarea
          aria-label="body editor"
          value={content}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <div ref={previewRef}>
          <pre>{content}</pre>
          {(comments ?? []).map((c) => (
            <button key={c.id} type="button" onClick={() => onActivateComment?.(c.id)}>
              {`activate ${c.id}`}
            </button>
          ))}
        </div>
      )
    ),
  }
})

const SLUG = 'cr-queue'

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: SLUG,
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: ['ops'],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Doc body',
  ...overrides,
})

const mkComment = (overrides: Partial<ArtifactComment> = {}): ArtifactComment => ({
  id: 'c1',
  author: 'joe',
  is_agent: false,
  body: 'first review note',
  thread_id: 'c1',
  status: 'open',
  scope: 'private',
  origin: 'local',
  sync_state: 'local_only',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  ...overrides,
})

/** Mount on the real route; a sibling route stands in for the library. */
function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page</div>} />
      <Route path="/chat" element={<div>chat page</div>} />
    </Routes>,
    { route: `/artifacts/${SLUG}` },
  )
}

/** Seed the four queries the page always issues, then mount and settle. */
async function mount(artifact: Artifact, versions: number[] = [1, 2]) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: SLUG, versions })
  const utils = renderRoute()
  await waitFor(() => expect(screen.getByText(artifact.name)).toBeInTheDocument())
  return utils
}

/** Open the inline editor and type, so `dirty` becomes true. */
async function typeIntoEditor(text: string) {
  fireEvent.click(screen.getByTitle('Edit content'))
  const box = await screen.findByLabelText('body editor')
  fireEvent.change(box, { target: { value: text } })
  await waitFor(() => expect(screen.getByText('• unsaved changes')).toBeInTheDocument())
  return box
}

const saveBtn = () => screen.getByTitle(/Save to Live/i)
const snapshotEditBtn = () => screen.getByTitle(/Snapshot \(Cmd\+Shift\+S\)/i)

/** Pick a row from the Radix-backed version select (open, then click). */
async function pickVersion(label: string) {
  fireEvent.click(screen.getByRole('combobox', { name: /Version/i }))
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

describe('ArtifactDetailPage — mutation paths', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    poppedOut = false
    popoutCalls.length = 0
    localStorage.clear()
    sessionStorage.clear()
    if (!URL.createObjectURL) {
      // Well-formed blob: URI, not a bare 'blob:test' literal — see the note in
      // WidgetFrame.test.tsx's beforeEach for why a malformed mock value here
      // risks a deferred ECONNREFUSED crashing an unrelated shard.
      // @ts-expect-error jsdom lacks blob URLs
      URL.createObjectURL = vi.fn().mockReturnValue('blob:http://localhost:6776/test')
      // @ts-expect-error jsdom lacks blob URLs
      URL.revokeObjectURL = vi.fn()
    }
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: SLUG, events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({})
    vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
    vi.mocked(api).getArtifactPublishProviders = vi
      .fn()
      .mockResolvedValue({ providers: [], kind: 'markdown' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  // ── save / snapshot ───────────────────────────────────────────────────────
  it('silent Save posts the buffer without a version bump and stays in the editor', async () => {
    await mount(mkArtifact())
    await typeIntoEditor('# edited body')
    fireEvent.click(saveBtn())
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, {
        content: '# edited body',
        snapshot: false,
      }),
    )
    // Silent save keeps the user in the editor so they can keep iterating.
    expect(await screen.findByLabelText('body editor')).toBeInTheDocument()
  })

  it('Snapshot bumps the version and drops out of the editor', async () => {
    await mount(mkArtifact())
    await typeIntoEditor('# snapshot me')
    fireEvent.click(snapshotEditBtn())
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, {
        content: '# snapshot me',
        snapshot: true,
      }),
    )
    // A snapshot is a deliberate checkpoint, so the editor closes.
    await waitFor(() => expect(screen.queryByLabelText('body editor')).toBeNull())
  })

  it('surfaces a save failure instead of silently dropping the buffer', async () => {
    await mount(mkArtifact())
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('disk full'))
    await typeIntoEditor('# will fail')
    fireEvent.click(saveBtn())
    await waitFor(() => expect(screen.getByText('disk full')).toBeInTheDocument())
    // The buffer is still open and still dirty — the user can copy their work out.
    expect(screen.getByLabelText('body editor')).toBeInTheDocument()
    expect(screen.getByText('• unsaved changes')).toBeInTheDocument()
  })

  it('Cmd+S saves silently and Cmd+Shift+S snapshots', async () => {
    await mount(mkArtifact())
    await typeIntoEditor('# keyboard')
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, {
        content: '# keyboard',
        snapshot: false,
      }),
    )
    fireEvent.keyDown(document, { key: 's', metaKey: true, shiftKey: true })
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, {
        content: '# keyboard',
        snapshot: true,
      }),
    )
  })

  it('Escape and Cancel both gate a dirty discard behind a confirm', async () => {
    await mount(mkArtifact())
    await typeIntoEditor('# unsaved work')
    // The guard is the in-app dialog, never window.confirm — the native
    // confirm freezes the renderer's event loop.
    const confirmSpy = vi.spyOn(window, 'confirm')
    fireEvent.keyDown(document, { key: 'Escape' })
    const first = await screen.findByRole('dialog')
    expect(within(first).getByText('Discard unsaved changes?')).toBeInTheDocument()
    fireEvent.click(within(first).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // Declined — the buffer survives.
    expect(screen.getByLabelText('body editor')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle(/Cancel \(Esc\)/i))
    const second = await screen.findByRole('dialog')
    fireEvent.click(within(second).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByLabelText('body editor')).toBeNull())
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('ignores Cmd+S while the discard dialog is open', async () => {
    // The native confirm blocked the event loop, so no shortcut could fire
    // mid-question; the async dialog must restore that property — a mid-dialog
    // save would persist the very draft the user is about to discard.
    await mount(mkArtifact())
    await typeIntoEditor('# unsaved work')
    fireEvent.keyDown(document, { key: 'Escape' })
    await screen.findByRole('dialog')
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('registers a beforeunload guard only while the buffer is dirty', async () => {
    await mount(mkArtifact())
    const addSpy = vi.spyOn(window, 'addEventListener')
    await typeIntoEditor('# guard me')
    await waitFor(() =>
      expect(addSpy.mock.calls.some(([type]) => type === 'beforeunload')).toBe(true),
    )
    const evt = new Event('beforeunload', { cancelable: true })
    const prevented = !window.dispatchEvent(evt)
    expect(prevented).toBe(true)
  })

  it('toggles the rendered preview of the edit buffer without committing it', async () => {
    await mount(mkArtifact())
    await typeIntoEditor('# preview me')
    fireEvent.click(screen.getByTitle(/Preview rendered output/i))
    // Preview swaps the textarea for the rendered body; nothing was posted.
    await waitFor(() => expect(screen.queryByLabelText('body editor')).toBeNull())
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
    expect(screen.getByText('• unsaved changes')).toBeInTheDocument()
  })

  it('snapshots live state when the record is live_dirty, and reports failures', async () => {
    await mount(mkArtifact({ live_dirty: true }))
    const btn = screen.getByTitle(/Snapshot — capture the current state/i)
    fireEvent.click(btn)
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { snapshot: true }),
    )
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('snapshot refused'))
    fireEvent.click(screen.getByTitle(/Snapshot — capture the current state/i))
    await waitFor(() => expect(screen.getByText('snapshot refused')).toBeInTheDocument())
  })

  // ── revert ────────────────────────────────────────────────────────────────
  it('a declined revert confirm leaves the artifact untouched', async () => {
    vi.mocked(api).artifactVersion = vi
      .fn()
      .mockResolvedValue(mkArtifact({ version: 1, content: '# old' }))
    await mount(mkArtifact({ version: 2 }))
    await pickVersion('v1')
    const revert = await screen.findByTitle('Revert to v1')
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(revert)
    expect(confirmSpy).toHaveBeenCalled()
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  it('surfaces a revert failure', async () => {
    vi.mocked(api).artifactVersion = vi
      .fn()
      .mockResolvedValueOnce(mkArtifact({ version: 1, content: '# old' }))
      .mockRejectedValue(new Error('version gone'))
    await mount(mkArtifact({ version: 2 }))
    await pickVersion('v1')
    fireEvent.click(await screen.findByTitle('Revert to v1'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByTitle('Revert to v1'))
    await waitFor(() => expect(screen.getByText('version gone')).toBeInTheDocument())
  })

  it('returning to Live from a historical version clears the selection', async () => {
    vi.mocked(api).artifactVersion = vi
      .fn()
      .mockResolvedValue(mkArtifact({ version: 1, content: '# old' }))
    await mount(mkArtifact({ version: 2 }))
    await pickVersion('v1')
    await waitFor(() => expect(screen.getByTitle('Revert to v1')).toBeInTheDocument())
    await pickVersion('Live')
    // Revert is meaningless on Live, so the control disappears again.
    await waitFor(() => expect(screen.queryByTitle('Revert to v1')).toBeNull())
    expect(screen.getByText(/Showing Live \(v2\)/)).toBeInTheDocument()
  })

  // ── document type ─────────────────────────────────────────────────────────
  it('changing the document type patches the record and reports failures', async () => {
    await mount(mkArtifact({ kind: 'markdown' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'Document type' }))
    fireEvent.click(await screen.findByRole('option', { name: 'text' }))
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { kind: 'text' }),
    )
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('kind locked'))
    fireEvent.click(screen.getByRole('combobox', { name: 'Document type' }))
    fireEvent.click(await screen.findByRole('option', { name: 'json' }))
    await waitFor(() => expect(screen.getByText('kind locked')).toBeInTheDocument())
  })

  it('re-picking the current type is a no-op', async () => {
    await mount(mkArtifact({ kind: 'markdown' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'Document type' }))
    fireEvent.click(await screen.findByRole('option', { name: 'markdown' }))
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  // ── tags ──────────────────────────────────────────────────────────────────
  it('removes a tag through a metadata-only patch', async () => {
    await mount(mkArtifact({ tags: ['ops', 'cr'] }))
    fireEvent.click(screen.getByLabelText('Remove tag ops'))
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { tags: ['cr'] }),
    )
  })

  it('surfaces a tag-write failure', async () => {
    await mount(mkArtifact({ tags: ['ops'] }))
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('tags rejected'))
    fireEvent.click(screen.getByLabelText('Remove tag ops'))
    await waitFor(() => expect(screen.getByText('tags rejected')).toBeInTheDocument())
  })

  it('a duplicate tag closes the input without a write', async () => {
    await mount(mkArtifact({ tags: ['ops'] }))
    fireEvent.click(screen.getByLabelText('Add a tag'))
    const input = await screen.findByLabelText('Add a tag')
    fireEvent.change(input, { target: { value: 'OPS' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByLabelText('Add a tag').tagName).toBe('BUTTON'))
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  it('space and comma both commit a tag; Escape abandons it', async () => {
    await mount(mkArtifact({ tags: [] }))
    fireEvent.click(screen.getByLabelText('Add a tag'))
    let input = await screen.findByLabelText('Add a tag')
    fireEvent.change(input, { target: { value: 'oncall' } })
    fireEvent.keyDown(input, { key: ',' })
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { tags: ['oncall'] }),
    )
    fireEvent.click(screen.getByLabelText('Add a tag'))
    input = await screen.findByLabelText('Add a tag')
    fireEvent.change(input, { target: { value: 'draft' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.getByLabelText('Add a tag').tagName).toBe('BUTTON'))
    expect(vi.mocked(api).updateArtifact).toHaveBeenCalledTimes(1)
  })

  it('blurring the tag input commits a typed tag and discards an empty one', async () => {
    await mount(mkArtifact({ tags: [] }))
    fireEvent.click(screen.getByLabelText('Add a tag'))
    let input = await screen.findByLabelText('Add a tag')
    fireEvent.blur(input)
    // Nothing typed → the input just closes.
    await waitFor(() => expect(screen.getByLabelText('Add a tag').tagName).toBe('BUTTON'))
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
    fireEvent.click(screen.getByLabelText('Add a tag'))
    input = await screen.findByLabelText('Add a tag')
    fireEvent.change(input, { target: { value: 'weekly' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { tags: ['weekly'] }),
    )
  })

  // ── star / rename ─────────────────────────────────────────────────────────
  it('starring patches the pinned flag and reports a failure', async () => {
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    await mount(mkArtifact({ pinned: false }))
    fireEvent.click(screen.getByLabelText('Star artifact'))
    await waitFor(() => expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith(SLUG, true))
    await waitFor(() => expect(screen.getByLabelText(/Remove star/i)).toBeInTheDocument())
    vi.mocked(api).setArtifactPinned = vi.fn().mockRejectedValue(new Error('pin refused'))
    fireEvent.click(screen.getByLabelText(/Remove star/i))
    await waitFor(() => expect(screen.getByText('pin refused')).toBeInTheDocument())
  })

  it('renaming patches the name; a rejected rename surfaces', async () => {
    await mount(mkArtifact())
    fireEvent.click(screen.getByTitle('Rename this artifact'))
    const field = await screen.findByLabelText('Artifact name')
    fireEvent.change(field, { target: { value: 'Release plan' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { name: 'Release plan' }),
    )
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('name taken'))
    fireEvent.click(screen.getByTitle('Rename this artifact'))
    const field2 = await screen.findByLabelText('Artifact name')
    fireEvent.change(field2, { target: { value: 'Something else' } })
    fireEvent.blur(field2)
    await waitFor(() => expect(screen.getByText('name taken')).toBeInTheDocument())
  })

  it('Escape abandons a rename without a write', async () => {
    await mount(mkArtifact())
    fireEvent.click(screen.getByTitle('Rename this artifact'))
    const field = await screen.findByLabelText('Artifact name')
    fireEvent.change(field, { target: { value: 'discarded' } })
    fireEvent.keyDown(field, { key: 'Escape' })
    await waitFor(() => expect(screen.getByTitle('Rename this artifact')).toBeInTheDocument())
    expect(vi.mocked(api).updateArtifact).not.toHaveBeenCalled()
  })

  // ── download ──────────────────────────────────────────────────────────────
  /** Capture the synthesized <a> the download handler clicks. */
  function captureDownload() {
    const seen: { download: string; type: string }[] = []
    const blobTypes: string[] = []
    const origCreate = URL.createObjectURL
    URL.createObjectURL = vi.fn((b: Blob) => {
      blobTypes.push(b.type)
      return 'blob:download'
    }) as typeof URL.createObjectURL
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function mockClick(this: HTMLAnchorElement) {
        seen.push({ download: this.download, type: blobTypes[blobTypes.length - 1] ?? '' })
      })
    return {
      seen,
      restore: () => {
        clickSpy.mockRestore()
        URL.createObjectURL = origCreate
      },
    }
  }

  it.each([
    ['markdown' as const, 'md', 'text/plain'],
    ['text' as const, 'txt', 'text/plain'],
    ['json' as const, 'json', 'application/json'],
    ['svg' as const, 'svg', 'image/svg+xml'],
  ])('downloads a %s artifact as .%s', async (kind, ext, mime) => {
    const cap = captureDownload()
    try {
      await mount(mkArtifact({ kind, content: 'body', name: 'CR Queue' }))
      fireEvent.click(screen.getByLabelText('Download'))
      await waitFor(() => expect(cap.seen.length).toBe(1))
      expect(cap.seen[0].download).toBe(`CR Queue-v2.${ext}`)
      expect(cap.seen[0].type).toBe(mime)
    } finally {
      cap.restore()
    }
  })

  it('downloads a widget as standalone html and strips unsafe filename chars', async () => {
    const cap = captureDownload()
    try {
      await mount(mkArtifact({ kind: 'widget', name: 'CR/Queue: v2?', content: '<b>hi</b>' }))
      fireEvent.click(screen.getByLabelText('Download'))
      await waitFor(() => expect(cap.seen.length).toBe(1))
      expect(cap.seen[0].download).toBe('CRQueue v2-v2.html')
      expect(cap.seen[0].type).toBe('text/html')
    } finally {
      cap.restore()
    }
  })

  it('falls back to the slug when the name has no safe characters left', async () => {
    const cap = captureDownload()
    try {
      await mount(mkArtifact({ kind: 'markdown', name: '???', content: 'x' }))
      fireEvent.click(screen.getByLabelText('Download'))
      await waitFor(() => expect(cap.seen.length).toBe(1))
      expect(cap.seen[0].download).toBe(`${SLUG}-v2.md`)
    } finally {
      cap.restore()
    }
  })

  // ── publish + popout toolbar branches ─────────────────────────────────────
  it('the Publish button toggles the publish panel', async () => {
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Publish'))
    expect(await screen.findByText('publish hub stub')).toBeInTheDocument()
    fireEvent.click(screen.getByText('close hub'))
    await waitFor(() => expect(screen.queryByText('publish hub stub')).toBeNull())
  })

  it('a popped-out artifact swaps the pop-out control for Focus + Bring back', async () => {
    poppedOut = true
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Focus popped-out window'))
    fireEvent.click(screen.getByLabelText('Bring artifact back to this window'))
    expect(popoutCalls).toEqual([`focus:${SLUG}`, `back:${SLUG}`])
    expect(screen.queryByLabelText('Pop out to window')).toBeNull()
  })

  // ── comments ──────────────────────────────────────────────────────────────
  it('adds a document-level comment from the sidebar', async () => {
    vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({})
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByText('Add comment'))
    const box = await screen.findByPlaceholderText(/Add a comment on the whole artifact/i)
    fireEvent.change(box, { target: { value: 'needs a summary' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledWith(SLUG, {
        text: 'needs a summary',
        scope: 'private',
      }),
    )
  })

  it('drives review / resolve / delete / edit from the comment sidebar', async () => {
    vi.mocked(api).artifactComments = vi
      .fn()
      .mockResolvedValue({ comments: [mkComment()] })
    vi.mocked(api).markCommentReview = vi.fn().mockResolvedValue({})
    vi.mocked(api).resolveComment = vi.fn().mockResolvedValue({})
    vi.mocked(api).deleteArtifactComment = vi.fn().mockResolvedValue({})
    vi.mocked(api).editArtifactComment = vi.fn().mockResolvedValue({})
    await mount(mkArtifact())
    // The sidebar auto-reveals once the artifact has a comment.
    await screen.findByText('first review note')
    fireEvent.click(screen.getByTitle('Advance to Review'))
    await waitFor(() => expect(vi.mocked(api).markCommentReview).toHaveBeenCalledWith(SLUG, 'c1'))
    fireEvent.click(screen.getByTitle('Resolve (human-only)'))
    await waitFor(() => expect(vi.mocked(api).resolveComment).toHaveBeenCalledWith(SLUG, 'c1'))
    fireEvent.click(screen.getByTitle('Edit comment'))
    const editor = await screen.findByPlaceholderText('Edit comment…')
    fireEvent.change(editor, { target: { value: 'revised note' } })
    fireEvent.keyDown(editor, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api).editArtifactComment).toHaveBeenCalledWith(SLUG, 'c1', {
        text: 'revised note',
      }),
    )
    fireEvent.click(screen.getByTitle('Delete'))
    await waitFor(() =>
      expect(vi.mocked(api).deleteArtifactComment).toHaveBeenCalledWith(SLUG, 'c1'),
    )
  })

  it('replying to a resolved thread reopens it; replying to an open one does not', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [mkComment({ id: 'r1', status: 'resolved', body: 'closed note' })],
    })
    vi.mocked(api).replyArtifactComment = vi.fn().mockResolvedValue({})
    vi.mocked(api).reopenComment = vi.fn().mockResolvedValue({})
    await mount(mkArtifact())
    // Resolved threads are collapsed behind a reveal toggle.
    fireEvent.click(await screen.findByText(/1 resolved/))
    fireEvent.click(await screen.findByTitle('Reply'))
    const box = await screen.findByPlaceholderText('Reply…')
    fireEvent.change(box, { target: { value: 'one more thing' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api).replyArtifactComment).toHaveBeenCalledWith(SLUG, 'r1', {
        text: 'one more thing',
      }),
    )
    // The auto-reopen is what keeps a resolved thread from swallowing a reply.
    await waitFor(() => expect(vi.mocked(api).reopenComment).toHaveBeenCalledWith(SLUG, 'r1'))
  })

  it('reopens a resolved thread from its own control', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [mkComment({ id: 'r2', status: 'resolved', body: 'done note' })],
    })
    vi.mocked(api).reopenComment = vi.fn().mockResolvedValue({})
    await mount(mkArtifact())
    fireEvent.click(await screen.findByText(/1 resolved/))
    fireEvent.click(await screen.findByTitle('Reopen this thread'))
    await waitFor(() => expect(vi.mocked(api).reopenComment).toHaveBeenCalledWith(SLUG, 'r2'))
  })

  it('refetches comments when a mutation fails', async () => {
    const comments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
    vi.mocked(api).artifactComments = comments
    vi.mocked(api).resolveComment = vi.fn().mockRejectedValue(new Error('nope'))
    await mount(mkArtifact())
    await screen.findByText('first review note')
    const before = comments.mock.calls.length
    fireEvent.click(screen.getByTitle('Resolve (human-only)'))
    // A failed mutation didn't change server state, so it only refetches locally.
    await waitFor(() => expect(comments.mock.calls.length).toBeGreaterThan(before))
  })

  it('clicking an anchored comment in the body opens its thread and marks it read', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [
        mkComment({ id: 'a1', anchor: { quote: 'Doc' } }),
        mkComment({ id: 'a2', parent_id: 'a1', thread_id: 'a1', body: 'a reply' }),
      ],
    })
    await mount(mkArtifact())
    fireEvent.click(await screen.findByText('activate a1'))
    // Both the root and its reply are marked read together.
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(`mc-cmt-read:${SLUG}`) || '[]').sort())
        .toEqual(['a1', 'a2']),
    )
  })

  it('scrolls the body to an anchored comment clicked in the sidebar', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [mkComment({ id: 's1', anchor: { quote: 'Doc' } })],
    })
    await mount(mkArtifact())
    const row = await screen.findByTitle('Scroll to the highlighted text')
    fireEvent.click(row)
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(`mc-cmt-read:${SLUG}`) || '[]'))
        .toEqual(['s1']),
    )
  })

  it('routes an iframe artifact comment click through the iframe scroll bridge', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [mkComment({ id: 'w1', anchor: { quote: 'hi' } })],
    })
    await mount(mkArtifact({ kind: 'widget', content: '<b>hi</b>' }))
    fireEvent.click(await screen.findByTitle('Scroll to the highlighted text'))
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(`mc-cmt-read:${SLUG}`) || '[]'))
        .toEqual(['w1']),
    )
  })

  it('recovers from a corrupt read-marker entry in local storage', async () => {
    localStorage.setItem(`mc-cmt-read:${SLUG}`, 'not-json')
    vi.mocked(api).artifactComments = vi
      .fn()
      .mockResolvedValue({ comments: [mkComment()] })
    await mount(mkArtifact())
    // A corrupt marker must not take the page down — it just reads as "unread".
    expect(await screen.findByText('first review note')).toBeInTheDocument()
  })

  // ── not-found ─────────────────────────────────────────────────────────────
  it('renders the not-found notice when a selected version yields no record', async () => {
    // The detail query succeeds, so the error card is not what renders — the
    // page falls through to the bare not-found notice for the missing snapshot.
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(undefined)
    await mount(mkArtifact({ version: 2 }))
    await pickVersion('v1')
    expect(await screen.findByText('Not found.')).toBeInTheDocument()
  })

  // ── nav guards while dirty ────────────────────────────────────────────────
  it('Back and the version picker both confirm before discarding a buffer', async () => {
    vi.mocked(api).artifactVersion = vi
      .fn()
      .mockResolvedValue(mkArtifact({ version: 1, content: '# old' }))
    await mount(mkArtifact({ version: 2 }))
    await typeIntoEditor('# in progress')
    const confirmSpy = vi.spyOn(window, 'confirm')
    fireEvent.click(screen.getByText('Back'))
    const backDialog = await screen.findByRole('dialog')
    expect(within(backDialog).getByText('Discard unsaved changes?')).toBeInTheDocument()
    fireEvent.click(within(backDialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // Declined — still on the artifact, buffer intact.
    expect(screen.queryByText('library page')).toBeNull()
    expect(screen.getByLabelText('body editor')).toBeInTheDocument()
    await pickVersion('v1')
    // The picker is gated the same way, so the buffer survives that too.
    const pickerDialog = await screen.findByRole('dialog')
    fireEvent.click(within(pickerDialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByLabelText('body editor')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Back'))
    const confirmedDialog = await screen.findByRole('dialog')
    fireEvent.click(within(confirmedDialog).getByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByText('library page')).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  // ── companion chat ────────────────────────────────────────────────────────
  it('the sidebar ask-agent action stages the address prompt on a new session', async () => {
    vi.mocked(api).artifactComments = vi
      .fn()
      .mockResolvedValue({ comments: [mkComment()] })
    vi.mocked(api).createChatSlot = vi
      .fn()
      .mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    await mount(mkArtifact())
    fireEvent.click(await screen.findByText('Ask agent to address'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    // The prompt is STAGED, never auto-sent — the embedded composer picks it up.
    await waitFor(() =>
      expect(sessionStorage.getItem('kirocrew_prefill')).toMatch(/open comment/),
    )
  })

  it('surfaces a failure to create the companion session', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockRejectedValue(new Error('no slot capacity'))
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByText('no slot capacity')).toBeInTheDocument())
  })

  it('closing the chat panel keeps the session and does not reopen it', async () => {
    vi.mocked(api).createChatSlot = vi
      .fn()
      .mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await screen.findByTestId('chat-page')
    fireEvent.click(screen.getByLabelText('Close chat panel'))
    await waitFor(() => expect(screen.queryByTestId('chat-page')).toBeNull())
    expect(vi.mocked(api).deleteChatSlot).not.toHaveBeenCalled()
  })

  it('the full-page escape hatch routes to the bound session', async () => {
    vi.mocked(api).createChatSlot = vi
      .fn()
      .mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([
      { key: 'slot-new', title: 'Artifact: CR Queue', messages: 0, running: false, artifact: SLUG },
    ])
    await mount(mkArtifact())
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await screen.findByTestId('chat-page')
    fireEvent.click(screen.getByLabelText('Open in chat page'))
    expect(await screen.findByText('chat page')).toBeInTheDocument()
  })

  it('re-opening a stale session nudges instead of silently taking a baseline', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([
      { key: 'slot-bound', title: 'Artifact: CR Queue', messages: 3, running: false,
        artifact: SLUG, last_activity_ts: '2026-05-21T22:00:00.000000+00:00' },
    ])
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    // The marker is persisted now, so an empty one means "never injected" rather than
    // "reloaded"; suppressing here left a resumed agent on a stale version for good.
    sessionStorage.clear()
    await mount(mkArtifact({ updated_at: '2026-05-22T09:00:00.000000+00:00' }))
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    await waitFor(() => expect(vi.mocked(api).chatSlotContext).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('closes the floating comment thread popover', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [mkComment({ id: 't1', anchor: { quote: 'Doc' } })],
    })
    await mount(mkArtifact())
    fireEvent.click(await screen.findByText('activate t1'))
    const popover = await screen.findByLabelText('Comment thread')
    fireEvent.click(within(popover).getByLabelText('Close'))
    await waitFor(() => expect(screen.queryByLabelText('Comment thread')).toBeNull())
  })

  // ── anchored-selection guards ─────────────────────────────────────────────
  /** Back `window.getSelection` with a real Range, the object the page reads. */
  function stubSelection(opts: {
    text: string
    isCollapsed?: boolean
    anchorNode: Node | null
    rangeNode?: Node
  }) {
    const range = document.createRange()
    const target = opts.rangeNode ?? opts.anchorNode
    if (target) range.selectNodeContents(target)
    range.getBoundingClientRect = () => ({
      left: 10, top: 10, bottom: 30, right: 60, width: 50, height: 20, x: 10, y: 10,
      toJSON: () => ({}),
    }) as DOMRect
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: opts.isCollapsed ?? false,
      anchorNode: opts.anchorNode,
      rangeCount: 1,
      getRangeAt: () => range,
      removeAllRanges: () => undefined,
      toString: () => opts.text,
    } as unknown as Selection)
  }

  /** The text node of the mocked native body, and the wrapper mouseup lands on. */
  function bodyNodes() {
    const pre = screen.getByText('# Doc body')
    return { node: pre.firstChild as Node, host: pre }
  }

  it('ignores a whitespace-only or collapsed selection', async () => {
    await mount(mkArtifact())
    const { node, host } = bodyNodes()
    stubSelection({ text: '   ', anchorNode: node })
    fireEvent.mouseDown(host)
    fireEvent.mouseUp(host)
    // Nothing to anchor to, so no comment composer opens.
    expect(screen.queryByPlaceholderText('Write a comment…')).toBeNull()
  })

  it('ignores a selection that starts outside the rendered body', async () => {
    await mount(mkArtifact())
    const { host } = bodyNodes()
    // Anchored in the toolbar, not the document body.
    stubSelection({ text: 'Download', anchorNode: screen.getByLabelText('Download') })
    fireEvent.mouseDown(host)
    fireEvent.mouseUp(host)
    expect(screen.queryByPlaceholderText('Write a comment…')).toBeNull()
  })

  it('ignores a selection whose range escapes the rendered body', async () => {
    await mount(mkArtifact())
    const { node, host } = bodyNodes()
    // anchorNode is inside the body but the range spans out of it.
    stubSelection({ text: 'Doc', anchorNode: node, rangeNode: document.body })
    fireEvent.mouseDown(host)
    fireEvent.mouseUp(host)
    expect(screen.queryByPlaceholderText('Write a comment…')).toBeNull()
  })

  it('opens the anchored comment composer on a real selection and cancels cleanly', async () => {
    await mount(mkArtifact())
    const { node, host } = bodyNodes()
    stubSelection({ text: 'Doc', anchorNode: node })
    fireEvent.mouseDown(host)
    fireEvent.mouseUp(host)
    const composer = await screen.findByPlaceholderText('Write a comment…')
    expect(composer).toBeInTheDocument()
    fireEvent.keyDown(composer, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByPlaceholderText('Write a comment…')).toBeNull())
  })
})

describe('ArtifactDetailPage — upstream sync banner', () => {
  const PROVIDERS = {
    providers: [{ name: 'companion', display_name: 'Companion Cloud' }],
    kind: 'markdown',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    poppedOut = false
    localStorage.clear()
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: SLUG, events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({})
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue(PROVIDERS)
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const publication = {
    artifact_id: 'pub-1',
    view_url: 'https://remote.example.com/artifact/pub-1',
    provider: 'companion',
    visibility: 'SHARED' as const,
    shared_with: [],
    auto_sync: false,
    last_synced_kirocrew_version: 1,
    version_map: {},
    published_at: '2026-07-01T00:00:00Z',
    published_by: 'joe',
    last_error: '',
  }

  const forkMetadata = {
    upstream_artifact_id: 'up-1',
    upstream_url: 'https://remote.example.com/artifact/up-1',
    upstream_owner: 'someone',
    upstream_version: 4,
    forked_at: '2026-06-01T00:00:00Z',
  }

  it('offers a publish snapshot when local edits are unpublished', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ live_dirty: true })
    await mount(mkArtifact({ publication, live_dirty: true }))
    const btn = await screen.findByTitle(/Snapshot the current content as a new version/i)
    expect(screen.getByText(/Local changes not yet published to/)).toBeInTheDocument()
    expect(screen.getByText(/Companion Cloud/)).toBeInTheDocument()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, { snapshot: true }),
    )
  })

  it('reports a failed publish snapshot inside the banner', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ live_dirty: true })
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(new Error('cloud down'))
    await mount(mkArtifact({ publication, live_dirty: true }))
    fireEvent.click(await screen.findByTitle(/Snapshot the current content as a new version/i))
    await waitFor(() => expect(screen.getByText('cloud down')).toBeInTheDocument())
  })

  it('surfaces an error field returned by the publish snapshot', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ live_dirty: true })
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({ error: 'quota exceeded' })
    await mount(mkArtifact({ publication, live_dirty: true }))
    fireEvent.click(await screen.findByTitle(/Snapshot the current content as a new version/i))
    await waitFor(() => expect(screen.getByText('quota exceeded')).toBeInTheDocument())
  })

  it('a benign no-op pull reads as a neutral notice, not a failure', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({})
    vi.mocked(api).pullLatest = vi
      .fn()
      .mockResolvedValue({ pull_result: { pulled: false, reason: 'already current' } })
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    fireEvent.click(await screen.findByTitle(/Pull the latest remote content/i))
    await waitFor(() => expect(screen.getByText('already current')).toBeInTheDocument())
  })

  it('a failed pull surfaces the error', async () => {
    vi.mocked(api).pullLatest = vi.fn().mockRejectedValue(new Error('remote unreachable'))
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    fireEvent.click(await screen.findByTitle(/Pull the latest remote content/i))
    await waitFor(() => expect(screen.getByText('remote unreachable')).toBeInTheDocument())
  })

  it('an error field on the pull response is surfaced as an error, not a notice', async () => {
    vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({ error: 'upstream 500' })
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    fireEvent.click(await screen.findByTitle(/Pull the latest remote content/i))
    await waitFor(() => expect(screen.getByText('upstream 500')).toBeInTheDocument())
  })

  it('an error field on the overwrite response is surfaced', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    vi.mocked(api).overwriteRemote = vi.fn().mockResolvedValue({ error: 'conflict' })
    await mount(mkArtifact({ publication }))
    fireEvent.click(await screen.findByTitle(/Push your local version up/i))
    await waitFor(() => expect(screen.getByText('conflict')).toBeInTheDocument())
  })

  it('a successful overwrite refetches the record', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    vi.mocked(api).overwriteRemote = vi
      .fn()
      .mockResolvedValue({ overwrite_result: { overwritten: true } })
    const detail = vi.fn().mockResolvedValue(mkArtifact({ publication }))
    vi.mocked(api).artifact = detail
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: SLUG, versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const before = detail.mock.calls.length
    fireEvent.click(await screen.findByTitle(/Push your local version up/i))
    await waitFor(() => expect(detail.mock.calls.length).toBeGreaterThan(before))
  })

  it('a successful pull refetches the record', async () => {
    vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({ pull_result: { pulled: true } })
    const detail = vi.fn().mockResolvedValue(mkArtifact({ fork_metadata: forkMetadata }))
    vi.mocked(api).artifact = detail
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: SLUG, versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const before = detail.mock.calls.length
    fireEvent.click(await screen.findByTitle(/Pull the latest remote content/i))
    await waitFor(() => expect(detail.mock.calls.length).toBeGreaterThan(before))
  })

  it('overwrite pushes the local version up and reports a refusal', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true, cloud_version: 7 })
    vi.mocked(api).overwriteRemote = vi
      .fn()
      .mockResolvedValueOnce({ overwrite_result: { overwritten: false, reason: 'remote locked' } })
    await mount(mkArtifact({ publication }))
    const btn = await screen.findByTitle(/Push your local version up/i)
    expect(screen.getByText(/v7/)).toBeInTheDocument()
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByText('remote locked')).toBeInTheDocument())
  })

  it('a thrown overwrite surfaces its message', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    vi.mocked(api).overwriteRemote = vi.fn().mockRejectedValue(new Error('push rejected'))
    await mount(mkArtifact({ publication }))
    fireEvent.click(await screen.findByTitle(/Push your local version up/i))
    await waitFor(() => expect(screen.getByText('push rejected')).toBeInTheDocument())
  })

  it('flushes an open edit buffer before pulling so it is checkpointed', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({ pull_result: { pulled: true } })
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    await typeIntoEditor('# mid-edit body')
    await act(async () => {
      fireEvent.click(screen.getByTitle(/Pull the latest remote content/i))
    })
    // The buffer becomes live_dirty server-side FIRST, so the pull versions it.
    await waitFor(() =>
      expect(vi.mocked(api).updateArtifact).toHaveBeenCalledWith(SLUG, {
        content: '# mid-edit body',
        snapshot: false,
      }),
    )
    // Flushing also closes the editor so the pulled content is what renders.
    await waitFor(() => expect(screen.queryByLabelText('body editor')).toBeNull())
  })

  it('renders nothing when no publish provider is registered', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi
      .fn()
      .mockResolvedValue({ providers: [], kind: 'markdown' })
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle(/Pull the latest remote content/i)).toBeNull()
  })

  it('stays silent for a published artifact with nothing to sync', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({})
    await mount(mkArtifact({ publication }))
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText(/Local changes not yet published to/)).toBeNull()
    expect(screen.queryByTitle(/Pull the latest remote content/i)).toBeNull()
  })

  it('links the remote copy through the safe-URL guard', async () => {
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    await mount(mkArtifact({ fork_metadata: forkMetadata }))
    const link = await screen.findByRole('link', { name: /View remote/i })
    expect(link).toHaveAttribute('href', forkMetadata.upstream_url)
    expect(within(screen.getByText(/Forked from/).closest('span') as HTMLElement)
      .getByText('someone')).toBeInTheDocument()
  })
})
