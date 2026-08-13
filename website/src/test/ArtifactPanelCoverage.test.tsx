/**
 * Coverage suite for `ArtifactPanel` — the side-panel Artifacts tab.
 *
 * The one existing sibling (`ArtifactPanel.copyIcon.test.tsx`) never mounts the
 * panel at all: it exercises `SelectionToolbar` in isolation. So everything the
 * panel itself owns was cold — the seed-vs-live artifact precedence, the
 * hydrating / load-failed branches, the three body kinds, the comments toggle,
 * the submit-to-chat bar, the Escape handling, and the whole fullscreen portal
 * (focus seeding, Tab trap, body-scroll lock). Each is pinned here.
 *
 * Kiro Crew convention: automocked api client + `renderWithProviders` on a real
 * route so `useNavigate` runs for real against a sibling route.
 *
 * Three harness substitutions, all deliberate:
 *  1. `ArtifactBody*` are replaced with markers. The real native body is a
 *     lazily-imported Monaco instance and the real iframe body navigates a blob
 *     page — neither belongs in a test of the panel's own wiring. The markers
 *     echo back the props the panel chose (kind, slug, flush) so the branch that
 *     selected them is still asserted.
 *  2. `SelectionToolbar` is replaced with plain buttons that invoke each action.
 *     A real toolbar needs a live DOM text selection with layout, which happy-dom
 *     does not produce, so the panel's selection actions are otherwise
 *     unreachable.
 *  3. `useFileArtifactComments` is replaced with a stateful stub. The real hook
 *     owns its own query + mutation graph (covered by its own suites); here it
 *     only has to hand back a controllable comment list and a working sidebar
 *     toggle, and to record the arguments the panel passes it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import type { CSSProperties } from 'react'
import ArtifactPanel from '../components/ArtifactPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import type { Artifact, ArtifactComment } from '../types'

const SLUG = 'ops-widget'
const NAME = 'Ops Widget'
const SEED_CONTENT = 'seeded body text'
const LIVE_CONTENT = 'live body text'
const SELECTED_TEXT = 'a selected phrase'
/** The submit-reset guard window in the component, in ms. */
const SUBMIT_GUARD_MS = 400
/** Mirrors the focus-trap selector the fullscreen overlay queries with. */
const FOCUSABLE_SELECTOR = 'button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'

vi.mock('../api/client')
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

/** Arguments the panel hands each comment layer; asserted per instance. */
interface LayerArgs {
  slug: string | null
  usesIframe?: boolean
  sidebarDefaultOpen?: boolean
  sidebarClassName?: string
  sidebarStyle?: CSSProperties
}
const layerArgs: LayerArgs[] = []
const anchorRequests: string[] = []
let stubComments: ArtifactComment[] = []

// A stateful stand-in: real `sidebarOpen` state (so the toggle is exercised for
// real) over a comment list the test controls. The two instances are told apart
// by `sidebarClassName` — only the non-fullscreen layer is given the stacked
// sizing — so each renders a distinguishable marker.
vi.mock('../components/FileArtifactComments', async () => {
  const { useState } = await import('react')
  function useFileArtifactComments(args: LayerArgs) {
    layerArgs.push(args)
    const [sidebarOpen, setSidebarOpen] = useState(args.sidebarDefaultOpen ?? true)
    const which = args.sidebarClassName ? 'stacked' : 'full'
    return {
      overlay: null,
      popovers: <div data-testid={`popovers-${which}`} />,
      sidebar: <div data-testid={`sidebar-${which}`} />,
      requestAnchoredComment: () => { anchorRequests.push(which) },
      toggleSidebar: () => setSidebarOpen((v: boolean) => !v),
      sidebarOpen,
      commentCount: stubComments.length,
      comments: stubComments,
      activeCommentId: null,
      scrollNonce: 0,
      unreadRootIds: new Set<string>(),
      activateComment: () => {},
      onIframeSelect: () => {},
      onIframeOpenThread: () => {},
      iframeScrollTarget: null,
    }
  }
  return { useFileArtifactComments }
})

const makeRect = () => document.createElement('div').getBoundingClientRect()

