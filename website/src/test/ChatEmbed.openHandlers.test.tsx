/**
 * ChatEmbed hands a host's `onFileOpen` / `onArtifactOpen` to the transcript,
 * so a tool row's file chip, a path citation in a reply and an artifact link
 * open inside the host app. These tests mount the REAL ChatMessageList and its
 * renderers and click the rendered controls; only the API boundaries are
 * stubbed: the app SDK's api hooks (the slot poll) and `fetch` (the batched
 * path-kind probe that decides whether a cited path becomes a link).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { stubPathKinds } from './pathKindStub'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const TOOL_PATH = '/home/me/project/loader.py'
const CITED_PATH = '/home/me/notes/plan.md'

// One transcript holding all three controls: a tool row whose input names a
// file (the chip), a reply citing a line range of another file, and a reply
// linking a saved artifact.
const TRANSCRIPT = {
  running: false,
  title: 'Embed',
  messages: [
    { role: 'user', content: 'Look at the loader', cls: '', ts: '2026-01-01T00:00:01Z' },
    {
      role: 'tool_call', content: '🔧 read_file', cls: '', ts: '2026-01-01T00:00:02Z',
      meta: { input_preview: JSON.stringify({ path: TOOL_PATH }) },
    },
    {
      role: 'assistant', cls: '', ts: '2026-01-01T00:00:03Z',
      content: `See \`${CITED_PATH}:10-16\` and the [queue report](/artifacts/cr-queue).`,
    },
  ],
}

const realFetch = globalThis.fetch

beforeEach(() => {
  vi.clearAllMocks()
  __resetPathKindCache()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue(TRANSCRIPT)
  mockPost.mockResolvedValue({})
  stubPathKinds(p => (p === TOOL_PATH || p === CITED_PATH ? 'file' : null))
})

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

async function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let view!: ReturnType<typeof render>
  await act(async () => {
    view = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
  })
  // The reply has rendered once its artifact link is in the document.
  await waitFor(() => expect(view.container.querySelector('a[href="/artifacts/cr-queue"]')).not.toBeNull())
  return view
}

const citationLink = (container: HTMLElement) => waitFor(() => {
  const c = container.querySelector(`code[data-path-kind="file"][data-path="${CITED_PATH}"]`) as HTMLElement | null
  expect(c).not.toBeNull()
  return c!
})

describe('ChatEmbed open handlers, clicked through the real transcript', () => {
  it('opens the tool row file chip through onFileOpen', async () => {
    const onFileOpen = vi.fn()
    const onArtifactOpen = vi.fn()
    await mount(<ChatEmbed slotKey="s" onFileOpen={onFileOpen} onArtifactOpen={onArtifactOpen} />)
    fireEvent.click(screen.getByRole('button', { name: `Open ${TOOL_PATH}` }))
    expect(onFileOpen).toHaveBeenCalledTimes(1)
    expect(onFileOpen).toHaveBeenCalledWith(TOOL_PATH)
    expect(onArtifactOpen).not.toHaveBeenCalled()
  })

  it('opens a cited line range in a reply through onFileOpen with both ends', async () => {
    const onFileOpen = vi.fn()
    const onArtifactOpen = vi.fn()
    const { container } = await mount(<ChatEmbed slotKey="s" onFileOpen={onFileOpen} onArtifactOpen={onArtifactOpen} />)
    fireEvent.click(await citationLink(container))
    expect(onFileOpen).toHaveBeenCalledTimes(1)
    expect(onFileOpen).toHaveBeenCalledWith(CITED_PATH, { line: 10, endLine: 16 })
    expect(onArtifactOpen).not.toHaveBeenCalled()
  })

  it('opens an artifact link through onArtifactOpen instead of following it', async () => {
    const onFileOpen = vi.fn()
    const onArtifactOpen = vi.fn()
    const { container } = await mount(<ChatEmbed slotKey="s" onFileOpen={onFileOpen} onArtifactOpen={onArtifactOpen} />)
    const anchor = container.querySelector('a[href="/artifacts/cr-queue"]')!
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    act(() => { anchor.dispatchEvent(click) })
    expect(click.defaultPrevented).toBe(true)
    expect(onArtifactOpen).toHaveBeenCalledTimes(1)
    expect(onArtifactOpen).toHaveBeenCalledWith('cr-queue')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('without host handlers: no file chip and the artifact link is followed', async () => {
    const { container } = await mount(<ChatEmbed slotKey="s" />)
    // The tool row itself is on screen; only its open affordance is absent.
    expect(container.textContent).toContain('read_file')
    expect(screen.queryByRole('button', { name: `Open ${TOOL_PATH}` })).toBeNull()
    // The artifact anchor is left to the browser: the click is not swallowed.
    const anchor = container.querySelector('a[href="/artifacts/cr-queue"]')!
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    expect(() => act(() => { anchor.dispatchEvent(click) })).not.toThrow()
    expect(click.defaultPrevented).toBe(false)
  })
})
