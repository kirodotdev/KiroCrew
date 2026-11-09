/**
 * "Steering in this chat", under the Context breakdown tab: one list item per
 * steering file, each saying where the file comes from, when it applies, and
 * whether it reaches this chat, as text a screen reader reads too.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, render, screen, cleanup, within } from '@testing-library/react'

import type { ChatSteering } from '../api/client/chatSlotSettings'
import { ChatSteeringList, ChatSteeringListView } from '../pages/ChatSteeringList'
import { sseSlots, updateSlot } from '../store/dashboardSlice'
import { createTestStore, renderWithProviders } from './helpers'

const steeringMock = vi.fn()
vi.mock('../api/client', () => ({
  api: { chatSlotSteering: (slot: string) => steeringMock(slot) },
}))

afterEach(() => {
  cleanup()
  steeringMock.mockReset()
})

const data: ChatSteering = {
  provider: 'codex',
  files: [
    { name: 'style.md', path: '~/.kiro/steering/style.md', source: 'global', applies: 'always', reaches: 'no', reason: 'harness_has_no_path' },
    { name: 'own.md', path: '~/standards/own.md', source: 'folder', applies: 'always', reaches: 'yes', reason: '' },
    { name: 'review.md', path: '~/p/.kiro/steering/review.md', source: 'project', applies: 'manual', reaches: 'harness', reason: 'harness_native' },
  ],
}

describe('ChatSteeringListView', () => {
  it('names the section and lists one item per file', () => {
    render(<ChatSteeringListView data={data} />)
    const section = screen.getByRole('region', { name: 'Steering in this chat' })
    const items = within(section).getAllByRole('listitem')
    expect(items.map(item => within(item).getByText(/\.md$/).textContent)).toEqual(['style.md', 'own.md', 'review.md'])
  })

  it('says plainly when a file does not reach the chat, and why', () => {
    render(<ChatSteeringListView data={data} />)
    const [style, own, review] = screen.getAllByRole('listitem')
    expect(style).toHaveTextContent('Global · Always applies')
    expect(style).toHaveTextContent("Not included: this chat's agent harness doesn't load global or project steering")
    expect(own).toHaveTextContent('Folder · Always applies')
    expect(own).toHaveTextContent('Included in this chat')
    expect(review).toHaveTextContent('Project · Applies when asked for')
    expect(review).toHaveTextContent('Loaded by the agent harness')
  })

  it('shows an empty state instead of an empty list', () => {
    render(<ChatSteeringListView data={{ provider: 'kas', files: [] }} />)
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByText('No steering files apply to this chat.')).toBeInTheDocument()
  })
})

describe('ChatSteeringList', () => {
  it('reads the list again when the chat moves to another project', async () => {
    steeringMock.mockResolvedValue({ provider: 'codex', files: [] })
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'chat-1', messages: 0, running: false, project: '/p/one' }] as never))
    renderWithProviders(<ChatSteeringList slot="chat-1" />, {
      store,
      queryDefaults: { staleTime: Infinity },
    })
    await screen.findByText('No steering files apply to this chat.')
    expect(steeringMock).toHaveBeenCalledTimes(1)
    act(() => {
      store.dispatch(updateSlot({ key: 'chat-1', project: '/p/two' }))
    })
    await vi.waitFor(() => expect(steeringMock).toHaveBeenCalledTimes(2))
  })
})
