import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { api } from '../api/client'
import type { Artifact } from '../types'
import { copyToClipboard } from '../utils/clipboard'
import { renderWithProviders } from './helpers'

vi.mock('../api/client')
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const BODY = 'alpha beta gamma'

const artifact: Artifact = {
  slug: 'notes',
  name: 'Notes',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: BODY,
}

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/notes' },
  )
}

function selectRaw(raw: string): boolean {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node: Node | null = null
  while (walker.nextNode()) {
    const text = walker.currentNode.textContent ?? ''
    if (text.includes(raw) && text.includes(BODY)) {
      node = walker.currentNode
      break
    }
  }
  if (!node) return false

  const start = (node.textContent ?? '').indexOf(raw)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + raw.length)
  range.getBoundingClientRect = () => ({
    left: 10, top: 10, bottom: 30, right: 60, width: 50, height: 20, x: 10, y: 10,
    toJSON: () => ({}),
  }) as DOMRect
  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: false,
    anchorNode: node,
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges: () => undefined,
    toString: () => raw,
  } as unknown as Selection)

  const host = node.parentElement as HTMLElement
  fireEvent.mouseDown(host)
  fireEvent.mouseUp(host)
  return true
}

describe('ArtifactDetailPage selected-text copy', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'notes', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'notes', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(copyToClipboard).mockResolvedValue(true)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('copies raw whitespace while keeping the comment anchor trimmed', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())

    expect(selectRaw(' beta ')).toBe(true)
    fireEvent.click(await screen.findByLabelText('Copy'))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(' beta '))

    fireEvent.change(screen.getByLabelText('Add a comment'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).postArtifactComment.mock.calls[0][1].anchor?.quote).toBe('beta')
  })
})
