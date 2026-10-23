/**
 * Line-anchored code comments on ArtifactDetailPage, end to end through the
 * page's own wiring: the page mounts `CodeCommentContext` over the markdown
 * body, a code block's gutter button opens the SAME type-first composer a
 * prose selection opens (through the toolbar's external-selection channel,
 * since Pierre's shadow DOM holds no light-DOM selection), and the post
 * carries a SOURCE-offset anchor that routes back to the fence as a chip.
 *
 * Pierre itself is replaced by a light-DOM stand-in that renders the gutter
 * slot and the annotation slots, so the test can click them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import type { LineAnnotation } from '@pierre/diffs'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactComment } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
// The highlight stage waits for the viewport; the stand-in renders at once.
vi.mock('../hooks/useNearViewport', () => ({ useNearViewport: () => true }))
vi.mock('../components/pierreStaging', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useStagedMount: () => true,
}))

interface StandInProps {
  options?: { onLineSelectionChange?: (r: { start: number; end: number } | null) => void }
  selectedLines?: { start: number; end: number } | null
  lineAnnotations?: LineAnnotation<unknown>[]
  renderAnnotation?: (a: LineAnnotation<unknown>) => React.ReactNode
  renderGutterUtility?: (getHoveredLine: () => { lineNumber: number } | undefined) => React.ReactNode
}
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: (props: StandInProps) => (
    <div data-testid="pierre-code" data-selected={JSON.stringify(props.selectedLines ?? null)}>
      {/* Stands in for a drag over the line numbers. */}
      <button type="button" onClick={() => props.options?.onLineSelectionChange?.({ start: 2, end: 2 })}>pick line 2</button>
      {/* The reader hovers content line 2 of the fence. */}
      {props.renderGutterUtility?.(() => ({ lineNumber: 2 }))}
      {props.lineAnnotations?.map((a, i) => <div key={i}>{props.renderAnnotation?.(a)}</div>)}
    </div>
  ),
}))

// Line 1 prose, line 2 the opening fence, lines 3-4 the code, line 5 closing.
const BODY = 'Intro text here.\n```ts\nconst a = 1\nconst b = 2\n```\n'
const LINE_TWO_AT = BODY.indexOf('const b = 2')

const mkArtifact = (): Artifact => ({
  slug: 'notes',
  name: 'Notes',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: BODY,
})

const codeComment = (): ArtifactComment => ({
  id: 'c1',
  thread_id: 'c1',
  text: 'rename b',
  status: 'open',
  author: 'me',
  created_at: '2026-05-21T22:40:00.000000+00:00',
  anchor: {
    quote: 'const b = 2',
    prefix: BODY.slice(Math.max(0, LINE_TWO_AT - 32), LINE_TWO_AT),
    suffix: BODY.slice(LINE_TWO_AT + 'const b = 2'.length),
    start_offset: LINE_TWO_AT,
    end_offset: LINE_TWO_AT + 'const b = 2'.length,
  },
} as unknown as ArtifactComment)

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/notes' },
  )
}

describe('ArtifactDetailPage code-block comments', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'notes', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'notes', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({ ok: true })
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('the gutter button opens the composer and posts a source-offset anchor for the hovered line', async () => {
    renderPage()
    const code = await screen.findByTestId('pierre-code')
    fireEvent.click(within(code).getByRole('button', { name: 'Add comment' }))

    const input = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(input, { target: { value: 'rename b' } })
    const submit = screen.getAllByRole('button', { name: 'Add comment' }).find(b => !code.contains(b))!
    fireEvent.click(submit)

    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    const [slug, body] = vi.mocked(api).postArtifactComment.mock.calls[0]
    expect(slug).toBe('notes')
    expect(body.text).toBe('rename b')
    const anchor = body.anchor as { quote: string; prefix?: string; suffix?: string; start_offset?: number; end_offset?: number }
    expect(anchor.quote).toBe('const b = 2')
    expect(anchor.start_offset).toBe(LINE_TWO_AT)
    expect(anchor.end_offset).toBe(LINE_TWO_AT + 'const b = 2'.length)
    expect(anchor.prefix).toMatch(/const a = 1\n$/)
    expect(anchor.suffix).toMatch(/^\n```/)
  })

  it('clears the picked line range once the comment it was made on posts', async () => {
    renderPage()
    const code = await screen.findByTestId('pierre-code')
    fireEvent.click(within(code).getByRole('button', { name: 'pick line 2' }))
    await waitFor(() => expect(code.dataset.selected).toBe(JSON.stringify({ start: 2, end: 2 })))
    fireEvent.click(within(code).getByRole('button', { name: 'Add comment' }))
    const input = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(input, { target: { value: 'rename b' } })
    // The refetch after the post returns the stored comment.
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [codeComment()] })
    fireEvent.click(screen.getAllByRole('button', { name: 'Add comment' }).find(b => !code.contains(b))!)
    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('pierre-code').dataset.selected).toBe('null'))
  })

  it('keeps the picked range when the post is refused', async () => {
    vi.mocked(api).postArtifactComment = vi.fn().mockRejectedValue(new Error('nope'))
    renderPage()
    const code = await screen.findByTestId('pierre-code')
    fireEvent.click(within(code).getByRole('button', { name: 'pick line 2' }))
    await waitFor(() => expect(code.dataset.selected).toBe(JSON.stringify({ start: 2, end: 2 })))
    fireEvent.click(within(code).getByRole('button', { name: 'Add comment' }))
    const input = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(input, { target: { value: 'rename b' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Add comment' }).find(b => !code.contains(b))!)
    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    // The refusal has settled once the composer says so.
    expect(await screen.findByText(/Couldn.t save your comment/)).toBeInTheDocument()
    expect(screen.getByTestId('pierre-code').dataset.selected).toBe(JSON.stringify({ start: 2, end: 2 }))
  })

  it('routes a stored code anchor to its fence as a chip that opens the thread', async () => {
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [codeComment()] })
    renderPage()
    const code = await screen.findByTestId('pierre-code')
    const chip = await within(code).findByRole('button', { name: 'Comment thread (1, unread)' })
    fireEvent.click(chip)
    expect(await screen.findByRole('dialog', { name: 'Comment thread' })).toBeInTheDocument()
  })

  it('claims only the code anchor; a prose anchor with no raw-source prefix stays off the fence', async () => {
    // A context-free anchor (older / iframe-free) with the same quote and offset.
    const prose = { ...codeComment(), id: 'p1', thread_id: 'p1', anchor: { quote: 'const b = 2', start_offset: LINE_TWO_AT, end_offset: LINE_TWO_AT + 11 } }
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [prose, codeComment()] })
    renderPage()
    const code = await screen.findByTestId('pierre-code')
    await within(code).findByRole('button', { name: 'Comment thread (1, unread)' })
    expect(within(code).getAllByRole('button', { name: /Comment thread/ })).toHaveLength(1)
  })
})