interface StubAction {
  id: string
  label: string
  onClick: (text: string, rect: DOMRect) => void
}
vi.mock('../components/SelectionToolbar', () => ({
  default: ({ actions }: { actions: StubAction[] }) => (
    <div data-testid="selection-toolbar">
      {actions.map((a) => (
        <span key={a.id}>
          <button type="button" aria-label={`selection ${a.id}`} onClick={() => a.onClick(SELECTED_TEXT, makeRect())}>
            {a.label}
          </button>
          <button type="button" aria-label={`selection ${a.id} blank`} onClick={() => a.onClick('', makeRect())}>
            {`${a.label} blank`}
          </button>
        </span>
      ))}
    </div>
  ),
}))

vi.mock('../components/ArtifactBody', () => ({
  ArtifactBodyNative: ({ kind, content, flush, onChange }: {
    kind: Artifact['kind']
    content: string
    flush?: boolean
    onChange: (v: string) => void
  }) => (
    <div data-testid="body-native" data-kind={kind} data-flush={String(!!flush)}>
      <span data-testid="body-native-content">{content}</span>
      <button type="button" onClick={() => onChange('edited')}>attempt body edit</button>
    </div>
  ),
  ArtifactBodyIframe: ({ artifact, slug }: { artifact: Artifact; slug: string }) => (
    <div data-testid="body-iframe" data-kind={artifact.kind} data-slug={slug} />
  ),
  ArtifactBodyImage: ({ artifact, slug }: { artifact: Artifact; slug: string }) => (
    <div data-testid="body-image" data-kind={artifact.kind} data-slug={slug} />
  ),
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: SLUG,
  name: NAME,
  kind: 'markdown',
  source: 'chat',
  description: 'Panel fixture',
  tags: [],
  version: 3,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T01:00:00Z',
  content: LIVE_CONTENT,
  ...overrides,
})

const mkComment = (overrides: Partial<ArtifactComment> = {}): ArtifactComment => ({
  id: 'c1',
  author: 'zezhen',
  is_agent: false,
  body: 'tighten this heading',
  thread_id: 'c1',
  status: 'open',
  scope: 'private',
  origin: 'local',
  sync_state: 'local_only',
  created_at: '2026-06-01T02:00:00Z',
  updated_at: '2026-06-01T02:00:00Z',
  ...overrides,
})

interface PanelOverrides {
  kind?: Artifact['kind']
  content?: string
  onSubmitComments?: (message: string) => void
  embedded?: boolean
}

function renderPanel({ kind = 'markdown', content = SEED_CONTENT, onSubmitComments, embedded }: PanelOverrides = {}) {
  const onClose = vi.fn()
  const utils = renderWithProviders(
    <Routes>
      <Route
        path="/"
        element={
          <ArtifactPanel
            slug={SLUG}
            kind={kind}
            content={content}
            onClose={onClose}
            onSubmitComments={onSubmitComments}
            embedded={embedded}
          />
        }
      />
      <Route path="/artifacts/:slug" element={<div>full artifact page</div>} />
    </Routes>,
  )
  return { onClose, ...utils }
}

/** Enter fullscreen and hand back the portal dialog. */
async function enterFullscreen() {
  fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
  return await screen.findByRole('dialog')
}

describe('ArtifactPanel', () => {
  beforeEach(() => {
    // The submit guard schedules a 400ms reset; a real timer would fire after
    // teardown and throw as an unhandled error. `shouldAdvanceTime` keeps the
    // clock moving so findBy*/waitFor behave as they do with real timers.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.clearAllMocks()
    layerArgs.length = 0
    anchorRequests.length = 0
    stubComments = []
    vi.mocked(api.artifact).mockResolvedValue(mkArtifact())
    // The provider tree's own boot query rides along on the automocked client;
    // resolving it keeps React Query's "data cannot be undefined" out of stderr.
    vi.mocked(api.themeBoot).mockResolvedValue({} as Awaited<ReturnType<typeof api.themeBoot>>)
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  describe('body selection', () => {
    it('renders the live artifact once loaded, flushed against the panel padding', async () => {
      renderPanel()
      expect(await screen.findByText(NAME)).toBeInTheDocument()
      const body = screen.getByTestId('body-native')
      expect(body.getAttribute('data-kind')).toBe('markdown')
      // Non-fullscreen drops the body's card chrome so a markdown artifact
      // matches a markdown FILE in the same panel.
      expect(body.getAttribute('data-flush')).toBe('true')
      expect(screen.getByTestId('body-native-content').textContent).toBe(LIVE_CONTENT)
    })

    it('keeps the body read-only — the change handler is a no-op', async () => {
      renderPanel()
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'attempt body edit' }))
      // No writer is wired up, so the content is unchanged and nothing throws.
      expect(screen.getByTestId('body-native-content').textContent).toBe(LIVE_CONTENT)
      expect(api.artifact).toHaveBeenCalledWith(SLUG)
    })

    it('renders the seeded kind and content until the live query resolves', () => {
      vi.mocked(api.artifact).mockReturnValue(new Promise<Artifact>(() => {}))
      renderPanel({ kind: 'text' })
      // Seed renders synchronously; the slug stands in for the not-yet-known name.
      expect(screen.getByText(SLUG)).toBeInTheDocument()
      expect(screen.getByTestId('body-native').getAttribute('data-kind')).toBe('text')
      expect(screen.getByTestId('body-native-content').textContent).toBe(SEED_CONTENT)
    })

    it('shows the hydrating state only when there is no seed content', () => {
      vi.mocked(api.artifact).mockReturnValue(new Promise<Artifact>(() => {}))
      renderPanel({ content: '' })
      expect(screen.getByText('Loading artifact…')).toBeInTheDocument()
      expect(screen.queryByTestId('body-native')).toBeNull()
    })

    it('shows the load-failure message when the fetch rejects with no seed', async () => {
      vi.mocked(api.artifact).mockRejectedValue(new Error('artifact gone'))
      renderPanel({ content: '' })
      expect(await screen.findByText(/Couldn’t load this artifact/)).toBeInTheDocument()
      expect(screen.queryByTestId('body-native')).toBeNull()
    })

    it('renders the image body for an image artifact', async () => {
      vi.mocked(api.artifact).mockResolvedValue(mkArtifact({ kind: 'image', content: '' }))
      renderPanel({ kind: 'image', content: '' })
      const body = await screen.findByTestId('body-image')
      expect(body.getAttribute('data-slug')).toBe(SLUG)
      expect(body.getAttribute('data-kind')).toBe('image')
    })

    it('renders the iframe body for a widget and drops the selection toolbar', async () => {
      vi.mocked(api.artifact).mockResolvedValue(mkArtifact({ kind: 'widget' }))
      renderPanel({ kind: 'widget' })
      const body = await screen.findByTestId('body-iframe')
      expect(body.getAttribute('data-slug')).toBe(SLUG)
      // Text selection is an in-iframe concern, so the DOM toolbar is not mounted.
      expect(screen.queryByTestId('selection-toolbar')).toBeNull()
      // Both comment layers are told the body is an iframe.
      expect(layerArgs.every((a) => a.usesIframe === true)).toBe(true)
    })
  })

  describe('comment layers', () => {
    it('gives the panel layer stacked sizing and a collapsed default', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const stacked = layerArgs.find((a) => a.sidebarClassName)
      const full = layerArgs.find((a) => !a.sidebarClassName)
      expect(stacked?.sidebarDefaultOpen).toBe(false)
      expect(stacked?.sidebarClassName).toContain('rounded-xl')
      expect(stacked?.sidebarStyle).toEqual({ maxHeight: 280, minHeight: 0 })
      // The fullscreen layer keeps the full-page default (open, unsized).
      expect(full?.sidebarDefaultOpen).toBeUndefined()
      expect(full?.slug).toBe(SLUG)
    })

    it('toggles the comments sidebar and shows the count pill', async () => {
      stubComments = [mkComment(), mkComment({ id: 'c2', thread_id: 'c2' })]
      renderPanel()
      await screen.findByText(NAME)
      const toggle = screen.getByRole('button', { name: 'Show comments' })
      expect(toggle.textContent).toBe('2')
      expect(toggle.getAttribute('aria-pressed')).toBe('false')
      expect(screen.queryByTestId('sidebar-stacked')).toBeNull()

      fireEvent.click(toggle)
      expect(await screen.findByTestId('sidebar-stacked')).toBeInTheDocument()
      const opened = screen.getByRole('button', { name: 'Hide comments' })
      expect(opened.getAttribute('aria-pressed')).toBe('true')

      fireEvent.click(opened)
      await waitFor(() => expect(screen.queryByTestId('sidebar-stacked')).toBeNull())
    })

    it('omits the count pill when there are no comments', async () => {
      renderPanel()
      await screen.findByText(NAME)
      expect(screen.getByRole('button', { name: 'Show comments' }).textContent).toBe('')
    })

    it('routes the selection comment action to the active layer', async () => {
      renderPanel()
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'selection comment' }))
      expect(anchorRequests).toEqual(['stacked'])
    })

    it('copies selected text, and ignores a blank selection', async () => {
      renderPanel()
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'selection copy' }))
      expect(copyToClipboard).toHaveBeenCalledWith(SELECTED_TEXT)

      vi.mocked(copyToClipboard).mockClear()
      fireEvent.click(screen.getByRole('button', { name: 'selection copy blank' }))
      expect(copyToClipboard).not.toHaveBeenCalled()
    })
  })

  describe('header, footer and navigation', () => {
    it('copies the slug from the footer', async () => {
      renderPanel()
      await screen.findByText(NAME)
      fireEvent.click(screen.getByTitle('Click to copy slug'))
      expect(copyToClipboard).toHaveBeenCalledWith(SLUG)
    })

    it('navigates to the full artifact page', async () => {
      renderPanel()
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'Open full artifact page' }))
      expect(await screen.findByText('full artifact page')).toBeInTheDocument()
    })

    it('renders as an embedded tab body without a resize handle', async () => {
      renderPanel({ embedded: true })
      await screen.findByText(NAME)
      expect(screen.queryByRole('separator')).toBeNull()
    })
  })

  describe('escape handling', () => {
    it('closes the panel on Escape', async () => {
      const { onClose } = renderPanel()
      await screen.findByText(NAME)
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    it('ignores keys other than Escape', async () => {
      const { onClose } = renderPanel()
      await screen.findByText(NAME)
      fireEvent.keyDown(document.body, { key: 'a' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('leaves Escape to the field while the user is typing', async () => {
      const onSubmitComments = vi.fn()
      stubComments = [mkComment()]
      const { onClose } = renderPanel({ onSubmitComments })
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'Toggle additional instruction' }))
      const field = await screen.findByLabelText('Additional instruction')
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('leaves Escape to a contenteditable target', async () => {
      const { onClose } = renderPanel()
      await screen.findByText(NAME)
      const editable = document.createElement('div')
      // happy-dom does not derive isContentEditable from the attribute, so the
      // property is set directly — the component reads exactly that property.
      Object.defineProperty(editable, 'isContentEditable', { value: true })
      document.body.appendChild(editable)
      fireEvent.keyDown(editable, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
      editable.remove()
    })
  })

  describe('fullscreen overlay', () => {
    it('locks body scroll while open and restores it on exit', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      expect(document.body.style.overflow).toBe('hidden')
      expect(within(dialog).getByText(NAME)).toBeInTheDocument()

      fireEvent.click(within(dialog).getByRole('button', { name: 'Exit full screen' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(document.body.style.overflow).toBe('')
    })

    it('keeps the body card chrome in fullscreen while the panel body stays flush', async () => {
      renderPanel()
      await screen.findByText(NAME)
      await enterFullscreen()
      const flushFlags = screen.getAllByTestId('body-native').map((b) => b.getAttribute('data-flush'))
      expect(flushFlags).toHaveLength(2)
      expect(flushFlags).toContain('true')
      expect(flushFlags).toContain('false')
    })

    it('opens the fullscreen comment sidebar by default and can collapse it', async () => {
      stubComments = [mkComment()]
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      expect(screen.getByTestId('sidebar-full')).toBeInTheDocument()
      fireEvent.click(within(dialog).getByRole('button', { name: 'Hide comments' }))
      await waitFor(() => expect(screen.queryByTestId('sidebar-full')).toBeNull())
    })

    it('swaps the panel popovers for the fullscreen layer popovers', async () => {
      renderPanel()
      await screen.findByText(NAME)
      expect(screen.getByTestId('popovers-stacked')).toBeInTheDocument()
      await enterFullscreen()
      expect(screen.queryByTestId('popovers-stacked')).toBeNull()
      expect(screen.getByTestId('popovers-full')).toBeInTheDocument()
    })

    it('exits fullscreen on Escape before closing the panel', async () => {
      const { onClose } = renderPanel()
      await screen.findByText(NAME)
      await enterFullscreen()
      fireEvent.keyDown(document.body, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(onClose).not.toHaveBeenCalled()

      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    it('seeds focus on the first control when the overlay mounts', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      const focusable = dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
      expect(document.activeElement).toBe(focusable[0])
    })

    it('traps Tab focus inside the overlay in both directions', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      // Same selector the overlay's own trap uses — a narrower one (buttons
      // only) would miss the tabbable footer row and pick the wrong "last".
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      expect(focusable.length).toBeGreaterThan(1)

      last.focus()
      fireEvent.keyDown(dialog, { key: 'Tab' })
      expect(document.activeElement).toBe(first)

      first.focus()
      fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
      expect(document.activeElement).toBe(last)

      // Mid-list Tab is left to the browser, and a non-Tab key is ignored.
      focusable[1].focus()
      fireEvent.keyDown(dialog, { key: 'Tab' })
      expect(document.activeElement).toBe(focusable[1])
      fireEvent.keyDown(dialog, { key: 'Enter' })
      expect(document.activeElement).toBe(focusable[1])
    })

    it('navigates to the full artifact page from the overlay header', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      fireEvent.click(within(dialog).getByRole('button', { name: 'Open full artifact page' }))
      expect(await screen.findByText('full artifact page')).toBeInTheDocument()
    })

    it('copies the slug from the overlay footer', async () => {
      renderPanel()
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      fireEvent.click(within(dialog).getByTitle('Click to copy slug'))
      expect(copyToClipboard).toHaveBeenCalledWith(SLUG)
    })
  })

  describe('submit to chat', () => {
    it('hides the submit bar when no submit channel is supplied', async () => {
      stubComments = [mkComment()]
      renderPanel()
      await screen.findByText(NAME)
      expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull()
    })

    it('hides the submit bar when every comment is agent-authored', async () => {
      stubComments = [mkComment({ id: 'a1', thread_id: 'a1', is_agent: true, author: 'kiro' })]
      renderPanel({ onSubmitComments: vi.fn() })
      await screen.findByText(NAME)
      expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull()
    })

    it('submits only the human comments and guards against a double fire', async () => {
      const onSubmitComments = vi.fn()
      stubComments = [
        mkComment(),
        mkComment({ id: 'a1', thread_id: 'a1', is_agent: true, author: 'kiro', body: 'agent note' }),
      ]
      renderPanel({ onSubmitComments })
      await screen.findByText(NAME)
      expect(screen.getByText('1 comment to send to this chat')).toBeInTheDocument()

      const submit = screen.getByRole('button', { name: 'Submit' })
      fireEvent.click(submit)
      expect(onSubmitComments).toHaveBeenCalledTimes(1)
      const message = onSubmitComments.mock.calls[0][0] as string
      expect(message).toContain(`${NAME} (${SLUG})`)
      expect(message).toContain('tighten this heading')
      expect(message).not.toContain('agent note')

      // The guard disables the button for one short window, then releases it.
      await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' })).toBeDisabled())
      await act(async () => { vi.advanceTimersByTime(SUBMIT_GUARD_MS) })
      await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled())
    })

    it('threads an extra instruction through the message and clears it after submit', async () => {
      const onSubmitComments = vi.fn()
      const instruction = 'apply all of these in one pass'
      stubComments = [mkComment()]
      renderPanel({ onSubmitComments })
      await screen.findByText(NAME)

      const toggle = screen.getByRole('button', { name: 'Toggle additional instruction' })
      expect(toggle.getAttribute('aria-pressed')).toBe('false')
      fireEvent.click(toggle)
      const field = await screen.findByLabelText('Additional instruction')
      expect(field).toHaveFocus()
      fireEvent.change(field, { target: { value: instruction } })

      fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
      const message = onSubmitComments.mock.calls[0][0] as string
      expect(message).toContain('OVERALL INSTRUCTION')
      expect(message).toContain(instruction)
      // Submitting collapses the affordance and drops the note.
      await waitFor(() => expect(screen.queryByLabelText('Additional instruction')).toBeNull())
    })

    it('omits the instruction block when the toggle was never opened', async () => {
      const onSubmitComments = vi.fn()
      stubComments = [mkComment()]
      renderPanel({ onSubmitComments })
      await screen.findByText(NAME)
      fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
      expect(onSubmitComments.mock.calls[0][0]).not.toContain('OVERALL INSTRUCTION')
    })

    it('renders a submit bar in the fullscreen overlay too', async () => {
      const onSubmitComments = vi.fn()
      stubComments = [mkComment(), mkComment({ id: 'c2', thread_id: 'c2' })]
      renderPanel({ onSubmitComments })
      await screen.findByText(NAME)
      const dialog = await enterFullscreen()
      const overlaySubmit = within(dialog).getByRole('button', { name: 'Submit' })
      fireEvent.click(overlaySubmit)
      expect(onSubmitComments).toHaveBeenCalledTimes(1)
      expect(within(dialog).getByText('2 comments to send to this chat')).toBeInTheDocument()
    })
  })
})
