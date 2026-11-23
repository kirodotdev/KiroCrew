/**
 * ChatEmbed hands a host's `onArtifactOpen` to the SDK default `skill_load` and
 * `subagent_completion` cards, so an `/artifacts/<slug>` link in a skill body or
 * a sub-agent result opens inside the host app instead of a new tab. These
 * tests mount the REAL ChatMessageList, its renderers and the cards, and click
 * the rendered anchor; only the app SDK's api hooks (the slot poll) are stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const LINK = '[Release checklist](/artifacts/release-checklist)'

// Each row draws its markdown only once its card is open: a failed sub-agent
// opens expanded; a skill card opens on its toggle, a multi-skill row on its name.
const CARDS = [
  {
    card: 'sub-agent completion card',
    row: {
      role: 'subagent', cls: '', ts: '2026-01-01T00:00:02Z',
      content: `[Subagent completion event]\nAgent \`53e3e5eb\` (kirocrew) failed ❌\nTask: Draft the release checklist\n\nSubagent result: ${LINK}`,
    },
    open: () => {},
  },
  {
    card: 'skill load card',
    row: {
      role: 'system', cls: '', ts: '2026-01-01T00:00:02Z', content: 'Loaded skill: release',
      meta: { kind: 'skill_load', skills: [{ name: 'release', body: `Skill body links ${LINK}` }] },
    },
    open: () => fireEvent.click(screen.getByTestId('skill-load-card-toggle')),
  },
  {
    card: 'skill load card, multi-skill row',
    row: {
      role: 'system', cls: '', ts: '2026-01-01T00:00:02Z', content: 'Loaded skills: alpha, beta',
      meta: { kind: 'skill_load', skills: [{ name: 'alpha', body: `Alpha links ${LINK}` }, { name: 'beta', body: 'Beta body' }] },
    },
    open: () => {
      fireEvent.click(screen.getByTestId('skill-load-card-toggle'))
      fireEvent.click(screen.getByRole('button', { name: 'alpha' }))
    },
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockPost.mockResolvedValue({})
})

async function mount(row: Record<string, unknown>, ui: React.ReactElement) {
  mockGet.mockResolvedValue({
    running: false,
    title: 'Embed',
    messages: [{ role: 'user', content: 'Where is the checklist?', cls: '', ts: '2026-01-01T00:00:01Z' }, row],
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let view!: ReturnType<typeof render>
  await act(async () => {
    view = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
  })
  await waitFor(() => expect(view.container.textContent).toContain('Where is the checklist?'))
  return view
}

const anchorIn = (container: HTMLElement) => waitFor(() => {
  const a = container.querySelector('a[href="/artifacts/release-checklist"]')
  expect(a).not.toBeNull()
  return a!
})

describe('ChatEmbed card artifact links, clicked through the real transcript', () => {
  it.each(CARDS)('the $card opens its artifact link through onArtifactOpen', async ({ row, open }) => {
    const onArtifactOpen = vi.fn()
    const { container } = await mount(row, <ChatEmbed slotKey="s" onArtifactOpen={onArtifactOpen} />)
    open()
    const anchor = await anchorIn(container)
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    act(() => { anchor.dispatchEvent(click) })
    expect(click.defaultPrevented).toBe(true)
    expect(onArtifactOpen).toHaveBeenCalledTimes(1)
    expect(onArtifactOpen).toHaveBeenCalledWith('release-checklist')
  })
})
